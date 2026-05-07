"""Tavily news source: rolling-window shutdown-event discovery.

Pipeline:
  1. Materialise (query, year, quarter) triples from YAML defaults
     overridden by constructor kwargs.
  2. Fan out one POST /search per triple under the configured
     CapacityLimiter, routed through gather_resilient so a bad
     call doesn't take down siblings.
  3. Flatten + filter (min_score, missing url/raw_content/score).
  4. Canonicalise URLs (strip utm_*, fbclid, gclid, ref, ref_src,
     feature, _ga; lowercase host; drop fragment; trailing slash).
  5. Drop mirror/aggregator hosts via mirror_domains.yml suffix match.
  6. Dedup on canonical URL keeping the highest-scored row.
  7. Sort (score desc, published_date desc, canonical_url asc) and
     cap at max_emit (default 200).
  8. Yield one RawEntry per kept row, with raw_content as
     markdown_text (no Wayback / Tavily-extract hop required).

Why collect-then-rank instead of streaming: parallel gather_resilient
completion order is non-deterministic. Score-best-wins matters more
than first-yield latency, and the peak memory across the
queries x years x 4 fan-out is trivial.
"""

from __future__ import annotations

import functools
import logging
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from importlib import resources
from typing import TYPE_CHECKING, Final, cast
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import anyio
import httpx
import yaml

from slopmortem.concurrency import gather_resilient
from slopmortem.corpus.sources._names import SOURCE_TAVILY_NEWS
from slopmortem.corpus.sources._throttle import HTTP_BAD_REQUEST, throttle_for
from slopmortem.http import safe_post
from slopmortem.models import RawEntry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

logger = logging.getLogger(__name__)

TAVILY_SEARCH_ENDPOINT: Final = "https://api.tavily.com/search"
DEFAULT_CONCURRENCY: Final = 5
DEFAULT_RPS: Final = 1.0
DEFAULT_MAX_RESULTS: Final = 20  # Tavily per-call cap

_TRACKING_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "fbclid",
        "gclid",
        "ref",
        "ref_src",
        "feature",
        "_ga",
    }
)

# Quarterly windows. Tavily's max_results=20 is per-call, so quarterly
# slicing forces the index to surface its top-20 four times per year.
_QUARTERS: Final[tuple[tuple[str, str], ...]] = (
    ("01-01", "03-31"),
    ("04-01", "06-30"),
    ("07-01", "09-30"),
    ("10-01", "12-31"),
)


# ---- YAML loading ------------------------------------------------------------


def _load_yaml(package: str, filename: str) -> object:
    text = resources.files(package).joinpath(filename).read_text(encoding="utf-8")
    # PyYAML's stub types safe_load as Any; downstream callers narrow with isinstance.
    return yaml.safe_load(text)  # pyright: ignore[reportAny]


@functools.cache
def _load_defaults() -> dict[str, object]:
    payload = _load_yaml("slopmortem.corpus.sources.queries", "tavily_news.yml")
    if not isinstance(payload, dict):
        msg = "tavily_news.yml must be a mapping"
        raise TypeError(msg)
    return cast("dict[str, object]", payload)


@functools.cache
def _load_mirror_domains() -> frozenset[str]:
    payload = _load_yaml("slopmortem.corpus.sources", "mirror_domains.yml")
    if not isinstance(payload, list):
        return frozenset()
    rows = cast("list[object]", payload)
    return frozenset(r.lower() for r in rows if isinstance(r, str) and r)


# ---- pure helpers ------------------------------------------------------------


def _canonicalize_url(url: str) -> str:
    """Normalise a URL for stable dedup."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    netloc = host
    if parsed.port is not None:
        netloc = f"{host}:{parsed.port}"
    pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    query = urlencode(pairs)
    path = parsed.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, parsed.params, query, ""))


def _parse_published_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _drop_mirror_hosts(
    rows: Iterable[dict[str, object]],
    *,
    mirrors: frozenset[str],
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for row in rows:
        host = row.get("host")
        if not isinstance(host, str):
            continue
        if any(host == d or host.endswith("." + d) for d in mirrors):
            logger.debug("tavily_news: dropping mirror host %s", host)
            continue
        out.append(row)
    return out


def _dedup_keep_highest_score(
    rows: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    # Rows reaching this point already passed _filter_and_canonicalise, so
    # canonical_url is a str and score is a float. Keep narrowing checks just
    # for the type checker; the runtime branches are unreachable.
    best: dict[str, dict[str, object]] = {}
    for row in rows:
        url = row.get("canonical_url")
        score = row.get("score")
        if not isinstance(url, str) or not isinstance(score, (int, float)):
            continue
        existing = best.get(url)
        if existing is None:
            best[url] = row
            continue
        existing_score = existing.get("score")
        if isinstance(existing_score, (int, float)) and float(score) > float(existing_score):
            best[url] = row
    return list(best.values())


def _build_call_descriptors(
    *,
    queries: list[str],
    start_year: int,
    end_year: int,
    today: date | None = None,
) -> list[dict[str, object]]:
    """Cross-product (query, year, quarter) → list of call payload dicts.

    Future quarters are skipped and the current quarter's ``end_date`` is
    clamped to ``today``: Tavily 400s on fully-future windows. ISO-8601
    dates sort lexicographically, so plain string compare is enough.
    """
    today_iso = (today or datetime.now(UTC).date()).isoformat()
    return [
        {
            "query": query,
            "year": year,
            "start_date": f"{year}-{q_start}",
            "end_date": min(f"{year}-{q_end}", today_iso),
        }
        for query in queries
        for year in range(start_year, end_year + 1)
        for q_start, q_end in _QUARTERS
        if f"{year}-{q_start}" <= today_iso
    ]


def _pick_int(override: int | None, yaml_val: object, fallback: int) -> int:
    if override is not None:
        return override
    if isinstance(yaml_val, int):
        return yaml_val
    return fallback


def _pick_float(override: float | None, yaml_val: object, fallback: float) -> float:
    if override is not None:
        return override
    if isinstance(yaml_val, (int, float)):
        return float(yaml_val)
    return fallback


def _pick_str(override: str | None, yaml_val: object, fallback: str) -> str:
    if override is not None:
        return override
    if isinstance(yaml_val, str) and yaml_val:
        return yaml_val
    return fallback


# ---- TavilyNewsSource --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CallSpec:
    query: str
    year: int
    start_date: str
    end_date: str


class TavilyNewsSource:
    """[Source] Tavily /search across (query, year, quarter) triples; one RawEntry per article."""

    def __init__(  # noqa: PLR0913 - YAML defaults overridable per-knob from CLI/config
        self,
        *,
        queries: list[str] | None = None,
        start_year: int | None = None,
        end_year: int | None = None,
        max_emit: int | None = None,
        min_score: float | None = None,
        search_depth: str | None = None,
        rps: float = DEFAULT_RPS,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        defaults = _load_defaults()
        yaml_queries_raw = defaults.get("queries")
        if yaml_queries_raw is not None and not isinstance(yaml_queries_raw, list):
            msg = "tavily_news.yml: queries must be a list"
            raise TypeError(msg)
        yaml_queries = cast("list[object]", yaml_queries_raw or [])
        yaml_year_range_raw = defaults.get("year_range")
        yaml_year_range: dict[str, object] = (
            cast("dict[str, object]", yaml_year_range_raw)
            if isinstance(yaml_year_range_raw, dict)
            else {}
        )

        self.queries: list[str] = queries or [q for q in yaml_queries if isinstance(q, str)]
        if not self.queries:
            msg = "TavilyNewsSource: at least one query is required"
            raise ValueError(msg)

        self.start_year: int = _pick_int(start_year, yaml_year_range.get("start"), 2024)
        self.end_year: int = _pick_int(end_year, yaml_year_range.get("end"), datetime.now(UTC).year)
        if self.end_year < self.start_year:
            msg = f"end_year ({self.end_year}) < start_year ({self.start_year})"
            raise ValueError(msg)

        self.max_emit: int = _pick_int(max_emit, defaults.get("max_emit"), 200)
        self.min_score: float = _pick_float(min_score, defaults.get("min_score"), 0.3)
        self.search_depth: str = _pick_str(search_depth, defaults.get("search_depth"), "basic")

        self.rps = rps
        self.concurrency = concurrency
        self._mirrors: frozenset[str] = _load_mirror_domains()

    async def _one_call(
        self,
        spec: _CallSpec,
        *,
        api_key: str,
        limiter: anyio.CapacityLimiter,
    ) -> list[dict[str, object]] | None:
        """Issue one POST /search; return its ``results`` list, or ``None`` on failure.

        ``None`` lets the caller distinguish "call failed" from "call returned
        zero rows", which the per-call counter relies on. Returning ``[]``
        would silently masquerade as an empty-but-successful page.
        """
        async with limiter:
            await throttle_for(TAVILY_SEARCH_ENDPOINT, rps=self.rps)
            try:
                resp = await safe_post(
                    TAVILY_SEARCH_ENDPOINT,
                    json={
                        "api_key": api_key,
                        "query": spec.query,
                        "topic": "news",
                        "start_date": spec.start_date,
                        "end_date": spec.end_date,
                        "search_depth": self.search_depth,
                        "max_results": DEFAULT_MAX_RESULTS,
                        "include_raw_content": True,
                    },
                )
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(
                    "tavily_news: fetch failed query=%r year=%s start=%s: %s",
                    spec.query,
                    spec.year,
                    spec.start_date,
                    exc,
                )
                return None

            if resp.status_code >= HTTP_BAD_REQUEST:
                logger.warning(
                    "tavily_news: HTTP %s query=%r start=%s",
                    resp.status_code,
                    spec.query,
                    spec.start_date,
                )
                return None

            try:
                payload: object = resp.json()  # pyright: ignore[reportAny]
            except ValueError:
                logger.warning(
                    "tavily_news: non-JSON response query=%r start=%s", spec.query, spec.start_date
                )
                return None

            if not isinstance(payload, dict):
                return None
            results: object = cast("dict[str, object]", payload).get("results")
            if not isinstance(results, list):
                return None
            return [
                cast("dict[str, object]", raw)
                for raw in cast("list[object]", results)
                if isinstance(raw, dict)
            ]

    def _filter_and_canonicalise(
        self, raw_results: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        kept: list[dict[str, object]] = []
        for row in raw_results:
            url = row.get("url")
            score = row.get("score")
            raw_content = row.get("raw_content")
            if not isinstance(url, str) or not url:
                continue
            if not isinstance(score, (int, float)) or float(score) < self.min_score:
                continue
            if not isinstance(raw_content, str) or not raw_content:
                continue
            canonical = _canonicalize_url(url)
            kept.append(
                {
                    "canonical_url": canonical,
                    "host": (urlparse(canonical).hostname or "").lower(),
                    "score": float(score),
                    "raw_content": raw_content,
                    "title": row.get("title", ""),
                    "published_date": row.get("published_date"),
                }
            )
        return kept

    async def fetch(self) -> AsyncIterator[RawEntry]:
        api_key = os.environ.get("TAVILY_API_KEY", "")
        if not api_key:
            logger.warning("tavily_news: TAVILY_API_KEY not set; yielding no entries")
            return

        descriptors = _build_call_descriptors(
            queries=self.queries, start_year=self.start_year, end_year=self.end_year
        )
        limiter = anyio.CapacityLimiter(self.concurrency)
        specs = [
            _CallSpec(
                query=cast("str", d["query"]),
                year=cast("int", d["year"]),
                start_date=cast("str", d["start_date"]),
                end_date=cast("str", d["end_date"]),
            )
            for d in descriptors
        ]
        results = await gather_resilient(
            *(self._one_call(s, api_key=api_key, limiter=limiter) for s in specs)
        )

        # ``_one_call`` swallows ``httpx.HTTPError`` / ``ValueError`` and returns
        # ``None``; anything else (SSRFBlockedError, TimeoutError, KeyError, ...)
        # surfaces here via ``gather_resilient`` and gets logged with its type so
        # an operator can tell a real outage from an API-shape miss.
        flat: list[dict[str, object]] = []
        failures = 0
        for r in results:
            if isinstance(r, Exception):
                logger.warning(
                    "tavily_news: unexpected error: %s: %s",
                    type(r).__name__,
                    r,
                )
                failures += 1
                continue
            if r is None:
                failures += 1
                continue
            flat.extend(r)
        if failures:
            logger.warning("tavily_news: %d/%d calls failed", failures, len(specs))

        kept = self._filter_and_canonicalise(flat)
        kept = _drop_mirror_hosts(kept, mirrors=self._mirrors)
        kept = _dedup_keep_highest_score(kept)

        # Sort key: score desc, published desc, canonical URL asc.
        # Missing/unparseable published_date sinks to the bottom of its score tier
        # via a 1900-01-01 sentinel — RFC 1123 dates always beat that.
        _date_sentinel = datetime(1900, 1, 1, tzinfo=UTC)

        def _sort_key(row: dict[str, object]) -> tuple[float, float, str]:
            published = _parse_published_date(row.get("published_date")) or _date_sentinel
            return (
                -float(cast("float", row["score"])),
                -published.timestamp(),
                cast("str", row["canonical_url"]),
            )

        kept.sort(key=_sort_key)

        capped = kept[: self.max_emit]
        if len(kept) > self.max_emit:
            logger.info("tavily_news: max_emit=%d reached, stopping", self.max_emit)
        for row in capped:
            yield RawEntry(
                source=SOURCE_TAVILY_NEWS,
                source_id=cast("str", row["canonical_url"]),
                url=cast("str", row["canonical_url"]),
                raw_html=None,
                markdown_text=cast("str", row["raw_content"]),
                fetched_at=datetime.now(UTC),
            )

        if not capped:
            logger.warning(
                "tavily_news: 0 entries after dedup (calls=%d, failures=%d)",
                len(specs),
                failures,
            )
