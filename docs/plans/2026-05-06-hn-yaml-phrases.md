# HN Algolia YAML-Driven Phrase Discovery Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or `/team-feature` to implement this plan task-by-task, per the Execution Strategy below. Steps use checkbox (`- [ ]`) syntax — these are **persistent durable state**, not visual decoration. The executor edits the plan file in place: `- [ ]` → `- [x]` the instant a step verifies, before moving on. On resume (new session, crash, takeover), the executor scans existing `- [x]` marks and skips them — these steps are NOT redone. TodoWrite mirrors this state in-session; the plan file is the source of truth across sessions.

**Goal:** Replace the hardcoded single-query `HNAlgoliaSource(query="post-mortem")` with a YAML-driven, multi-phrase, chronologically-paginated discovery source that surfaces long-tail startup obituaries. Specifically: dead competitors like Mattermark (120pts, 2017-12-22) currently invisible to the source because (a) the post title doesn't contain "post-mortem" and (b) relevance-sorted top-30 buries everything below ~500 points.

**Architecture:** One independent module change to `slopmortem/corpus/sources/hn_algolia.py`. The constructor takes `queries_yaml_path` instead of a single `query` string. The YAML lists shutdown phrases (`"shutting down"`, `"winding down"`, etc.) plus per-window pagination settings and a date range. The source loops `phrases × year_windows × pages`, calls HN Algolia's `search_by_date` endpoint (chronological — *not* `search`'s relevance ranking), dedups by `objectID` across phrases and windows, and yields one `RawEntry` per unique hit. Year-window slicing (one query per calendar year per phrase) is what makes the long tail reachable: a single open-ended pagination at 30 hits/page would burn its entire budget on the most-recent few years and never touch 2015–18 obituaries (validated: 20 pages of `"shut down"` only reaches Nov 2023; 70+ pages would be needed to hit 2017's Mattermark obituary directly). Year-windowing guarantees coverage of each year independently. Existing rate limiter, robots policy, and SSRF guard apply unchanged. The CLI wires the new constructor; default `just ingest` behaviour shifts (broader recall, more entries) — treated as a baseline change per the project's cassette discipline.

**Tech Stack:** Python 3.13, anyio, httpx, pydantic v2, PyYAML, pytest + pytest-recording (vcrpy cassettes), basedpyright strict.

## Why phrase-only, not named-suspect

An earlier draft of this design proposed a hybrid YAML mixing shutdown phrases with named-suspect company queries (`"Mattermark"`, `"DataFox"`, `"ConcourseQ"`). Walked back: per-name queries don't scale (curating names is a manual treadmill), they duplicate the role of `curated.py` (which is the *correct* place for known-suspect URLs), and they don't generalise across the long tail of unknown-name dead startups slopmortem actually wants to surface. Phrase-only configuration is discovery-shaped and language-shaped — it catches obituaries by how they're written, not by who they're about. Named suspects, when needed, belong in `curated.py` with a Wayback URL already attached.

## Why `search_by_date` instead of `search`, with year-window slicing

Validated against the live HN Algolia API:

- `query="shut down"` relevance-sorted top 15 returns mega-engagement stories (YouTube ban 1553pts, Facebook 1227pts, Mozilla Pocket 1222pts). **Mattermark at 120pts isn't in the top 15** — buried under unrelated viral posts.
- `query="shutting down"` relevance-sorted top 15 has 14/15 legit obituaries (RethinkDB, Quibi, BuzzFeed News, Stadia, etc.) — phrase quality is high — but the same engagement-floor problem applies to anything below ~400pts.
- `query="shut down"` via `search_by_date` with a narrow Dec-21–29-2017 window returns Mattermark cleanly as the most-engaged item in that window.
- `query="shut down"` via `search_by_date` *without* a window: page 0 covers ~6 weeks (May 2026 → Mar 2026); page 19 covers ~37 days (Dec 2023 → Nov 2023). At 30 hits/page, **a flat 20-page cap only reaches back to Nov 2023**. Reaching Dec 2017 would need ~70 pages per phrase — burning ~$15–22 per ingest in downstream Tavily + Haiku costs and over-paginating high-volume recent years just to scrape low-volume old ones.

Conclusion: relevance ranking promotes mega-engagement; flat chronological pagination wastes budget on recent noise. **Year-window slicing** — one query per phrase per calendar year between `date_from` and `date_to` — gives each year its own bounded page budget. With `pages_per_window=3`, every year between 2015 and today gets up to 90 hits per phrase, total per-run cost ≈ ~$7–9 in Tavily + Haiku for full date coverage (vs. ~$15–22 for the flat-pagination approach that *might* reach the same era).

## Execution Strategy

**Subagents** — default; no spec override. The YAML-creation, source-refactor, and CLI/test wiring can run sequentially in one session, or split across two subagents if `/team-feature` is used. Wiring depends on the source signature being stable.

## Task Dependency Graph

- Task 1 [AFK]: YAML config + source refactor → depends on `none` → batch 1
- Task 2 [AFK]: CLI wiring, tests, cassette refresh → depends on `Task 1` → batch 2

## Agent Assignments

- Task 1: YAML + source refactor → python-development:python-pro
- Task 2: CLI wiring + tests + cassette refresh → python-development:python-pro
- Polish: post-implementation-polish → general-purpose

---

## File Structure

**New:**
- `slopmortem/corpus/sources/hn_queries.yaml` — phrase list + pagination defaults. Tracked in git; this is the documented default surface (operators wanting personal phrases override via a `slopmortem.local.toml` pointer or by passing a different path at construction).

**Modified:**
- `slopmortem/corpus/sources/hn_algolia.py` — replace single-query constructor with `queries_yaml_path` kwarg; switch `/search` → `/search_by_date`; loop phrases × pages; dedup by `objectID`. Endpoint base, rate-limit plumbing, RawEntry shape unchanged.
- `slopmortem/cli/_ingest_cmd.py` (the `sources` list, currently at line 317) — change `HNAlgoliaSource(query="post-mortem", rps=5.0)` to `HNAlgoliaSource(queries_yaml_path=_default_hn_queries_yaml(), rps=5.0)`. Add a `_default_hn_queries_yaml()` helper next to `_default_curated_yaml()` (mirror its traversal exactly — no `.resolve()`).

**New tests:**
- `tests/sources/test_hn_algolia_yaml.py` — YAML loading, multi-phrase fan-out, page-cap, objectID dedup, fixture-based cassette round-trip.

**Modified / removed tests:**
- `tests/sources/test_hn_algolia.py` — every test in this file targets the old surface (`HNAlgoliaSource(query=...)`, `build_url(page=...)`, `since_epoch=`). After the Step 1.5 refactor those names no longer exist, so this file *must* be either rewritten against the new YAML constructor or deleted. The new `test_hn_algolia_yaml.py` covers the same ground (URL prefix, pagination, hit→entry shape, cassette round-trip), so deletion is the simpler path. Step 1.7 below makes this an explicit checkbox — don't skip it or `just typecheck` will fail with `AttributeError: ... has no attribute 'build_url' / 'since_epoch'`.
- `tests/sources/cassettes/test_hn_algolia/` — orphaned by the deletion above; remove the directory in the same commit so the cassette tree mirrors the test tree.

---

## Pros and Cons of Key Decisions

**Phrase-only YAML vs hybrid (phrases + named suspects):**
- Pros of phrase-only: scales without manual curation; no overlap with `curated.py`'s role; catches the unknown-name long tail.
- Pros of hybrid: guaranteed coverage of named-known-dead competitors.
- **Pick phrase-only.** Named suspects belong in `curated.py` with a hand-picked Wayback URL — that's a higher-quality entry than whatever HN's `search_by_date` happens to surface for a given name. Use the right tool for each job.

**`search_by_date` vs `search`:**
- Pros of `search_by_date`: reaches the long tail; not biased by HN engagement; deterministic pagination.
- Pros of `search`: fewer pages to cover the "interesting" obituaries (relevance-ranked top 30 catches the famous ones).
- **Pick `search_by_date`.** Validated: relevance ranking misses Mattermark (120pts) entirely. The whole point of slopmortem is the long tail; relevance ranking is the wrong tool.

**Year-window slicing (`pages_per_window`, default 3):**
- Pros: every year between `date_from` and today gets its own page budget — old years (2015–18) reach the obituaries that flat-pagination would miss; high-volume recent years don't blow the entire phrase budget.
- Cons: more total API calls than flat pagination (8 phrases × ~12 years × 3 pages ≈ 288 calls vs. 8 × 20 = 160 for the flat scheme); slightly more complex source code (year-loop wrapping the page-loop).
- Cons: low-volume years (where a phrase has fewer than 90 hits) hit empty pages early — wasted requests, but free against HN.
- **Slice by year, cap each window at 3 pages.** Validated cost: ~$7–9 per ingest in downstream Tavily + Haiku, vs. ~$15–22 for a flat 70-page scheme that would also reach Mattermark. The year-window scheme reaches every year deterministically; flat pagination can exhaust its budget on 2024–26 noise and never touch 2015–18. Operator can raise `pages_per_window` for deeper per-year sweeps when they want more historical density.

**Dedup by `objectID` across phrases:**
- Pros: prevents the same HN story being processed N times (Mattermark obituary matches both `"shut down"` and `"shutting down"`).
- Cons: the dedup set lives in memory for the duration of `fetch()` — fine because the upper bound is small.
- **In-memory set keyed by `objectID`.** Simple, correct, no infrastructure.

**Drop URL-less hits (Ask-HN-style self-posts):**
- The new `_hit_to_entry` returns `None` when `url` is missing or empty, and the emitted `markdown_text` only carries title + HN metadata (no `story_text` / `comment_text` body). This is a deliberate behavior change vs. the prior implementation, which kept self-posts with `url=None` and inlined the post body.
- Pros: every emitted entry has a target URL the downstream `TavilyEnricher` / `WaybackEnricher` can hit; bodies come from the actual obituary page rather than a Show/Ask HN paraphrase, which is higher signal for the slop classifier.
- Cons: Show/Ask-HN obituaries (a small minority of shutdown discussion) drop on the floor. Acceptable: those entries' Tavily pass would have nothing to fetch anyway, and curated.yml is the right place for hand-vetted self-post URLs.
- **Accept the regression.** Test `test_skips_hits_missing_url` codifies the behavior so a future change doesn't silently re-introduce URL-less hits.

**Default `just ingest` behaviour change:**
- Pros of accepting the change: matches the project's "evolve defaults deliberately" philosophy; the YAML is in-tree and reviewable.
- Cons: cassette/eval baselines shift on first deployment.
- **Accept the change; re-record cassettes in Task 2.** The old `query="post-mortem"` produced too narrow a corpus to be load-bearing. Per the `CLAUDE.md` rule, treat the YAML and re-recorded cassettes as a single coherent commit.

---

## Task 1: YAML config + source refactor

**Why:** The source today is hardcoded to a single relevance-sorted query that misses the long tail. Pulling phrases into a tracked YAML and switching to chronological pagination unlocks ~50× more candidates per run with no infrastructure change.

**Files:**
- Create: `slopmortem/corpus/sources/hn_queries.yaml`
- Modify: `slopmortem/corpus/sources/hn_algolia.py`
- Create: `tests/sources/test_hn_algolia_yaml.py`
- Delete: `tests/sources/test_hn_algolia.py` (every test targets the old constructor surface; superseded by the new file)
- Delete: `tests/sources/cassettes/test_hn_algolia/` (orphaned by the deletion above)

- [x] **Step 1.1: Verify the live endpoint shape**

Run: `curl -sSI 'https://hn.algolia.com/api/v1/search_by_date?query=shutting+down&tags=story&hitsPerPage=5'`
Expected: `HTTP/2 200`. Then a single-bound follow-up: `curl -sS 'https://hn.algolia.com/api/v1/search_by_date?query=%22shutting+down%22&tags=story&numericFilters=created_at_i%3E1420070400&page=0&hitsPerPage=5'` — expected: JSON with `hits[]`, each item has `objectID`, `title`, `url`, `created_at`, `created_at_i`, `points`, `num_comments`. Then a **two-bound** check that exercises the comma-AND form actually used by the source: `curl -sS 'https://hn.algolia.com/api/v1/search_by_date?query=%22shut+down%22&tags=story&numericFilters=created_at_i%3E1483228800%2Ccreated_at_i%3C1514764800&page=0&hitsPerPage=10'` — expected: JSON `hits[]` where every `created_at_i` is between 1483228800 (2017-01-01) and 1514764800 (2018-01-01). If either response shape diverges or the two-bound filter returns hits outside the window, stop and update this plan before proceeding.

- [x] **Step 1.2: Create the YAML config**

Create `slopmortem/corpus/sources/hn_queries.yaml`:

```yaml
# HN Algolia phrase-driven discovery config. Phrases are matched against
# story titles + bodies via /search_by_date (chronological, not relevance-
# ranked). Each (phrase, calendar-year) window paginates up to
# ``pages_per_window`` × ``hits_per_page`` hits. Year-window slicing is
# what makes the long tail reachable — flat pagination from "most recent"
# only covers ~2.5 years before exhausting a 20-page budget.
# Dedup is by HN ``objectID`` across phrases and windows.
#
# To add a phrase: append a string to ``phrases``. Re-record the HN cassette
# (see ``docs/cassettes.md``) before committing.
defaults:
  date_from: "2015-01-01"
  date_to: ""                # empty = open-ended (today)
  pages_per_window: 3        # max pages per (phrase, year) window — 3 × 30 = 90 hits/window
  hits_per_page: 30

phrases:
  - "shutting down"
  - "winding down"
  - "winds down"
  - "wound down"
  - "is closing"
  - "we're closing"
  - "post-mortem"
  - "sunsetting"
```

- [x] **Step 1.3: Write the failing tests**

Create `tests/sources/test_hn_algolia_yaml.py`:

```python
"""HNAlgoliaSource: YAML-driven phrase discovery, pagination, dedup."""

from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent
from typing import Any
from unittest.mock import AsyncMock

import pytest

from slopmortem.corpus.sources import HNAlgoliaSource

CASSETTE_FILE = (
    Path(__file__).parent / "cassettes" / "test_hn_algolia_yaml" / "test_round_trip.yaml"
)


def _yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "hn_queries.yaml"
    p.write_text(dedent(body).strip() + "\n")
    return p


class _FakeResp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._payload


def _hits_payload(hits: list[dict[str, Any]]) -> dict[str, Any]:
    return {"hits": hits, "nbHits": len(hits), "page": 0, "nbPages": 1}


def _setup_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "slopmortem.corpus.sources.hn_algolia.respect_robots",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "slopmortem.corpus.sources.hn_algolia.throttle_for",
        AsyncMock(return_value=None),
    )


def test_build_url_uses_search_by_date_endpoint(tmp_path: Path) -> None:
    """Catches accidental swap to the relevance-ranked /search endpoint —
    ported from the deleted test_hn_algolia.py guard."""
    yaml_path = _yaml(
        tmp_path,
        """
        defaults:
          date_from: "2017-01-01"
          date_to: "2017-12-31"
          pages_per_window: 1
          hits_per_page: 30
        phrases:
          - "shut down"
        """,
    )
    src = HNAlgoliaSource(queries_yaml_path=yaml_path)
    win_start, win_end = src._windows[0]  # noqa: SLF001 — assert URL shape
    url = src._build_url("shut down", page=0, win_start=win_start, win_end=win_end)  # noqa: SLF001
    assert url.startswith("https://hn.algolia.com/api/v1/search_by_date?"), url
    assert not url.startswith("https://hn.algolia.com/api/v1/search?"), url


def test_build_url_quotes_phrase_for_phrase_match(tmp_path: Path) -> None:
    """Multi-word phrases must be wrapped in literal double-quotes so HN
    Algolia phrase-matches them; bare tokens AND-search and explode recall."""
    yaml_path = _yaml(
        tmp_path,
        """
        defaults:
          date_from: "2017-01-01"
          date_to: "2017-12-31"
          pages_per_window: 1
          hits_per_page: 30
        phrases:
          - "shutting down"
        """,
    )
    src = HNAlgoliaSource(queries_yaml_path=yaml_path)
    win_start, win_end = src._windows[0]  # noqa: SLF001
    url = src._build_url("shutting down", page=0, win_start=win_start, win_end=win_end)  # noqa: SLF001
    # quote_plus('"shutting down"') == '%22shutting+down%22'
    assert "query=%22shutting+down%22" in url, url


async def test_emits_one_entry_per_phrase_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _yaml(
        tmp_path,
        """
        defaults:
          date_from: "2016-01-01"
          date_to: "2016-12-31"
          pages_per_window: 1
          hits_per_page: 30
        phrases:
          - "shutting down"
        """,
    )

    payload = _hits_payload([
        {
            "objectID": "1",
            "title": "RethinkDB is shutting down",
            "url": "https://rethinkdb.com/blog/sunset",
            "created_at": "2016-10-06T00:00:00Z",
            "created_at_i": 1475712000,
            "points": 1674,
            "num_comments": 800,
        }
    ])
    monkeypatch.setattr(
        "slopmortem.corpus.sources.hn_algolia.safe_get",
        AsyncMock(return_value=_FakeResp(payload)),
    )
    _setup_throttle(monkeypatch)

    src = HNAlgoliaSource(queries_yaml_path=yaml_path)
    entries = [e async for e in src.fetch()]
    assert len(entries) == 1
    e = entries[0]
    assert e.source == "hn_algolia"
    assert e.source_id == "1"
    assert e.url == "https://rethinkdb.com/blog/sunset"
    assert e.markdown_text is not None
    assert "RethinkDB" in e.markdown_text


async def test_dedups_objectid_across_phrases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same story matches two phrases — emit it once."""
    yaml_path = _yaml(
        tmp_path,
        """
        defaults:
          date_from: "2017-01-01"
          date_to: "2017-12-31"
          pages_per_window: 1
          hits_per_page: 30
        phrases:
          - "shut down"
          - "shutting down"
        """,
    )

    duplicated = _hits_payload([
        {
            "objectID": "15984772",
            "title": "Mattermark (YC S12) to shut down after selling to FullContact",
            "url": "https://techcrunch.com/2017/12/21/mattermark-to-shut-down/",
            "created_at": "2017-12-22T00:00:00Z",
            "created_at_i": 1513900800,
            "points": 120,
            "num_comments": 58,
        }
    ])
    monkeypatch.setattr(
        "slopmortem.corpus.sources.hn_algolia.safe_get",
        AsyncMock(return_value=_FakeResp(duplicated)),
    )
    _setup_throttle(monkeypatch)

    src = HNAlgoliaSource(queries_yaml_path=yaml_path)
    entries = [e async for e in src.fetch()]
    assert len(entries) == 1
    assert entries[0].source_id == "15984772"


async def test_paginates_until_empty_within_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pagination terminates on an empty page within a single window."""
    yaml_path = _yaml(
        tmp_path,
        """
        defaults:
          date_from: "2018-01-01"
          date_to: "2018-12-31"
          pages_per_window: 5
          hits_per_page: 30
        phrases:
          - "winding down"
        """,
    )

    pages = [
        _hits_payload([{"objectID": str(i), "title": f"x{i}", "url": f"https://x/{i}", "created_at": "2018-06-01T00:00:00Z", "created_at_i": 1527811200, "points": 1, "num_comments": 0}])
        for i in range(3)
    ] + [_hits_payload([])]  # empty page terminates within-window pagination

    fake = AsyncMock(side_effect=[_FakeResp(p) for p in pages])
    monkeypatch.setattr("slopmortem.corpus.sources.hn_algolia.safe_get", fake)
    _setup_throttle(monkeypatch)

    src = HNAlgoliaSource(queries_yaml_path=yaml_path)
    entries = [e async for e in src.fetch()]
    assert len(entries) == 3
    # Should have hit 4 pages (3 with hits + 1 empty terminator), not all 5.
    assert fake.call_count == 4


async def test_iterates_one_window_per_year(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """date_from=2015-01-01, date_to=2017-12-31 should yield 3 windows × 1 page = 3 calls."""
    yaml_path = _yaml(
        tmp_path,
        """
        defaults:
          date_from: "2015-01-01"
          date_to: "2017-12-31"
          pages_per_window: 1
          hits_per_page: 30
        phrases:
          - "sunsetting"
        """,
    )

    call_log: list[str] = []

    async def fake_get(url: str, **_: object) -> _FakeResp:
        call_log.append(url)
        return _FakeResp(_hits_payload([]))  # empty — we only care about call count

    monkeypatch.setattr("slopmortem.corpus.sources.hn_algolia.safe_get", AsyncMock(side_effect=fake_get))
    _setup_throttle(monkeypatch)

    src = HNAlgoliaSource(queries_yaml_path=yaml_path)
    entries = [e async for e in src.fetch()]
    assert entries == []
    # 3 years × 1 phrase × 1 page = 3 calls. Each URL must contain a different
    # numericFilters range bracketing one calendar year.
    assert len(call_log) == 3
    # Spot-check: the three URLs reference distinct year-start epochs
    # (2015-01-01 = 1420070400, 2016-01-01 = 1451606400, 2017-01-01 = 1483228800).
    epochs = {1420070400, 1451606400, 1483228800}
    for epoch in epochs:
        assert any(f"created_at_i%3E{epoch}" in u or f"created_at_i>{epoch}" in u for u in call_log), (
            f"expected one URL bracketing year starting at epoch {epoch}; got {call_log}"
        )


async def test_respects_pages_per_window_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _yaml(
        tmp_path,
        """
        defaults:
          date_from: "2020-01-01"
          date_to: "2020-12-31"
          pages_per_window: 2
          hits_per_page: 30
        phrases:
          - "sunsetting"
        """,
    )

    # Always returns one hit — would paginate forever without the cap.
    def make_page(call_idx: int) -> _FakeResp:
        return _FakeResp(_hits_payload([
            {
                "objectID": f"obj-{call_idx}",
                "title": f"x{call_idx}",
                "url": f"https://x/{call_idx}",
                "created_at": "2020-06-01T00:00:00Z",
                "created_at_i": 1590969600,
                "points": 1,
                "num_comments": 0,
            }
        ]))

    fake = AsyncMock(side_effect=[make_page(i) for i in range(10)])
    monkeypatch.setattr("slopmortem.corpus.sources.hn_algolia.safe_get", fake)
    _setup_throttle(monkeypatch)

    src = HNAlgoliaSource(queries_yaml_path=yaml_path)
    entries = [e async for e in src.fetch()]
    # 1 window × 2-page cap × 1 hit per page = 2 entries; 2 API calls.
    assert len(entries) == 2
    assert fake.call_count == 2


async def test_skips_hits_missing_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _yaml(
        tmp_path,
        """
        defaults:
          date_from: "2020-01-01"
          date_to: "2020-12-31"
          pages_per_window: 1
          hits_per_page: 30
        phrases:
          - "wound down"
        """,
    )

    payload = _hits_payload([
        {"objectID": "no-url", "title": "Ask HN: how", "created_at_i": 1577836800, "points": 1, "num_comments": 0},
        {"objectID": "ok", "title": "X", "url": "https://x", "created_at_i": 1577836800, "points": 1, "num_comments": 0},
    ])
    monkeypatch.setattr(
        "slopmortem.corpus.sources.hn_algolia.safe_get",
        AsyncMock(return_value=_FakeResp(payload)),
    )
    _setup_throttle(monkeypatch)

    src = HNAlgoliaSource(queries_yaml_path=yaml_path)
    entries = [e async for e in src.fetch()]
    assert len(entries) == 1
    assert entries[0].source_id == "ok"


@pytest.mark.vcr
async def test_round_trip(tmp_path: Path) -> None:
    """Live cassette: single year-window covering 2017 so the Mattermark
    obituary surfaces and the cassette stays small."""
    if not CASSETTE_FILE.exists() and not os.environ.get("RECORD"):
        pytest.skip(f"no cassette at {CASSETTE_FILE}; rerun with RECORD=1 to record")
    yaml_path = _yaml(
        tmp_path,
        """
        defaults:
          date_from: "2017-01-01"
          date_to: "2017-12-31"
          pages_per_window: 3
          hits_per_page: 30
        phrases:
          - "shut down"
        """,
    )
    src = HNAlgoliaSource(queries_yaml_path=yaml_path)
    entries = [e async for e in src.fetch()]
    # Mattermark obituary (Dec 22 2017) lives in this year-window.
    assert any("Mattermark" in (e.markdown_text or "") for e in entries)
```

- [x] **Step 1.4: Run tests, confirm they fail**

Run: `uv run pytest tests/sources/test_hn_algolia_yaml.py -v`
Expected: most tests fail because `HNAlgoliaSource.__init__` still takes `query=`, not `queries_yaml_path=`.

- [x] **Step 1.5: Refactor the source**

Edit `slopmortem/corpus/sources/hn_algolia.py`. Replace the existing `HNAlgoliaSource` class:

```python
"""HN Algolia phrase-driven discovery source.

YAML-driven, multi-phrase, year-window-sliced, chronologically-paginated.
For each phrase, the source iterates one (epoch_start, epoch_end) window
per calendar year between ``date_from`` and ``date_to``, and within each
window paginates ``/search_by_date`` (NOT ``/search`` — relevance ranking
buries the long tail) up to ``pages_per_window``. Pagination terminates
on an empty page within a window. Dedup is by HN ``objectID`` across all
phrases and windows.

Year-window slicing is what makes the long tail reachable: a flat
chronological pagination from "most recent" only covers ~6 weeks per page,
so 20 pages = ~Nov 2023 onward — never reaching 2017's Mattermark
obituary. Per-year windows give every year its own bounded budget.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import quote_plus

import yaml

from slopmortem.corpus.sources._names import SOURCE_HN_ALGOLIA
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

ENDPOINT: Final = "https://hn.algolia.com/api/v1/search_by_date"
DEFAULT_PAGES_PER_WINDOW: Final = 3
DEFAULT_HITS_PER_PAGE: Final = 30
DEFAULT_LOOKBACK_YEARS: Final = 11  # if date_from is unset, look back this many years


def _epoch(date_str: str) -> int | None:
    """Parse YYYY-MM-DD to UTC epoch seconds; return None for empty string."""
    if not date_str:
        return None
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


def _year_windows(
    date_from_epoch: int | None,
    date_to_epoch: int | None,
) -> list[tuple[int, int]]:
    """Yield (epoch_start, epoch_end) per calendar year between bounds (inclusive).

    - If ``date_to_epoch`` is unset, defaults to "now".
    - If ``date_from_epoch`` is unset, defaults to ``DEFAULT_LOOKBACK_YEARS``
      before ``date_to``.
    - Each window is clamped to its calendar year boundary, but the first
      and last windows are clamped to the actual ``date_from``/``date_to``.
    """
    end_dt = (
        datetime.fromtimestamp(date_to_epoch, tz=UTC)
        if date_to_epoch is not None
        else datetime.now(UTC)
    )
    start_dt = (
        datetime.fromtimestamp(date_from_epoch, tz=UTC)
        if date_from_epoch is not None
        else end_dt.replace(
            year=end_dt.year - DEFAULT_LOOKBACK_YEARS, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    )

    windows: list[tuple[int, int]] = []
    year = start_dt.year
    while year <= end_dt.year:
        year_start = datetime(year, 1, 1, tzinfo=UTC)
        year_end = datetime(year, 12, 31, 23, 59, 59, tzinfo=UTC)
        win_start = max(start_dt, year_start)
        win_end = min(end_dt, year_end)
        if win_start <= win_end:
            windows.append((int(win_start.timestamp()), int(win_end.timestamp())))
        year += 1
    return windows


class HNAlgoliaSource:
    """[Source] Phrase-driven HN obituary discovery via /search_by_date,
    sliced into one query per calendar year per phrase."""

    def __init__(
        self,
        *,
        queries_yaml_path: Path,
        user_agent: str = USER_AGENT,
        rps: float = 5.0,
    ) -> None:
        cfg_obj: object = yaml.safe_load(queries_yaml_path.read_text(encoding="utf-8"))
        if not isinstance(cfg_obj, dict):
            raise ValueError(f"hn_queries.yaml: expected mapping, got {type(cfg_obj).__name__}")
        cfg = cast("dict[str, Any]", cfg_obj)  # pyright: ignore[reportExplicitAny]
        defaults_obj = cfg.get("defaults") or {}
        if not isinstance(defaults_obj, dict):
            raise ValueError("hn_queries.yaml: 'defaults' must be a mapping")
        defaults = cast("dict[str, Any]", defaults_obj)  # pyright: ignore[reportExplicitAny]
        phrases_obj = cfg.get("phrases") or []
        if not isinstance(phrases_obj, list):
            raise ValueError("hn_queries.yaml: 'phrases' must be a list")
        self.phrases: list[str] = [p for p in phrases_obj if isinstance(p, str) and p.strip()]
        if not self.phrases:
            raise ValueError("hn_queries.yaml: 'phrases' must contain at least one non-empty entry")

        self.date_from_epoch: int | None = _epoch(str(defaults.get("date_from") or ""))
        self.date_to_epoch: int | None = _epoch(str(defaults.get("date_to") or ""))
        self.pages_per_window: int = int(defaults.get("pages_per_window", DEFAULT_PAGES_PER_WINDOW))
        self.hits_per_page: int = int(defaults.get("hits_per_page", DEFAULT_HITS_PER_PAGE))
        self.user_agent = user_agent
        self.rps = rps

        self._windows: list[tuple[int, int]] = _year_windows(
            self.date_from_epoch, self.date_to_epoch
        )

    def _build_url(self, phrase: str, page: int, win_start: int, win_end: int) -> str:
        # Wrap the phrase in literal double-quotes so HN Algolia treats it as
        # a phrase match instead of AND-ing the tokens. Without quoting,
        # ``shutting down`` matches anything containing both words anywhere
        # (titles, comments, body) — e.g. an Ask-HN about "shoot...down" — and
        # our cost / signal estimates assume phrase semantics.
        quoted_phrase = f'"{phrase}"'
        # Inclusive lower bound (``>=``) preserves the old source's edge
        # behaviour for stories landing exactly on a year boundary.
        numeric = f"created_at_i>={win_start},created_at_i<{win_end}"
        return (
            f"{ENDPOINT}?query={quote_plus(quoted_phrase)}&tags=story"
            f"&page={page}&hitsPerPage={self.hits_per_page}"
            f"&numericFilters={quote_plus(numeric)}"
        )

    @staticmethod
    def _hit_to_entry(hit: dict[str, Any]) -> RawEntry | None:  # pyright: ignore[reportExplicitAny]
        # Annotate every ``hit.get(...)`` as ``object`` immediately so subsequent
        # ``isinstance`` / ``str(...)`` calls don't propagate ``Any``; basedpyright
        # strict with ``reportAny=error`` otherwise flags the implicit __str__.
        object_id: object = hit.get("objectID")
        url: object = hit.get("url")
        title: object = hit.get("title") or ""
        created_at: object = hit.get("created_at") or ""
        points: object = hit.get("points")
        num_comments: object = hit.get("num_comments")
        if not isinstance(object_id, str) or not object_id:
            return None
        if not isinstance(url, str) or not url:
            # Ask-HN / Show-HN self-posts have no URL and would have nothing
            # for the Tavily enricher to fetch. See "Drop URL-less hits" in
            # the plan rationale.
            return None
        title_str = title if isinstance(title, str) else str(title)
        markdown = (
            f"# {title_str}\n\n"
            f"hn_object_id: {object_id}\n"
            f"created_at: {created_at}\n"
            f"points: {points}\n"
            f"num_comments: {num_comments}\n"
        ).strip()
        return RawEntry(
            source=SOURCE_HN_ALGOLIA,
            source_id=object_id,
            url=url,
            raw_html=None,
            markdown_text=markdown,
            fetched_at=datetime.now(UTC),
        )

    async def _fetch_page(
        self, phrase: str, page: int, win_start: int, win_end: int
    ) -> list[dict[str, Any]] | None:  # pyright: ignore[reportExplicitAny]
        url = self._build_url(phrase, page, win_start, win_end)
        # Robots is host-cached and checked once at the top of fetch();
        # skip per-call recheck.
        await throttle_for(url, rps=self.rps)
        resp = await safe_get(url)
        if resp.status_code >= HTTP_BAD_REQUEST:
            logger.warning(
                "hn_algolia: HTTP %s for phrase=%r window=(%d,%d) page=%d",
                resp.status_code, phrase, win_start, win_end, page,
            )
            return None
        payload: object = resp.json()
        if not isinstance(payload, dict):
            return None
        payload_dict = cast("dict[str, Any]", payload)  # pyright: ignore[reportExplicitAny]
        hits_obj: object = payload_dict.get("hits") or []
        if not isinstance(hits_obj, list):
            return None
        return [cast("dict[str, Any]", h) for h in hits_obj if isinstance(h, dict)]  # pyright: ignore[reportExplicitAny]

    async def fetch(self) -> AsyncIterator[RawEntry]:
        # Robots check is per-host, not per-URL: hit it once up front so a
        # blocked endpoint short-circuits the entire run instead of being
        # re-checked across every (phrase, year, page) iteration. ``ENDPOINT``
        # is a valid URL on hn.algolia.com — respect_robots only inspects host.
        if not await respect_robots(ENDPOINT, user_agent=self.user_agent):
            logger.info("hn_algolia: robots blocked %s; skipping source", ENDPOINT)
            return
        seen: set[str] = set()
        for phrase in self.phrases:
            for win_start, win_end in self._windows:
                for page in range(self.pages_per_window):
                    hits = await self._fetch_page(phrase, page, win_start, win_end)
                    if hits is None or not hits:
                        # None = HTTP error or response shape mismatch (logged
                        # in _fetch_page); empty = window exhausted. Either
                        # way, stop paginating this window and move on.
                        break
                    for hit in hits:
                        entry = self._hit_to_entry(hit)
                        if entry is None or entry.source_id in seen:
                            continue
                        seen.add(entry.source_id)
                        yield entry
```

- [x] **Step 1.6: Run tests**

Run: `uv run pytest tests/sources/test_hn_algolia_yaml.py -v -k "not round_trip"`
Expected: 8 PASS (`build_url_uses_search_by_date_endpoint`, `build_url_quotes_phrase_for_phrase_match`, `emits_one_entry_per_phrase_match`, `dedups_objectid_across_phrases`, `paginates_until_empty_within_window`, `iterates_one_window_per_year`, `respects_pages_per_window_cap`, `skips_hits_missing_url`).

- [x] **Step 1.7: Remove the stale single-query tests + cassette tree**

The old `tests/sources/test_hn_algolia.py` exercises `HNAlgoliaSource(query=...)`, `build_url(page=...)`, and `since_epoch=` — none of which exist after Step 1.5. Skipping this step is **not optional**: `just typecheck` and `just test` both fail without it. The new `test_hn_algolia_yaml.py` already ports the URL-prefix `/search_by_date` guard (`test_build_url_uses_search_by_date_endpoint`), so delete the old file rather than rewrite:

```
git rm tests/sources/test_hn_algolia.py
git rm -r tests/sources/cassettes/test_hn_algolia/
```

- [x] **Step 1.8: Lint + typecheck + targeted tests**

Run: `just lint && just typecheck && uv run pytest tests/sources/test_hn_algolia_yaml.py -v -k "not round_trip"`
Expected: clean lint, clean typecheck, 8 PASS. If basedpyright complains about formatting fields off `hit.get(...)`, the `: object` narrowing in `_hit_to_entry` was lost during the edit — restore it.

- [x] **Step 1.9: Commit**

```
git add slopmortem/corpus/sources/hn_algolia.py slopmortem/corpus/sources/hn_queries.yaml tests/sources/test_hn_algolia_yaml.py
git rm tests/sources/test_hn_algolia.py tests/sources/cassettes/test_hn_algolia/*
git commit -m "feat: hn yaml phrase-driven discovery"
```

---

## Task 2: CLI wiring + cassette refresh

**Why:** The CLI still constructs `HNAlgoliaSource(query="post-mortem", rps=5.0)` — `NameError` at runtime once Task 1 lands. Cassettes capturing the old request shape are stale.

**Files:**
- Modify: `slopmortem/cli/_ingest_cmd.py` (constructor call + helper for default YAML path)
- Re-record: any HN-touching cassettes under `tests/.../cassettes/`

- [x] **Step 2.1: Add the default-YAML helper**

In `slopmortem/cli/_ingest_cmd.py`, immediately below the existing `_default_curated_yaml()` helper, add a mirror — same `Path(__file__).parent.parent` traversal (no `.resolve()`) so the two helpers stay parallel:

```python
def _default_hn_queries_yaml() -> Path:
    return Path(__file__).parent.parent / "corpus" / "sources" / "hn_queries.yaml"
```

- [x] **Step 2.2: Update the source-list construction**

In `_run_ingest` (the `sources: list[Source] = [...]` block, currently around line 317), replace the existing line:

```python
HNAlgoliaSource(query="post-mortem", rps=5.0),
```

with:

```python
HNAlgoliaSource(queries_yaml_path=_default_hn_queries_yaml(), rps=5.0),
```

- [x] **Step 2.3: Smoke-test the CLI**

Scope the smoke test to HN only — running every source pulls Crunchbase + curated state into the picture and slows the loop. `--dry-run` still constructs the OpenRouter client, so `OPENROUTER_API_KEY` must be set in `.env`/env (a stub value works; no LLM calls fire under `FakeSlopClassifier`).

```
uv run slopmortem ingest --dry-run --only-source hn_algolia --limit 50 \
  2>&1 | tee /tmp/slopmortem_hn_smoke.log | tail -60
```

Expected:
- Dry-run completes with no error.
- `/tmp/slopmortem_hn_smoke.log` shows `hn_algolia: HTTP 200 ...` traffic across multiple (phrase, year-window, page) requests. With the default YAML (8 phrases × ~12 windows × up to 3 pages) the upper bound is ~288 calls; many windows will short-circuit on empty pages.
- `seen` and `would_process` in the result table are non-zero, and reflect the broader recall vs. the prior single-query baseline.

If the run errors with a stale-cassette message, proceed to Step 2.4.

- [x] **Step 2.3a: Source-level smoke (per-entry visibility)**

`--dry-run` only prints aggregated counts via Rich; the per-entry stream
is invisible because the CLI doesn't configure stdlib logging
(`logger.info` in `hn_algolia.py` is dropped at the root logger's default
WARNING level). To eyeball what the new YAML actually emits, bypass the
ingest pipeline and iterate the source directly. Hits the live HN
Algolia API only — no LLM, no journal, no Qdrant.

```bash
uv run python -c '
import anyio
from pathlib import Path
from slopmortem.corpus.sources import HNAlgoliaSource

async def main():
    src = HNAlgoliaSource(
        queries_yaml_path=Path("slopmortem/corpus/sources/hn_queries.yaml"),
        rps=5.0,
    )
    n = 0
    async for e in src.fetch():
        title = (e.markdown_text or "").splitlines()[0].lstrip("# ")[:90]
        print(f"{e.source_id:<10} {e.url[:70]:<70} {title}")
        n += 1
        if n >= 50:
            break
    print(f"-- {n} entries")

anyio.run(main)
'
```

Expected: ~50 distinct HN object IDs, each with a URL and a title that
plausibly mentions a shutdown / winding / sunsetting phrase. Sanity
signals:

- Titles dominated by genuine obituaries (named startups + a phrase from
  the YAML) — recall is working.
- Titles uniformly off-topic ("Ask HN: …", "When to shoot your customer
  in the foot", …) — phrase quoting regressed in `_build_url`; re-check
  Step 1.5's `quoted_phrase` wrapping.
- Run terminates instantly with `-- 0 entries` — `_year_windows` is
  empty (check `date_from` / `date_to` parsing) or `respect_robots`
  short-circuited (check the `ENDPOINT` host).

- [x] **Step 2.4: Identify and re-record stale cassettes**

Stale cassette indicators are `NoCannedResponseError` from the test suite or `vcr` mismatch errors during `just test`. Find them:

```
uv run pytest tests/sources -v 2>&1 | grep -E "(NoCannedResponseError|cassette)" | head -20
```

For each stale cassette under `tests/sources/cassettes/test_hn_algolia*/` (or equivalent):

```
RECORD=1 uv run pytest <stale_test_id> -v
```

Inspect the resulting YAML before commit; if it captured a transient 5xx, re-record. The project's `docs/cassettes.md` covers cassette hygiene if anything is unclear.

- [x] **Step 2.5: Verify Mattermark surfaces in a tight live run**

Optional but high-value sanity check that proves end-to-end:

```
RECORD=1 uv run pytest tests/sources/test_hn_algolia_yaml.py::test_round_trip -v
```

Expected: PASS (the assertion `any("Mattermark" in (e.markdown_text or "") for e in entries)` holds against the live API for the Dec 21–23 2017 window).

If it fails, the live HN Algolia response shape has changed since the validation in this plan. Stop and update the response parser before committing.

- [x] **Step 2.6: Lint + typecheck + full test suite**

Run: `just lint && just typecheck && just test`
Expected: all clean.

- [x] **Step 2.7: Commit**

```
git add slopmortem/cli/_ingest_cmd.py tests/sources/cassettes/
git commit -m "wiring: hn yaml phrase source and refreshed cassettes"
```

---

## Out of scope

- **Per-source budget caps.** Same rationale as the DefiLlama plan: defer until a real overrun lands.
- **Named-suspect lookups (e.g. "Mattermark", "DataFox").** Belong in `curated.py` with a hand-picked Wayback URL, not in this YAML. Phrase-only by design.
- **Adding a `--hn-queries-yaml <path>` CLI flag.** Operators who want to override the default should do it via `slopmortem.local.toml` plumbing — out of scope here. Constructor kwarg is enough for tests and programmatic use.
- **Body extraction.** The source emits `RawEntry.url = <article URL>`. The existing `TavilyEnricher` (gated by `--tavily-enrich`) is what fills `raw_html`/`markdown_text` from the URL. Tightening that coupling — e.g. forcing Tavily on like the DefiLlama plan does — is a separate decision; HN URLs more often resolve to live sites than DefiLlama-Wayback URLs do, so the implication isn't as clean.
- **Re-recording the eval cassettes.** Same as the DefiLlama plan: the eval runner is gated by curated post-mortems plus the existing two sources (per `docs/cassettes.md`). Verify with `just eval` after Task 2 — if it diverges, that's a separate plan.
- **Updating `docs/architecture.md`.** Add a one-line note pointing to this plan once it lands; full re-write isn't warranted.
- **Switching the YAML loader to `importlib.resources`.** `tavily_news.py` loads its packaged YAML via `importlib.resources.files(...)`, which keeps it wheel-installable. This plan reads the YAML through a `Path` constructor kwarg (mirroring `CuratedSource`), which works because `slopmortem` is consumed via `uv sync` from a checkout — never as a built wheel. If wheel distribution ever becomes a goal, both `CuratedSource` and `HNAlgoliaSource` should switch together; doing only HN here would create a half-converted surface.
