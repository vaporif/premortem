"""TavilyNewsSource: URL canonicalisation, dedup, mirror-host drop, fetch integration."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from slopmortem.corpus.sources import TavilyNewsSource
from slopmortem.corpus.sources.tavily_news import (
    _build_call_descriptors,
    _canonicalize_url,
    _dedup_keep_highest_score,
    _drop_mirror_hosts,
    _parse_published_date,
)

CASSETTE_FILE = Path(__file__).parent / "cassettes" / "test_tavily_news" / "test_round_trip.yaml"


def _ok_resp(payload: object) -> httpx.Response:
    return httpx.Response(200, json=payload)


# ---- _canonicalize_url -------------------------------------------------------


def test_canonicalize_strips_utm_and_lowercases_host() -> None:
    raw = (
        "HTTPS://Example.COM/Path/?utm_source=newsletter&utm_medium=email"
        "&utm_campaign=x&utm_term=y&utm_content=z&fbclid=abc&gclid=def"
        "&ref=foo&ref_src=bar&feature=baz&_ga=GA1.x"
        "&keep=this#section"
    )
    canon = _canonicalize_url(raw)
    assert canon == "https://example.com/Path?keep=this"


def test_canonicalize_drops_trailing_slash() -> None:
    assert _canonicalize_url("https://example.com/path/") == "https://example.com/path"


def test_canonicalize_preserves_root_slash() -> None:
    assert _canonicalize_url("https://example.com/") == "https://example.com/"


def test_canonicalize_preserves_port() -> None:
    assert (
        _canonicalize_url("https://EXAMPLE.com:8443/path?utm_source=x")
        == "https://example.com:8443/path"
    )


# ---- _dedup_keep_highest_score -----------------------------------------------


def test_dedup_keeps_highest_score() -> None:
    rows: list[dict[str, object]] = [
        {"canonical_url": "https://x/a", "score": 0.4, "title": "lo"},
        {"canonical_url": "https://x/a", "score": 0.6, "title": "hi"},
        {"canonical_url": "https://x/a", "score": 0.5, "title": "mid"},
        {"canonical_url": "https://x/b", "score": 0.7, "title": "other"},
    ]
    out = _dedup_keep_highest_score(rows)
    by_url = {r["canonical_url"]: r for r in out}
    assert len(out) == 2
    assert by_url["https://x/a"]["title"] == "hi"
    assert by_url["https://x/a"]["score"] == 0.6


# ---- _drop_mirror_hosts ------------------------------------------------------


def test_drop_mirror_hosts_suffix_match() -> None:
    rows: list[dict[str, object]] = [
        {"canonical_url": "https://bundle.app/article", "host": "bundle.app"},
        {"canonical_url": "https://m.bundle.app/article", "host": "m.bundle.app"},
        {"canonical_url": "https://news.google.com/foo", "host": "news.google.com"},
        {"canonical_url": "https://techcrunch.com/keep", "host": "techcrunch.com"},
    ]
    out = _drop_mirror_hosts(rows, mirrors=frozenset({"bundle.app", "news.google.com"}))
    urls = {r["canonical_url"] for r in out}
    assert urls == {"https://techcrunch.com/keep"}


# ---- _parse_published_date ---------------------------------------------------


def test_parse_published_rfc1123() -> None:
    dt = _parse_published_date("Tue, 18 Nov 2024 11:05:19 GMT")
    assert dt == datetime(2024, 11, 18, 11, 5, 19, tzinfo=UTC)


def test_parse_published_returns_none_on_garbage() -> None:
    assert _parse_published_date("not a date") is None
    assert _parse_published_date(None) is None
    assert _parse_published_date("") is None


# ---- _build_call_descriptors -------------------------------------------------


def test_build_call_descriptors_quarterly_windows() -> None:
    calls = _build_call_descriptors(
        queries=["q1", "q2"], start_year=2024, end_year=2024, today=date(2025, 1, 1)
    )
    # 2 queries x 1 year x 4 quarters = 8 calls
    assert len(calls) == 8
    quarters = {(c["start_date"], c["end_date"]) for c in calls}
    assert ("2024-01-01", "2024-03-31") in quarters
    assert ("2024-04-01", "2024-06-30") in quarters
    assert ("2024-07-01", "2024-09-30") in quarters
    assert ("2024-10-01", "2024-12-31") in quarters


def test_build_call_descriptors_skips_future_quarters_and_clamps_current() -> None:
    """Tavily 400s on fully-future windows; current quarter's end is clamped to today."""
    calls = _build_call_descriptors(
        queries=["q"], start_year=2026, end_year=2026, today=date(2026, 5, 7)
    )
    quarters = {(c["start_date"], c["end_date"]) for c in calls}
    assert quarters == {
        ("2026-01-01", "2026-03-31"),
        ("2026-04-01", "2026-05-07"),
    }


def test_build_call_descriptors_skips_when_start_year_in_future() -> None:
    calls = _build_call_descriptors(
        queries=["q"], start_year=2027, end_year=2027, today=date(2026, 5, 7)
    )
    assert calls == []


# ---- TavilyNewsSource.fetch integration --------------------------------------


def _result(
    *,
    url: str,
    score: float,
    raw_content: str = "body text long enough to keep",
    published: str = "Tue, 18 Nov 2024 11:05:19 GMT",
) -> dict[str, object]:
    return {
        "title": f"title for {url}",
        "url": url,
        "score": score,
        "published_date": published,
        "raw_content": raw_content,
    }


async def test_emits_dedup_sorted_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fan-out → dedup → mirror-drop → score-filter → sort → cap."""
    page_a = {
        "results": [
            _result(url="https://example.com/post-1", score=0.6),
            _result(url="https://example.com/post-1?utm_source=x", score=0.8),  # dup, higher
            _result(url="https://bundle.app/repost", score=0.9),  # mirror, drop
            _result(url="https://example.com/post-2", score=0.05),  # below min_score
        ]
    }
    page_b = {
        "results": [
            _result(url="https://example.com/post-3", score=0.5),
        ]
    }
    calls: list[dict[str, object]] = []

    async def fake_post(url: str, *, json: dict[str, object], **_: object) -> httpx.Response:
        assert url == "https://api.tavily.com/search"
        calls.append(json)
        # Alternate the two pages so different (query, year, quarter) tuples see different rows.
        return _ok_resp(page_a if len(calls) % 2 == 1 else page_b)

    monkeypatch.setattr("slopmortem.corpus.sources.tavily_news.safe_post", fake_post)
    monkeypatch.setattr(
        "slopmortem.corpus.sources.tavily_news.throttle_for", AsyncMock(return_value=None)
    )
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-key")

    src = TavilyNewsSource(
        queries=["q1"],
        start_year=2024,
        end_year=2024,
        min_score=0.3,
        max_emit=10,
        search_depth="basic",
    )
    entries = [e async for e in src.fetch()]
    # 4 (q1, 2024, Q1..Q4) calls, alternating pages: post-1 (dedup→0.8), post-3 (0.5).
    # Mirror dropped, score-0.05 dropped.
    urls = [e.url for e in entries]
    assert urls == ["https://example.com/post-1", "https://example.com/post-3"]
    e0 = entries[0]
    assert e0.source == "tavily_news"
    assert e0.source_id == "https://example.com/post-1"
    assert e0.markdown_text == "body text long enough to keep"
    assert e0.raw_html is None


async def test_per_call_failure_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """One failing call must not abort siblings."""
    raises_once: dict[str, int] = {"n": 0}

    async def fake_post(url: str, *, json: dict[str, object], **_: object) -> httpx.Response:
        raises_once["n"] += 1
        if raises_once["n"] == 1:
            msg = "boom"
            raise httpx.ConnectError(msg)
        return _ok_resp({"results": [_result(url=f"https://x/{raises_once['n']}", score=0.5)]})

    monkeypatch.setattr("slopmortem.corpus.sources.tavily_news.safe_post", fake_post)
    monkeypatch.setattr(
        "slopmortem.corpus.sources.tavily_news.throttle_for", AsyncMock(return_value=None)
    )
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-key")

    src = TavilyNewsSource(
        queries=["q1"],
        start_year=2024,
        end_year=2024,
        min_score=0.3,
        max_emit=20,
    )
    entries = [e async for e in src.fetch()]
    # 4 calls fired, 1 failed → 3 succeeded → 3 unique results.
    assert len(entries) == 3


async def test_max_emit_caps_yield(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_post(url: str, *, json: dict[str, object], **_: object) -> httpx.Response:
        # Each call returns 5 unique results.
        seed = hash((json.get("query"), json.get("start_date"))) & 0xFFFF
        results = [_result(url=f"https://x/{seed}-{i}", score=0.4 + i * 0.05) for i in range(5)]
        return _ok_resp({"results": results})

    monkeypatch.setattr("slopmortem.corpus.sources.tavily_news.safe_post", fake_post)
    monkeypatch.setattr(
        "slopmortem.corpus.sources.tavily_news.throttle_for", AsyncMock(return_value=None)
    )
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-key")

    src = TavilyNewsSource(
        queries=["q1"], start_year=2024, end_year=2024, min_score=0.3, max_emit=3
    )
    entries = [e async for e in src.fetch()]
    assert len(entries) == 3


async def test_fetch_no_api_key_yields_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defence in depth: even if the CLI gate is bypassed, the source warns and yields nothing."""
    called = False

    async def fake_post(url: str, *, json: dict[str, object], **_: object) -> httpx.Response:
        nonlocal called
        called = True
        return _ok_resp({"results": []})

    monkeypatch.setattr("slopmortem.corpus.sources.tavily_news.safe_post", fake_post)
    monkeypatch.setattr(
        "slopmortem.corpus.sources.tavily_news.throttle_for", AsyncMock(return_value=None)
    )
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    src = TavilyNewsSource(queries=["q1"], start_year=2024, end_year=2024)
    entries = [e async for e in src.fetch()]
    assert entries == []
    assert called is False


async def test_yaml_defaults_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructed without args, the source picks up YAML defaults."""
    captured: list[dict[str, object]] = []

    async def fake_post(url: str, *, json: dict[str, object], **_: object) -> httpx.Response:
        captured.append(json)
        return _ok_resp({"results": []})

    monkeypatch.setattr("slopmortem.corpus.sources.tavily_news.safe_post", fake_post)
    monkeypatch.setattr(
        "slopmortem.corpus.sources.tavily_news.throttle_for", AsyncMock(return_value=None)
    )
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-key")

    src = TavilyNewsSource()
    _ = [e async for e in src.fetch()]
    # YAML default has 10 queries; year_range.start=2024, end defaults to current.
    # Therefore at least 10 queries x 1 year x 4 quarters = 40 calls.
    assert len(captured) >= 40
    # search_depth="basic" propagates from YAML.
    assert all(c["search_depth"] == "basic" for c in captured)


@pytest.mark.vcr(filter_post_data_parameters=["api_key"])
async def test_round_trip() -> None:
    if not CASSETTE_FILE.exists() and not os.environ.get("RECORD"):
        pytest.skip(f"no cassette at {CASSETTE_FILE}; rerun with RECORD=1 to record")
    src = TavilyNewsSource(
        queries=["startup shuts down"],
        start_year=2024,
        end_year=2024,
        max_emit=5,
        # Constrain to a single quarter via direct override at call site if needed.
    )
    entries = [e async for e in src.fetch()]
    assert all(e.source == "tavily_news" for e in entries)
    for e in entries:
        assert e.url is not None
        assert e.markdown_text  # non-empty body
