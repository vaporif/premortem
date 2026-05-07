"""DefiLlama source: classify_death, Wayback resolution, fetch integration, cassette round-trip."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest

from slopmortem.corpus.sources import DefiLlamaSource
from slopmortem.corpus.sources.defillama import classify_death, wayback_snapshot_near

CASSETTE_FILE = (
    Path(__file__).parent / "cassettes" / "test_defillama" / "test_defillama_round_trip.yaml"
)


class _FakeResp:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self) -> object:
        return self._payload


def _series(points: list[tuple[str, float]]) -> dict[str, list[dict[str, Any]]]:
    """Helper: build a chainTvls per-chain payload from (iso_date, tvl_usd) tuples.

    Wraps the daily series under `"tvl"` to match the live API shape — DefiLlama
    returns `chainTvls[<chain>] = {"tvl": [...], "tokensInUsd": [...], "tokens": [...]}`,
    not a bare list of points.
    """
    return {
        "tvl": [
            {
                "date": int(datetime.fromisoformat(d).replace(tzinfo=UTC).timestamp()),
                "totalLiquidityUSD": v,
            }
            for d, v in points
        ]
    }


# ---- classify_death ----------------------------------------------------------


def test_classify_death_dead_zombie() -> None:
    # Primitive-shaped: peak $1.7M in May 2022, current $58K (~3.4% of peak).
    series = _series(
        [("2022-05-31", 1_720_000.0)] + [(f"2025-01-{d:02d}", 58_000.0) for d in range(1, 30)]
    )
    verdict = classify_death({"Ethereum": series})
    assert verdict.status == "dead"
    assert verdict.peak_tvl == 1_720_000.0
    assert verdict.peak_date == datetime(2022, 5, 31, tzinfo=UTC).date()


def test_classify_death_alive() -> None:
    series = _series([("2024-01-01", 1_000_000.0), ("2024-12-01", 1_000_000.0)])
    verdict = classify_death({"Ethereum": series})
    assert verdict.status == "alive"


def test_classify_death_never_launched() -> None:
    # peak < $1M floor
    series = _series([(f"2024-12-{d:02d}", 50_000.0) for d in range(1, 30)])
    verdict = classify_death({"Ethereum": series})
    assert verdict.status == "never_launched"


def test_classify_death_too_early() -> None:
    # peak last week — no time to call death
    today = datetime.now(UTC).date()
    series = _series(
        [
            ((today - timedelta(days=7)).isoformat(), 5_000_000.0),
            ((today - timedelta(days=1)).isoformat(), 100.0),
        ]
    )
    verdict = classify_death({"Ethereum": series})
    assert verdict.status == "too_early"


def test_classify_death_recently_dipped() -> None:
    # peak long ago, recent 90d mean still healthy → not death yet
    # (one-day spike to zero doesn't count)
    today = datetime.now(UTC).date()
    series = _series(
        [
            ((today - timedelta(days=400)).isoformat(), 10_000_000.0),
            *[((today - timedelta(days=i)).isoformat(), 8_000_000.0) for i in range(89, 1, -1)],
            ((today - timedelta(days=1)).isoformat(), 0.0),  # one-day blip
        ]
    )
    verdict = classify_death({"Ethereum": series})
    # current_tvl_pct fails first; either status acceptable as long as != "dead"
    assert verdict.status == "alive"


def test_classify_death_dedupes_intra_day_duplicates() -> None:
    """`chainTvls[<chain>]["tvl"]` can include multiple points stamped to the same date.

    Validated live: Primitive's series on 2026-05-06 shipped two end-of-day rows
    ($58,678 then $58,168). Naive summing inflates "current" to ~$117K and trips
    the 5%-of-peak threshold backwards — a real zombie classifies as alive.
    Dedupe (last-write-wins) per (chain, date) keeps current_tvl honest.
    """
    today = datetime.now(UTC).date()
    today_iso = today.isoformat()
    series = _series(
        [("2022-05-31", 1_720_000.0)]
        + [((today - timedelta(days=i)).isoformat(), 58_000.0) for i in range(89, 0, -1)]
        + [(today_iso, 58_500.0), (today_iso, 58_168.0)]  # two points stamped to today
    )
    verdict = classify_death({"Ethereum": series})
    assert verdict.status == "dead"
    assert verdict.current_tvl == 58_168.0  # last-write-wins, not the 116,668 sum


# ---- wayback_snapshot_near ---------------------------------------------------


_CDX_HEADER = ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]
_PRIM_URL = "https://primitive.xyz/"
_PRIM_KEY = "xyz,primitive)/"


async def test_wayback_picks_closest_200(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _CDX_HEADER,
        [_PRIM_KEY, "20220301000000", _PRIM_URL, "text/html", "200", "X", "1"],
        [_PRIM_KEY, "20220525000000", _PRIM_URL, "text/html", "200", "Y", "1"],
        [_PRIM_KEY, "20220601000000", _PRIM_URL, "text/html", "404", "Z", "1"],
    ]
    fake = AsyncMock(return_value=_FakeResp(rows))
    monkeypatch.setattr("slopmortem.corpus.sources.defillama.safe_get", fake)

    target = datetime(2022, 5, 30, tzinfo=UTC).date()
    result = await wayback_snapshot_near(_PRIM_URL, target)
    assert result is not None
    assert "20220525000000" in result
    assert "primitive.xyz" in result


async def test_wayback_returns_none_when_no_200(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _CDX_HEADER,
        [_PRIM_KEY, "20220301000000", _PRIM_URL, "text/html", "404", "X", "1"],
    ]
    fake = AsyncMock(return_value=_FakeResp(rows))
    monkeypatch.setattr("slopmortem.corpus.sources.defillama.safe_get", fake)

    result = await wayback_snapshot_near(
        "https://primitive.xyz/", datetime(2022, 5, 30, tzinfo=UTC).date()
    )
    assert result is None


# ---- DefiLlamaSource.fetch integration ---------------------------------------


def _setup_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "slopmortem.corpus.sources.defillama.respect_robots",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "slopmortem.corpus.sources.defillama.throttle_for",
        AsyncMock(return_value=None),
    )


async def test_emits_dead_protocol_with_wayback_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The full happy path: shortlist → detail → classify_death dead → Wayback → RawEntry."""
    bulk = [
        {
            "id": "1",
            "name": "Alive",
            "slug": "alive",
            "url": "https://alive.example",
            "tvl": 5_000_000.0,
        },
        {
            "id": "2",
            "name": "Dead Protocol",
            "slug": "dead-protocol",
            "url": "https://dead.example",
            "tvl": 58_000.0,
            "category": "Options",
            "description": "On-chain options.",
        },
    ]
    detail = {
        "name": "Dead Protocol",
        "url": "https://dead.example",
        "category": "Options",
        "description": "On-chain options.",
        "chainTvls": {
            "Ethereum": _series(
                [("2022-05-31", 1_720_000.0)]
                + [(f"2025-01-{d:02d}", 58_000.0) for d in range(1, 30)]
            ),
        },
    }
    cdx_rows = [
        _CDX_HEADER,
        ["example,dead)/", "20220525120000", "https://dead.example/", "text/html", "200", "X", "1"],
    ]

    async def fake_get(url: str, **_: object) -> _FakeResp:
        if url.endswith("/protocols"):
            return _FakeResp(bulk)
        if "/protocol/dead-protocol" in url:
            return _FakeResp(detail)
        if "web.archive.org/cdx" in url:
            return _FakeResp(cdx_rows)
        msg = f"unexpected url: {url}"
        raise AssertionError(msg)

    target = "slopmortem.corpus.sources.defillama.safe_get"
    monkeypatch.setattr(target, AsyncMock(side_effect=fake_get))
    _setup_throttle(monkeypatch)

    src = DefiLlamaSource(shortlist_tvl_ceiling_usd=100_000.0)
    entries = [e async for e in src.fetch()]
    assert len(entries) == 1
    e = entries[0]
    assert e.source == "defillama"
    assert e.source_id == "dead-protocol"
    assert e.url is not None
    assert "web.archive.org" in e.url
    assert "20220525120000" in e.url
    # Body is intentionally empty: TavilyEnricher's "skip if markdown_text non-empty"
    # short-circuit means a seed body would block extraction of the real Wayback pitch.
    assert e.markdown_text is None
    assert e.raw_html is None


async def test_skips_when_no_wayback_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    bulk = [
        {"id": "1", "name": "Dead", "slug": "dead", "url": "https://dead.example", "tvl": 50_000.0}
    ]
    detail = {
        "name": "Dead",
        "url": "https://dead.example",
        "chainTvls": {
            "Ethereum": _series(
                [("2022-01-01", 5_000_000.0), ("2025-01-01", 0.0)],
            ),
        },
    }
    cdx_rows = [_CDX_HEADER]

    async def fake_get(url: str, **_: object) -> _FakeResp:
        if url.endswith("/protocols"):
            return _FakeResp(bulk)
        if "/protocol/" in url:
            return _FakeResp(detail)
        return _FakeResp(cdx_rows)

    target = "slopmortem.corpus.sources.defillama.safe_get"
    monkeypatch.setattr(target, AsyncMock(side_effect=fake_get))
    _setup_throttle(monkeypatch)

    src = DefiLlamaSource(shortlist_tvl_ceiling_usd=100_000.0)
    entries = [e async for e in src.fetch()]
    assert entries == []


async def test_max_emit_caps_yield(monkeypatch: pytest.MonkeyPatch) -> None:
    bulk = [
        {
            "id": str(i),
            "name": f"D{i}",
            "slug": f"d{i}",
            "url": f"https://d{i}.example",
            "tvl": 50_000.0,
        }
        for i in range(10)
    ]

    def make_detail(i: int) -> dict[str, object]:
        return {
            "name": f"D{i}",
            "url": f"https://d{i}.example",
            "chainTvls": {
                "Ethereum": _series([("2022-01-01", 5_000_000.0), ("2025-01-01", 0.0)]),
            },
        }

    async def fake_get(url: str, **_: object) -> _FakeResp:
        if url.endswith("/protocols"):
            return _FakeResp(bulk)
        if "/protocol/" in url:
            slug = url.rsplit("/", 1)[-1]
            return _FakeResp(make_detail(int(slug[1:])))
        return _FakeResp(
            [
                _CDX_HEADER,
                [
                    "example,d)/",
                    "20220601000000",
                    "https://d.example/",
                    "text/html",
                    "200",
                    "X",
                    "1",
                ],
            ]
        )

    target = "slopmortem.corpus.sources.defillama.safe_get"
    monkeypatch.setattr(target, AsyncMock(side_effect=fake_get))
    _setup_throttle(monkeypatch)

    src = DefiLlamaSource(shortlist_tvl_ceiling_usd=100_000.0, max_emit=3)
    entries = [e async for e in src.fetch()]
    assert len(entries) == 3


async def test_bulk_httpx_error_returns_no_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient httpx error on /protocols must not propagate out of fetch()."""
    fake = AsyncMock(side_effect=httpx.ReadTimeout("boom"))
    monkeypatch.setattr("slopmortem.corpus.sources.defillama.safe_get", fake)
    _setup_throttle(monkeypatch)

    src = DefiLlamaSource()
    entries = [e async for e in src.fetch()]
    assert entries == []


async def test_cdx_httpx_error_drops_candidate_not_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Wayback CDX timeout drops the affected candidate; siblings still yield.

    Plan §1.1 explicitly anticipates Wayback CDX flakiness; the source must
    treat a CDX exception identically to a status-200-less window — skip the
    candidate, keep iterating.
    """
    bulk = [
        {
            "id": "1",
            "name": "Flaky",
            "slug": "flaky",
            "url": "https://flaky.example",
            "tvl": 50_000.0,
        },
        {
            "id": "2",
            "name": "Healthy",
            "slug": "healthy",
            "url": "https://healthy.example",
            "tvl": 50_000.0,
        },
    ]

    def make_detail(name: str) -> dict[str, object]:
        return {
            "name": name,
            "url": f"https://{name.lower()}.example",
            "chainTvls": {
                "Ethereum": _series([("2022-01-01", 5_000_000.0), ("2025-01-01", 0.0)]),
            },
        }

    cdx_ok = [
        _CDX_HEADER,
        [
            "example,healthy)/",
            "20220525120000",
            "https://healthy.example/",
            "text/html",
            "200",
            "X",
            "1",
        ],
    ]

    async def fake_get(url: str, **_: object) -> _FakeResp:
        if url.endswith("/protocols"):
            return _FakeResp(bulk)
        if "/protocol/flaky" in url:
            return _FakeResp(make_detail("Flaky"))
        if "/protocol/healthy" in url:
            return _FakeResp(make_detail("Healthy"))
        if "web.archive.org/cdx" in url and "flaky.example" in url:
            timeout_msg = "cdx down"
            raise httpx.ReadTimeout(timeout_msg)
        if "web.archive.org/cdx" in url and "healthy.example" in url:
            return _FakeResp(cdx_ok)
        msg = f"unexpected url: {url}"
        raise AssertionError(msg)

    monkeypatch.setattr(
        "slopmortem.corpus.sources.defillama.safe_get", AsyncMock(side_effect=fake_get)
    )
    _setup_throttle(monkeypatch)

    src = DefiLlamaSource(shortlist_tvl_ceiling_usd=100_000.0)
    entries = [e async for e in src.fetch()]
    assert len(entries) == 1
    assert entries[0].source_id == "healthy"


async def test_fetch_runs_candidates_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fan-out: peak in-flight ``safe_get`` calls > 1 across the candidate set.

    With sequential iteration peak would be 1; the limiter caps it at concurrency.
    The test only asserts ``>= 2`` so it stays robust to scheduler quirks.
    """
    bulk = [
        {
            "id": str(i),
            "name": f"D{i}",
            "slug": f"d{i}",
            "url": f"https://d{i}.example",
            "tvl": 50_000.0,
        }
        for i in range(5)
    ]

    def make_detail(slug: str) -> dict[str, object]:
        return {
            "name": slug,
            "url": f"https://{slug}.example",
            "chainTvls": {
                "Ethereum": _series([("2022-01-01", 5_000_000.0), ("2025-01-01", 0.0)]),
            },
        }

    cdx_ok = [
        _CDX_HEADER,
        ["example,d)/", "20220601000000", "https://d.example/", "text/html", "200", "X", "1"],
    ]

    in_flight = 0
    peak = 0
    state_lock = anyio.Lock()

    async def slow_get(url: str, **_: object) -> _FakeResp:
        nonlocal in_flight, peak
        if url.endswith("/protocols"):
            return _FakeResp(bulk)
        async with state_lock:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            await anyio.sleep(0.05)
        finally:
            async with state_lock:
                in_flight -= 1
        if "/protocol/" in url:
            slug = url.rsplit("/", 1)[-1]
            return _FakeResp(make_detail(slug))
        return _FakeResp(cdx_ok)

    monkeypatch.setattr(
        "slopmortem.corpus.sources.defillama.safe_get", AsyncMock(side_effect=slow_get)
    )
    _setup_throttle(monkeypatch)

    src = DefiLlamaSource(shortlist_tvl_ceiling_usd=100_000.0, concurrency=3, max_emit=5)
    with anyio.fail_after(5):
        entries = [e async for e in src.fetch()]
    assert len(entries) == 5
    assert peak >= 2


async def test_wayback_concurrency_bounds_cdx_fanout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wayback CDX peak in-flight ≤ wayback_concurrency, regardless of detail-fetch concurrency.

    Detail-fetch and CDX share no slot pool — Wayback rejects parallel TCP opens
    past a small per-IP cap, so the wayback limiter must isolate it from the
    higher detail-fetch concurrency.
    """
    bulk = [
        {
            "id": str(i),
            "name": f"D{i}",
            "slug": f"d{i}",
            "url": f"https://d{i}.example",
            "tvl": 50_000.0,
        }
        for i in range(8)
    ]

    def make_detail(slug: str) -> dict[str, object]:
        return {
            "name": slug,
            "url": f"https://{slug}.example",
            "chainTvls": {
                "Ethereum": _series([("2022-01-01", 5_000_000.0), ("2025-01-01", 0.0)]),
            },
        }

    cdx_ok = [
        _CDX_HEADER,
        ["example,d)/", "20220601000000", "https://d.example/", "text/html", "200", "X", "1"],
    ]

    cdx_in_flight = 0
    cdx_peak = 0
    state_lock = anyio.Lock()

    async def slow_get(url: str, **_: object) -> _FakeResp:
        nonlocal cdx_in_flight, cdx_peak
        if url.endswith("/protocols"):
            return _FakeResp(bulk)
        if "web.archive.org/cdx" in url:
            async with state_lock:
                cdx_in_flight += 1
                cdx_peak = max(cdx_peak, cdx_in_flight)
            try:
                await anyio.sleep(0.05)
            finally:
                async with state_lock:
                    cdx_in_flight -= 1
            return _FakeResp(cdx_ok)
        # protocol detail — fast, doesn't count toward CDX peak
        slug = url.rsplit("/", 1)[-1]
        return _FakeResp(make_detail(slug))

    monkeypatch.setattr(
        "slopmortem.corpus.sources.defillama.safe_get", AsyncMock(side_effect=slow_get)
    )
    _setup_throttle(monkeypatch)

    # High detail-fetch concurrency, low wayback concurrency — verifies the
    # decoupling holds.
    src = DefiLlamaSource(
        shortlist_tvl_ceiling_usd=100_000.0,
        concurrency=8,
        wayback_concurrency=2,
        max_emit=8,
    )
    with anyio.fail_after(5):
        entries = [e async for e in src.fetch()]
    assert len(entries) == 8
    assert cdx_peak <= 2
    assert cdx_peak >= 1


@pytest.mark.vcr
async def test_defillama_round_trip() -> None:
    if not CASSETTE_FILE.exists() and not os.environ.get("RECORD"):
        pytest.skip(f"no cassette at {CASSETTE_FILE}; rerun with RECORD=1 to record")
    # Tight max_emit so the cassette stays small and the test is fast.
    src = DefiLlamaSource(shortlist_tvl_ceiling_usd=100_000.0, max_emit=5)
    entries = [e async for e in src.fetch()]
    assert all(e.source == "defillama" for e in entries)
    # If anything was emitted, it must be Wayback-anchored.
    for e in entries:
        assert e.url is not None
        assert "web.archive.org" in e.url
