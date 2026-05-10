# Recall Verifier Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or `/team-feature` to implement this plan task-by-task, per the Execution Strategy below. Steps use checkbox (`- [ ]`) syntax — these are **persistent durable state**, not visual decoration. The executor edits the plan file in place: `- [ ]` → `- [x]` the instant a step verifies, before moving on. On resume (new session, crash, takeover), the executor scans existing `- [x]` marks and skips them — these steps are NOT redone. TodoWrite mirrors this state in-session; the plan file is the source of truth across sessions.

**Goal:** Tighten the LLM-recall verifier (`slopmortem/stages/recall_verify.py`) so it stops dropping the population it exists to surface (vanished homepages corroborated by Wayback) and stops admitting the population it exists to reject (HTML-noise matches, paywalled stubs, hallucinated domains).

**Architecture:** The verifier keeps five layers but their responsibilities sharpen. L2 stops gating on the homepage URL entirely — the homepage is provenance, not a citation, so its liveness is not load-bearing — and falls through HEAD→GET on the evidence URL since many real news sites (paywalls, CDNs, anti-bot) return 401/403/405 on HEAD even when GET works. L3 strips HTML through the existing `extract_clean` helper, applies word-boundary regex, expands the death/distress vocabulary, and short-circuits on bodies under 500 characters. L4 collapses to body composition: `WaybackEnricher.enrich()` is called directly (no outer retry — the enricher already retries 3× internally with exponential backoff and swallows transient errors by returning the entry unchanged). The persisted body always contains the news article (the citation L5 verifies against); when Wayback anchors, its snapshot prepends the news section under a "Vendor description (archived)" marker so downstream synthesis can read both the value-prop and the death narrative from a single Qdrant entry. Wayback is purely advisory — neither its absence nor its failure can drop a candidate, since archive.org coverage is patchy enough that "no snapshot" doesn't reliably indicate hallucination. The hallucination guard concentrates in L5: it reads the news article body even when L4 anchored on a Wayback snapshot, and returns a tri-state verdict (`dead`/`struggling`/`alive`) with a separate confidence threshold for the struggling tier and a verbatim `evidence_quote` requirement that catches names appearing only in unrelated contexts. Persistence emits a span event when the existing three-tier resolver merges into a pre-existing entity. Persisted recall entries flow through the standard chunker (no single-chunk path) — each chunk inherits the same payload so synthesis still sees the full combined body, and per-chunk vectors give the marketing-copy section a focused signal that matches pitches better than a diluted whole-body vector would.

**Tech Stack:** Python 3.13, anyio (no bare asyncio), httpx (existing `safe_get`/`safe_head`), pydantic v2 (strict, no `Any` leaks), trafilatura (existing via `corpus._extract`), basedpyright strict, pytest with `asyncio_mode="auto"` + `pytest-xdist`.

## Execution Strategy

**Subagents** — Task 0 dispatches first as a gating pre-flight: it answers "does the body-construction strategy actually let recall entries reach synthesis?" If Task 0 fails, the body-construction is the MVP bottleneck and Tasks 1–6 are wasted effort until that's fixed; pause and rethink rather than ship verifier hardening that won't move the outcome. Task 1 dispatches once Task 0 passes. Tasks 2, 3, and 4 all modify `slopmortem/stages/recall_verify.py` and run sequentially after Task 1 lands. The two-stage review gate (spec compliance, then code quality) applies per task.

## Task Dependency Graph

- Task 0 [AFK]: Retrieval-survival pre-flight test (prove a recall entry actually lands in `top_n` after persist) → depends on `none` → batch 0
- Task 1 [AFK]: L3 hygiene (HTML strip, word boundaries, expanded keywords, body-length gate) → depends on `Task 0` → batch 1
- Task 2 [AFK]: L2/L4 restructure (drop homepage HEAD gate, HEAD→GET fallback on evidence URL, direct Wayback call without outer retry, body selection) → depends on `Task 1` → batch 2
- Task 3 [AFK]: L5 tri-state schema + combined persisted body + prompt update (cassette re-record deferred to Task 6) → depends on `Task 2` → batch 3
- Task 4 [AFK]: Dedup telemetry span event from the resolver → depends on `Task 3` → batch 4
- Task 5 [AFK]: Recall flow gap closures (post-recall gap measurement, skip slop on recall) → depends on `Task 4` → batch 5
- Task 6 [AFK]: Synthesis read-side wiring for tri-state verdict + unified cassette re-record (covers Task 3's `recall_deathness` SHA AND Task 6's `synthesize` SHA in one `eval-record` run) → depends on `Task 5` → batch 6
- Polish: post-implementation-polish → depends on `Tasks 1-6` → batch 7

## Agent Assignments

- Task 0: Retrieval-survival pre-flight → python-development:python-pro
- Task 1: L3 hygiene → python-development:python-pro
- Task 2: L2/L4 restructure → python-development:python-pro
- Task 3: L5 tri-state + combined body → python-development:python-pro
- Task 4: dedup telemetry → python-development:python-pro
- Task 5: Recall flow gap closures → python-development:python-pro
- Task 6: Synthesis read-side wiring + cassette re-record → python-development:python-pro
- Polish: post-implementation-polish → general-purpose

---

## File Structure

**New:**

- `tests/stages/test_recall_retrieval_survival.py` — Task 0 test: persist one verified recall entry against an in-memory corpus, run `retrieve` + `llm_rerank` (real embed + sparse, stubbed rerank for determinism) with the original pitch, assert the recall entry's `canonical_id` lands in `top_K` AND its rerank perspective-score mean ≥ `min_similarity_score`. Gates the whole plan.
- `tests/stages/test_recall_verify_l3_hygiene.py` — Task 1 tests: HTML strip, word-boundary regex, body-length sanity, expanded keyword admits, kept `acquired`/`acquisition`.
- `tests/stages/test_recall_verify_l2_l4_bprime.py` — Task 2 tests: dead homepage admits when Wayback anchors; Wayback empty admits at evidence_only tier; Wayback transport failure retries once then admits at rate-limited tier; evidence_url HEAD still gates.
- `tests/stages/test_recall_verify_l5_tristate.py` — Task 3 tests: dead admits at any confidence ≥ min; struggling admits only above struggling_min; alive drops; news body fed to L5 when Wayback anchored; persisted body still Wayback when anchored.
- `tests/stages/test_recall_persist_dedup_event.py` — Task 4 tests: span event emitted when resolver returns `alias_blocked` or `resolver_flipped`; not emitted on `inserted`.
- `tests/stages/test_recall_persist_gap_closures.py` — Task 5 tests: slop classifier is not invoked on recall entries; `RECALL_GAP_SCORE_AFTER` emits with the post-recall qualifying count.
- `tests/fixtures/recall/sidebar_bleed.html` — Task 1 fixture: HTML page where vendor name is in `<aside class="trending">` and the death keyword is in `<nav>`. Trafilatura extraction must produce a body that fails L3.
- `tests/fixtures/recall/paywall_stub.html` — Task 1 fixture: HTML page that extracts to under 100 chars (`<main><p>Sign in to read more</p></main>`).
- `tests/fixtures/cassettes/recall/recall_deathness_tristate.yaml` — Task 3 cassette: re-recorded after the prompt update.

**Modified:**

- `slopmortem/stages/recall_verify.py` — Tasks 1, 2, 3, 4. The bulk of the change. Order matters because each task builds on the prior one's signature changes.
- `slopmortem/llm/prompts/recall_deathness.j2` — Task 3. Tri-state output schema; instruction sharpened to distinguish `dead` from `struggling` from `alive`.
- `slopmortem/tracing/events.py` — Tasks 1, 3, 4, 5. New SpanEvent members: `RECALL_REJECTED_L3_BODY_TOO_SHORT`, `RECALL_DEDUPED_EXISTING`, `RECALL_REJECTED_L5_ALIVE`, `RECALL_GAP_SCORE_AFTER`. Closed StrEnum per the project convention. **Remove** `RECALL_REJECTED_L5_NOT_DEAD` (orphaned by Task 3's tri-state). Task 2 deliberately adds no SpanEvent — see Step 2.1.
- `slopmortem/config.py` — Task 3. Add `recall_struggling_min_confidence: float = 0.85` (separate, stricter than the existing `recall_deathness_min_confidence` for `dead` verdicts).
- `slopmortem/pipeline.py` — Tasks 3, 5. Task 3 threads `struggling_min_confidence=config.recall_struggling_min_confidence` through the call site at `pipeline.py:221` (`verify_and_persist_all`). Task 5 adds a second `compute_coverage_gap` call at the tail of `_run_recall_branch` (after re-rerank) and emits `RECALL_GAP_SCORE_AFTER` with the post-recall qualifying count, so prod traces can join before/after per query.
- `slopmortem/stages/recall_persist.py` — Task 5. Two changes: (a) skip the chunker for recall entries and persist as a single Qdrant point — typical combined body (news ~1-2k tok + Wayback ~0.5-2k tok) fits well under nomic-embed's 8192-tok limit, and chunking would split the news section from the Wayback section, defeating the Task 3 combine; (b) skip the slop classifier for recall entries — L5 is the stricter gate operating on the death-citation substrate, and slop was tuned on a different body shape, so running it on the combined body risks false-quarantining L5-verified entries.
- `slopmortem/models.py` — Task 3. Replace `_DeathnessJudgment.died: bool` with `verdict: Literal["dead", "struggling", "alive"]`; keep `confidence` and `evidence_quote`. (`_DeathnessJudgment` is a private model in `recall_verify.py` today — promote to `models.py` if cassette tooling needs to reference it; otherwise keep private and just edit the field.)
- `slopmortem/ingest/_journal_writes.py` — Task 4. Wire the new `RECALL_DEDUPED_EXISTING` span event when `resolve_entity` returns an `alias_blocked` action AND the row's `source == SOURCE_LLM_RECALL`. (`resolver_flipped` already emits `RESOLVER_FLIP_DETECTED` via `res.span_events`; double-emitting on the same condition would just clutter the trace.) Add `from slopmortem.corpus.sources._names import SOURCE_LLM_RECALL` to the imports. Keep entirely passive — emission only, no behavior change.
- `tests/stages/test_recall_verify.py` — Tasks 1 and 3. Existing file imports `_DEATH_KEYWORDS` (now a `tuple`, membership check still works) and constructs `_DEATHNESS_PASS = '{"died": true, ...}'` literals (line 27) plus per-test variants — every L5 test must switch to `verdict` schema. Update sequenced replies and threshold knobs to take `struggling_min_confidence`.
- `tests/stages/test_recall_persist.py` — Task 4. Update for the new `RECALL_DEDUPED_EXISTING` emission on `alias_blocked` recall rows.

---

## Pre-flight (read before starting any task)

Project conventions that bite if missed:

- `uv` for everything. `just install`, `just test`, `just lint`, `just typecheck`. Don't invoke `pip` or `python -m venv`.
- Strict basedpyright with `reportAny="error"`. No `# type: ignore` to silence. Use `cast` with a one-line comment if a third-party stub is missing.
- Pydantic v2 only. `BaseModel`, `model_validator(mode="after")`. `StrEnum` or `Literal[*_TAXONOMY_VALUES]` for closed sets.
- Anyio, not bare asyncio. `anyio.to_thread.run_sync` for blocking calls (sqlite, DNS). `gather_resilient` at fan-out points.
- Fakes over mocks. `slopmortem/llm/fake.py`, `FakeSlopClassifier`, `InMemoryCorpus` in `ingest.py`. New tests must not import `unittest.mock`.
- Tests must be parallel-safe (`pytest-xdist`). Filesystem state lives in `tmp_path`. No `/tmp` direct writes.
- Cassettes in `tests/fixtures/cassettes/...`. If a cassette test raises `NoCannedResponseError`, a prompt or model changed — re-record the affected scope only, don't widen the matcher.
- New SpanEvents go in the `slopmortem/tracing/events.py` StrEnum. Free-form strings get rejected.
- `slopmortem.toml` is the documented default surface; if you add a config key, document it there. Personal overrides go in `slopmortem.local.toml` (gitignored).
- Each task is one commit, terse subject (`recall:`, `cli:`, `journal:`, etc.). No `Co-Authored-By` trailers.

---

## Task 0 — Retrieval-survival pre-flight test

**Goal:** Before any verifier work, prove the combined-body construction produces a *dense+sparse vector* that lands in `top_K_retrieve` against the pitch's vector. Recall fires at most once per query (`pipeline.py:152-259`); after persist, `retrieve` runs a second time inside `_run_recall_branch` and that's the only path back to synthesis. If the combined-body vector dilutes pitch-similarity below the retrieve cut, no amount of L1–L5 tightening fixes the MVP outcome. Gate the plan on this answer.

**Scope: dense-retrieval cut only — no rerank assertion.** Rerank operates on `summarize` output (which only exists after persist runs and does its own LLM call), and stubbing the rerank LLM makes any assertion tautological — if I tell the stub to admit the entry, the assertion that it was admitted is true by construction. Rerank survival is what `just eval`'s end-to-end cassettes exercise; Task 0 stays focused on the one thing only it can answer cheaply and deterministically: "does this body shape produce a vector that matches this pitch's vector."

**Why this is task 0, not a polish step:** Tasks 1–6 spend ~2 weeks of work plus a $2 cassette re-record on a verifier that gates whether candidates *enter* the corpus. None of that improves the MVP outcome if the candidates can't *exit* via the second retrieve. A failing Task 0 means the body-construction strategy (Task 3's combined body, Task 5's persistence) needs to change before the verifier hardening ships. A passing Task 0 means the rest of the plan is well-targeted.

**Pros/cons:**

- *Pro:* runs in seconds (modulo the one-time fastembed model download). No Qdrant container, no LLM calls, fully hermetic.
- *Pro:* answers the only design question the rest of the plan can't answer analytically: whether a real recall entry's combined-body vector is similar enough to a real pitch's vector to clear `top_K_retrieve`.
- *Pro:* re-runnable after every body-construction change in Tasks 3 and 5 as a regression guard.
- *Con:* requires picking one representative pitch + one known-good vendor pair. Cherry-picked, so a pass doesn't guarantee robustness across the recall population — but a fail is conclusive (the construction is wrong), and that's the asymmetry we want.
- *Con:* tests dense+sparse only, not rerank. A pass at retrieve + a fail at rerank would still ship a broken MVP. Mitigated because rerank operates on `summarize` output, which is exercised by `just eval` cassettes — different test surface, deliberate split.

**Files:**
- Create: `tests/stages/test_recall_retrieval_survival.py`
- Create: `tests/fixtures/recall/survival_pitch.txt` — one realistic pitch (~150 words), sector-tagged.
- Create: `tests/fixtures/recall/survival_vendor.json` — `{name, homepage_url, evidence_url, status, failure_year}` for a known-good vendor whose Wayback snapshot exists and whose news citation is real.
- Create: `tests/fixtures/recall/survival_news_body.html` — captured news article HTML so the test runs offline.
- Create: `tests/fixtures/recall/survival_wayback_body.txt` — captured Wayback markdown so the test runs offline.

### Steps

- [x] **Step 0.1: Capture the fixtures**

Pick one representative pitch + one vendor pair. The vendor must:
- Have a real Wayback snapshot of its homepage (verify via `https://archive.org/wayback/available?url=<homepage>`).
- Have a real news citation that establishes death (Chapter 11 filing, TechCrunch shutdown post, court filing).
- Be in a sector that overlaps the test pitch's sector or "other".

Save the fetched bodies under `tests/fixtures/recall/` so the test runs offline (no `requires_qdrant`, no live HTTP).

- [x] **Step 0.2: Write the survival test**

```python
# tests/stages/test_recall_retrieval_survival.py
"""Pre-flight: prove a persisted recall entry's vector survives top-K retrieve.

Gates the rest of the recall-verifier-hardening plan. If it fails, the
combined-body construction is the MVP bottleneck — Tasks 1-6 won't move
the outcome until the construction's vector matches pitch vectors well
enough to clear top_K_retrieve.

Scope: dense + sparse retrieval only. No rerank assertion — rerank
operates on `summarize` output (a separate LLM call inside the persist
tail), and stubbing the rerank LLM produces a tautological assertion.
End-to-end rerank behavior is exercised by `just eval` cassettes.

Runs offline: real fastembed (nomic-embed) + real sparse encoder. First
run downloads the ~550MB ONNX model — marked `slow` so default `just
test` doesn't trigger it.
"""
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from slopmortem.config import load_config
from slopmortem.corpus import extract_clean
from slopmortem.corpus.sources._names import SOURCE_LLM_RECALL
from slopmortem.ingest import IngestResult, NullProgress
# InMemoryCorpus must implement both read+write Protocols. If the project
# only has FakeCorpus today, extend it with the read-side hybrid_search
# method needed by retrieve(); document the addition in the test header.
from slopmortem.models import RawEntry
from slopmortem.stages import extract_facets, persist_recall_entry, retrieve

FIXTURES = Path(__file__).parent.parent / "fixtures/recall"


@pytest.mark.slow  # first run downloads ~550MB fastembed ONNX
async def test_recall_entry_lands_in_top_k_after_persist(tmp_path):
    pitch = (FIXTURES / "survival_pitch.txt").read_text()
    vendor = json.loads((FIXTURES / "survival_vendor.json").read_text())
    news_html = (FIXTURES / "survival_news_body.html").read_text()
    wayback_md = (FIXTURES / "survival_wayback_body.txt").read_text()

    config = load_config()
    corpus = InMemoryCorpus()  # read+write Protocols, see header note
    embed_client = ...  # real fastembed-backed client
    sparse_encoder = ...  # real sparse encoder
    journal = tmp_journal(tmp_path)

    # Build the combined body. Task 3 will introduce _combine_recall_body;
    # until then, mirror its shape inline. After Task 3 lands, swap this
    # block for a direct call to `_combine_recall_body(...)` so the test
    # tracks the production combiner (see Task 3 step list).
    combined_body = (
        f"# Vendor description (archived)\n\n{wayback_md}"
        f"\n\n---\n\n# Failure citation\n\n"
        f"Source: {vendor['evidence_url']}\n"
        f"Status (LLM-suggested): {vendor['status']} ({vendor['failure_year']})\n\n"
        f"{extract_clean(news_html)}"
    )
    entry = RawEntry(
        source=SOURCE_LLM_RECALL,
        source_id=f"survival:{vendor['name']}",
        url=vendor["homepage_url"],
        markdown_text=combined_body,
        raw_html=None,
        fetched_at=datetime.now(UTC),
    )

    # Persist through the standard tail. FakeSlopClassifier(score=0.0) keeps
    # the entry; the facet/summarize LLM stubs return canned valid JSON for
    # the post-Task-5 skip_slop=True path (slop call never fires; only the
    # facet+summarize LLM calls do).
    await persist_recall_entry(
        entry,
        tier="wayback_anchored",
        journal=journal,
        corpus=corpus,
        embed_client=embed_client,
        llm=FakeLLMClient([_canned_facet(), _canned_summary()]),
        slop_classifier=FakeSlopClassifier(score=0.0),
        sparse_encoder=sparse_encoder,
        config=config,
        post_mortems_root=tmp_path / "post_mortems",
        progress=NullProgress(),
        result=IngestResult(),
    )

    # Run retrieve against the pitch. REAL embed + REAL sparse, no LLM
    # stubbing on this path — `extract_facets` uses a stub for determinism
    # but it doesn't affect the dense+sparse signal we're asserting on.
    facets = await extract_facets(
        pitch,
        FakeLLMClient([_canned_facets_for_pitch()]),
        model=config.model_facet,
        max_tokens=config.max_tokens_facet,
    )
    retrieved = await retrieve(
        description=pitch,
        facets=facets,
        corpus=corpus,
        embedding_client=embed_client,
        cutoff_iso=None,
        strict_deaths=False,
        k_retrieve=config.K_retrieve,
        sparse_encoder=sparse_encoder,
        strict_sector_filter=False,
        strict_sector_filter_excludes_other=True,
    )

    # The only assertion: did the recall entry's vector clear top_K?
    canonical_ids = [c.canonical_id for c in retrieved]
    assert any(vendor["name"].lower() in c.payload.name.lower() for c in retrieved), (
        f"Recall entry vector not in top-{config.K_retrieve} after persist. "
        f"Combined-body construction is the MVP bottleneck — pause Tasks 1-6 "
        f"and revisit body shape (e.g. persist Wayback marketing copy alone, "
        f"keep news body only as payload metadata) before resuming."
    )
```

- [x] **Step 0.3: Run the test**

Run: `uv run pytest tests/stages/test_recall_retrieval_survival.py -v --runslow`
Expected: PASS. The pitch+vendor pair is cherry-picked to be a known-good case; if this fails on a known-good case, the construction is broken.

If the test FAILS:
- **Pause Tasks 1-6.** They won't fix the MVP outcome.
- The combined body's vector is too diluted vs the pitch. Likely fixes (pick one and re-run):
  - Persist Wayback marketing copy as `markdown_text`; keep news body only in a separate `payload` metadata field that synthesis can read but the embed call doesn't see.
  - Reverse the section order — news first, marketing second — and see if the vector shifts toward the news-narrative shape.
  - Drop the Wayback section entirely; persist news-body only and accept that synthesis sees less context.
- Pick one fix, re-run Task 0, *then* resume the rest of the plan with the corrected construction.

If the test PASSES:
- The body construction's dense+sparse signal is sound. Proceed to Task 1. End-to-end rerank+synthesis behavior will be validated by `just eval` after Task 6.

- [x] **Step 0.4: Commit**

```
recall: pre-flight retrieval-survival test (gates verifier hardening)
```

---

## Task 1 — L3 hygiene

**Goal:** Strip HTML before the L3 keyword scan, gate on minimum body length, replace substring matching with word-boundary regex, expand the death/distress vocabulary. Keep `acquired`/`acquisition` in the keyword set (Plan B reasoning: false-positive cost is one Haiku call; false-negative loses real fire-sale exits). Drop the proximity check entirely (trafilatura main-article extraction makes it redundant).

**Pros/cons of the keyword-expansion-without-i18n choice:**

- *Pro:* catches `shuttered`, `Chapter 11`, `liquidation`, `delisted`, etc. — vocabulary missing from the original set.
- *Pro:* zero new dependencies and no per-language maintenance burden.
- *Con:* still misses non-English citations (Mandarin, French, etc.). Acceptable because most niche-vendor failures are covered by English-language news aggregators (Tech in Asia, Restofworld). Revisit only if eval shows this population is materially undercaught.

**Files:**
- Modify: `slopmortem/stages/recall_verify.py:74-98` (`_DEATH_KEYWORDS`), `:128-134` (`_body_anchors_name_and_death`), `:298-316` (L3 logic in `verify_suggestion`)
- Modify: `tests/stages/test_recall_verify.py` — `_DEATH_KEYWORDS` membership tests still pass against `tuple`, but any test that asserts `frozenset` semantics needs an update.
- Create: `tests/stages/test_recall_verify_l3_hygiene.py`
- Create: `tests/fixtures/recall/sidebar_bleed.html`
- Create: `tests/fixtures/recall/paywall_stub.html`
- Modify: `slopmortem/tracing/events.py` (new `RECALL_REJECTED_L3_BODY_TOO_SHORT`)

### Steps

- [x] **Step 1.1: Write the failing test for body-length sanity gate**

```python
# tests/stages/test_recall_verify_l3_hygiene.py
from pathlib import Path
import pytest
from slopmortem.models import RecallSuggestion
from slopmortem.stages.recall_verify import verify_suggestion
# Test fixtures: a FakeWayback that returns the seed unchanged, a FakeLLM
# whose .complete() raises if called (so we can prove L5 was never reached).

PAYWALL_HTML = (Path(__file__).parent.parent / "fixtures/recall/paywall_stub.html").read_text()

async def test_l3_drops_when_body_under_500_chars(httpx_mock, fake_wayback, llm_call_blocker):
    httpx_mock.add_response(method="HEAD", url="https://news.example/x", status_code=200)
    httpx_mock.add_response(method="GET", url="https://news.example/x", text=PAYWALL_HTML)
    suggestion = RecallSuggestion(
        name="Acme",
        homepage_url="https://acme.test",
        evidence_url="https://news.example/x",
        status="dead",
        failure_year=2023,
    )
    result = await verify_suggestion(
        suggestion,
        wayback=fake_wayback,
        llm=llm_call_blocker,
        model_recall_deathness="anthropic/claude-haiku-4-5-20251001",
        max_tokens_recall_deathness=512,
        min_confidence=0.7,
    )
    assert result is None
```

- [x] **Step 1.2: Create the paywall fixture**

```html
<!-- tests/fixtures/recall/paywall_stub.html -->
<!DOCTYPE html>
<html><head><title>Article</title></head>
<body><main><p>Sign in to read more.</p></main></body>
</html>
```

- [x] **Step 1.3: Run the test, confirm it fails for the right reason**

Run: `uv run pytest tests/stages/test_recall_verify_l3_hygiene.py::test_l3_drops_when_body_under_500_chars -v`
Expected: FAIL — current code admits the suggestion or fails L3 with the wrong span event.

- [x] **Step 1.4: Add `RECALL_REJECTED_L3_BODY_TOO_SHORT` to the SpanEvent StrEnum**

```python
# slopmortem/tracing/events.py — add inside class SpanEvent(StrEnum):
RECALL_REJECTED_L3_BODY_TOO_SHORT = "recall.rejected_l3_body_too_short"
```

- [x] **Step 1.5: Replace L3 body fetch with extract-then-length-gate-then-scan**

`extract_clean` already returns `""` below its `LENGTH_FLOOR=500` (`slopmortem/corpus/_extract.py:111-121`). Treat the empty string as a hard reject — no fallback to raw HTML, since (a) the sidebar-bleed test relies on trafilatura's `<aside>` strip and the fallback would reintroduce noise, and (b) the import-linter contract forbids reaching into `corpus._extract` from `stages`, so import via the public `slopmortem.corpus` re-export.

Move the `Final` constant to module scope (`Final` is invalid inside a function body).

```python
# At module top of slopmortem/stages/recall_verify.py
from slopmortem.corpus import extract_clean

# extract_clean already enforces LENGTH_FLOOR=500. Setting the verifier's own
# floor identical means we reject only when extraction produced nothing real;
# a 500-char article body is the minimum signal density we'll keep.
_L3_MIN_BODY_CHARS: Final = 500

# inside verify_suggestion, after the GET succeeds:
evidence_body = extract_clean(evidence_resp.text)
if len(evidence_body) < _L3_MIN_BODY_CHARS:
    logger.info("recall_verify: L3 body too short (%d chars) for %s", len(evidence_body), evidence)
    _emit_event(SpanEvent.RECALL_REJECTED_L3_BODY_TOO_SHORT)
    return None
```

- [x] **Step 1.6: Run the body-length test, confirm it passes**

Run: `uv run pytest tests/stages/test_recall_verify_l3_hygiene.py::test_l3_drops_when_body_under_500_chars -v`
Expected: PASS.

- [x] **Step 1.7: Write the failing test for HTML sidebar-bleed rejection**

```python
SIDEBAR_HTML = (Path(__file__).parent.parent / "fixtures/recall/sidebar_bleed.html").read_text()

async def test_l3_rejects_when_name_only_in_sidebar(httpx_mock, fake_wayback, llm_call_blocker):
    httpx_mock.add_response(method="HEAD", url="https://news.example/y", status_code=200)
    httpx_mock.add_response(method="GET", url="https://news.example/y", text=SIDEBAR_HTML)
    suggestion = RecallSuggestion(
        name="Acme",
        homepage_url="https://acme.test",
        evidence_url="https://news.example/y",
        status="dead",
        failure_year=2023,
    )
    result = await verify_suggestion(
        suggestion,
        wayback=fake_wayback,
        llm=llm_call_blocker,
        model_recall_deathness="anthropic/claude-haiku-4-5-20251001",
        max_tokens_recall_deathness=512,
        min_confidence=0.7,
    )
    assert result is None
```

- [x] **Step 1.8: Create the sidebar-bleed fixture**

```html
<!-- tests/fixtures/recall/sidebar_bleed.html -->
<!DOCTYPE html>
<html><head><title>Different vendor went bankrupt</title></head>
<body>
<nav><a href="/trending">Trending: shutdown news</a></nav>
<main>
<article>
<h1>Northwind Cloud filed for bankruptcy</h1>
<p>Northwind Cloud, the data-orchestration startup, filed Chapter 7 yesterday after running out of runway. The company had raised $40M across two rounds and grew to forty employees before contracting in late 2023.</p>
<p>Northwind's CEO declined to comment. The filing lists $12M in liabilities and $200K in remaining assets. Customers will be migrated to a third-party operator over the next ninety days.</p>
</article>
</main>
<aside class="trending"><h3>Related</h3><ul><li>Acme raises Series B</li></ul></aside>
</body>
</html>
```

(Trafilatura extracts the `<main>` content and drops the `<aside>` — body will mention `Northwind` and `Chapter 7`/`bankruptcy`/`shutdown` but never `Acme`. L3 must reject.)

- [x] **Step 1.9: Run the sidebar test, confirm it passes given Step 1.5 already strips HTML**

Run: `uv run pytest tests/stages/test_recall_verify_l3_hygiene.py::test_l3_rejects_when_name_only_in_sidebar -v`
Expected: PASS (the F1.1 strip from Step 1.5 already handles this).

- [x] **Step 1.10: Write the failing test for word-boundary regex**

```python
async def test_l3_does_not_match_substring_inside_word(httpx_mock, fake_wayback, llm_call_blocker):
    # Body contains "enclosed" and "disclosed" but no real death keyword,
    # plus the vendor name. Substring match would falsely admit.
    body = (
        "<html><body><main><p>Acme's quarterly disclosed financials enclosed in this "
        "release show steady growth. The team enclosed a multi-year roadmap and disclosed "
        "no material liabilities. " + "Filler content. " * 60 + "</p></main></body></html>"
    )
    httpx_mock.add_response(method="HEAD", url="https://news.example/z", status_code=200)
    httpx_mock.add_response(method="GET", url="https://news.example/z", text=body)
    suggestion = RecallSuggestion(
        name="Acme",
        homepage_url="https://acme.test",
        evidence_url="https://news.example/z",
        status="dead",
        failure_year=2023,
    )
    result = await verify_suggestion(
        suggestion,
        wayback=fake_wayback,
        llm=llm_call_blocker,
        model_recall_deathness="anthropic/claude-haiku-4-5-20251001",
        max_tokens_recall_deathness=512,
        min_confidence=0.7,
    )
    assert result is None
```

- [x] **Step 1.11: Run the substring test, confirm it fails (current code admits via substring)**

Run: `uv run pytest tests/stages/test_recall_verify_l3_hygiene.py::test_l3_does_not_match_substring_inside_word -v`
Expected: FAIL — current substring scan matches `closed` inside `enclosed`/`disclosed`.

- [x] **Step 1.12: Replace the substring scan with a precompiled word-boundary regex**

In `slopmortem/stages/recall_verify.py`, replace `_DEATH_KEYWORDS: Final[frozenset[str]]` and `_body_anchors_name_and_death`:

```python
import re

# Single regex compiled once at import. Word-boundary anchored, multi-word
# entries match across whitespace runs ("shut\s+down").
_DEATH_KEYWORDS: Final[tuple[str, ...]] = (
    # Terminal
    "shutdown", "shut down", "shuttered", "closed", "ceased",
    "defunct", "dissolved", "bankrupt", "bankruptcy",
    "Chapter 11", "Chapter 7", "liquidation", "wound down",
    "wind-down", "wind down", "going out of business",
    "out of business", "obituary", "delisted", "cease operations",
    "acquired", "acquisition",
    # Distress
    "layoffs", "layoff", "restructuring", "struggling",
    "missed payroll", "downsizing", "troubled",
)

def _build_death_regex() -> re.Pattern[str]:
    parts = sorted(_DEATH_KEYWORDS, key=len, reverse=True)  # longest first
    escaped = [re.escape(p).replace(r"\ ", r"\s+") for p in parts]
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.IGNORECASE)

_DEATH_REGEX: Final = _build_death_regex()


def _body_anchors_name_and_death(name: str, body: str) -> _AnchorResult:
    if name.lower() not in body.lower():
        return "name_missing"
    if not _DEATH_REGEX.search(body):
        return "keyword_missing"
    return "ok"
```

- [x] **Step 1.13: Run the substring test, confirm it now passes**

Run: `uv run pytest tests/stages/test_recall_verify_l3_hygiene.py::test_l3_does_not_match_substring_inside_word -v`
Expected: PASS.

- [x] **Step 1.14: Write the test for expanded keyword admits (`shuttered`, `Chapter 11`)**

```python
async def test_l3_admits_shuttered_keyword(httpx_mock, fake_wayback, fake_llm_admits):
    body = (
        "<html><body><main><p>Acme Security shuttered last month after failing to "
        "raise a Series B. The thirty-person company had been struggling for two "
        "quarters before the board voted to wind down operations. " + "More detail. " * 80
        + "</p></main></body></html>"
    )
    httpx_mock.add_response(method="HEAD", url="https://news.example/a", status_code=200)
    httpx_mock.add_response(method="GET", url="https://news.example/a", text=body)
    # ... rest of the harness, expect verify_suggestion to return non-None ...
```

(`fake_llm_admits` is a tiny test helper LLM that returns `{"died": true, "confidence": 0.9, ...}` — it lives in this test file or the existing `slopmortem/llm/fake.py` if a similar helper already exists.)

- [x] **Step 1.15: Run all Task 1 tests, confirm green**

Run: `uv run pytest tests/stages/test_recall_verify_l3_hygiene.py -v`
Expected: all PASS.

- [x] **Step 1.16: Run typecheck and lint**

Run: `just typecheck && just lint`
Expected: both pass.

- [x] **Step 1.17: Commit**

```
recall: L3 hygiene — extract_clean, body-length gate, word-boundary regex, expanded vocab
```

---

## Task 2 — L2/L4 restructure: drop homepage HEAD gate, HEAD→GET fallback, direct Wayback call

**Goal:** Stop gating on `homepage_url`. The verifier's job is to verify the `evidence_url` citation — the homepage is provenance, not corroboration, so its liveness is not load-bearing. On the evidence URL, fall through HEAD→GET so paywalled and anti-bot citations don't false-drop (many real news sites return 401/403/405 on HEAD even when GET works). Wayback becomes pure body-enrichment with a *direct* call — `WaybackEnricher.enrich()` already retries 3× internally with exponential backoff (`wayback.py:62-99`) and swallows transient errors by returning the entry unchanged, so an outer retry wrapper is dead code in production. Failure or empty result falls through to evidence-only persistence and never drops a candidate. The hallucination guard moves entirely to L5: its verbatim `evidence_quote` requirement is the gate that catches names appearing only in unrelated contexts.

**Pros/cons:**

- *Pro:* recall verifier no longer fails on legitimate paywalled/CDN-protected citations that respond to GET but not HEAD — historically the largest false-drop class in production verifiers of this shape.
- *Pro:* recall verifier no longer fails when archive.org is rate-limited or when a real but obscure vendor never got crawled (Wayback coverage is patchy enough that "no snapshot" is not a reliable hallucination signal).
- *Pro:* simpler code — L4 is body-selection logic, not a gate. Smaller test surface than the original soft-gate truth table. No outer retry helper to maintain.
- *Pro:* matches the L1–L5 contract: each layer rejects on its own positive evidence; no layer rejects on absence of corroboration.
- *Con:* loses the (weak, patchy) Wayback-corroborates-realness signal. A hallucinated vendor name that happens to appear verbatim in a death-context news article would admit. Mitigated because (a) L3's word-boundary regex requires actual name presence, (b) L5 demands a verbatim `evidence_quote` substantiating the verdict against the news body, and (c) Wayback's empty answer was never reliable in the first place.
- *Con (HEAD→GET fallback):* one extra GET on hosts that legitimately 404. Bounded by `_FETCH_TIMEOUT_S=40`; the GET would have run anyway at L3 if HEAD had succeeded.

**Files:**
- Modify: `slopmortem/stages/recall_verify.py:267-359` (`verify_suggestion` body — drop homepage HEAD branch; HEAD→GET fallback on evidence URL; direct `wayback.enrich()` call without outer wrapper; rewrite L4 as body selection)
- Create: `tests/stages/test_recall_verify_l2_l4_bprime.py`

### Steps

- [x] **Step 2.1: No new SpanEvent in this task**

`RECALL_VERIFIED_EVIDENCE_ONLY_RATE_LIMITED` is *not* added — its trigger condition can't occur in production. `WaybackEnricher.enrich()` (`slopmortem/corpus/sources/wayback.py:163-202`) catches every transient error inside `_safe_get_with_retry` and returns the seed entry unchanged on failure; the outer `try/except` in `verify_suggestion` only fires for fakes that explicitly `raise` (test-only path). Adding a span event for a path that can only fire in tests pollutes production telemetry. Similarly `RECALL_REJECTED_L4_NO_ARCHIVE` is not added — Wayback's absence no longer drops candidates.

- [x] **Step 2.2: Failing test — dead homepage admits when Wayback anchors**

```python
# tests/stages/test_recall_verify_l2_l4_bprime.py
async def test_homepage_head_does_not_gate_when_wayback_anchors(httpx_mock, fake_wayback_anchored, fake_llm_admits):
    # Homepage HEAD returns 404; Wayback supplies a snapshot containing the vendor name.
    httpx_mock.add_response(method="HEAD", url="https://acme.test", status_code=404)
    httpx_mock.add_response(method="HEAD", url="https://news.example/x", status_code=200)
    httpx_mock.add_response(method="GET",  url="https://news.example/x", text=NEWS_BODY_DEAD)
    suggestion = RecallSuggestion(name="Acme", homepage_url="https://acme.test",
                                  evidence_url="https://news.example/x", status="dead", failure_year=2023)
    result = await verify_suggestion(suggestion, wayback=fake_wayback_anchored, llm=fake_llm_admits, ...)
    assert result is not None
    entry, tier = result
    assert tier == "wayback_anchored"
```

- [x] **Step 2.3: Failing test — Wayback empty falls through to evidence_only (no drop)**

```python
async def test_wayback_empty_admits_at_evidence_only_tier(httpx_mock, fake_wayback_empty, fake_llm_admits):
    # Wayback returns the seed unchanged; homepage HEAD returns 404 (irrelevant — no longer gates).
    httpx_mock.add_response(method="HEAD", url="https://news.example/x", status_code=200)
    httpx_mock.add_response(method="GET",  url="https://news.example/x", text=NEWS_BODY_DEAD)
    result = await verify_suggestion(...)
    assert result is not None
    _, tier = result
    assert tier == "evidence_only"
```

- [x] **Step 2.4: Failing test — Wayback transient failure does not crash the verifier**

```python
async def test_wayback_transient_failure_does_not_drop(httpx_mock, fake_llm_admits):
    """Real WaybackEnricher catches transient errors internally. Verify the
    verifier still admits when wayback raises — even though that path only
    fires for fakes, it's a refactor guard against future Wayback changes
    that surface exceptions to the caller.
    """
    class RaisingWayback:
        async def enrich(self, seed):
            raise httpx.ReadTimeout("ia is down")
    httpx_mock.add_response(method="HEAD", url="https://news.example/x", status_code=200)
    httpx_mock.add_response(method="GET",  url="https://news.example/x", text=NEWS_BODY_DEAD)
    result = await verify_suggestion(..., wayback=RaisingWayback(), llm=fake_llm_admits, ...)
    assert result is not None
    _, tier = result
    assert tier == "evidence_only"
```

- [x] **Step 2.5: Failing test — evidence_url HEAD failure falls through to GET; only GET 4xx drops**

```python
async def test_evidence_head_405_falls_through_to_get(httpx_mock, fake_wayback_anchored, fake_llm_admits):
    """Many news sites return 405 on HEAD but serve GET correctly. The
    verifier must not drop on HEAD failure if GET succeeds.
    """
    httpx_mock.add_response(method="HEAD", url="https://news.example/x", status_code=405)
    httpx_mock.add_response(method="GET",  url="https://news.example/x", text=NEWS_BODY_DEAD)
    result = await verify_suggestion(..., wayback=fake_wayback_anchored, llm=fake_llm_admits, ...)
    assert result is not None  # admitted via GET fallback


async def test_evidence_get_404_drops(httpx_mock, fake_wayback_anchored, llm_call_blocker):
    """When GET also fails (the URL is genuinely dead), drop. L5 must not run."""
    httpx_mock.add_response(method="HEAD", url="https://news.example/x", status_code=404)
    httpx_mock.add_response(method="GET",  url="https://news.example/x", status_code=404)
    result = await verify_suggestion(..., wayback=fake_wayback_anchored, llm=llm_call_blocker, ...)
    assert result is None
```

(L5 should never run when both HEAD and GET fail; the LLM blocker proves it.)

- [x] **Step 2.6: Run all four tests, confirm they fail**

Run: `uv run pytest tests/stages/test_recall_verify_l2_l4_bprime.py -v`
Expected: FAIL — current verifier drops on `homepage_url` HEAD 404 and on `evidence_url` HEAD 405.

- [x] **Step 2.7: Refactor `verify_suggestion`**

Drop the homepage-HEAD branch entirely. Add HEAD→GET fallback on the evidence URL. Call Wayback directly (no outer wrapper). Collapse L4 to body selection.

```python
# In verify_suggestion, replace the L2 loop and L3 GET block with:

# L2: gate evidence_url only. The homepage_url is provenance — not gated.
# HEAD→GET fallthrough: many real news sites (paywalls, CDNs, anti-bot)
# return 401/403/405 on HEAD even when GET works. Try HEAD first because
# it's cheap; on any failure shape, run the L3 GET that would have run
# anyway and let its status code be the gate.
head_failed = False
try:
    head_resp = await safe_head(evidence, timeout=_FETCH_TIMEOUT_S)
except (SSRFBlockedError, httpx.HTTPError) as exc:
    logger.info("recall_verify: L2 HEAD failed for %s, falling through to GET: %r", evidence, exc)
    head_failed = True
else:
    if head_resp.status_code >= _HTTP_BAD_REQUEST:
        logger.info(
            "recall_verify: L2 HEAD %s for %s, falling through to GET",
            head_resp.status_code, evidence,
        )
        head_failed = True

# L3: GET evidence body. This is the authoritative gate when HEAD failed.
try:
    evidence_resp = await safe_get(evidence, timeout=_FETCH_TIMEOUT_S)
except (SSRFBlockedError, httpx.HTTPError) as exc:
    logger.info("recall_verify: L3 GET failed for %s: %r", evidence, exc)
    _emit_event(
        SpanEvent.RECALL_REJECTED_L2,
        attributes={"stage": "get", "head_failed": str(head_failed)},
    )
    return None
if evidence_resp.status_code >= _HTTP_BAD_REQUEST:
    logger.info("recall_verify: L3 GET %s for %s", evidence_resp.status_code, evidence)
    _emit_event(
        SpanEvent.RECALL_REJECTED_L2,
        attributes={"stage": "get", "head_failed": str(head_failed)},
    )
    return None

# L3 body extraction + keyword scan — Task 1 logic unchanged.
evidence_body = extract_clean(evidence_resp.text)
if len(evidence_body) < _L3_MIN_BODY_CHARS:
    logger.info("recall_verify: L3 body too short (%d chars) for %s", len(evidence_body), evidence)
    _emit_event(SpanEvent.RECALL_REJECTED_L3_BODY_TOO_SHORT)
    return None
anchor = _body_anchors_name_and_death(suggestion.name, evidence_body)
if anchor != "ok":
    _log_and_emit_l3_rejection(anchor, name=suggestion.name, evidence=evidence)
    return None

# L4: Wayback enrichment, advisory only. Direct call — WaybackEnricher.enrich
# already retries 3× internally with exponential backoff and swallows
# transient errors by returning the entry unchanged (wayback.py:62-99,
# 163-202). Outer try/except is a refactor guard for fakes that raise;
# in production this branch is dead.
seed = RawEntry(
    source=SOURCE_LLM_RECALL,
    source_id=_recall_source_id(suggestion),
    url=homepage,
    markdown_text=None,
    raw_html=None,
    fetched_at=datetime.now(UTC),
)
try:
    enriched = await wayback.enrich(seed)
except (httpx.HTTPError, RuntimeError) as exc:
    # Refactor guard: real WaybackEnricher catches these internally. Logged
    # so a future Wayback change that starts surfacing exceptions doesn't go
    # unnoticed in prod.
    logger.info("recall_verify: wayback raised (unexpected — enricher should swallow): %r", exc)
    enriched = seed

wayback_anchored = bool(
    enriched.markdown_text
    and suggestion.name.lower() in enriched.markdown_text.lower()
)
if wayback_anchored:
    # ``wayback_anchored`` is True ⇒ ``enriched.markdown_text`` is non-empty.
    # Basedpyright won't narrow ``str | None`` across the intermediate bool;
    # the assert pulls double duty as type narrowing and a refactor guard.
    assert enriched.markdown_text is not None
    tier = "wayback_anchored"
    body = enriched.markdown_text
    _emit_event(SpanEvent.RECALL_VERIFIED_WAYBACK_ANCHORED)
else:
    tier = "evidence_only"
    body = evidence_body
    _emit_event(SpanEvent.RECALL_VERIFIED_EVIDENCE_ONLY)
```

- [x] **Step 2.8: Run all Task 2 tests, confirm green**

Run: `uv run pytest tests/stages/test_recall_verify_l2_l4_bprime.py -v`
Expected: all PASS.

- [x] **Step 2.9: Re-run Task 1 tests to confirm no regression**

Run: `uv run pytest tests/stages/test_recall_verify_l3_hygiene.py -v`
Expected: still PASS.

- [x] **Step 2.10: Run typecheck, lint, full test suite**

Run: `just typecheck && just lint && just test`
Expected: all pass.

- [x] **Step 2.11: Commit**

```
recall: drop homepage HEAD gate; HEAD→GET fallback on evidence URL; direct Wayback call
```

---

## Task 3 — L5 tri-state + combined persisted body

**Goal:** Persist a combined body — news article (always) plus the Wayback snapshot (when anchored, prepended under a section marker) — so downstream synthesis can read both the vendor's value-prop and the death narrative from a single Qdrant entry. Feed the news article body to L5 always (the deathness judge needs the death citation, not marketing copy). Replace the binary `died: bool` with `verdict: Literal["dead","struggling","alive"]` + a separate `struggling_min_confidence` config knob (default 0.85, stricter than the existing dead threshold).

**Pros/cons of combining bodies vs picking one:**

- *Pro:* synthesizer reads the same death citation L5 verified against, plus value-prop context for pitch comparison. Wayback's role becomes consistent — pure enrichment, never load-bearing alone.
- *Pro:* closes the prior asymmetry where Wayback was advisory for gating but canonical for storage. If we don't trust Wayback's "empty" answer to drop, we shouldn't trust its "anchored" answer to displace the death citation either.
- *Con:* slightly larger persisted chunks (typical news + snapshot fits well within Qdrant chunk budget). Section markers (`# Vendor description (archived)`, `# Failure citation`) help the chunker preserve semantic boundaries.

**Pros/cons of tri-state vs binary:**

- *Pro:* expresses what L3 admits — both terminal and distress. Binary forced Haiku to round struggling to either dead (false positive) or alive (drops a real distress signal).
- *Con:* Haiku 3-way classification is empirically less calibrated than binary. Mitigated by the conservative `struggling_min_confidence` default and by treating "struggling" as a separate persistence flag downstream rather than collapsing it into "dead."
- *Cost:* one cassette re-record (~$2 via `just eval-record` for the affected scope only).

**Files:**
- Modify: `slopmortem/stages/recall_verify.py` (`_DeathnessJudgment`, `_l5_deathness_judgment`, `_l5_admits` → renamed `_l5_decide`, `verify_suggestion` body-source + return tuple)
- Modify: `slopmortem/llm/prompts/recall_deathness.j2`
- Modify: `slopmortem/config.py` (new `recall_struggling_min_confidence`)
- Modify: `slopmortem.toml` (document the new key)
- Modify: `slopmortem/pipeline.py:221` (single call site for `verify_and_persist_all`; thread `struggling_min_confidence`)
- Modify: `slopmortem/tracing/events.py` (add `RECALL_REJECTED_L5_ALIVE`, **remove** `RECALL_REJECTED_L5_NOT_DEAD` — orphaned by tri-state)
- Modify: `slopmortem/models.py` — add `deathness_verdict: Literal["dead", "struggling"] | None = None` to `CandidatePayload` (mirrors the existing `verification_tier` field at `models.py:217`). `None` for non-recall sources; populated only from L5.
- Modify: `slopmortem/ingest/_helpers.py:168` (`_build_payload` accepts `deathness_verdict` and passes it through to `CandidatePayload`).
- Modify: `slopmortem/ingest/_journal_writes.py:80` (`_process_entry` accepts `deathness_verdict` sibling to `verification_tier`; threads to `_build_payload`).
- Modify: `slopmortem/ingest/_ingest.py:295` (`_write_phase` accepts `deathness_verdict` sibling to `verification_tier`; threads to `_process_entry`).
- Modify: `slopmortem/stages/recall_persist.py` (`persist_recall_entry` accepts `deathness_verdict` sibling to `tier`; passes to `write_phase`).
- Modify: `tests/stages/test_recall_verify.py` — every test that ships `'{"died": true, ...}'` JSON literals (existing `_DEATHNESS_PASS` at line 27 plus per-test variants) needs swapping to `'{"verdict": "dead", ...}'`. Plumb `struggling_min_confidence` through every call to `verify_suggestion` / `verify_and_persist_all`.
- Modify: `tests/stages/test_recall_verify_l2_l4_bprime.py` (created in Task 2) — the Task 2 tests destructure `entry, tier = result` and `_, tier = result`. Task 3 widens the return to a 3-tuple `(entry, tier, verdict)`; update both destructures to the new shape so Task 2's tests stay green after Task 3 lands.
- Create: `tests/stages/test_recall_verify_l5_tristate.py`
- Re-record: deferred to Task 6. Task 3 changes the `recall_deathness` prompt SHA; Task 6 will change the `synthesize` prompt SHA. Recording once at the end of Task 6 avoids paying the $2 ceiling twice. Until Task 6 lands, any cassette test exercising `recall_deathness` will raise `NoCannedResponseError` — Step 3.9 expects this and treats it as PASS-modulo-cassettes.

### Steps

- [ ] **Step 3.1: Add `recall_struggling_min_confidence` to config**

```python
# slopmortem/config.py — in the appropriate Config section
recall_struggling_min_confidence: float = Field(default=0.85, ge=0.0, le=1.0)
```

```toml
# slopmortem.toml — under [recall]
struggling_min_confidence = 0.85  # higher than dead's threshold; struggling is fuzzier
```

- [ ] **Step 3.2: Update `_DeathnessJudgment` to tri-state**

```python
# slopmortem/stages/recall_verify.py
class _DeathnessJudgment(BaseModel):
    """L5 verdict: did the verified evidence body actually establish death,
    distress, or neither.
    """
    verdict: Literal["dead", "struggling", "alive"]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_quote: str
```

- [ ] **Step 3.3: Update the prompt template for tri-state output**

```jinja
{# slopmortem/llm/prompts/recall_deathness.j2 — system block #}
You are judging whether an evidence body establishes that a vendor company
has DIED, is STRUGGLING (still operating but visibly impaired), or is ALIVE
(continuing operations without material distress).

Output JSON matching the schema. Use:
- "dead" only when the body explicitly states the company shut down,
  liquidated, dissolved, filed Chapter 7, or otherwise ceased to exist as
  an operating entity. Acquisitions count as "dead" ONLY when the body
  describes a fire-sale, distress sale, asset-only purchase after
  insolvency, or acquisition price materially below total funding raised.
- "struggling" when the body describes layoffs, restructuring, missed
  payroll, downsizing, or other ongoing distress — but the company has
  not ceased operations.
- "alive" when the body does not establish either of the above. Healthy
  M&A (strategic acquisition at or above funding raised, talent acqui-hire
  with no distress signal, portfolio expansion) is "alive" — the company
  ceased as an independent entity but did not fail. The slopmortem
  population is failure cases; healthy exits are out of scope.

Confidence is your estimate that the verdict is correct given the body.
Lower for ambiguous wording, higher for explicit statements with dates.
evidence_quote is a verbatim short span from the body that supports the
verdict.

Disambiguation examples:
- "Acme acquired by Cisco for $400M to expand security portfolio"
  (Acme had raised $80M total) → "alive" (healthy strategic acquisition).
- "Acme's assets sold for $500K to a competitor after Chapter 7 filing"
  → "dead" (fire-sale after insolvency).
- "Acme cut 40% of staff and is restructuring around a smaller core
  product" → "struggling" (distress, still operating).
```

(User block stays unchanged in shape — pitch + name + status + body.)

- [ ] **Step 3.4: Rename `_l5_admits` → `_l5_decide` returning the verdict**

The existing `_l5_admits` returns `bool`, which discards the verdict and prevents downstream synthesis from telling `dead` from `struggling`. Rename to `_l5_decide` and return `Literal["dead","struggling"] | None` (`None` = drop). The verdict propagates through `verify_suggestion`'s return tuple and lands in `CandidatePayload.deathness_verdict`. Synthesis can then weight terminal vs distress citations differently — the whole point of the tri-state.

```python
type _AdmitVerdict = Literal["dead", "struggling"]

async def _l5_decide(
    *,
    suggestion: RecallSuggestion,
    body: str,
    llm: LLMClient,
    model: str,
    max_tokens: int,
    min_confidence: float,           # for "dead" verdicts (existing knob)
    struggling_min_confidence: float, # for "struggling" verdicts (new knob)
) -> _AdmitVerdict | None:
    judgment = await _l5_deathness_judgment(
        suggestion=suggestion, body=body, llm=llm, model=model, max_tokens=max_tokens,
    )
    if judgment is None:
        _emit_event(SpanEvent.RECALL_REJECTED_L5_LOW_CONFIDENCE)
        return None
    if judgment.verdict == "alive":
        logger.info("recall_verify: L5 ruled %r alive (confidence=%.2f)",
                    suggestion.name, judgment.confidence)
        _emit_event(SpanEvent.RECALL_REJECTED_L5_ALIVE)
        return None
    threshold = min_confidence if judgment.verdict == "dead" else struggling_min_confidence
    if judgment.confidence < threshold:
        logger.info(
            "recall_verify: L5 %s confidence %.2f below threshold %.2f for %r",
            judgment.verdict, judgment.confidence, threshold, suggestion.name,
        )
        _emit_event(SpanEvent.RECALL_REJECTED_L5_LOW_CONFIDENCE)
        return None
    return judgment.verdict
```

Add the new SpanEvent member: `RECALL_REJECTED_L5_ALIVE = "recall.rejected_l5_alive"`. **Remove** `RECALL_REJECTED_L5_NOT_DEAD` (`tracing/events.py:45`) — the tri-state replacement makes it unreachable. Grep-confirm no other emitter references it before deleting.

- [ ] **Step 3.5: Add the body-combiner helper and rewrite the body-selection block**

Add a small helper (module-private to `recall_verify.py`) that joins the news article and the Wayback snapshot under section markers. The news article is the citation L5 verifies against and is therefore always present; Wayback contributes a "Vendor description (archived)" prefix only when it anchored.

```python
# slopmortem/stages/recall_verify.py — module-private helper
def _combine_recall_body(
    *,
    wayback_body: str | None,
    evidence_url: str,
    news_body: str,
    suggestion: RecallSuggestion,
) -> str:
    """Compose the persisted body from the L3 news article (always) and the
    L4 Wayback snapshot (when anchored). Section markers preserve semantic
    boundaries for the chunker so news content and snapshot content don't
    blur into a single chunk.

    The status/year line is labeled "LLM-suggested" because it comes from the
    Sonnet recall payload, not from the news body — downstream synthesis
    should treat it as a hint, not a fact.
    """
    parts: list[str] = []
    if wayback_body:
        parts.append(f"# Vendor description (archived)\n\n{wayback_body}")
    parts.append(
        f"# Failure citation\n\n"
        f"Source: {evidence_url}\n"
        f"Status (LLM-suggested): {suggestion.status} ({suggestion.failure_year})\n\n"
        f"{news_body}"
    )
    return "\n\n---\n\n".join(parts)
```

Then in `verify_suggestion`, after L4 sets `wayback_anchored` and `enriched`. The function's return type widens from `tuple[RawEntry, VerificationTier] | None` to `tuple[RawEntry, VerificationTier, _AdmitVerdict] | None`:

```python
# L5 body: always the news article — the death citation we verified at L3.
# Wayback marketing copy never says "we died" so it's the wrong substrate
# for the deathness judgment.
l5_body = evidence_body

verdict = await _l5_decide(
    suggestion=suggestion,
    body=l5_body,
    llm=llm,
    model=model_recall_deathness,
    max_tokens=max_tokens_recall_deathness,
    min_confidence=min_confidence,
    struggling_min_confidence=struggling_min_confidence,
)
if verdict is None:
    return None

# Persisted body: news article (always) + Wayback snapshot (when anchored).
# Synthesizer can read both the value-prop and the death narrative from a
# single Qdrant entry; the verifier and synthesizer agree on which document
# represents this vendor.
wayback_body = enriched.markdown_text if wayback_anchored else None
combined = _combine_recall_body(
    wayback_body=wayback_body,
    evidence_url=str(evidence),
    news_body=evidence_body,
    suggestion=suggestion,
)
final = seed.model_copy(update={"markdown_text": combined})
return final, tier, verdict
```

- [ ] **Step 3.6: Thread `struggling_min_confidence` AND `deathness_verdict` through the call chain**

Two parallel pipes. `struggling_min_confidence` flows downward (config → gate); `deathness_verdict` flows upward (gate → payload). Mirror the existing `verification_tier` thread end-to-end so synthesis sees both axes (anchored-vs-evidence-only AND dead-vs-struggling) per recall entry.

Downward pipe (`struggling_min_confidence`):
1. Add `struggling_min_confidence=config.recall_struggling_min_confidence` at `pipeline.py:221`.
2. Thread through `verify_and_persist_all` → `verify_suggestion` → `_l5_decide`.

Upward pipe (`deathness_verdict`):
1. `verify_suggestion` returns `tuple[RawEntry, VerificationTier, _AdmitVerdict] | None` (Step 3.5).
2. `verify_and_persist_all` widens its `persist` callback signature: `Callable[[RawEntry, VerificationTier, _AdmitVerdict], Awaitable[None]]`.
3. `_persist` in `pipeline.py:196` accepts the new `verdict` arg and forwards to `persist_recall_entry`.
4. `persist_recall_entry` (`stages/recall_persist.py`) accepts `deathness_verdict: _AdmitVerdict` sibling to `tier`; passes it as a kwarg to `write_phase`.
5. `_write_phase` (`ingest/_ingest.py:295`) accepts `deathness_verdict: Literal["dead","struggling"] | None = None` sibling to `verification_tier`; passes to `_process_entry`.
6. `_process_entry` (`ingest/_journal_writes.py:80`) accepts the same kwarg; passes to `_build_payload`.
7. `_build_payload` (`ingest/_helpers.py:168`) accepts the same kwarg and writes it onto `CandidatePayload.deathness_verdict`.
8. `CandidatePayload` (`models.py:189`) gains the new field after `verification_tier`:
   ```python
   deathness_verdict: Literal["dead", "struggling"] | None = None
   ```
   Default `None` preserves payload shape for non-recall sources; existing crunchbase/web rows continue to deserialize without migration.

Note: `_AdmitVerdict` is module-private to `recall_verify.py`. To avoid leaking it across import boundaries, declare the field on `CandidatePayload` and the carrier kwargs as the bare `Literal["dead","struggling"] | None` on each layer. The Literal is structurally identical and keeps the leaf module's alias private.

- [ ] **Step 3.7: Write tests for tri-state thresholds**

```python
# tests/stages/test_recall_verify_l5_tristate.py
@pytest.mark.parametrize("verdict,confidence,expect_admit", [
    ("dead",       0.95, True),
    ("dead",       0.50, False),  # below dead threshold (0.7)
    ("struggling", 0.95, True),
    ("struggling", 0.80, False),  # below struggling threshold (0.85)
    ("alive",      0.99, False),
])
async def test_l5_tristate_thresholds(verdict, confidence, expect_admit, ...):
    ...
```

- [ ] **Step 3.8: Write the test that L5 reads news only and persisted body combines both**

```python
async def test_l5_news_body_and_persisted_combined_when_anchored(...):
    """L5's prompt body must be the news article only. Persisted markdown_text
    must contain BOTH the Wayback snapshot (under the archived-description
    section) and the news article (under the failure-citation section).
    The verdict propagates as the third element of the return tuple.
    """
    captured_l5_body = []
    class CapturingLLM(...):
        async def complete(self, user, **kw):
            captured_l5_body.append(user)
            return FakeCompletion(text='{"verdict":"dead","confidence":0.9,"evidence_quote":"x"}')
    # ... fake_wayback anchors with marketing copy "Acme — secure your stack" ...
    # ... evidence body says "Acme shut down in 2023" ...
    result = await verify_suggestion(...)
    assert result is not None
    entry, tier, verdict = result
    assert tier == "wayback_anchored"
    assert verdict == "dead"
    # Persisted body contains BOTH sources under labeled sections.
    assert "# Vendor description (archived)" in entry.markdown_text
    assert "secure your stack" in entry.markdown_text
    assert "# Failure citation" in entry.markdown_text
    assert "shut down in 2023" in entry.markdown_text
    # L5 saw only the news article.
    assert "shut down in 2023" in captured_l5_body[0]
    assert "secure your stack" not in captured_l5_body[0]


async def test_struggling_verdict_lands_in_qdrant_payload(...):
    """End-to-end: verify_and_persist_all → persist_recall_entry → _build_payload
    → CandidatePayload.deathness_verdict. A 'struggling' admit must reach the
    payload so synthesis can weight it differently from a 'dead' admit.
    """
    # ... fake LLM returns {"verdict":"struggling","confidence":0.9,...} ...
    fake_corpus = FakeCorpus()  # captures upserted points
    await run_query(...)
    assert len(fake_corpus.upserted) == 1
    payload = fake_corpus.upserted[0].payload
    assert payload["deathness_verdict"] == "struggling"
    assert payload["verification_tier"] in ("wayback_anchored", "evidence_only")
```

Add a sibling test for the evidence-only tier (no Wayback section, just the failure-citation section):

```python
async def test_persisted_body_omits_wayback_section_when_not_anchored(...):
    # ... fake_wayback returns seed unchanged (no anchor) ...
    result = await verify_suggestion(...)
    entry, tier, verdict = result
    assert tier == "evidence_only"
    assert verdict == "dead"
    assert "# Vendor description (archived)" not in entry.markdown_text
    assert "# Failure citation" in entry.markdown_text
```

- [ ] **Step 3.9: Run all Task 3 tests, expect failures pending cassette**

Run: `uv run pytest tests/stages/test_recall_verify_l5_tristate.py -v`
Expected: tri-state threshold tests PASS (they use stub LLM); the news-body test PASSES; any cassette-backed test FAILS with `NoCannedResponseError` because the prompt SHA changed.

- [ ] **Step 3.10: Run typecheck, lint, full non-eval test suite**

Run: `just typecheck && just lint && just test`
Expected: all pass except cassette-backed eval tests, which will raise `NoCannedResponseError` until Task 6's unified re-record. The non-eval suite (`just test`, which excludes `requires_qdrant` + `slow` markers and offline-cassette evals) should be green.

If `just test` happens to include cassette-backed eval scopes that trip on the prompt-SHA change, mark them with `pytest.skip(reason="cassette pending Task 6 re-record")` for the duration of Tasks 3-5, and remove the skip markers in Task 6 after re-recording.

- [ ] **Step 3.11: Commit**

```
recall: L5 tri-state verdict + L5 reads news body when Wayback anchored
```

---

## Task 4 — Dedup telemetry from the resolver

**Goal:** Emit `RECALL_DEDUPED_EXISTING` when the existing three-tier resolver in `_journal_writes.py:91` merges an `llm_recall` row via the `alias_blocked` action. Pure observability — no behavior change. Lets the audit dashboard see how often LLM recall surfaces something already in the corpus.

**Scope note (validated against existing telemetry):** `resolve_entity` already pushes `RESOLVER_FLIP_DETECTED` into `res.span_events` for `resolver_flipped` (verified at `slopmortem/corpus/_entity_resolution.py:528`). Emitting on both actions would double-fire on the flip path. Restrict the new event to `alias_blocked` so each canonical-merge condition has one trace event; `resolver_flipped` keeps its existing emission and the audit dashboard joins on `source` to recover the recall-scoped view.

**Files:**
- Modify: `slopmortem/ingest/_journal_writes.py` (after the `resolve_entity` call)
- Modify: `slopmortem/tracing/events.py` (new `RECALL_DEDUPED_EXISTING`)
- Create: `tests/stages/test_recall_persist_dedup_event.py`

### Steps

- [ ] **Step 4.1: Add the SpanEvent member**

```python
# slopmortem/tracing/events.py
RECALL_DEDUPED_EXISTING = "recall.deduped_existing"
```

- [ ] **Step 4.2: Write the failing test**

```python
async def test_resolver_alias_blocked_emits_recall_deduped_existing(...):
    """Persist an llm_recall entry whose canonical already exists under
    crunchbase. The resolver should return alias_blocked; the wiring must
    emit RECALL_DEDUPED_EXISTING. The resolver_flipped path is covered by
    the existing RESOLVER_FLIP_DETECTED test and is intentionally NOT a
    second emitter here.
    """
    captured = []
    monkeypatch.setattr(Laminar, "event", lambda *, name, attributes=None: captured.append(name))
    # ... set up an in-memory journal with an existing crunchbase row for acme.com ...
    # ... call persist_recall_entry with a new llm_recall suggestion for the same domain ...
    assert "recall.deduped_existing" in captured
```

- [ ] **Step 4.3: Run the test, confirm it fails**

Run: `uv run pytest tests/stages/test_recall_persist_dedup_event.py -v`
Expected: FAIL — event not currently emitted.

- [ ] **Step 4.4: Wire the emission in `_journal_writes.py`**

Add the `SOURCE_LLM_RECALL` import (currently absent from this file):

```python
# slopmortem/ingest/_journal_writes.py — at the existing import block
from slopmortem.corpus.sources._names import SOURCE_LLM_RECALL
```

Insertion point matters: `_process_entry` returns `SKIPPED` early on `alias_blocked` and `resolver_flipped` at `_journal_writes.py:103-104`. The new emit must land **between line 102 (`span_events.extend(res.span_events)`) and line 103 (`if res.action in ("alias_blocked", "resolver_flipped"): return ProcessOutcome.SKIPPED`)** — otherwise it never fires on the very condition it targets. Concretely: append the emit immediately after `span_events.extend(...)` and before the early-return guard.

```python
if res.action == "alias_blocked" and entry.source == SOURCE_LLM_RECALL:
    Laminar.event(
        name=SpanEvent.RECALL_DEDUPED_EXISTING.value,
        attributes={"action": res.action, "source_id": entry.source_id},
    )
```

Style match: this file already calls `Laminar.event(name=SpanEvent.X.value, attributes={...})` directly (see `_journal_writes.py:178`), no `is_initialized` gate. `resolver_flipped` is intentionally not in the condition — `RESOLVER_FLIP_DETECTED` already covers it via `res.span_events`.

- [ ] **Step 4.5: Run the test, confirm it passes**

Run: `uv run pytest tests/stages/test_recall_persist_dedup_event.py -v`
Expected: PASS.

- [ ] **Step 4.6: Run typecheck, lint, full test suite**

Run: `just typecheck && just lint && just test`
Expected: all pass.

- [ ] **Step 4.7: Commit**

```
ingest: emit RECALL_DEDUPED_EXISTING span when resolver merges an llm_recall row
```

---

## Task 5 — Recall flow gap closures

**Goal:** Close two gaps the prior tasks leave open:

1. **Post-recall gap measurement.** `compute_coverage_gap` fires once before recall (emits `RECALL_GAP_SCORE`); add a second call after re-rerank emitting `RECALL_GAP_SCORE_AFTER`, so prod telemetry can answer "did recall close the gap" per query and threshold calibration stops being offline-only.
2. **Skip slop classifier on recall entries.** L5 is the stricter gate operating on the death-citation substrate; the slop classifier was tuned on a different body shape, and running it on the combined body risks false-quarantining L5-verified entries. The verifier and slop classifier judging the same population on different evidence shapes is the contradiction this fix removes.

**Why no single-chunk path:** an earlier draft of this plan persisted recall entries as a single Qdrant chunk regardless of length, on the theory that chunking would "split the value-prop section from the death narrative, defeating the Task 3 combine." That was wrong about how chunking interacts with synthesis: in `_embed_and_upsert` (`_fan_out.py:139`) every chunk inherits the same payload (`base_payload | chunk_idx`) and synthesis reads `candidate.payload.body` — the *full combined body* — not the chunk text. So chunking does not affect what synthesis sees. What chunking *does* affect is the dense vector. With single-chunk + combined body, you get one vector mixing marketing copy and failure-citation prose; pitches semantically match marketing copy much more strongly than failure narratives, so the diluted vector ranks worse for pitch similarity than separate chunks would. Chunking is heading-aware (`_chunk.py:31-66`), so the section markers from `_combine_recall_body` keep boundaries clean and the marketing-copy chunk gets a focused vector — exactly the signal recall needs for the MVP outcome. The standard chunker is the right code path; no recall-specific bypass.

**Pros/cons:**

- *Pro (gap 1):* live ops can answer "was recall worth the budget on this query?" without an eval re-run. Cost is one extra `compute_coverage_gap` call (a pure Python computation over already-loaded data; no LLM).
- *Pro (gap 2):* eliminates the silent two-judge disagreement; saves the slop classifier LLM call per recall entry.
- *Con (gap 2):* slop quarantine no longer acts as a backstop for recall. Defensible because L5 is stricter and operates on the citation directly; if a deferred regression hits L5 (e.g. prompt drift), recall-flow polluted entries land in the corpus until re-tuning. Watch the log for any L5 admits whose synthesis output looks slop-like.

**Files:**
- Modify: `slopmortem/stages/recall_persist.py` (pass `skip_slop=True` through to the lifted ingest phases)
- Modify: `slopmortem/ingest/_ingest.py` (`_classify_phase` accepts `skip_slop: bool = False`)
- Modify: `slopmortem/pipeline.py` (`_run_recall_branch` tail: rerun `compute_coverage_gap` after `llm_rerank`, emit `RECALL_GAP_SCORE_AFTER`)
- Modify: `slopmortem/tracing/events.py` (add `RECALL_GAP_SCORE_AFTER`)
- Create: `tests/stages/test_recall_persist_gap_closures.py`

### Steps

- [ ] **Step 5.1: Add the new SpanEvent member**

```python
# slopmortem/tracing/events.py
RECALL_GAP_SCORE_AFTER = "recall.gap_score_after"
```

- [ ] **Step 5.2: Failing test — slop classifier is not invoked for recall entries**

```python
async def test_recall_entry_bypasses_slop_classifier(...):
    slop_calls = []
    class CountingSlop:
        async def score(self, body):
            slop_calls.append(body)
            return 0.99  # would normally quarantine
    entry = RawEntry(source=SOURCE_LLM_RECALL, ...)
    await persist_recall_entry(entry, slop_classifier=CountingSlop(), ...)
    assert slop_calls == []  # never called for recall
    # ...and the entry persisted (slop's would-be quarantine ignored)
    assert len(fake_corpus.upserted) == 1
```

- [ ] **Step 5.3: Failing test — `RECALL_GAP_SCORE_AFTER` emits with post-recall qualifying count**

```python
async def test_post_recall_gap_score_emits_after_rerank(...):
    captured = []
    monkeypatch.setattr(Laminar, "event", lambda *, name, attributes=None: captured.append((name, attributes)))
    # Set up a query where coverage_gap is True; recall persists 2 entries;
    # re-retrieve+re-rerank now produces 5 qualifying candidates.
    await run_query(input_ctx, ...)
    after = next((attrs for name, attrs in captured if name == str(SpanEvent.RECALL_GAP_SCORE_AFTER)), None)
    assert after is not None
    assert after["qualifying"] == "5"
    assert after["required"] == str(config.N_synthesize)
```

- [ ] **Step 5.4: Run the two tests, confirm they fail**

Run: `uv run pytest tests/stages/test_recall_persist_gap_closures.py -v`
Expected: all FAIL.

- [ ] **Step 5.5: Plumb `skip_slop` through `_classify_phase`**

```python
# slopmortem/ingest/_ingest.py — _classify_phase signature add
skip_slop: bool = False,

# inside the loop body, replace the slop-score call:
if skip_slop:
    score = 0.0  # bypassed for verified recall entries
else:
    score = await classify_one(entry, slop_classifier=slop_classifier, ...)
```

`classify_one` and the quarantine path stay intact; the journal idempotency check and the post_mortems disk write are unaffected.

- [ ] **Step 5.6: Wire `skip_slop=True` from `persist_recall_entry`**

The snippet below adds the Task 5 flag. Preserve the `deathness_verdict=deathness_verdict` pass-through Task 3 already wired into the `write_phase` call — don't drop it when copying this snippet. No `single_chunk` flag — the standard chunker handles recall bodies (see "Why no single-chunk path" in this task's preamble).

```python
# slopmortem/stages/recall_persist.py
keepers = await classify_phase(
    [entry],
    enrichers=(),
    slop_classifier=slop_classifier,
    journal=journal,
    config=config,
    post_mortems_root=post_mortems_root,
    dry_run=False,
    force=False,
    progress=progress,
    result=result,
    skip_slop=True,  # L5 is the stricter gate; slop tuned on a different body shape
)
# ...
await write_phase(
    keepers,
    fanout,
    journal=journal,
    corpus=corpus,
    embed_client=embed_client,
    llm=llm,
    config=config,
    post_mortems_root=post_mortems_root,
    force=False,
    sparse_encoder=sparse_encoder,
    progress=progress,
    result=result,
    verification_tier=tier,
    deathness_verdict=deathness_verdict,  # added in Task 3 — keep
)
```

- [ ] **Step 5.7: Add the post-recall gap measurement in `_run_recall_branch`**

```python
# slopmortem/pipeline.py — at the tail of _run_recall_branch, after llm_rerank
gap_after = compute_coverage_gap(
    retrieved=new_retrieved,
    ranked=new_reranked.ranked,
    pitch_sector=facets.sector,
    min_similarity_score=config.min_similarity_score,
    n_synthesize=config.N_synthesize,
)
if Laminar.is_initialized():
    Laminar.event(
        name=str(SpanEvent.RECALL_GAP_SCORE_AFTER),
        attributes={
            "qualifying": str(gap_after.qualifying),
            "required": str(gap_after.required),
            "gap_closed": str(not gap_after.gap),
            "pitch_sector": facets.sector,
        },
    )
```

The `gap_closed` derived attribute makes the join-on-trace query trivial (one boolean per query, no before/after subtraction needed).

- [ ] **Step 5.8: Re-run Task 0 (retrieval-survival pre-flight) against the now-chunked recall path**

Run: `uv run pytest tests/stages/test_recall_retrieval_survival.py -v`
Expected: PASS. Task 0 was written against the combined-body construction and is the regression guard for "does a real recall entry still survive retrieve+rerank after the Task 5 changes." If it fails here, the chunker's behavior on the combined body is the issue — debug before continuing.

- [ ] **Step 5.9: Run all gap-closure tests, confirm green**

Run: `uv run pytest tests/stages/test_recall_persist_gap_closures.py -v`
Expected: all PASS.

- [ ] **Step 5.10: Re-run prior task tests to confirm no regression**

Run: `uv run pytest tests/stages/ tests/cli/ -v`
Expected: still PASS. Particularly verify the existing `test_recall_persist.py` (Task 4) still passes with the new `skip_slop` flag defaulting to False on the non-recall ingest path.

- [ ] **Step 5.11: Run typecheck, lint, full test suite**

Run: `just typecheck && just lint && just test`
Expected: all pass.

- [ ] **Step 5.12: Commit**

```
recall: skip slop + post-recall gap measurement
```

---

## Task 6 — Synthesis read-side wiring for tri-state verdict

**Goal:** Make the synthesis stage verdict-aware so a `struggling` admit produces a "currently struggling" report (forward-looking distress signal) instead of a "failed startup" report (post-mortem). Without this, Task 3's tri-state lands a struggling-but-alive vendor in the user-facing slopmortem report as a dead company — a correctness regression on what binary `died: bool` previously did right by rounding-and-dropping.

**Scope decision: only `synthesize.j2` + `synthesize.py` change.** `consolidate_risks` operates on per-candidate `lessons_for_input` text already framed by synthesis; verdict context flows through naturally as prose. Forcing a second prompt change would double the cassette-record cost without adding signal the consolidator can't already infer from the lesson text. If a future eval shows consolidate over-weighting struggling-derived lessons, that's a follow-up plan.

**Pros/cons:**

- *Pro:* tri-state finally pays for itself — struggling vendors land in the report labeled correctly, not as failed startups. Removes the user-facing correctness bug that ships if Tasks 1-5 land without read-side wiring.
- *Pro:* one cassette re-record covers both Task 3's `recall_deathness` SHA change and Task 6's `synthesize` SHA change. Cost stays at one $2 ceiling.
- *Con:* synthesis prompt grows. The "struggling" framing rule joins the existing `llm_recall` source rule and the security clause; any additional rule increases the chance Haiku/Sonnet drift on one of them. Mitigated by the cassette re-record (which calibrates against current model state) and by limiting the new rule to one paragraph.
- *Con:* schema unchanged — `failure_causes` field still shipped for struggling vendors. The prompt rule re-frames its semantic meaning ("current distress signals" rather than "post-mortem causes") rather than splitting the schema. Splitting would force a downstream model and renderer change for marginal clarity gain.

**Files:**

- Modify: `slopmortem/stages/synthesize.py` — `synthesize_prompt_kwargs` adds `deathness_verdict` derived from `candidate.payload.deathness_verdict`. Default to `"dead"` when None (back-compat: existing crunchbase/web rows have no verdict and were already-curated post-mortems, so "dead" is the correct legacy assumption).
- Modify: `slopmortem/llm/prompts/synthesize.j2` — Trusted facts block adds `deathness_status: {{ deathness_verdict }}`. New rule explaining how `struggling` reframes `failure_causes` and `lessons_for_input`.
- Modify: `slopmortem/render.py` — when a synthesis row's source candidate carries `deathness_verdict == "struggling"`, the rendered section heading reads "currently struggling" rather than the default failure-case framing. (Locate the per-synthesis section in `render.py`; if `Synthesis` doesn't carry the verdict, plumb it through `Synthesis.from_llm` similar to `source`.)
- Modify: `slopmortem/models.py` — `Synthesis` gains `deathness_verdict: Literal["dead", "struggling"] | None = None`. `Synthesis.from_llm` accepts a new kwarg `deathness_verdict` that the synthesize stage passes from `candidate.payload.deathness_verdict`. Default `None` preserves existing call sites (tests + crunchbase rows) without migration.
- Modify: `tests/stages/test_synthesize.py` — new test: a candidate with `deathness_verdict="struggling"` produces a synthesis whose prose contains the "currently struggling" framing AND whose `Synthesis.deathness_verdict` field round-trips. Existing tests with no verdict default to `None` → renderer default behavior.
- Modify: `tests/test_render.py` (or wherever the renderer is tested) — assert struggling vendors get the "currently struggling" heading.
- Re-record: `just eval-record` once at the end of Task 6 — covers Task 3's `recall_deathness` SHA AND Task 6's `synthesize` SHA in one $2 ceiling.

### Steps

- [ ] **Step 6.1: Failing test — synthesis prose for a struggling candidate is forward-looking**

```python
# tests/stages/test_synthesize.py
async def test_synthesize_struggling_candidate_uses_distress_framing(...):
    """A candidate with deathness_verdict='struggling' must produce synthesis
    prose framed as ongoing distress, not as a historical post-mortem.

    The contract: the LLM sees a 'deathness_status: struggling' line in
    Trusted facts and the prompt's struggling-framing rule, and emits
    failure_causes / lessons_for_input that read as forward-looking risks.
    """
    payload = make_candidate_payload(
        name="Acme Security",
        deathness_verdict="struggling",
        # ... other defaults ...
    )
    candidate = Candidate(canonical_id="acme-001", payload=payload, ...)
    fake_llm = FakeLLMClient([
        # The fake echoes the prompt back so the test can inspect the rendered prompt.
        FakeCompletion(text=_make_struggling_synthesis_json()),
    ])
    syn = await synthesize(candidate, ctx=..., llm=fake_llm, config=...)
    rendered_prompt = fake_llm.captured_prompts[0]
    assert "deathness_status: struggling" in rendered_prompt
    assert "currently struggling" in rendered_prompt.lower() or "ongoing distress" in rendered_prompt.lower()
    assert syn.deathness_verdict == "struggling"
```

- [ ] **Step 6.2: Plumb `deathness_verdict` through `synthesize_prompt_kwargs`**

```python
# slopmortem/stages/synthesize.py
def synthesize_prompt_kwargs(candidate: Candidate, *, pitch: str) -> dict[str, Any]:
    payload = candidate.payload
    facets = payload.facets
    return {
        "pitch": pitch,
        "candidate_id": candidate.canonical_id,
        "candidate_name": payload.name,
        "candidate_body": payload.body,
        "source": payload.source or "unknown",
        # Default to "dead" when None preserves the legacy assumption: pre-Task-3
        # crunchbase/web rows were already-curated post-mortems and the prompt's
        # post-mortem framing is correct for them.
        "deathness_status": payload.deathness_verdict or "dead",
        "founding_date": payload.founding_date.isoformat() if payload.founding_date else None,
        "failure_date": payload.failure_date.isoformat() if payload.failure_date else None,
        "sub_sector": facets.sub_sector,
        "customer_type": facets.customer_type,
        "geography": facets.geography,
        "monetization": facets.monetization,
        "product_type": facets.product_type,
        "price_point": facets.price_point,
    }
```

- [ ] **Step 6.3: Update `synthesize.j2`**

Add to the system block (after the existing `llm_recall` rule, before the SECURITY clause):

```jinja
- `deathness_status` is one of "dead" or "struggling". When "dead", treat
  the candidate as a completed post-mortem: `failure_causes` lists what
  killed the company, `lessons_for_input` are imperative one-liners
  drawn from a closed history. When "struggling", the company is still
  operating but visibly impaired — frame `failure_causes` as the current
  distress signals (active layoffs, restructuring, missed payroll, etc.)
  and `lessons_for_input` as forward-looking risks the new founder
  should pre-empt. `where_diverged` and `why_similar` keep their normal
  meanings in both cases. Do NOT claim the company died or has shut
  down when `deathness_status` is "struggling".
```

Add to the Trusted facts block in the user message:

```jinja
- deathness_status: {{ deathness_status }}
```

- [ ] **Step 6.4: Thread the verdict into `Synthesis`**

```python
# slopmortem/models.py — extend Synthesis
class Synthesis(BaseModel):
    # ... existing fields ...
    deathness_verdict: Literal["dead", "struggling"] | None = None

    @classmethod
    def from_llm(
        cls,
        llm: LLMSynthesis,
        *,
        founding_date: date | None,
        failure_date: date | None,
        sources: list[str],
        injection_detected: bool,
        source: str | None,
        deathness_verdict: Literal["dead", "struggling"] | None = None,
    ) -> Synthesis:
        # ... existing body, plus pass deathness_verdict into the constructor ...
```

```python
# slopmortem/stages/synthesize.py — in synthesize(), at the return:
return Synthesis.from_llm(
    llm_parsed,
    founding_date=candidate.payload.founding_date,
    failure_date=candidate.payload.failure_date,
    sources=candidate.payload.sources,
    injection_detected=injection_detected,
    source=candidate.payload.source,
    deathness_verdict=candidate.payload.deathness_verdict,
)
```

- [ ] **Step 6.5: Wire the renderer**

Locate the per-synthesis section header in `slopmortem/render.py`. When `synthesis.deathness_verdict == "struggling"`, the heading reads e.g. `## {name} — currently struggling` rather than the default failure-case framing. Keep the change scoped to the heading + any "failed in YYYY" inline label that would otherwise read wrong on a struggling vendor. Do not restructure the section — synthesis prose carries the rest.

- [ ] **Step 6.6: Run Task 6 tests, expect failures pending cassette**

Run: `uv run pytest tests/stages/test_synthesize.py tests/test_render.py -v`
Expected: the synthesis-stage stub-LLM tests PASS; any cassette-backed eval that exercises `synthesize` FAILS with `NoCannedResponseError` because the prompt SHA changed (Step 6.7 fixes).

- [ ] **Step 6.7: Re-record eval cassettes (covers Task 3 + Task 6 prompt SHAs)**

Both `recall_deathness` (Task 3) and `synthesize` (Task 6) have changed prompt SHAs. `just eval-record` re-records the entire eval surface in one run.

Run: `just eval-record`
Expected: cassettes under `tests/fixtures/cassettes/evals/<vendor>/...` refresh. Vendor recordings whose `recall_deathness` JSON shipped `died: bool` now ship `verdict: "dead"|"struggling"|"alive"`; vendor recordings whose `synthesize` prompts didn't include `deathness_status` now do.

Cost ceiling: $2 (`justfile:32-37` `--max-cost-usd 2.0`). Confirm the user has authorized the spend before invoking — CLAUDE.md explicitly says "don't run unprompted".

If `eval-record` overruns the ceiling and aborts mid-record, partial cassettes are committed. Re-run; the cassette layer is replay-or-record, not transactional. A scoped re-record flag remains out of scope (see "What's out of scope").

- [ ] **Step 6.8: Run typecheck, lint, full test suite**

Run: `just typecheck && just lint && just test && just eval`
Expected: all pass. The eval pass at the tail confirms the re-recorded cassettes deserialize cleanly and produce the same eval-pass shape as before.

- [ ] **Step 6.9: Commit**

```
synthesize: verdict-aware prompt + Synthesis.deathness_verdict + render
```

(Cassette refresh lands in the same commit — the cassette files are part of the same prompt-SHA bump and don't make sense to split.)

---

## Cross-cutting checks (after all tasks)

- [ ] **CC.1: Re-run Task 0 retrieval-survival test as the final regression guard**

Run: `uv run pytest tests/stages/test_recall_retrieval_survival.py -v`
Expected: PASS. Confirms that all of Tasks 1-6 together still leave a real recall entry able to land in `top_n` after persist. If this fails after passing in Task 0 and Task 5, something downstream of persistence regressed retrieval — bisect by task before declaring done.

- [ ] **CC.2: Run the full eval against the offline cassette set**

Run: `just eval`
Expected: passes. Recall-fallback eval cases should show admit-rate change consistent with the fixes (more vanished-vendor admits now that homepage HEAD no longer gates and Wayback is advisory, more paywalled-citation admits via the L2 HEAD→GET fallback, fewer sidebar-bleed false admits via Task 1, fewer paywall-stub false admits via Task 1's body-length gate).

- [ ] **CC.3: Confirm import-linter still passes**

Run: `uv run lint-imports`
Expected: pass. `stages.recall_verify` imports `extract_clean` from the public `slopmortem.corpus` re-export (NOT `corpus._extract` — that one is in `corpus-leaf.forbidden_modules` for source `slopmortem.stages`, see `.importlinter:23-37`).

- [ ] **CC.4: Update `slopmortem.toml` defaults block**

Confirm the new key `recall_struggling_min_confidence` is documented in `slopmortem.toml` with a comment explaining the difference from `recall_deathness_min_confidence`.

- [ ] **CC.5: Update `docs/architecture.md` recall section**

Add one paragraph: "The verifier no longer gates on the homepage URL. On the evidence URL, HEAD failure falls through to GET so paywalled and anti-bot citations don't false-drop on hosts that respond to GET but not HEAD. Wayback enrichment is advisory — its failure or absence never drops a candidate, since archive.org coverage is patchy enough that 'no snapshot' is not a reliable realness signal; `WaybackEnricher.enrich()` is called directly because the enricher already retries 3× internally and swallows transient errors. The persisted body always contains the news article (the citation L5 verifies against); when Wayback anchors, its snapshot is prepended under a 'Vendor description (archived)' section so synthesis can read both the value-prop and the death narrative from a single entry. Persisted recall entries flow through the standard heading-aware chunker — section markers keep boundaries clean, each chunk inherits the same payload so synthesis still sees the full combined body, and per-chunk vectors give the marketing-copy section a focused signal that matches pitches better than a diluted whole-body vector would. The slop classifier is bypassed for recall entries — L5 is the stricter gate operating on the death-citation substrate. L5 is tri-state (`dead` / `struggling` / `alive`); admitted verdicts ride the qdrant payload as `deathness_verdict` and the synthesis prompt reads them, so a struggling vendor produces a forward-looking distress report rather than a post-mortem. After recall persists and re-rerank runs, `compute_coverage_gap` fires a second time emitting `RECALL_GAP_SCORE_AFTER`, so prod telemetry can answer 'did recall close the gap on this query' without an offline eval re-run."

- [ ] **CC.6: Polish pass**

Dispatch `post-implementation-polish` (per Agent Assignments) — covers Tasks 0-6.

---

## What's out of scope

- **i18n keyword packs (FR/DE/ES/JA).** Plan B's stance held: the F-prime English vocabulary expansion in Task 1 covers most of the niche-vendor population through English news aggregators. Revisit only if eval shows a measurable miss.
- **Recall-batch rollback CLI (`slopmortem purge --source ...`).** Source-scoped delete of journal rows + Qdrant chunks was scoped (filter on `provenance_id`, journal `delete_by_source` in one transaction, `--dry-run` / `--yes` / `--since` filters) and deferred to keep this plan focused on verifier correctness. Until it lands, recovery from a bad recall batch falls back to `just nuke` (full corpus). Build it before the next prompt or threshold change that could regress recall admits — Task 3's prompt rewrite is exactly the kind of change that motivates having it.
- **Scoped cassette re-record.** `just eval-record` re-records the full eval surface; there is no per-prompt scope flag. A new flag on `slopmortem.evals.runner` (filter by prompt template name) would shrink the unified Task 6 re-record cost, but designing it is its own piece of work. Task 6 batches Task 3's `recall_deathness` and Task 6's `synthesize` SHA changes into one $2 record; further prompt edits in this surface area should batch with the next plan that ships any prompt change rather than re-record solo.
- **Consolidate-risks verdict awareness.** `consolidate_risks.j2` doesn't read `deathness_verdict` directly. The verdict context flows through `Synthesis.lessons_for_input` text already framed by synthesis (Task 6), so consolidate inherits the distinction via prose. If a future eval shows consolidate over-weighting struggling-derived lessons (e.g. high-severity risks driven mostly by struggling vendors should perhaps be medium), a follow-up plan can pass per-lesson verdict tags.
- **TTL re-verify on llm_recall entries.** Already deferred in `2026-05-09-recall-fallback-improvements.md` — needs a journal schema migration.
- **Wayback rate-limit detection beyond `WaybackEnricher`'s built-in retries.** The enricher's 3-attempt schedule with exponential backoff handles the common transient case; if recurring rate-limits become a real signal that escapes the enricher, build a separate adaptive-backoff path then.
- **Recall-specific single-chunk Qdrant persist.** Investigated and rejected (see Task 5 preamble): chunking does not affect what synthesis sees because every chunk inherits the same payload, and per-chunk vectors actually rank pitch-similarity better than a diluted whole-body vector would. The standard heading-aware chunker is the right path; section markers from `_combine_recall_body` keep boundaries clean.

---

## Notes for the implementer

- Each task is one commit. Use the project's terse commit style (`recall:`, `cli:`, `journal:`, `ingest:`). No `Co-Authored-By` trailers.
- Don't bump pinned models. None of these tasks need to.
- All new SpanEvents go in the closed StrEnum at `slopmortem/tracing/events.py` — free-form strings get rejected.
- The L5 LLM call counts against the per-query `Budget`. The tri-state schema doesn't change call count or token budget; verify the budget tracker still sees it (it should, via the shared `Budget` injected into the LLMClient).
- Import-linter contract: `stages` may not import from `corpus._*` private modules. `extract_clean` is already re-exported via `slopmortem.corpus.__init__` (used by `slopmortem/ingest/_helpers.py:10`); Task 1 imports it from the public surface.
