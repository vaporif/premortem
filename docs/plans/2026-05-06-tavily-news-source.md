# Tavily News Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or `/team-feature` to implement this plan task-by-task, per the Execution Strategy below. Steps use checkbox (`- [ ]`) syntax — these are **persistent durable state**, not visual decoration. The executor edits the plan file in place: `- [ ]` → `- [x]` the instant a step verifies, before moving on. On resume (new session, crash, takeover), the executor scans existing `- [x]` marks and skips them — these steps are NOT redone. TodoWrite mirrors this state in-session; the plan file is the source of truth across sessions.

**Goal:** Add a `TavilyNewsSource` adapter that pulls dead/struggling-startup news from Tavily's `/search` endpoint with `topic=news` and quarterly date windows, plus a generic `--only-source NAME` CLI flag that filters the source list down to a single named source. The source emits `RawEntry`s whose `markdown_text` already carries the article body (no Wayback hop, no `TavilyEnricher` hop), gated behind opt-in `--enable-tavily-news` so the default `just ingest` stays bit-identical.

**Architecture:** One module under `slopmortem/corpus/sources/` implementing the existing `Source` Protocol. Configuration lives in YAML alongside the module so operators can tune queries, year windows, and thresholds without redeploying. Each Tavily call routes through `safe_post` (SSRF guard) and `throttle_for` (per-host token bucket). Fan-out across `(query, year, quarter)` triples uses `gather_resilient` under an `anyio.CapacityLimiter(5)` so one bad query never takes down siblings. The pipeline collects-then-ranks (canonicalise URL → drop mirror hosts → dedup highest-score → sort by score+date → cap) before yielding for cassette stability and score-best-wins behaviour. `--only-source` is wired via a local `_SOURCE_REGISTRY` dict in `_ingest_cmd.py` — no `Source` Protocol changes, no per-class attribute.

**Tech Stack:** Python 3.13, `anyio`, `httpx` (via `safe_post`), `pydantic` v2 (for `RawEntry`), `pyyaml` for the queries file, `pytest` + `pytest-recording` (vcrpy cassettes), `basedpyright` strict.

## Execution Strategy

**Subagents** — default; no spec override. Two sequential tasks: source + exports + YAML + tests, then CLI wiring + `--only-source` + reliability rank. Task 2 imports the class produced by Task 1, so they cannot run in parallel.

## Task Dependency Graph

- Task 1 [AFK]: Tavily news source + YAML + exports → depends on `none` → batch 1
- Task 2 [AFK]: CLI wiring + `--only-source` + reliability rank → depends on `Task 1` → batch 2

## Agent Assignments

- Task 1: Source + YAML + tests + re-exports → python-development:python-pro
- Task 2: CLI wiring + reliability rank + only-source → python-development:python-pro
- Polish: post-implementation-polish → general-purpose

---

## File Structure

**New:**
- `slopmortem/corpus/sources/tavily_news.py` — `TavilyNewsSource` plus module-level helpers (`_canonicalize_url`, `_build_call_descriptors`, `_dedup_keep_highest_score`, `_drop_mirror_hosts`, `_load_yaml`, `_parse_published_date`).
- `slopmortem/corpus/sources/queries/__init__.py` — empty package marker (so `importlib.resources` can locate `tavily_news.yml`).
- `slopmortem/corpus/sources/queries/tavily_news.yml` — queries + defaults. Loaded at `__init__` time, overridable via constructor kwargs.
- `slopmortem/corpus/sources/mirror_domains.yml` — host blocklist for known aggregators / syndicated-content rehosts (peer to `platform_domains.yml`).

**Modified:**
- `slopmortem/corpus/sources/_names.py` — add `SOURCE_TAVILY_NEWS: Final = "tavily_news"`.
- `slopmortem/corpus/sources/__init__.py` — re-export `TavilyNewsSource`.
- `slopmortem/cli/_ingest_cmd.py` — add `--enable-tavily-news`, four override flags (`--tavily-news-start-year`, `--tavily-news-end-year`, `--tavily-news-max-emit`, `--tavily-news-search-depth`), and the generic `--only-source NAME` filter. Add a local `_SOURCE_REGISTRY` table; thread the new params through `functools.partial` and `_run_ingest`; assert `TAVILY_API_KEY` is set when the source is enabled.
- `slopmortem/ingest/_helpers.py` — extend `_RELIABILITY_RANK` with `tavily_news → 4`.

**New tests:**
- `tests/sources/test_tavily_news.py` — pure-fn tests for URL canonicalisation, dedup, mirror-host drop, score filter, max_emit cap, score-zero filler rejection, RFC 1123 parse; mocked-`safe_post` integration test for the full `fetch()` flow; cassette round-trip test (skipped without `RECORD=1`).

**Modified tests:**
- `tests/ingest/test_reliability_rank.py` — add `(SOURCE_TAVILY_NEWS, 4)` parametrize case.
- `tests/test_cli_ingest.py` — three regression tests: `--enable-tavily-news` without `TAVILY_API_KEY` exits non-zero; `--only-source tavily_news` runs the source in isolation and auto-enables; `--only-source nonexistent` lists valid names and exits non-zero.

---

## Pros and Cons of Key Decisions

The full pros/cons treatment lives in the design spec at `docs/specs/2026-05-06-tavily-news-source-design.md`. The choices that shape this plan, in summary:

- **Generic `--only-source` flag** beats per-source subcommands because adding a new source needs only a registry entry plus (for opt-in sources) one extra branch in the auto-enable block, instead of a whole new typer subcommand. Auto-selected.
- **Collect-then-rank** beats streaming because parallel `gather_resilient` completion order is non-deterministic — score-best-wins matters more than first-yield latency, and the ~24 MB peak memory across 120 calls is trivial.
- **`min_score=0.3` default** is empirically the boundary where named-event signal becomes dominant; tunable via YAML.
- **`search_depth=basic`** (1 credit/call) leaves 2× headroom on the 1k-credit free tier vs `advanced`; CLI override exists for cost-aware one-offs.
- **Rolling `year_range=2024..current`** matches Tavily's actual news-index coverage (empty before 2024 for these queries) and keeps the source working as the calendar advances.
- **Quarterly windows** beat full-year windows because `max_results=20` is per-call, so quarterly slicing 4× the surface area against a single saturating quarter.
- **`include_raw_content=true`** keeps the body in the search response (no `/extract` hop, no `TavilyEnricher` implication, zero extra credits).
- **YAML config** for queries beats Python constants because operators tune queries far more often than they tune code.
- **Opt-in CLI flag** keeps `just ingest` bit-identical and matches the project's cassette-discipline rule.
- **Reliability rank `4`** slots cleanly: Curated 0, HN 1, Crunchbase 2, DefiLlama 3, Tavily News 4, dead-letter 9.

---

## Task 1: Source + YAML + tests + re-exports

**Why:** The source is the new behaviour. Helpers (`_canonicalize_url`, `_dedup_keep_highest_score`, `_drop_mirror_hosts`, `_build_call_descriptors`, `_parse_published_date`) are pure functions that test in isolation; the integration test patches `safe_post` to exercise the full pipeline without burning Tavily credits. The YAML defaults plus the mirror-domains list ship together so the source is operable on first import. The package re-export lands in this task too — Task 2 imports `TavilyNewsSource` and would `ImportError` otherwise.

**API verification:**
- `curl -sS -X POST 'https://api.tavily.com/search' -H 'content-type: application/json' -d '{"api_key":"<key>","query":"startup shuts down","topic":"news","start_date":"2024-10-01","end_date":"2024-12-31","search_depth":"basic","max_results":3,"include_raw_content":true}' | python -m json.tool | head -80`
- Expected: a JSON object with a `results` array; each entry has `title`, `url`, `score` (0..1 float), `published_date` (RFC 1123: `Tue, 18 Nov 2024 11:05:19 GMT`), and `raw_content` (full article body string, ~7 KB on `basic`). If `published_date` is missing or arrives in ISO 8601 instead of RFC 1123, the API has changed — surface and pause before continuing.

**Files:**
- Create: `slopmortem/corpus/sources/tavily_news.py`
- Create: `slopmortem/corpus/sources/queries/__init__.py`
- Create: `slopmortem/corpus/sources/queries/tavily_news.yml`
- Create: `slopmortem/corpus/sources/mirror_domains.yml`
- Create: `tests/sources/test_tavily_news.py`
- Modify: `slopmortem/corpus/sources/_names.py`
- Modify: `slopmortem/corpus/sources/__init__.py`

- [x] **Step 1.1: Verify the live endpoint shape**

Run the `curl` under **API verification** above with a real `TAVILY_API_KEY`. Confirm:

1. The response is a JSON object whose top-level `results` is a non-empty array.
2. Each result has `title`, `url`, `score` (numeric), `published_date` (parses with `email.utils.parsedate_to_datetime` — RFC 1123), and `raw_content` (non-empty string).
3. `score` for at least one result is ≥ 0.3 (sanity-check against the default `min_score`).

If any of these fail, **stop and surface** — the spec assumes the current shape, and downstream code (date sort, dedup, body extraction) breaks silently if the shape drifted. Capture the actual response and either update the spec or reach out before proceeding.

- [x] **Step 1.2: Add the source-name constant**

Edit `slopmortem/corpus/sources/_names.py`. Append after `SOURCE_CRUNCHBASE_CSV`:

```python
SOURCE_TAVILY_NEWS: Final = "tavily_news"
```

The literal string `"tavily_news"` is the identifier used in `RawEntry.source`, the `_RELIABILITY_RANK` key, and the `--only-source` argument. Don't reuse a hyphenated form anywhere — keys diverging is the dead-letter-rank failure mode.

- [x] **Step 1.3: Write the queries YAML and mirror-domain blocklist**

Create `slopmortem/corpus/sources/queries/__init__.py` as an empty file (package marker so `importlib.resources` can locate the YAML inside the package).

Create `slopmortem/corpus/sources/queries/tavily_news.yml`:

```yaml
queries:
  - "startup shuts down"
  - "startup ceases operations"
  - "startup files for bankruptcy"
  - "company winds down"
  - "startup files chapter 11"
  - "startup lays off all staff"
  - "tech startup closes operations"
  - "startup runs out of cash"
  - "startup fails to raise funding"
  - "company shutters operations"
year_range:
  start: 2024  # Tavily news index empirically empty for these queries before 2024
  # `end` intentionally omitted: source defaults to datetime.now(UTC).year.
  # Set explicitly only for one-off historical sweeps via YAML or CLI.
max_emit: 200
min_score: 0.3
search_depth: basic     # basic = 1 credit/call, advanced = 2 credits/call
```

Create `slopmortem/corpus/sources/mirror_domains.yml`:

```yaml
# Aggregator / syndicated-content rehosts. Suffix match: a host matches when
# it equals the listed domain or ends with `.<domain>`. Match is host-only
# and case-insensitive (canonicalisation lowercases hosts upstream).
#
# Seed list intentionally small. Easy to grow if filler reappears at scores
# above min_score.
- bundle.app          # observed mirroring TechCrunch in 2026-05-06 probe
- flipboard.com
- feedly.com
- smartnews.com
- inoreader.com
- news.google.com
```

- [x] **Step 1.4: Write the failing tests**

Create `tests/sources/test_tavily_news.py`. Tests cover four layers: pure helpers (`_canonicalize_url`, `_dedup_keep_highest_score`, `_drop_mirror_hosts`, `_parse_published_date`), filter behaviours (min_score, max_emit, score-zero filler), error isolation (per-call failure doesn't take down siblings), and a cassette round-trip skipped when no recording exists.

```python
"""TavilyNewsSource: URL canonicalisation, dedup, mirror-host drop, fetch integration."""

from __future__ import annotations

import os
from datetime import UTC, datetime
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

CASSETTE_FILE = (
    Path(__file__).parent / "cassettes" / "test_tavily_news" / "test_round_trip.yaml"
)


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
    rows = [
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
    rows = [
        {"canonical_url": "https://bundle.app/article"},
        {"canonical_url": "https://m.bundle.app/article"},
        {"canonical_url": "https://news.google.com/foo"},
        {"canonical_url": "https://techcrunch.com/keep"},
    ]
    out = _drop_mirror_hosts(rows, mirrors={"bundle.app", "news.google.com"})
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
        queries=["q1", "q2"], start_year=2024, end_year=2024
    )
    # 2 queries x 1 year x 4 quarters = 8 calls
    assert len(calls) == 8
    quarters = {(c["start_date"], c["end_date"]) for c in calls}
    assert ("2024-01-01", "2024-03-31") in quarters
    assert ("2024-04-01", "2024-06-30") in quarters
    assert ("2024-07-01", "2024-09-30") in quarters
    assert ("2024-10-01", "2024-12-31") in quarters


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
            raise httpx.ConnectError("boom")
        return _ok_resp({
            "results": [_result(url=f"https://x/{raises_once['n']}", score=0.5)]
        })

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
    # YAML default has 10 queries; year_range.start=2024, end defaults to current → ≥1 year × 4 quarters.
    # Therefore at least 10 × 1 × 4 = 40 calls.
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
```

- [x] **Step 1.5: Run the tests, confirm they fail**

Run: `uv run pytest tests/sources/test_tavily_news.py -v`
Expected: `ImportError` on `from slopmortem.corpus.sources import TavilyNewsSource` (the module and the re-export don't exist yet).

- [x] **Step 1.6: Implement the source**

Create `slopmortem/corpus/sources/tavily_news.py`. The module exposes one class (`TavilyNewsSource`) and six module-level helpers (`_canonicalize_url`, `_build_call_descriptors`, `_dedup_keep_highest_score`, `_drop_mirror_hosts`, `_load_yaml`, `_parse_published_date`) so each piece tests in isolation.

```python
"""Tavily news source: rolling-window shutdown-event discovery.

Pipeline:
  1. Materialise (query, year, quarter) triples from YAML defaults
     overridden by constructor kwargs.
  2. Fan out one POST /search per triple under
     anyio.CapacityLimiter(5), routed through gather_resilient so a
     bad call doesn't take down siblings.
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
than first-yield latency, and the ~24 MB peak memory across 120 calls
is trivial.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
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
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

TAVILY_SEARCH_ENDPOINT: Final = "https://api.tavily.com/search"
DEFAULT_CONCURRENCY: Final = 5
DEFAULT_RPS: Final = 1.0
DEFAULT_MAX_RESULTS: Final = 20  # Tavily per-call cap

# URL canonicalisation: tracking parameters to drop.
# Intentionally excludes ``feature`` — YouTube and a few news outlets use it
# as a real content selector, not a tracking tag.
_TRACKING_PARAMS: Final[frozenset[str]] = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "ref_src", "_ga",
})

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
    """Load a YAML file packaged inside ``slopmortem``."""
    text = resources.files(package).joinpath(filename).read_text(encoding="utf-8")
    return yaml.safe_load(text)


def _load_defaults() -> dict[str, object]:
    payload = _load_yaml("slopmortem.corpus.sources.queries", "tavily_news.yml")
    if not isinstance(payload, dict):
        msg = "tavily_news.yml must be a mapping"
        raise TypeError(msg)
    return cast("dict[str, object]", payload)


def _load_mirror_domains() -> set[str]:
    payload = _load_yaml("slopmortem.corpus.sources", "mirror_domains.yml")
    if not isinstance(payload, list):
        return set()
    rows = cast("list[object]", payload)
    return {r.lower() for r in rows if isinstance(r, str) and r}


# ---- pure helpers ------------------------------------------------------------


def _canonicalize_url(url: str) -> str:
    """Normalise a URL for stable dedup.

    - lowercase scheme + host
    - drop fragment
    - drop tracking query params
    - normalise trailing slash on non-root paths
    """
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
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _drop_mirror_hosts(
    rows: Iterable[dict[str, object]],
    *,
    mirrors: set[str],
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for row in rows:
        canonical = row.get("canonical_url")
        if not isinstance(canonical, str):
            continue
        host = (urlparse(canonical).hostname or "").lower()
        if any(host == d or host.endswith("." + d) for d in mirrors):
            logger.debug("tavily_news: dropping mirror host %s", host)
            continue
        out.append(row)
    return out


def _dedup_keep_highest_score(
    rows: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    best: dict[str, dict[str, object]] = {}
    for row in rows:
        url = row.get("canonical_url")
        score = row.get("score")
        if not isinstance(url, str) or not isinstance(score, (int, float)):
            continue
        existing = best.get(url)
        existing_score = existing["score"] if existing is not None else None
        if (
            existing is None
            or (isinstance(existing_score, (int, float)) and float(score) > float(existing_score))
        ):
            best[url] = row
    return list(best.values())


def _build_call_descriptors(
    *,
    queries: list[str],
    start_year: int,
    end_year: int,
) -> list[dict[str, object]]:
    """Cross-product (query, year, quarter) → list of call payload dicts."""
    out: list[dict[str, object]] = []
    for query in queries:
        for year in range(start_year, end_year + 1):
            for q_start, q_end in _QUARTERS:
                out.append({
                    "query": query,
                    "year": year,
                    "start_date": f"{year}-{q_start}",
                    "end_date": f"{year}-{q_end}",
                })
    return out


# ---- TavilyNewsSource --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CallSpec:
    query: str
    year: int
    start_date: str
    end_date: str


class TavilyNewsSource:
    """[Source] Tavily /search across (query, year, quarter) triples; emits one RawEntry per article."""

    def __init__(
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
        yaml_queries = defaults.get("queries") or []
        if not isinstance(yaml_queries, list):
            msg = "tavily_news.yml: queries must be a list"
            raise TypeError(msg)
        yaml_year_range = defaults.get("year_range") or {}
        if not isinstance(yaml_year_range, dict):
            yaml_year_range = {}

        self.queries: list[str] = queries or [
            q for q in cast("list[object]", yaml_queries) if isinstance(q, str)
        ]
        if not self.queries:
            msg = "TavilyNewsSource: at least one query is required"
            raise ValueError(msg)

        yaml_start = yaml_year_range.get("start")
        yaml_end = yaml_year_range.get("end")
        self.start_year: int = (
            start_year if start_year is not None else int(yaml_start) if isinstance(yaml_start, int) else 2024
        )
        self.end_year: int = (
            end_year
            if end_year is not None
            else int(yaml_end) if isinstance(yaml_end, int) else datetime.now(UTC).year
        )
        if self.end_year < self.start_year:
            msg = f"end_year ({self.end_year}) < start_year ({self.start_year})"
            raise ValueError(msg)

        yaml_max_emit = defaults.get("max_emit", 200)
        self.max_emit: int = (
            max_emit if max_emit is not None else int(yaml_max_emit) if isinstance(yaml_max_emit, int) else 200
        )

        yaml_min_score = defaults.get("min_score", 0.3)
        self.min_score: float = (
            min_score
            if min_score is not None
            else float(yaml_min_score) if isinstance(yaml_min_score, (int, float)) else 0.3
        )

        yaml_depth = defaults.get("search_depth", "basic")
        self.search_depth: str = (
            search_depth if search_depth is not None else str(yaml_depth) if yaml_depth else "basic"
        )

        self.rps = rps
        self.concurrency = concurrency
        self._mirrors: set[str] = _load_mirror_domains()

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
                    spec.query, spec.year, spec.start_date, exc,
                )
                return None

            if resp.status_code >= HTTP_BAD_REQUEST:
                logger.warning(
                    "tavily_news: HTTP %s query=%r start=%s",
                    resp.status_code, spec.query, spec.start_date,
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
            out: list[dict[str, object]] = []
            for raw in cast("list[object]", results):
                if isinstance(raw, dict):
                    out.append(cast("dict[str, object]", raw))
            return out

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
            kept.append({
                "canonical_url": _canonicalize_url(url),
                "score": float(score),
                "raw_content": raw_content,
                "title": row.get("title", ""),
                "published_date": row.get("published_date"),
            })
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
        # ``None``; ``gather_resilient`` only surfaces unexpected ``Exception``s.
        # Both paths count as a failed call so the operator log matches reality.
        flat: list[dict[str, object]] = []
        failures = 0
        for r in results:
            if isinstance(r, Exception) or r is None:
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

        emitted = 0
        for row in kept:
            if emitted >= self.max_emit:
                logger.info("tavily_news: max_emit=%d reached, stopping", self.max_emit)
                break
            yield RawEntry(
                source=SOURCE_TAVILY_NEWS,
                source_id=cast("str", row["canonical_url"]),
                url=cast("str", row["canonical_url"]),
                raw_html=None,
                markdown_text=cast("str", row["raw_content"]),
                fetched_at=datetime.now(UTC),
            )
            emitted += 1

        if emitted == 0:
            logger.warning("tavily_news: 0 entries after dedup (calls=%d, failures=%d)", len(specs), failures)
```

- [x] **Step 1.7: Re-export `TavilyNewsSource`**

Edit `slopmortem/corpus/sources/__init__.py`. Add the import alphabetically (after `HNAlgoliaSource`, before `TavilyEnricher`):

```python
from slopmortem.corpus.sources.tavily_news import TavilyNewsSource as TavilyNewsSource
```

Add `"TavilyNewsSource"` to `__all__` (alphabetical placement, after `"TavilyEnricher"`):

```python
__all__ = [
    "CrunchbaseCsvSource",
    "CuratedSource",
    "Enricher",
    "HNAlgoliaSource",
    "Source",
    "TavilyEnricher",
    "TavilyNewsSource",
    "WaybackEnricher",
]
```

- [x] **Step 1.8: Run the tests, confirm they pass**

Run: `uv run pytest tests/sources/test_tavily_news.py -v -k "not round_trip"`
Expected: 14 PASS — 4 `_canonicalize_url` cases (`strips_utm`, `drops_trailing_slash`, `preserves_root_slash`, `preserves_port`), 1 `_dedup_keep_highest_score`, 1 `_drop_mirror_hosts`, 2 `_parse_published_date`, 1 `_build_call_descriptors`, 5 `TavilyNewsSource.fetch` (`emits_dedup_sorted_capped`, `per_call_failure_isolated`, `max_emit_caps_yield`, `fetch_no_api_key_yields_nothing`, `yaml_defaults_loaded`). The `round_trip` cassette test is excluded.

If a test fails because `_canonicalize_url`'s netloc rebuild loses the host casing or strips the port, fix the URL builder before moving on — `safe_post` re-resolves `Host` headers from the URL, so a mangled netloc breaks SSRF validation.

- [x] **Step 1.9: Lint + typecheck**

Run: `just lint && just typecheck`
Expected: clean. If `basedpyright` reports `reportAny` on Tavily payload accesses, narrow with `cast` and a one-line comment — never `# type: ignore`.

- [x] **Step 1.10: Commit**

```
git add slopmortem/corpus/sources/tavily_news.py \
        slopmortem/corpus/sources/queries/__init__.py \
        slopmortem/corpus/sources/queries/tavily_news.yml \
        slopmortem/corpus/sources/mirror_domains.yml \
        slopmortem/corpus/sources/_names.py \
        slopmortem/corpus/sources/__init__.py \
        tests/sources/test_tavily_news.py
git commit -m "feat(sources): add tavily news source"
```

---

## Task 2: CLI wiring + `--only-source` + reliability rank

**Why:** The source class exists but nothing calls it. Add the opt-in `--enable-tavily-news` flag, the four per-source override flags, the generic `--only-source NAME` filter, the reliability rank entry, and three regression tests covering the wiring assertions.

**Coordination caveat:** if the DefiLlama plan (`docs/plans/2026-05-06-defillama-source.md`) has already landed, `_RELIABILITY_RANK` and the source registry already include its entries — extend them rather than overwriting. If DefiLlama lands after Tavily News, the order stays primary-source → derived-reporting (Crunchbase 2 → DefiLlama 3 → Tavily News 4); if it lands first, this plan's `tavily_news → 4` slots cleanly on top.

**Files:**
- Modify: `slopmortem/ingest/_helpers.py`
- Modify: `slopmortem/cli/_ingest_cmd.py`
- Modify: `tests/ingest/test_reliability_rank.py`
- Modify: `tests/test_cli_ingest.py`

- [x] **Step 2.1: Extend `_RELIABILITY_RANK`**

Edit `slopmortem/ingest/_helpers.py:11-15`. Update the import block to include `SOURCE_TAVILY_NEWS` (alphabetical):

```python
from slopmortem.corpus.sources._names import (
    SOURCE_CRUNCHBASE_CSV,
    SOURCE_CURATED,
    SOURCE_HN_ALGOLIA,
    SOURCE_TAVILY_NEWS,
)
```

Edit `_RELIABILITY_RANK` at lines 41-45. After the existing `SOURCE_CRUNCHBASE_CSV: 2` entry, add `SOURCE_TAVILY_NEWS: 4`:

```python
_RELIABILITY_RANK: Final[dict[str, int]] = {
    SOURCE_CURATED: 0,
    SOURCE_HN_ALGOLIA: 1,
    SOURCE_CRUNCHBASE_CSV: 2,
    SOURCE_TAVILY_NEWS: 4,
}
```

If `SOURCE_DEFILLAMA: 3` is already present (DefiLlama plan landed first), insert `SOURCE_TAVILY_NEWS: 4` after it instead.

- [x] **Step 2.2: Extend the reliability-rank regression test**

Edit `tests/ingest/test_reliability_rank.py:7-11`. Add `SOURCE_TAVILY_NEWS` to the import block (alphabetical):

```python
from slopmortem.corpus.sources._names import (
    SOURCE_CRUNCHBASE_CSV,
    SOURCE_CURATED,
    SOURCE_HN_ALGOLIA,
    SOURCE_TAVILY_NEWS,
)
```

Append to the existing `@pytest.mark.parametrize` list (after the `SOURCE_CRUNCHBASE_CSV` row):

```python
        (SOURCE_TAVILY_NEWS, 4),
```

`test_unknown_source_lands_at_dead_letter_rank` already covers the fallback case — do not duplicate it.

- [x] **Step 2.3: Run the reliability test**

Run: `uv run pytest tests/ingest/test_reliability_rank.py -v`
Expected: 5 PASS (4 parametrized cases + the existing `test_unknown_source_lands_at_dead_letter_rank`).

- [x] **Step 2.4: Add the CLI flags**

Edit `slopmortem/cli/_ingest_cmd.py`. Five edits:

**1. Import the source class.** Update the existing block at lines 36-42 to add `TavilyNewsSource` (alphabetical, between `HNAlgoliaSource` and `TavilyEnricher`):

```python
from slopmortem.corpus.sources import (
    CrunchbaseCsvSource,
    CuratedSource,
    HNAlgoliaSource,
    TavilyEnricher,
    TavilyNewsSource,
    WaybackEnricher,
)
```

**2. Add the new typer Options.** Insert just before the existing `post_mortems_root` option in `ingest_cmd`:

```python
    enable_tavily_news: Annotated[
        bool,
        typer.Option(
            "--enable-tavily-news",
            help=(
                "Enable the Tavily news shutdown-event source. "
                "Requires TAVILY_API_KEY. Bodies are returned inline; "
                "no Tavily-extract hop is implied."
            ),
        ),
    ] = False,
    tavily_news_start_year: Annotated[
        int | None,
        typer.Option(
            "--tavily-news-start-year",
            help="Override year_range.start for the Tavily news source.",
        ),
    ] = None,
    tavily_news_end_year: Annotated[
        int | None,
        typer.Option(
            "--tavily-news-end-year",
            help="Override year_range.end for the Tavily news source. Defaults to current year.",
        ),
    ] = None,
    tavily_news_max_emit: Annotated[
        int | None,
        typer.Option(
            "--tavily-news-max-emit",
            help="Override the Tavily news source's max_emit cap.",
        ),
    ] = None,
    tavily_news_search_depth: Annotated[
        str | None,
        typer.Option(
            "--tavily-news-search-depth",
            help="Override search_depth for the Tavily news source: basic (1 credit) or advanced (2).",
        ),
    ] = None,
    only_source: Annotated[
        str | None,
        typer.Option(
            "--only-source",
            help=(
                "Run only the named source, auto-enabling its --enable-* flag if any. "
                "Accepts source identifiers (curated, hn_algolia, crunchbase_csv, tavily_news)."
            ),
        ),
    ] = None,
```

**3. Forward the new params through `functools.partial`.** Append to the kwarg block at lines 121-133:

```python
            enable_tavily_news=enable_tavily_news,
            tavily_news_start_year=tavily_news_start_year,
            tavily_news_end_year=tavily_news_end_year,
            tavily_news_max_emit=tavily_news_max_emit,
            tavily_news_search_depth=tavily_news_search_depth,
            only_source=only_source,
```

**4. Extend the `_run_ingest` signature.** Update the keyword-only block in `_run_ingest`'s signature (lines 179-191) to insert the new params after `tavily_enrich` and before `post_mortems_root`. Preserve every existing parameter — the snippet below shows the full block:

```python
    *,
    dry_run: bool,
    force: bool,
    reconcile_flag: bool,
    reclassify: bool,
    list_review: bool,
    limit: int | None,
    crunchbase_csv: Path | None,
    enrich_wayback: bool,
    tavily_enrich: bool,
    enable_tavily_news: bool,
    tavily_news_start_year: int | None,
    tavily_news_end_year: int | None,
    tavily_news_max_emit: int | None,
    tavily_news_search_depth: str | None,
    only_source: str | None,
    post_mortems_root: Path,
```

**5. Add `import os` at the top of the file** if it isn't already imported (it isn't — `_ingest_cmd.py` currently uses none of `os`).

- [x] **Step 2.5: Add the source registry and `--only-source` filter**

Still in `slopmortem/cli/_ingest_cmd.py`, add the registry table near the other module-level helpers (above `_default_curated_yaml`). The registry maps a source-name string to the spec it needs to be filterable: which `--enable-*` flag (if any) gates it, and a `gate` callable that decides whether the source is currently constructable from the run's kwargs.

```python
@dataclass(frozen=True)
class _SourceSpec:
    class_name: str  # constructed-source class name, used by the --only-source filter
    enable_flag: str | None  # name of the kwarg in _run_ingest, or None for always-on sources
    gate: Callable[..., bool]  # returns True if the source can run given kwargs


def _crunchbase_gate(*, crunchbase_csv: Path | None, **_: object) -> bool:
    return crunchbase_csv is not None


_SOURCE_REGISTRY: dict[str, _SourceSpec] = {
    "curated": _SourceSpec(class_name="CuratedSource", enable_flag=None, gate=lambda **_: True),
    "hn_algolia": _SourceSpec(class_name="HNAlgoliaSource", enable_flag=None, gate=lambda **_: True),
    "crunchbase_csv": _SourceSpec(
        class_name="CrunchbaseCsvSource", enable_flag="crunchbase_csv", gate=_crunchbase_gate
    ),
    "tavily_news": _SourceSpec(
        class_name="TavilyNewsSource", enable_flag="enable_tavily_news", gate=lambda **_: True
    ),
}
```

Add the imports at the top of the file (neither is currently imported in `_ingest_cmd.py`):

```python
from collections.abc import Callable
from dataclasses import dataclass
```

If the DefiLlama plan landed first, `_SOURCE_REGISTRY` already exists and already contains `defillama`. Extend in-place: keep the existing entries and add `"tavily_news": _SourceSpec(class_name="TavilyNewsSource", enable_flag="enable_tavily_news", gate=lambda **_: True)`.

In `_run_ingest`, just after the read-only short-circuits return (currently after `if reconcile_flag: ...`), add the `--only-source` resolution block:

```python
    if only_source is not None:
        if only_source not in _SOURCE_REGISTRY:
            valid = ", ".join(sorted(_SOURCE_REGISTRY))
            raise typer.BadParameter(
                f"--only-source: unknown source {only_source!r}. Valid: {valid}."
            )
        spec = _SOURCE_REGISTRY[only_source]
        # Auto-enable: flip the source's --enable-* flag on if it has one.
        # Each opt-in flag needs an explicit branch — Python's keyword-only
        # parameter binding can't be table-driven without ``locals()`` tricks.
        # Add a branch when introducing a new opt-in source.
        if spec.enable_flag == "enable_tavily_news":
            enable_tavily_news = True
        # crunchbase_csv is gated by a path argument, not a boolean — require it explicitly.
        if only_source == "crunchbase_csv" and crunchbase_csv is None:
            raise typer.BadParameter(
                "--only-source crunchbase_csv requires --crunchbase-csv PATH."
            )
```

`enable_flag=None` sources (`curated`, `hn_algolia`) need no auto-enable. If DefiLlama's plan landed first, add a parallel branch for its flag (and a `class_name` entry in `_SOURCE_REGISTRY`).

**Add the Tavily-key assertion** for `--enable-tavily-news`. Place it after the `only_source` block, before `_build_ingest_deps` runs:

```python
    if enable_tavily_news and not os.environ.get("TAVILY_API_KEY"):
        raise typer.BadParameter(
            "--enable-tavily-news requires TAVILY_API_KEY: the Tavily news source "
            "calls /search and pulls article bodies via include_raw_content. "
            "Set TAVILY_API_KEY in .env or unset --enable-tavily-news."
        )
```

The source itself reads `TAVILY_API_KEY` via `os.environ.get` inside the module (matching `TavilyEnricher`); `slopmortem/config.py`'s `tavily_api_key: SecretStr` is intentionally not threaded through.

- [x] **Step 2.6: Wire the source into the sources list and apply the filter**

Still in `slopmortem/cli/_ingest_cmd.py`, edit the existing sources-list block (currently lines 224-229). Construct the full source list first, then filter on `--only-source` if set:

```python
    sources: list[Source] = [
        CuratedSource(yaml_path=_default_curated_yaml(), rps=3.0),
        HNAlgoliaSource(query="post-mortem", rps=5.0),
    ]
    if crunchbase_csv is not None:
        sources.append(CrunchbaseCsvSource(csv_path=crunchbase_csv))
    if enable_tavily_news:
        sources.append(
            TavilyNewsSource(
                start_year=tavily_news_start_year,
                end_year=tavily_news_end_year,
                max_emit=tavily_news_max_emit,
                search_depth=tavily_news_search_depth,
            )
        )

    if only_source is not None:
        # _SOURCE_REGISTRY[only_source] was already validated in Step 2.5.
        wanted_class = _SOURCE_REGISTRY[only_source].class_name
        sources = [s for s in sources if type(s).__name__ == wanted_class]
        if not sources:
            raise typer.BadParameter(
                f"--only-source {only_source!r}: source enabled but not constructed; "
                "check that its prerequisites (e.g. --crunchbase-csv path) are present."
            )
```

If DefiLlama's plan added entries here, keep them — concatenate, do not overwrite.

- [x] **Step 2.7: Add the regression tests**

Edit `tests/test_cli_ingest.py`. Append three tests at the end of the file, matching the existing `monkeypatch` + `_fake_deps` + `CliRunner` pattern.

```python
def test_enable_tavily_news_without_api_key_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """--enable-tavily-news without TAVILY_API_KEY exits non-zero with a clear message."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr("slopmortem.cli._ingest_cmd._build_ingest_deps", _fake_deps)
    runner = CliRunner()
    result = runner.invoke(app, ["ingest", "--enable-tavily-news", "--dry-run"])
    assert result.exit_code != 0, result.output
    combined = result.output + (result.stderr or "")
    assert "TAVILY_API_KEY" in combined


def test_only_source_tavily_news_runs_in_isolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--only-source tavily_news auto-enables and filters the source list to just that source."""
    captured: dict[str, object] = {}

    async def fake_ingest(**kwargs: object) -> object:
        captured["sources"] = kwargs["sources"]
        return MagicMock(dry_run=True, processed=0)

    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-key")
    monkeypatch.setattr("slopmortem.cli._ingest_cmd.ingest", fake_ingest)
    monkeypatch.setattr("slopmortem.cli._ingest_cmd._build_ingest_deps", _fake_deps)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "ingest",
            "--dry-run",
            "--only-source",
            "tavily_news",
            "--post-mortems-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    sources = captured["sources"]
    assert isinstance(sources, list)
    classnames = [type(s).__name__ for s in sources]
    assert classnames == ["TavilyNewsSource"]


def test_only_source_unknown_name_lists_valid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--only-source <unknown> exits non-zero and surfaces the registered source names."""
    monkeypatch.setattr("slopmortem.cli._ingest_cmd._build_ingest_deps", _fake_deps)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "ingest",
            "--dry-run",
            "--only-source",
            "definitely-not-a-source",
            "--post-mortems-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0, result.output
    combined = result.output + (result.stderr or "")
    assert "tavily_news" in combined
    assert "curated" in combined
```

- [x] **Step 2.8: Run the three new tests**

Run: `uv run pytest tests/test_cli_ingest.py -v`
Expected: 7 PASS — the existing 4 (`test_default_curated_yaml_resolves_to_existing_file`, `test_ingest_dry_run_dispatches_to_orchestrator`, `test_ingest_tavily_enrich_appends_enricher`, `test_ingest_with_crunchbase_csv_appends_source`) plus the 3 new ones above.

If `test_only_source_tavily_news_runs_in_isolation` fails because `sources` is empty, the auto-enable step in `_run_ingest` (Step 2.5) didn't flip `enable_tavily_news` to `True` before the sources-list construction in Step 2.6 — re-check ordering inside `_run_ingest`.

- [x] **Step 2.9: Smoke-test the CLI surface**

Run: `uv run slopmortem ingest --help | grep -E '(enable-tavily-news|only-source|tavily-news-)'`
Expected: at least 6 matching lines (the boolean flag, four override flags, and `--only-source`). If a flag is missing, Step 2.4 dropped a typer Option — re-add and re-run.

- [x] **Step 2.10: Dry-run with the source enabled (cheap; one-quarter window)**

If a `TAVILY_API_KEY` is configured locally, smoke the live integration. Otherwise skip this step.

```
TAVILY_API_KEY=$TAVILY_API_KEY uv run slopmortem ingest \
  --dry-run \
  --only-source tavily_news \
  --tavily-news-start-year 2024 \
  --tavily-news-end-year 2024 \
  --tavily-news-max-emit 5 \
  --post-mortems-root /tmp/slopmortem_smoke \
  2>&1 | tee /tmp/slopmortem_smoke.log | tail -40
```

Expected:
- Dry-run completes with no error.
- `/tmp/slopmortem_smoke.log` contains either a successful per-call WARN/INFO trail with non-zero `seen` in the result table, or a `tavily_news: 0 entries after dedup` line if the cap-at-5 happened to hit only sub-threshold rows.
- No `TAVILY_API_KEY` complaint.

If the run fails with `HTTP 401` or similar, the key isn't reaching the source — verify `TAVILY_API_KEY` is exported, not just in `.env`.

- [x] **Step 2.11: Lint + typecheck + full test suite**

Run: `just lint && just typecheck && just test`
Expected: all clean. The full test run is the last gate before commit because the CLI wiring touches an import block that other tests indirectly load (`test_cli_smoke`, `test_cli_reconcile`, etc.).

- [x] **Step 2.12: Commit**

```
git add slopmortem/ingest/_helpers.py \
        slopmortem/cli/_ingest_cmd.py \
        tests/ingest/test_reliability_rank.py \
        tests/test_cli_ingest.py
git commit -m "wiring: opt-in tavily-news source plus --only-source filter"
```

---

## Optional: Record live cassette (skip in CI)

After Task 1 lands, the round-trip cassette test is skipped by default. To capture the cassette:

- [ ] **Step C.1: Record the cassette**

```
TAVILY_API_KEY=$TAVILY_API_KEY RECORD=1 uv run pytest \
  tests/sources/test_tavily_news.py::test_round_trip -v
```

Expected: writes `tests/sources/cassettes/test_tavily_news/test_round_trip.yaml`.
The cassette captures one real `/search` call with `max_emit=5`; size will be ~50 KB (5 results × ~10 KB raw_content per result on `basic`).

- [ ] **Step C.2: Re-run without RECORD to confirm replay**

Run: `uv run pytest tests/sources/test_tavily_news.py -v`
Expected: all tests PASS, including `test_round_trip` (replays from cassette).

- [ ] **Step C.3: Inspect the cassette before commit**

```
ls -lh tests/sources/cassettes/test_tavily_news/test_round_trip.yaml
grep -i 'api_key' tests/sources/cassettes/test_tavily_news/test_round_trip.yaml || echo OK
```

The `api_key` grep should print `OK`. The `@pytest.mark.vcr(filter_post_data_parameters=["api_key"])` decorator added in Step 1.4 strips the key from the recorded request body. If the key is still present, the decorator was dropped or the filter name mismatched the JSON field — re-record with the decorator restored before committing.

- [ ] **Step C.4: Commit the cassette**

```
git add tests/sources/cassettes/test_tavily_news/
git commit -m "test: record cassette for tavily news source"
```

---

## Out of Scope

- **Per-source budget caps.** Spending limits per adapter would require touching `slopmortem/budget.py`. Defer until a real overrun lands. `max_emit` is a yield-count proxy, not a true budget.
- **Domain include/exclude lists.** Tavily exposes `include_domains` / `exclude_domains` parameters; the `min_score` threshold and the small mirror blocklist caught all observed filler in the spec's probe.
- **Sector-targeted queries.** Crypto-specific phrases returned pure filler in API testing. The 10 generic death verbs catch sector-diverse events without that failure mode.
- **Incremental ingest mode.** The default `year_range=2024..current` is a rolling window. A finer-grained "only fetch since last run" mode waits for scheduled ingest.
- **Pitch enrichment** for thin-pitch entries. News articles describe failure events, not original pitches. Spec calls for a separate `PitchEnricher` ingest stage rather than bolting onto this source.
- **Updating `docs/architecture.md`.** Add a one-line pointer to this plan once it lands.
- **Refactoring `TavilyEnricher` onto `safe_post`.** Out of scope; separate cleanup if anyone cares.
- **Re-recording eval cassettes.** The eval runner is gated by curated post-mortems plus the existing two sources; this source doesn't shift eval cassettes. Verify with `just eval` after Task 2 — divergence is a separate plan.
