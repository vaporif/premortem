# DefiLlama Source Implementation Plan

**Companion design spec:** `docs/specs/2026-05-06-defillama-source-design.md` covers the WHAT/WHY (goals, decisions, pros/cons, out-of-scope). This plan covers the HOW (file-by-file, line-by-line, ordered checkboxes). Spec changes require revisiting this plan; implementation drift from the spec should be reconciled into the spec, not absorbed silently.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or `/team-feature` to implement this plan task-by-task, per the Execution Strategy below. Steps use checkbox (`- [ ]`) syntax — these are **persistent durable state**, not visual decoration. The executor edits the plan file in place: `- [ ]` → `- [x]` the instant a step verifies, before moving on. On resume (new session, crash, takeover), the executor scans existing `- [x]` marks and skips them — these steps are NOT redone. TodoWrite mirrors this state in-session; the plan file is the source of truth across sessions.

**Goal:** Add a single new `Source` adapter that consumes DefiLlama's `/protocols` and `/protocol/{slug}` JSON endpoints, identifies dead/zombie protocols via peak-relative TVL trajectory, anchors each entry's URL to a peak-era Wayback snapshot, and emits a `RawEntry` whose body the existing `TavilyEnricher` then fills with the original (live-era) pitch text. Fills the crypto-native long-tail gap that the existing curated YAML, HN Algolia, and Crunchbase CSV sources miss — without depending on RSS feeds whose tail-window primitive can't reach historical data.

**Architecture:** One independent module under `slopmortem/corpus/sources/` implementing the existing `Source` Protocol (`fetch() -> AsyncIterable[RawEntry]`). The adapter routes through `safe_get`, `respect_robots`, and `throttle_for` so the SSRF guard, robots policy, and per-host token bucket apply uniformly. Two helpers live in the same module: `classify_death(chain_tvls)` decides dead/alive from the TVL series, and `wayback_snapshot_near(url, target_date)` resolves the live URL to a peak-era `web.archive.org` snapshot via the CDX API. The source's emitted `RawEntry.url` points at the Wayback snapshot, so the existing `TavilyEnricher` extracts the original pitch body without any new enricher. Wired into `_ingest_cmd.py` behind an opt-in `--enable-defillama` flag, which **implies** `--tavily-enrich` and fails at startup if `TAVILY_API_KEY` is unset (without Tavily, this source produces empty entries — Wayback HTML alone is unusable). The default `just ingest` remains bit-identical to current behaviour.

**Tech Stack:** Python 3.13, anyio, httpx, pydantic v2, pytest + pytest-recording (vcrpy cassettes), basedpyright strict.

## Why scope was trimmed

An earlier draft of this plan added five sources (DefiLlama + four RSS feeds: rekt.news, web3isgoinggreat.com, TechCrunch shutdown tag, Crunchbase News closed tag). Pre-execution review surfaced a structural problem: **RSS feeds expose only a publisher-controlled tail window** (typically 10-30 most recent items, no `?page=2`, no cursor). They can never reach historical data, so their one-shot ingest yield is ~60-90 entries combined and they only earn their keep if cronned over months. The build cost (4 adapters + tests + cassettes + a shared parser) didn't justify the corpus expansion. DefiLlama is the one source from that draft whose primitive — a full-table JSON API — actually delivers depth on a single fetch. Everything else was deferred to a future plan if and when scheduled ingest lands.

## Execution Strategy

**Subagents** — default; no spec override. The single source adapter and the wiring task can run sequentially in one session, or split across two subagents if `/team-feature` is used. The wiring task depends on the source existing.

## Task Dependency Graph

- Task 1 [AFK]: DefiLlama source → depends on `none` → batch 1
- Task 2 [AFK]: CLI wiring, reliability rank, exports → depends on `Task 1` → batch 2

## Agent Assignments

- Task 1: DefiLlama source → python-development:python-pro
- Task 2: CLI wiring + reliability rank + exports → python-development:python-pro
- Polish: post-implementation-polish → general-purpose

---

## File Structure

**New:**
- `slopmortem/corpus/sources/defillama.py` — `DefiLlamaSource` plus two module-level helpers (`classify_death`, `wayback_snapshot_near`):
  1. GET `https://api.llama.fi/protocols` (one bulk call).
  2. Shortlist by current TVL — protocols with `0 < tvl < shortlist_tvl_ceiling_usd` (default `$100_000`) become candidates. The ceiling intentionally sits above the typical zombie tail (validated: Primitive sits at $58K = 3.4% of its $1.72M peak — a $10K floor would have missed it).
  3. For each shortlisted candidate, GET `https://api.llama.fi/protocol/{slug}` and run `classify_death(chainTvls)` — peak ≥ `peak_floor_usd` ($1M) AND `current_tvl/peak_tvl ≤ dead_threshold_pct` (5%) AND the trailing 90-day mean is also ≤ 5% of peak AND `days_since_peak ≥ min_days_since_peak` (180). Skips alive, never-launched, and recently-dipped candidates.
  4. For dead candidates, call `wayback_snapshot_near(live_url, peak_date)` — Wayback CDX search in a ±30-day window around peak (widened to ±180 days on miss), filter `statuscode == "200"`, pick the snapshot closest to `peak_date`. Skip the candidate entirely if no usable snapshot exists.
  5. Yield one `RawEntry` per surviving candidate with `url = wayback_snapshot_url` and a seed `markdown_text` carrying name/category/chain/peak-tvl/death-date/description (so the row is non-empty even if Tavily later fails). Death metadata rides inside that markdown body — `RawEntry` has no `extra` field and adding one is out of scope.
  6. `max_emit` (default `300`) caps per-run yield to bound downstream cost (each entry costs one Haiku slop call plus a Tavily extract).

**Modified:**
- `slopmortem/corpus/sources/_names.py` — add `SOURCE_DEFILLAMA` constant.
- `slopmortem/corpus/sources/__init__.py` — re-export `DefiLlamaSource`.
- `slopmortem/cli/_ingest_cmd.py` — import the source, add `--enable-defillama` CLI flag (default `False`), thread it through `functools.partial` and `_run_ingest`'s signature, register it in `_SOURCE_REGISTRY` with an enable-branch in the `--only-source` block, and append to the `sources` list when the flag is set. Locations called out by landmark in Step 2.6 (line numbers drift; landmarks don't).
- `slopmortem/ingest/_helpers.py` — extend `_RELIABILITY_RANK` with `defillama → 3` (programmatic primary on-chain data, slots between `crunchbase_csv` at rank 2 and `tavily_news` at rank 4).

**New tests:**
- `tests/sources/test_defillama.py` — fake JSON response monkeypatch + cassette round-trip + `max_emit` cap.

**Modified tests:**
- `tests/ingest/test_reliability_rank.py` — add `(SOURCE_DEFILLAMA, 3)` parametrize case.
- `tests/test_cli_ingest.py` — add a regression test asserting that `--enable-defillama` without `TAVILY_API_KEY` exits non-zero with a message naming the env var.

---

## Pros and Cons of Key Decisions

**Peak-relative death detection vs raw TVL floor:**
- Pros of peak-relative: actually detects zombies. Validated: Primitive Finance sits at $58K current / $1.72M peak — a raw `<$10K` floor would have missed it; a $50K floor would still miss it; a peak-relative `≤5%` rule catches it cleanly.
- Pros of raw floor: one bulk API call, no per-candidate fan-out, simpler.
- Cons of peak-relative: requires an additional `/protocol/{slug}` call per shortlisted candidate to retrieve `chainTvls`. Validated live 2026-05-06: with a $100K shortlist ceiling, **1,621 of 7,451** protocols pass the shortlist — so worst case is ~1.6K detail calls per run (in practice fewer, since `max_emit=300` short-circuits once the quota fills; observed dead-rate in the shortlist is ~50%, so ~600 detail calls fill 300 emissions). Free, unmetered endpoint — acceptable.
- **Pick peak-relative.** The point of this source is to surface dead protocols; a detection rule that misses zombies (the dominant failure mode in DeFi) defeats the purpose. The bulk-call shortcut is preserved as a shortlist filter (current TVL < ceiling) — only candidates that pass it incur the per-protocol detail call.

**Internal Wayback resolution vs relying on the optional `WaybackEnricher`:**
- Pros of internal: `RawEntry.url` already points at the snapshot, so `TavilyEnricher` extracts the right page in one pass; the source's contract becomes "yields entries that are immediately usable" without a runtime flag dance.
- Pros of external (the existing `--enrich-wayback` enricher): keeps source modules thin; one place handles archive resolution.
- Cons of external: the existing `WaybackEnricher` runs uniformly on every entry; for DefiLlama specifically we need the snapshot anchored to the *peak TVL date*, which only the source has access to (peak comes from `chainTvls`). Generalising that into the enricher leaks DefiLlama-specific semantics upward.
- **Pick internal.** The Wayback target date is a DefiLlama-specific signal; resolution belongs in the source. Validated: peak-era snapshots return real pitch text (Primitive's *"Derivatives Without Counterparties... no lockups"*), end-of-life snapshots return death notices that aren't useful pitch corpus.

**Tavily implication and key check:**
- Pros: catches misconfiguration at startup instead of producing 300 silent empty entries.
- Cons: couples two CLI flags; operators who deliberately want the source-without-body case can't get it.
- **Pick implication + assertion.** No realistic use case for "DefiLlama without Tavily" — Wayback HTML pre-extraction is unreadable; the slop classifier and embeddings will both choke on it. `--enable-defillama` sets `tavily_enrich = True` internally and refuses to start if `TAVILY_API_KEY` is unset. The error message names both the missing env var and the implicating flag.

**Shortlist TVL ceiling (default $100k):**
- Pros: drastically reduces per-protocol detail calls (only candidates plausibly near the dead threshold get fetched).
- Cons: misses "10%-of-peak" deaths where current TVL is high in absolute terms (e.g. $5M current / $50M peak). Those exist but are rarer and more contentious to call dead.
- **$100K ceiling.** Captures all clean zombies validated so far (Primitive at $58K) without scanning the long alive tail. Tunable via constructor kwarg if a future plan wants to widen.

**Opt-in CLI flag vs always-on:**
- Pros of opt-in: existing `just ingest` output stays bit-identical; new source doesn't quietly add cost or Tavily dependency; cassette/eval baselines don't shift on first deployment.
- Cons of opt-in: the operator has to remember to turn it on.
- **Pick opt-in.** Project rule: don't bump model/source defaults without re-recording cassettes (per `CLAUDE.md`). Same logic applies to ingest sources.

**One adapter vs adding it to a generic feed adapter:**
- N/A — this is the only new source, so no abstraction question. Match the existing `HNAlgoliaSource` / `CuratedSource` / `CrunchbaseCsvSource` shape.

---

## Task 1: DefiLlama source

**Why:** DefiLlama's `/protocols` and `/protocol/{slug}` endpoints are the most complete machine-readable view of DeFi protocols and their TVL trajectories. A protocol whose current TVL is ≤5% of its historical peak (sustained for 90 days, with peak ≥$1M and ≥180 days behind us) is overwhelmingly dead or abandoned. Anchoring an entry's URL to a peak-era Wayback snapshot — instead of the live URL, which is usually empty or dead for these protocols — gives the existing `TavilyEnricher` a real pitch to extract. Direct fit for the Touchmarket and Extractor adjacencies; the long tail of dead options/derivatives protocols is invisible to the existing curated/HN/Crunchbase sources. No auth required for either DefiLlama or Wayback CDX.

**API verification:**
- `curl -sS 'https://api.llama.fi/protocols' | python -m json.tool | head -80` — bulk list. Expected fields per object: `id`, `name`, `slug`, `url`, `description`, `chain`, `category`, `tvl`, `listedAt` (epoch seconds).
- `curl -sS 'https://api.llama.fi/protocol/primitive' | python -m json.tool | head -40` — single protocol detail. Expected top-level keys include `chainTvls`, `description`, `url`, `twitter`, `github`, `audits`, `audit_links`, `category`. **Schema note:** `chainTvls[<chain>]` is itself a *dict* (`{"tvl": [...], "tokensInUsd": [...], "tokens": [...]}`), not a bare list. The daily series lives at `chainTvls[<chain>]["tvl"]` as `[{date, totalLiquidityUSD}, ...]`. **Primitive sanity check (validated live 2026-05-06):** peak $1,719,052 on 2022-05-18, current $58,168 (3.38% of peak) — well under the 5% dead threshold. The daily series can include duplicate timestamps for "today" (Primitive's live series shipped two end-of-day points for 2026-05-06: $58,678 and $58,168), so per-chain dedupe must keep the last value, not sum.
- `curl -sS 'https://web.archive.org/cdx/search/cdx?url=primitive.xyz&from=20220501&to=20220701&output=json&limit=5'` — Wayback CDX. Expected: JSON 2D array, header row first, then snapshot rows with `[urlkey, timestamp, original, mimetype, statuscode, digest, length]`. At least one row with `statuscode == "200"` should appear. **Operational note:** Wayback CDX is intermittently flaky (504s, 503s, read timeouts). The narrow→wide window fallback in `wayback_snapshot_near` is the only retry mechanism — if both queries fail the candidate is silently dropped. Validated 2026-05-06 against 9 shortlisted candidates: 5 classified `dead`, 1 dropped to no-coverage (~20% loss). Plan for ~370 dead-classified candidates fetched to fill `max_emit=300` after attrition.

**Files:**
- Create: `slopmortem/corpus/sources/defillama.py`
- Create: `tests/sources/test_defillama.py`
- Modify: `slopmortem/corpus/sources/_names.py`

- [ ] **Step 1.1: Verify the live endpoints**

Run all three `curl` commands listed under **API verification** above and confirm:

1. `/protocols` returns a JSON array with the expected fields on the first item.
2. `/protocol/primitive` returns a `chainTvls` object whose deepest series shows a TVL peak well above $1M followed by a sustained collapse — confirming that the validation reference case still holds.
3. The Wayback CDX query returns at least one `statuscode == "200"` row in the May–June 2022 window.

If any of these fail, **stop and surface** — either the DefiLlama API has changed (update endpoints in this plan) or Wayback dropped the snapshots (pick a different reference protocol like `siren`). Do not proceed with implementation against a stale spec.

- [ ] **Step 1.2: Add the source-name constant**

Edit `slopmortem/corpus/sources/_names.py`. Append after `SOURCE_TAVILY_NEWS` (the current last entry):

```python
SOURCE_DEFILLAMA: Final = "defillama"
```

- [ ] **Step 1.3: Write the failing tests**

Create `tests/sources/test_defillama.py`. The tests cover three layers:

1. `classify_death` as a pure function over synthetic `chainTvls` (dead, alive, never_launched, too_early, recently_dipped).
2. `wayback_snapshot_near` against a mocked CDX response (picks closest 200, widens window on miss, returns `None` when no 200 exists).
3. The `DefiLlamaSource.fetch()` integration path — bulk list → shortlist → detail → classify → Wayback resolve → `RawEntry`. Asserts the entry's `url` points at the Wayback snapshot, not the live URL.

```python
"""DefiLlama source: classify_death, Wayback resolution, fetch integration, cassette round-trip."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

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
            {"date": int(datetime.fromisoformat(d).replace(tzinfo=UTC).timestamp()), "totalLiquidityUSD": v}
            for d, v in points
        ]
    }


# ---- classify_death ----------------------------------------------------------


def test_classify_death_dead_zombie() -> None:
    # Primitive-shaped: peak $1.7M in May 2022, current $58K (~3.4% of peak).
    series = _series([("2022-05-31", 1_720_000.0)] + [
        (f"2025-01-{d:02d}", 58_000.0) for d in range(1, 30)
    ])
    verdict = classify_death({"Ethereum": series})
    assert verdict.status == "dead"
    assert verdict.peak_tvl == 1_720_000.0
    assert verdict.peak_date == datetime(2022, 5, 31, tzinfo=UTC).date()


def test_classify_death_alive() -> None:
    series = _series([(f"2024-12-{d:02d}", 1_000_000.0) for d in range(1, 30)])
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
    series = _series([
        ((today - timedelta(days=7)).isoformat(), 5_000_000.0),
        ((today - timedelta(days=1)).isoformat(), 100.0),
    ])
    verdict = classify_death({"Ethereum": series})
    assert verdict.status == "too_early"


def test_classify_death_recently_dipped() -> None:
    # peak long ago, recent 90d mean still healthy → not death yet (one-day spike to zero doesn't count)
    today = datetime.now(UTC).date()
    series = _series([
        ((today - timedelta(days=400)).isoformat(), 10_000_000.0),
        *[
            ((today - timedelta(days=i)).isoformat(), 8_000_000.0)
            for i in range(89, 1, -1)
        ],
        ((today - timedelta(days=1)).isoformat(), 0.0),  # one-day blip
    ])
    verdict = classify_death({"Ethereum": series})
    assert verdict.status == "alive"  # current_tvl_pct fails first; either status acceptable as long as != "dead"


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
        + [
            ((today - timedelta(days=i)).isoformat(), 58_000.0)
            for i in range(89, 0, -1)
        ]
        + [(today_iso, 58_500.0), (today_iso, 58_168.0)]  # two points stamped to today
    )
    verdict = classify_death({"Ethereum": series})
    assert verdict.status == "dead"
    assert verdict.current_tvl == 58_168.0  # last-write-wins, not the 116,668 sum


# ---- wayback_snapshot_near ---------------------------------------------------


async def test_wayback_picks_closest_200(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
        ["xyz,primitive)/", "20220301000000", "https://primitive.xyz/", "text/html", "200", "X", "1"],
        ["xyz,primitive)/", "20220525000000", "https://primitive.xyz/", "text/html", "200", "Y", "1"],
        ["xyz,primitive)/", "20220601000000", "https://primitive.xyz/", "text/html", "404", "Z", "1"],
    ]
    fake = AsyncMock(return_value=_FakeResp(rows))
    monkeypatch.setattr("slopmortem.corpus.sources.defillama.safe_get", fake)

    target = datetime(2022, 5, 30, tzinfo=UTC).date()
    result = await wayback_snapshot_near("https://primitive.xyz/", target)
    assert result is not None
    assert "20220525000000" in result
    assert "primitive.xyz" in result


async def test_wayback_returns_none_when_no_200(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
        ["xyz,primitive)/", "20220301000000", "https://primitive.xyz/", "text/html", "404", "X", "1"],
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
        ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
        ["example,dead)/", "20220525120000", "https://dead.example/", "text/html", "200", "X", "1"],
    ]

    async def fake_get(url: str, **_: object) -> _FakeResp:
        if url.endswith("/protocols"):
            return _FakeResp(bulk)
        if "/protocol/dead-protocol" in url:
            return _FakeResp(detail)
        if "web.archive.org/cdx" in url:
            return _FakeResp(cdx_rows)
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("slopmortem.corpus.sources.defillama.safe_get", AsyncMock(side_effect=fake_get))
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
    assert e.markdown_text is not None
    assert "Dead Protocol" in e.markdown_text
    assert "Options" in e.markdown_text


async def test_skips_when_no_wayback_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    bulk = [{"id": "1", "name": "Dead", "slug": "dead", "url": "https://dead.example", "tvl": 0.0}]
    detail = {
        "name": "Dead",
        "url": "https://dead.example",
        "chainTvls": {
            "Ethereum": _series(
                [("2022-01-01", 5_000_000.0), ("2025-01-01", 0.0)],
            ),
        },
    }
    cdx_rows = [["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]]

    async def fake_get(url: str, **_: object) -> _FakeResp:
        if url.endswith("/protocols"):
            return _FakeResp(bulk)
        if "/protocol/" in url:
            return _FakeResp(detail)
        return _FakeResp(cdx_rows)

    monkeypatch.setattr("slopmortem.corpus.sources.defillama.safe_get", AsyncMock(side_effect=fake_get))
    _setup_throttle(monkeypatch)

    src = DefiLlamaSource(shortlist_tvl_ceiling_usd=100_000.0)
    entries = [e async for e in src.fetch()]
    assert entries == []


async def test_max_emit_caps_yield(monkeypatch: pytest.MonkeyPatch) -> None:
    bulk = [
        {"id": str(i), "name": f"D{i}", "slug": f"d{i}", "url": f"https://d{i}.example", "tvl": 0.0}
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
        return _FakeResp([
            ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
            ["example,d)/", "20220601000000", "https://d.example/", "text/html", "200", "X", "1"],
        ])

    monkeypatch.setattr("slopmortem.corpus.sources.defillama.safe_get", AsyncMock(side_effect=fake_get))
    _setup_throttle(monkeypatch)

    src = DefiLlamaSource(shortlist_tvl_ceiling_usd=100_000.0, max_emit=3)
    entries = [e async for e in src.fetch()]
    assert len(entries) == 3


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
```

- [ ] **Step 1.4: Run tests, confirm they fail**

Run: `uv run pytest tests/sources/test_defillama.py -v`
Expected: `ImportError` or `AttributeError` for `DefiLlamaSource`.

- [ ] **Step 1.5: Implement the source**

Create `slopmortem/corpus/sources/defillama.py`. The module exposes one class (`DefiLlamaSource`) and two helpers (`classify_death`, `wayback_snapshot_near`) so each piece is unit-testable in isolation.

```python
"""DefiLlama source: peak-relative dead-protocol detection with Wayback-anchored URLs.

Pipeline:
  1. GET /protocols (bulk).
  2. Shortlist by current `tvl < shortlist_tvl_ceiling_usd` ($100K default).
  3. For each candidate, GET /protocol/{slug}; run `classify_death` on `chainTvls`.
  4. For "dead" verdicts, resolve a peak-era Wayback snapshot via CDX.
  5. Emit `RawEntry` whose `url` is the Wayback snapshot — `TavilyEnricher`
     downstream extracts the original (live-era) pitch body.

Why peak-relative instead of a raw TVL floor: zombies sit well above zero.
Validated reference: Primitive Finance peaked at $1.72M (2022-05-31) and
sits at ~$58K today (3.4% of peak). A raw $10K or $50K floor misses it; a
5%-of-peak rule catches it cleanly.

`max_emit` (default 300) bounds per-run cost: each entry triggers one Haiku
slop call (~$0.0008) plus one Tavily extract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal, cast

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

# Death-classifier defaults. All tunable via `DefiLlamaSource` constructor kwargs.
DEFAULT_DEAD_THRESHOLD_PCT: Final = 0.05
DEFAULT_PEAK_FLOOR_USD: Final = 1_000_000.0
DEFAULT_MIN_DAYS_SINCE_PEAK: Final = 180
DEFAULT_SHORTLIST_TVL_CEILING_USD: Final = 100_000.0
DEFAULT_MAX_EMIT: Final = 300

# Wayback CDX search windows around the peak date (in days).
WAYBACK_WINDOW_NARROW_DAYS: Final = 30
WAYBACK_WINDOW_WIDE_DAYS: Final = 180


DeathStatus = Literal["dead", "alive", "never_launched", "too_early", "unknown"]


@dataclass(frozen=True, slots=True)
class DeathVerdict:
    status: DeathStatus
    peak_tvl: float | None = None
    peak_date: date | None = None
    current_tvl: float | None = None


def _merge_chain_series(chain_tvls: dict[str, dict[str, Any]]) -> list[tuple[date, float]]:  # pyright: ignore[reportExplicitAny]
    """Sum daily totalLiquidityUSD across chains; return sorted (date, tvl) pairs.

    Live `chainTvls[<chain>]` is a dict (`{"tvl": [...], "tokensInUsd": [...], "tokens": [...]}`),
    not a bare list — the daily series lives at `["tvl"]`. Within a chain, dedupe
    on date with last-write-wins: DefiLlama can ship multiple intra-day snapshots
    for "today", and naive summing inflates the current value (e.g. Primitive's
    2026-05-06 shipped $58,678 and $58,168 on the same date).
    """
    totals: dict[date, float] = {}
    for chain_payload in chain_tvls.values():
        if not isinstance(chain_payload, dict):
            continue
        series_obj: object = chain_payload.get("tvl")
        if not isinstance(series_obj, list):
            continue
        per_day: dict[date, float] = {}
        for point in series_obj:
            if not isinstance(point, dict):
                continue
            ts: object = point.get("date")
            tvl: object = point.get("totalLiquidityUSD")
            if not isinstance(ts, (int, float)) or not isinstance(tvl, (int, float)):
                continue
            d = datetime.fromtimestamp(float(ts), tz=UTC).date()
            per_day[d] = float(tvl)  # last-write-wins on duplicate timestamps
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
    """Decide whether a protocol's TVL trajectory indicates death.

    Returns ``DeathVerdict("dead", ...)`` only when:
      - peak TVL >= ``peak_floor_usd`` (filters never-launched / dust);
      - days since peak >= ``min_days_since_peak`` (avoids premature calls);
      - current TVL <= ``threshold_pct`` * peak;
      - mean of the trailing 90 days <= ``threshold_pct`` * peak (sustained, not a dip).

    Other statuses: ``alive``, ``never_launched``, ``too_early``, ``unknown``.
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

    if current_tvl / peak_tvl > threshold_pct:
        return DeathVerdict("alive", peak_tvl, peak_date, current_tvl)
    if last_90_mean / peak_tvl > threshold_pct:
        return DeathVerdict("alive", peak_tvl, peak_date, current_tvl)

    return DeathVerdict("dead", peak_tvl, peak_date, current_tvl)


async def wayback_snapshot_near(
    url: str,
    target_date: date,
    *,
    user_agent: str = USER_AGENT,
) -> str | None:
    """Resolve ``url`` to a Wayback snapshot URL near ``target_date`` (status 200 only).

    Tries a narrow window first (``±30 days``), widens to ``±180 days`` on miss,
    returns ``None`` if no 200-status snapshot exists in either window.
    """
    for window_days in (WAYBACK_WINDOW_NARROW_DAYS, WAYBACK_WINDOW_WIDE_DAYS):
        from_d = (target_date - timedelta(days=window_days)).strftime("%Y%m%d")
        to_d = (target_date + timedelta(days=window_days)).strftime("%Y%m%d")
        cdx_url = (
            f"{WAYBACK_CDX_ENDPOINT}?url={url}&from={from_d}&to={to_d}"
            f"&output=json&limit=50"
        )
        if not await respect_robots(cdx_url, user_agent=user_agent):
            logger.info("defillama: robots blocked %s", cdx_url)
            return None
        await throttle_for(cdx_url, rps=2.0)
        resp = await safe_get(cdx_url)
        if resp.status_code >= HTTP_BAD_REQUEST:
            logger.warning("defillama: wayback HTTP %s for %s", resp.status_code, url)
            continue
        payload: object = resp.json()
        if not isinstance(payload, list) or len(payload) < 2:  # noqa: PLR2004 - header + at least one row
            continue
        rows = cast("list[object]", payload)
        candidates: list[tuple[date, str, str]] = []
        for raw in rows[1:]:  # skip header
            if not isinstance(raw, list) or len(raw) < 5:  # noqa: PLR2004 - need statuscode index
                continue
            row = cast("list[object]", raw)
            ts = row[1]
            original = row[2]
            statuscode = row[4]
            if not isinstance(ts, str) or not isinstance(original, str) or statuscode != "200":
                continue
            try:
                snap_date = datetime.strptime(ts[:8], "%Y%m%d").replace(tzinfo=UTC).date()
            except ValueError:
                continue
            candidates.append((snap_date, ts, original))
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

    def __init__(
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
        return resp.json()

    async def _process_candidate(self, row: dict[str, Any]) -> RawEntry | None:  # pyright: ignore[reportExplicitAny]
        slug: object = row.get("slug")
        live_url: object = row.get("url")
        if not isinstance(slug, str) or not slug:
            return None
        if not isinstance(live_url, str) or not live_url:
            logger.info("defillama: %s missing url, skipping", slug)
            return None

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
```

- [ ] **Step 1.6: Run tests**

Run: `uv run pytest tests/sources/test_defillama.py -v -k "not round_trip"`
Expected: 11 PASS — six `classify_death` cases (`dead_zombie`, `alive`, `never_launched`, `too_early`, `recently_dipped`, `dedupes_intra_day_duplicates`), two `wayback` cases (`picks_closest_200`, `returns_none_when_no_200`), three `DefiLlamaSource.fetch` cases (`emits_dead_protocol_with_wayback_url`, `skips_when_no_wayback_coverage`, `max_emit_caps_yield`). The `defillama_round_trip` cassette test is excluded by `-k`.

If the `@pytest.mark.vcr` round-trip case picks up an unexpected `vcr_config` collection error, mirror the fixture pattern used by an existing cassette test (e.g. one of `tests/sources/test_*` already on cassettes) — `pytest-recording`'s discovery is project-config dependent.

- [ ] **Step 1.7: Lint + typecheck**

Run: `just lint && just typecheck`
Expected: clean.

- [ ] **Step 1.8: Commit**

```
git add slopmortem/corpus/sources/defillama.py slopmortem/corpus/sources/_names.py tests/sources/test_defillama.py
git commit -m "feat(sources): add defillama low-tvl source"
```

---

## Task 2: CLI wiring, reliability rank, exports

**Why:** The source exists but nothing instantiates it. Add the opt-in CLI flag, register the reliability rank, re-export from the package `__init__`, register in `_SOURCE_REGISTRY` for `--only-source` support, and add regression tests.

**Files:**
- Modify: `slopmortem/corpus/sources/__init__.py`
- Modify: `slopmortem/cli/_ingest_cmd.py`
- Modify: `slopmortem/ingest/_helpers.py`
- Modify: `tests/ingest/test_reliability_rank.py`
- Modify: `tests/test_cli_ingest.py` (add regression for the missing-key guard)

- [ ] **Step 2.1: Re-export the new class**

Edit `slopmortem/corpus/sources/__init__.py`. Add the import alphabetically (between `CuratedSource` and `HNAlgoliaSource`):

```python
from slopmortem.corpus.sources.defillama import DefiLlamaSource as DefiLlamaSource
```

Add to `__all__` (alphabetical placement):

```python
"DefiLlamaSource",
```

- [ ] **Step 2.2: Verify the import wiring**

Run: `uv run python -c "from slopmortem.corpus.sources import DefiLlamaSource; print('ok')"`
Expected: `ok`.

- [ ] **Step 2.3: Extend `_RELIABILITY_RANK`**

Edit `slopmortem/ingest/_helpers.py`. Add `SOURCE_DEFILLAMA` to the existing `_names` import block (alphabetical placement keeps the diff small) so it reads:

```python
from slopmortem.corpus.sources._names import (
    SOURCE_CRUNCHBASE_CSV,
    SOURCE_CURATED,
    SOURCE_DEFILLAMA,
    SOURCE_HN_ALGOLIA,
    SOURCE_TAVILY_NEWS,
)
```

Add **one row** to `_RELIABILITY_RANK` (do not rewrite the whole dict — `SOURCE_TAVILY_NEWS: 4` must stay). The result:

```python
_RELIABILITY_RANK: Final[dict[str, int]] = {
    SOURCE_CURATED: 0,
    SOURCE_HN_ALGOLIA: 1,
    SOURCE_CRUNCHBASE_CSV: 2,
    SOURCE_DEFILLAMA: 3,
    SOURCE_TAVILY_NEWS: 4,
}
```

- [ ] **Step 2.4: Extend the regression test**

`tests/ingest/test_reliability_rank.py` already exists and already imports
`SOURCE_TAVILY_NEWS` plus parametrizes it at rank 4. Extend it:

1. Add `SOURCE_DEFILLAMA` to the existing `from slopmortem.corpus.sources._names import (...)` block (alphabetical placement: between `SOURCE_CURATED` and `SOURCE_HN_ALGOLIA`).

2. Insert one parametrize row between the `SOURCE_CRUNCHBASE_CSV` and
`SOURCE_TAVILY_NEWS` rows:

```python
(SOURCE_DEFILLAMA, 3),
```

The existing `test_unknown_source_lands_at_dead_letter_rank` already covers
the default-fallback case — do not duplicate it.

- [ ] **Step 2.5: Run reliability test**

Run: `uv run pytest tests/ingest/test_reliability_rank.py -v`
Expected: 6 PASS (5 parametrized cases including the existing
`SOURCE_TAVILY_NEWS` row + the existing
`test_unknown_source_lands_at_dead_letter_rank`).

- [ ] **Step 2.6: Add CLI flag + wiring (with Tavily implication and key assertion)**

The flag touches seven landmarks in `slopmortem/cli/_ingest_cmd.py`. Skipping
any leaves the flag silently ignored, raising `NameError`, or — worst case —
emitting 300 empty entries because Tavily silently no-ops on missing key.
Line numbers drift; the landmarks below are anchored to existing
identifiers.

> **Pattern note.** `--enable-tavily-news` already does a "fail if
> `TAVILY_API_KEY` unset" guard a few lines above where this plan adds its
> own. Keep them as **two separate guards** — they're symmetric in shape but
> not equivalent in semantics. `--enable-defillama` *additionally* forces
> `tavily_enrich = True` because Wayback HTML is unreadable without the
> Tavily extractor, whereas the news source returns bodies inline and
> doesn't imply the enricher. Don't try to fold them into one block.

**1. Update the `from slopmortem.corpus.sources import (...)` block** to add
`DefiLlamaSource` (alphabetical). After this edit the block contains
`CrunchbaseCsvSource, CuratedSource, DefiLlamaSource, HNAlgoliaSource,
TavilyEnricher, TavilyNewsSource, WaybackEnricher`. Do **not** drop
`TavilyNewsSource`:

```python
from slopmortem.corpus.sources import (
    CrunchbaseCsvSource,
    CuratedSource,
    DefiLlamaSource,
    HNAlgoliaSource,
    TavilyEnricher,
    TavilyNewsSource,
    WaybackEnricher,
)
```

**2. Add the typer Option to `ingest_cmd`.** Insert between the existing
`tavily_enrich` flag and `enable_tavily_news`:

```python
    enable_defillama: Annotated[
        bool,
        typer.Option(
            "--enable-defillama",
            help=(
                "Enable the DefiLlama dead-protocol source. "
                "Implies --tavily-enrich; requires TAVILY_API_KEY."
            ),
        ),
    ] = False,
```

**3. Forward it through `functools.partial`.** Append one kwarg after
`tavily_enrich=tavily_enrich,` (and before `enable_tavily_news=...`):

```python
            tavily_enrich=tavily_enrich,
            enable_defillama=enable_defillama,
            enable_tavily_news=enable_tavily_news,
```

**4. Add the parameter to `_run_ingest`.** Insert one `bool` kwarg after
`tavily_enrich: bool,` and before `enable_tavily_news: bool,`:

```python
    enable_defillama: bool,
```

**5. Register in `_SOURCE_REGISTRY` for `--only-source` support.** At the
module-level `_SOURCE_REGISTRY` dict, add a row (alphabetical placement
keeps it adjacent to `crunchbase_csv`):

```python
_SOURCE_REGISTRY: dict[str, _SourceSpec] = {
    "curated": _SourceSpec(source_class=CuratedSource),
    "hn_algolia": _SourceSpec(source_class=HNAlgoliaSource),
    "crunchbase_csv": _SourceSpec(source_class=CrunchbaseCsvSource),
    "defillama": _SourceSpec(source_class=DefiLlamaSource),
    "tavily_news": _SourceSpec(source_class=TavilyNewsSource),
}
```

Inside the existing `if only_source is not None:` block in `_run_ingest`,
add a branch alongside the `TavilyNewsSource` one (the inline comment there
already says "Add one when introducing a new opt-in source" — that's this):

```python
        if spec.source_class is DefiLlamaSource:
            enable_defillama = True
```

Update the `--only-source` typer Option `help=` string to include
`defillama` in its valid-source list:

```
"Accepts source identifiers (curated, hn_algolia, crunchbase_csv, defillama, tavily_news)."
```

While you're there, update the `--limit` help string to reflect the new
chain order: `curated -> HN -> Crunchbase -> DefiLlama -> TavilyNews`. Both
strings drift silently otherwise.

**6. Imply Tavily and assert the key.** Inside `_run_ingest`, immediately
*after* the existing `if enable_tavily_news and not os.environ.get(...)`
guard and *before* the `_build_ingest_deps(...)` call, add:

```python
    if enable_defillama:
        if not os.environ.get("TAVILY_API_KEY"):
            msg = (
                "--enable-defillama requires TAVILY_API_KEY: the DefiLlama source "
                "anchors entries to Wayback snapshots whose bodies are extracted "
                "by the Tavily enricher. Set TAVILY_API_KEY in .env or unset "
                "--enable-defillama."
            )
            raise typer.BadParameter(msg)
        # Implication: TavilyEnricher must run for DefiLlama entries to have a body.
        tavily_enrich = True
```

`os` is already imported at the top of `_ingest_cmd.py` — no new import.

**7. Wire the new source.** After the existing `if crunchbase_csv is not None:`
block and before the `if enable_tavily_news:` block, add:

```python
    if enable_defillama:
        sources.append(DefiLlamaSource())
```

This keeps the in-list order `Curated, HN, Crunchbase?, DefiLlama?, TavilyNews?`,
matching the reliability rank ordering.

**8. Add a regression test for the assertion.** Append to
`tests/test_cli_ingest.py` (alongside the existing
`test_enable_tavily_news_without_api_key_fails`):

```python
def test_enable_defillama_without_tavily_key_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr("slopmortem.cli._ingest_cmd._build_ingest_deps", _fake_deps)
    runner = CliRunner()
    result = runner.invoke(app, ["ingest", "--enable-defillama", "--dry-run"])
    assert result.exit_code != 0, result.output
    combined = result.output + (result.stderr or "")
    assert "TAVILY_API_KEY" in combined
```

- [ ] **Step 2.7: Smoke-test the CLI flag**

Run: `uv run slopmortem ingest --help | grep enable-defillama`
Expected: one line describing the flag.

- [ ] **Step 2.8: Dry-run with the flag on**

`_gather_entries` runs sources sequentially in list order
(`Curated, HN, Crunchbase?, DefiLlama?, TavilyNews?`) and breaks the moment
the limit fills, so a small `--limit` will never reach DefiLlama. Use a
high limit and inspect the per-source log line. Do **not** also pass
`--enable-tavily-news` for this smoke unless you want the news source to
spend Tavily credits — DefiLlama's own Tavily extracts will already
exercise the key:

```
uv run slopmortem ingest \
  --dry-run \
  --enable-defillama \
  --limit 500 \
  2>&1 | tee /tmp/slopmortem_smoke.log | tail -40
```

Expected:
- Dry-run completes with no error.
- `/tmp/slopmortem_smoke.log` contains a `defillama: ...` log line (either a
  successful parse or the `max_emit=300 reached, stopping` cap message).
- `seen` and `would_process` in the result table are non-zero.

Verify per-source coverage:
```
grep '^defillama' /tmp/slopmortem_smoke.log
```
Expected: at least one matching line. If the line shows `HTTP 4xx/5xx`, the
endpoint changed — capture the failure URL and either fix the endpoint or
flag it before continuing.

- [ ] **Step 2.9: Lint + typecheck + full test suite**

Run: `just lint && just typecheck && just test`
Expected: all clean.

- [ ] **Step 2.10: Commit**

```
git add slopmortem/corpus/sources/__init__.py slopmortem/cli/_ingest_cmd.py slopmortem/ingest/_helpers.py tests/ingest/test_reliability_rank.py tests/test_cli_ingest.py
git commit -m "wiring: opt-in flag for defillama source"
```

---

## Optional: Record live cassette (skip in CI)

After Task 1 lands, the round-trip cassette test is skipped by default. To capture it:

- [ ] **Step C.1: Record the cassette**

```
RECORD=1 uv run pytest tests/sources/test_defillama.py::test_defillama_round_trip -v
```
Expected: writes `tests/sources/cassettes/test_defillama/test_defillama_round_trip.yaml`.

- [ ] **Step C.2: Re-run without RECORD to confirm replay**

Run: `uv run pytest tests/sources/test_defillama.py -v`
Expected: all four tests PASS (replay from cassette).

- [ ] **Step C.3: Commit the cassette**

```
git add tests/sources/cassettes/test_defillama/
git commit -m "test: record cassette for defillama source"
```

Note: the cassette captures the live URL response. Inspect the YAML before commit; if it caught a transient 5xx, re-record. The project's `docs/cassettes.md` covers cassette hygiene if anything is unclear.

---

## Out of scope

- **Per-source budget caps.** Adding spend limits or rate caps per adapter requires touching `slopmortem/budget.py`. Defer until a real overrun lands. The `max_emit` cap in this source is a yield-count proxy, not a true budget.
- **Pivot detection.** Some protocols don't die outright — they pivot (Opyn v1 → Squeeth) or merge (Ribbon → Aevo). `classify_death` will mark these `alive` because TVL legitimately moved or stayed under a new product wrapping the same contracts. Surfacing these as "the original product is dead" requires either an LLM check on Wayback homepage diffs or a `curated.py` override list. Out of scope here; defer to either a curated override or a future plan that adds a homepage-diff classifier.
- **Death-classifier tunables surfaced as CLI flags.** `dead_threshold_pct`, `peak_floor_usd`, `min_days_since_peak`, and `shortlist_tvl_ceiling_usd` are constructor kwargs only. CLI exposure can wait until an operator actually wants to tune them at runtime.
- **RSS-based sources** (rekt.news, web3isgoinggreat.com, TechCrunch tag, Crunchbase News tag). Deferred indefinitely: RSS exposes only a publisher-controlled tail window with no pagination, so one-shot yield is tiny and historical reach is zero. Worth revisiting only if/when scheduled ingest lands and the project decides to accrete recent items over months.
- **Re-recording the eval cassettes.** The new source expands the corpus, but the eval runner is gated by curated post-mortems plus the existing two sources (per `docs/cassettes.md`). Eval cassettes don't need re-recording for this change. Verify with `just eval` after Task 2 — if it diverges, that's a separate plan.
- **Updating `docs/architecture.md`.** Add a one-line note pointing to this plan once it lands; full re-write isn't warranted.
