"""DefiLlama source: peak-relative dead-protocol detection, anchored to Wayback snapshots.

Peak-relative because zombies sit well above zero — Primitive Finance peaked at
$1.72M and lingers at ~$58K (3.4%); a raw $10K/$50K floor misses it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from urllib.parse import urlencode

from slopmortem.corpus.sources._names import SOURCE_DEFILLAMA
from slopmortem.corpus.sources._throttle import (
    HTTP_BAD_REQUEST,
    USER_AGENT,
    respect_robots,
    throttle_for,
)
from slopmortem.http import safe_get
from slopmortem.models import RawEntry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

BULK_ENDPOINT: Final = "https://api.llama.fi/protocols"
DETAIL_ENDPOINT_BASE: Final = "https://api.llama.fi/protocol"
WAYBACK_CDX_ENDPOINT: Final = "https://web.archive.org/cdx/search/cdx"

DEFAULT_DEAD_THRESHOLD_PCT: Final = 0.05
DEFAULT_PEAK_FLOOR_USD: Final = 1_000_000.0
DEFAULT_MIN_DAYS_SINCE_PEAK: Final = 180
DEFAULT_SHORTLIST_TVL_CEILING_USD: Final = 100_000.0
DEFAULT_MAX_EMIT: Final = 500

WAYBACK_WINDOW_NARROW_DAYS: Final = 30
WAYBACK_WINDOW_WIDE_DAYS: Final = 180


type DeathStatus = Literal["dead", "alive", "never_launched", "too_early", "unknown"]


@dataclass(frozen=True, slots=True)
class DeathVerdict:
    status: DeathStatus
    peak_tvl: float | None = None
    peak_date: date | None = None
    current_tvl: float | None = None


def _merge_chain_series(chain_tvls: dict[str, dict[str, Any]]) -> list[tuple[date, float]]:  # pyright: ignore[reportExplicitAny]
    """Sum daily totalLiquidityUSD across chains; dedupe intra-day duplicates last-write-wins.

    DefiLlama ships multiple "today" snapshots; naive summing double-counts current TVL.
    """
    totals: dict[date, float] = {}
    for chain_payload in chain_tvls.values():
        if not isinstance(chain_payload, dict):  # pyright: ignore[reportUnnecessaryIsInstance] — runtime defence; Any values are not statically narrowable
            continue
        series_obj: object = chain_payload.get("tvl")
        if not isinstance(series_obj, list):
            continue
        per_day: dict[date, float] = {}
        for point in series_obj:  # pyright: ignore[reportUnknownVariableType] — list[Any] from JSON
            if not isinstance(point, dict):
                continue
            ts: object = point.get("date")  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType] — Any from JSON
            tvl: object = point.get("totalLiquidityUSD")  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType] — Any from JSON
            if not isinstance(ts, (int, float)) or not isinstance(tvl, (int, float)):
                continue
            d = datetime.fromtimestamp(float(ts), tz=UTC).date()
            per_day[d] = float(tvl)
        for d, v in per_day.items():
            totals[d] = totals.get(d, 0.0) + v
    return sorted(totals.items())


def classify_death(
    chain_tvls: dict[str, dict[str, Any]],  # pyright: ignore[reportExplicitAny]
    *,
    threshold_pct: float = DEFAULT_DEAD_THRESHOLD_PCT,
    peak_floor_usd: float = DEFAULT_PEAK_FLOOR_USD,
    min_days_since_peak: int = DEFAULT_MIN_DAYS_SINCE_PEAK,
) -> DeathVerdict:
    """Classify a TVL trajectory; "dead" requires the trailing-90d mean to also be below threshold.

    The 90d-mean check guards against treating a brief dip as death.
    """
    series = _merge_chain_series(chain_tvls)
    if not series:
        return DeathVerdict("unknown")

    peak_date, peak_tvl = max(series, key=lambda x: x[1])
    current_date, current_tvl = series[-1]
    days_since_peak = (current_date - peak_date).days

    if peak_tvl < peak_floor_usd:
        return DeathVerdict("never_launched", peak_tvl, peak_date, current_tvl)
    if days_since_peak < min_days_since_peak:
        return DeathVerdict("too_early", peak_tvl, peak_date, current_tvl)

    last_90 = [tvl for _d, tvl in series if _d > current_date - timedelta(days=90)]
    last_90_mean = sum(last_90) / len(last_90) if last_90 else 0.0

    if current_tvl / peak_tvl > threshold_pct or last_90_mean / peak_tvl > threshold_pct:
        return DeathVerdict("alive", peak_tvl, peak_date, current_tvl)

    return DeathVerdict("dead", peak_tvl, peak_date, current_tvl)


def _parse_cdx_row(raw: object) -> tuple[date, str, str] | None:
    if not isinstance(raw, list):
        return None
    row = cast("list[object]", raw)
    if len(row) < 5:  # noqa: PLR2004 — need statuscode at index 4
        return None
    ts, original, statuscode = row[1], row[2], row[4]
    if not isinstance(ts, str) or not isinstance(original, str) or statuscode != "200":
        return None
    try:
        snap_date = datetime.strptime(ts[:8], "%Y%m%d").replace(tzinfo=UTC).date()
    except ValueError:
        return None
    return snap_date, ts, original


async def wayback_snapshot_near(
    url: str,
    target_date: date,
    *,
    user_agent: str = USER_AGENT,
) -> str | None:
    """Resolve ``url`` to a status-200 Wayback snapshot near ``target_date`` (±30d, then ±180d)."""
    for window_days in (WAYBACK_WINDOW_NARROW_DAYS, WAYBACK_WINDOW_WIDE_DAYS):
        from_d = (target_date - timedelta(days=window_days)).strftime("%Y%m%d")
        to_d = (target_date + timedelta(days=window_days)).strftime("%Y%m%d")
        query = urlencode({"url": url, "from": from_d, "to": to_d, "output": "json", "limit": 50})
        cdx_url = f"{WAYBACK_CDX_ENDPOINT}?{query}"
        if not await respect_robots(cdx_url, user_agent=user_agent):
            logger.info("defillama: robots blocked %s", cdx_url)
            return None
        await throttle_for(cdx_url, rps=2.0)
        resp = await safe_get(cdx_url)
        if resp.status_code >= HTTP_BAD_REQUEST:
            logger.warning("defillama: wayback HTTP %s for %s", resp.status_code, url)
            continue
        payload = cast("object", resp.json())
        if not isinstance(payload, list):
            continue
        rows = cast("list[object]", payload)
        if len(rows) < 2:  # noqa: PLR2004 — header + at least one row
            continue
        candidates = [r for raw in rows[1:] if (r := _parse_cdx_row(raw)) is not None]
        if not candidates:
            continue
        closest = min(candidates, key=lambda c: abs((c[0] - target_date).days))
        _snap_date, ts, original = closest
        return f"https://web.archive.org/web/{ts}/{original}"
    return None


def _seed_markdown(detail: dict[str, Any], verdict: DeathVerdict) -> str:  # pyright: ignore[reportExplicitAny]
    """Minimal pitch-shaped seed text. TavilyEnricher fills the real body from Wayback."""
    name: object = detail.get("name") or ""
    category: object = detail.get("category") or ""
    chain: object = detail.get("chain") or ""
    description: object = detail.get("description") or ""
    parts = [
        f"# {name}",
        "",
        f"category: {category}",
        f"chain: {chain}",
        f"tvl_peak_usd: {verdict.peak_tvl}",
        f"peak_date: {verdict.peak_date}",
        f"tvl_current_usd: {verdict.current_tvl}",
        "",
        str(description),
    ]
    return "\n".join(parts).strip()


class DefiLlamaSource:
    """[Source] Yields dead/zombie DeFi protocols, anchored to peak-era Wayback snapshots."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        dead_threshold_pct: float = DEFAULT_DEAD_THRESHOLD_PCT,
        peak_floor_usd: float = DEFAULT_PEAK_FLOOR_USD,
        min_days_since_peak: int = DEFAULT_MIN_DAYS_SINCE_PEAK,
        shortlist_tvl_ceiling_usd: float = DEFAULT_SHORTLIST_TVL_CEILING_USD,
        max_emit: int = DEFAULT_MAX_EMIT,
        user_agent: str = USER_AGENT,
        rps: float = 1.0,
    ) -> None:
        self.dead_threshold_pct = dead_threshold_pct
        self.peak_floor_usd = peak_floor_usd
        self.min_days_since_peak = min_days_since_peak
        self.shortlist_tvl_ceiling_usd = shortlist_tvl_ceiling_usd
        self.max_emit = max_emit
        self.user_agent = user_agent
        self.rps = rps

    async def _fetch_json(self, url: str) -> object | None:
        if not await respect_robots(url, user_agent=self.user_agent):
            logger.info("defillama: robots blocked %s", url)
            return None
        await throttle_for(url, rps=self.rps)
        resp = await safe_get(url)
        if resp.status_code >= HTTP_BAD_REQUEST:
            logger.warning("defillama: HTTP %s for %s", resp.status_code, url)
            return None
        return cast("object", resp.json())

    async def _classify_candidate(
        self, slug: str, live_url: str
    ) -> tuple[dict[str, Any], DeathVerdict, str] | None:  # pyright: ignore[reportExplicitAny]
        detail_payload = await self._fetch_json(f"{DETAIL_ENDPOINT_BASE}/{slug}")
        if not isinstance(detail_payload, dict):
            return None
        detail = cast("dict[str, Any]", detail_payload)  # pyright: ignore[reportExplicitAny]

        chain_tvls_raw: object = detail.get("chainTvls") or {}
        if not isinstance(chain_tvls_raw, dict):
            return None
        chain_tvls = cast("dict[str, dict[str, Any]]", chain_tvls_raw)  # pyright: ignore[reportExplicitAny]

        verdict = classify_death(
            chain_tvls,
            threshold_pct=self.dead_threshold_pct,
            peak_floor_usd=self.peak_floor_usd,
            min_days_since_peak=self.min_days_since_peak,
        )
        if verdict.status != "dead" or verdict.peak_date is None:
            logger.info("defillama: %s verdict=%s, skipping", slug, verdict.status)
            return None

        snapshot_url = await wayback_snapshot_near(
            live_url, verdict.peak_date, user_agent=self.user_agent
        )
        if snapshot_url is None:
            logger.info("defillama: %s no wayback coverage near %s", slug, verdict.peak_date)
            return None

        return detail, verdict, snapshot_url

    async def _process_candidate(self, row: dict[str, Any]) -> RawEntry | None:  # pyright: ignore[reportExplicitAny]
        slug: object = row.get("slug")
        live_url: object = row.get("url")
        if not isinstance(slug, str) or not slug:
            return None
        if not isinstance(live_url, str) or not live_url:
            logger.info("defillama: %s missing url, skipping", slug)
            return None

        result = await self._classify_candidate(slug, live_url)
        if result is None:
            return None
        detail, verdict, snapshot_url = result

        return RawEntry(
            source=SOURCE_DEFILLAMA,
            source_id=slug,
            url=snapshot_url,
            raw_html=None,
            markdown_text=_seed_markdown(detail, verdict),
            fetched_at=datetime.now(UTC),
        )

    async def fetch(self) -> AsyncIterator[RawEntry]:
        bulk_payload = await self._fetch_json(BULK_ENDPOINT)
        if not isinstance(bulk_payload, list):
            logger.warning("defillama: unexpected /protocols payload type")
            return
        rows = cast("list[object]", bulk_payload)

        emitted = 0
        for raw in rows:
            if emitted >= self.max_emit:
                logger.info("defillama: max_emit=%d reached, stopping", self.max_emit)
                return
            if not isinstance(raw, dict):
                continue
            row = cast("dict[str, Any]", raw)  # pyright: ignore[reportExplicitAny]
            tvl_field: object = row.get("tvl")
            if not isinstance(tvl_field, (int, float)):
                continue
            tvl_value = float(tvl_field)
            if tvl_value <= 0 or tvl_value >= self.shortlist_tvl_ceiling_usd:
                continue
            entry = await self._process_candidate(row)
            if entry is not None:
                yield entry
                emitted += 1
