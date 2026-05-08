# LLM Recall Fallback — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or `/team-feature` to implement this plan task-by-task, per the Execution Strategy below. Steps use checkbox (`- [ ]`) syntax — these are **persistent durable state**, not visual decoration. The executor edits the plan file in place: `- [ ]` → `- [x]` the instant a step verifies, before moving on. On resume (new session, crash, takeover), the executor scans existing `- [x]` marks and skips them — these steps are NOT redone. TodoWrite mirrors this state in-session; the plan file is the source of truth across sessions.

**Goal:** Close the vendor-side coverage gap that the Hacken/Extractor pitch (and any future niche-B2B pitch) hits today, where retrieval surfaces vertically-mismatched comparables (Norse Corp / Carbon Black for a `crypto_web3` pitch) because the corpus has no real Web3-security analogs and the embeddings are vertically promiscuous.

**Architecture summary:**

1. **Strict sector filter** at retrieve time (`MatchAny([pitch.sector, "other"])`) excludes wrong-vertical entries while keeping the misclassification-tolerant `other` bucket reachable. Gated by `config.strict_sector_filter`; default off.
2. **Coverage gate** runs after the first rerank. Fires on `len==0`, low mean similarity, OR vertical-axis collapse on the rerank's `business_model` + `market` axes.
3. **LLM recall stage** asks Opus 4.7 to name dead/struggling vendors in the pitch's vertical, with citations. Output is structured JSON.
4. **Verifier (L1–L4)** validates each suggestion: schema → URL HEAD on homepage + evidence_url → `evidence_url` body must contain the name AND a death/struggle keyword → Wayback snapshot of homepage as a corroborating signal (sets `verification_tier="wayback_anchored"` when present, `"evidence_only"` when absent — does not gate). News-article anchoring keeps robots-excluded and underrepresented vendors reachable; those are exactly the populations recall is meant to surface.
5. **Persistence** writes verified suggestions as regular `RawEntry` rows with `source=llm_recall`. Cache is the corpus itself — future pitches hit them through normal vector search.
6. **Telecom backfill** is deferred to its own plan — see Task 8. The taxonomy value `telecom` was added 2026-05-08 in commit `9d24034`, but migrating the 7 already-ingested `other`-tagged entries needs a new code path (re-extract facets + update Qdrant payload) that doesn't exist yet.

**Gap relative to spec:** the design spec mentions extending an existing `--purge-source <name>` mechanism so a bad recall batch can be evicted. That mechanism does not exist in the codebase today (no matches for `purge_source` / `--purge-source` anywhere under `slopmortem/`). It is **not built in this plan**. The first verified recall hit makes a permanent corpus entry until a separate plan adds the eviction path. If the verifier is doing its job this is fine; if recall lands and a hallucination slips through, an emergency `slopmortem nuke` is the only recovery. Track in a follow-up if the team wants a softer eviction tool.

**Tech Stack:** Python 3.13, anyio, httpx (via existing `safe_get` / new `safe_head`), pydantic v2, basedpyright strict, pytest. Reuses existing `WaybackEnricher` (`slopmortem/corpus/sources/wayback.py`), `LLMClient`, `Budget`, journal+Qdrant ingest tail, slop classifier.

## Execution Strategy

**Subagents** — Tasks within each batch can run in parallel (no shared file ownership). Across batches must run sequentially because later batches import models/types from earlier ones.

## Task Dependency Graph

- Task 1 [AFK]: Strict sector filter (config + Corpus protocol + qdrant_store + tests) → depends on `none` → batch 1
- Task 2 [AFK]: Coverage gate (pure function + calibration fixtures + tests) → depends on `none` → batch 1
- Task 3 [AFK]: `RecallSuggestion` model + recall prompt template + recall stage + cassette → depends on `none` → batch 1
- Task 4 [AFK]: Verifier (L1–L4) using `safe_head` + `WaybackEnricher` + tests → depends on `Task 3` → batch 2
- Task 5 [AFK]: Persistence helper (writes through existing journal+qdrant tail) + tests → depends on `Task 3` → batch 2
- Task 6 [AFK]: Pipeline wiring (recall branch + second retrieve+rerank + meta flags) → depends on `Tasks 1, 2, 3, 4, 5` → batch 3
- Task 7 [AFK]: CLI flag `--enable-llm-recall` + `SOURCE_LLM_RECALL` constant + reliability rank → depends on `Task 6` → batch 4
- Task 8: Telecom backfill — DEFERRED to a separate plan; not built here.
- Polish: post-implementation-polish → depends on `Task 7` → batch 5

## Agent Assignments

- Tasks 1–7: python-development:python-pro
- Polish: post-implementation-polish → general-purpose

---

## File Structure

**New:**

- `slopmortem/stages/llm_recall.py` — `detect_coverage_gap()` pure predicate; `llm_recall()` async callable that builds the prompt, calls the LLM, parses to `list[RecallSuggestion]`, and caps to `recall_max_suggestions_per_pitch`.
- `slopmortem/stages/recall_verify.py` — `verify_suggestion()` running L1–L4 layers; `verify_and_persist_all()` fans out across suggestions via `gather_resilient`.
- `slopmortem/stages/recall_persist.py` — `persist_recall_entry()` constructs a `RawEntry` and routes it through the existing ingest tail (slop classify → facet/summarize → journal + qdrant write).
- `slopmortem/llm/prompts/llm_recall.j2` — Opus prompt: pitch + facets + current top-N → JSON list of `RecallSuggestion`-shaped objects with `[]` on uncertainty.
- `tests/stages/test_coverage_gate.py` — pure-fn tests + 3-fixture calibration corpus.
- `tests/stages/test_llm_recall.py` — stub-LLM tests + recorded Opus cassette for the Hacken pitch.
- `tests/stages/test_recall_verify.py` — L1–L4 unit tests with mocked `safe_head`, mocked `safe_get` (evidence body), and mocked `WaybackEnricher` (corroboration only).
- `tests/stages/test_recall_persist.py` — idempotency + dedup test.
- `tests/test_pipeline_recall_fallback.py` — end-to-end with fake corpus, fake LLM, fake verifier.
- `tests/fixtures/coverage_gate/` — three calibration pitch+candidate pairs (Hacken→fire, Splunk-OT→quiet, one TBD third).
- `tests/fixtures/cassettes/recall/llm_recall_hacken.yaml` — recorded Opus call.

**Modified:**

- `slopmortem/config.py` — add `strict_sector_filter: bool = False`, `enable_llm_recall: bool = False`, `force_llm_recall: bool = False`, `model_recall: str = "anthropic/claude-opus-4-7"`, `max_tokens_recall: int = 4096`, `recall_max_suggestions_per_pitch: int = 8`. (Note: `recall_coverage_axis_threshold` is *not* added — the new unified trigger doesn't use vertical-axis collapse. No per-stage `recall_budget_usd_cap` either — the pipeline-level `Budget` plus `max_tokens_recall` already bound spend; a third unenforced cap would just be config noise.)
- `slopmortem/corpus/_store.py` — extend `Corpus.query()` Protocol with `strict_sector_filter: bool` parameter.
- `slopmortem/corpus/_qdrant_store.py` — implement strict-sector-filter logic in `query()` around the existing `_build_recency_filter` call (line ~180), AND-ing the new sector filter. The `MatchAny([pitch.sector, "other"])` shape lives here.
- `slopmortem/stages/retrieve.py` — pass `config.strict_sector_filter` through to `corpus.query()`.
- `slopmortem/corpus/sources/_names.py` — add `SOURCE_LLM_RECALL: Final = "llm_recall"`.
- `slopmortem/stages/__init__.py` — re-export `detect_coverage_gap`, `llm_recall`, `verify_and_persist_all`, `persist_recall_entry`.
- `slopmortem/pipeline.py` — insert recall branch after first `llm_rerank`; new `QueryPhase.RECALL`; second `retrieve` + `llm_rerank` if recall produced any verified rows.
- `slopmortem/models.py` — add `RecallSuggestion` (LLM output shape) plus `RecallSuggestionList` wrapper (root-must-be-object for OpenRouter strict `json_schema`); add `verification_tier: Literal["wayback_anchored", "evidence_only"] | None = None` to `CandidatePayload`; add `PipelineMeta.coverage_gap: bool`, `PipelineMeta.recall_used: bool`, `PipelineMeta.recall_persisted_count: int`.
- `slopmortem/http.py` — add `safe_head(url, *, timeout)`. Mirrors `safe_get`'s SSRF guard, issues `HEAD`.
- `slopmortem/ingest/_helpers.py` — extend `_RELIABILITY_RANK` with `("llm_recall", 6)`.
- `slopmortem/cli/_query_cmd.py` — add `--enable-llm-recall` and `--force-llm-recall` boolean flags; thread `config.enable_llm_recall=True` and `config.force_llm_recall=True` respectively. The two are independent — `--force-llm-recall` doesn't imply `--enable-llm-recall` (they OR-combine in the pipeline check).
- `slopmortem/render.py` — surface `coverage_gap`, `recall_used`, `recall_persisted_count` in the rendered report's pipeline-meta block (one line each, only when non-default).

**Already done (taxonomy prerequisite):**

- `slopmortem/corpus/taxonomy.yml` — `telecom` added between `marketing_adtech` and `other` in commit `9d24034`. Pydantic `SectorLit` literal picks it up automatically at module load.

---

## Decisions

### Strict sector filter as `MatchAny([pitch.sector, "other"])`, not pure `MatchValue(pitch.sector)`

- Pros of `MatchAny+other`: `other`-tagged entries stay reachable. Robust to misclassification at ingest (the 7 telecom-shaped `other` entries are an empirical example).
- Pros of pure `MatchValue`: maximum strictness; no `other` noise on clean-sector queries.
- Cons of pure `MatchValue`: every `other` entry becomes a false negative until reclassified. The `other` bucket holds ~2% of the corpus; permanent hiding is too costly.
- **Pick `MatchAny+other`.** Add `strict_sector_filter_excludes_other: bool = False` config flag for ops who want maximum strictness once the `other` bucket has been audited.

### Skip the strict filter when `pitch.sector == "other"`

- Pros: pitch sector is unknown — filtering on `other` would either match the 52 corpus `other` entries (noise) or nothing. Either is worse than today's boost-only behavior.
- Cons: cross-sector pitches don't get the strict-filter benefit.
- **Pick skip.** Three short-circuits in `_build_sector_filter`: `not pitch_sector`, `pitch_sector == "other"`, `not config.strict_sector_filter`. Each returns `None` (no filter applied).

### Recall trigger is unified count-based, plus a force-on knob

**Trigger:** `count(c ∈ ranked where c.perspective_scores.mean() ≥ config.min_similarity_score AND (pitch.sector == "other" OR retrieved_by_id[c.candidate_id].payload.facets.sector ∈ [pitch.sector, "other"])) < config.N_synthesize`

In English: count rerank candidates that are both *high-quality* (mean perspective ≥ `min_similarity_score`) and *in-sector* (sector matches the pitch, or `"other"` as the misclassification safety valve, or the pitch sector itself is `"other"` so the check is degenerate). If fewer than `N_synthesize` qualify, the report cannot be filled cleanly — fire recall.

**Force knob:** `config.force_llm_recall: bool = False`. When `True`, recall fires every query regardless of trigger state. Final firing rule: `should_fire = (config.enable_llm_recall and trigger_fired) or config.force_llm_recall`. The two flags are OR-combined; `force_llm_recall=True` does not require `enable_llm_recall=True` (operators using `--force-llm-recall` for evaluation, cassette recording, or thin-corpus enrichment shouldn't have to also set `--enable-llm-recall`).

**Why this trigger over the old three-signal OR:**

- One signal replaces three — `len==0` is the special case `qualifying_count==0`; the old "weak match" mean check is replaced by per-candidate thresholding (catches the 3-strong-2-weak case the mean would hide); vertical-axis collapse is replaced by exact sector membership (deterministic, no rerank-axis tuning).
- Closes the strict-filter sparse-result gap. Pitch matches `crypto_web3`, strict filter narrows to 1 high-quality match — old triggers all stayed quiet (len≠0; mean≥4.0; sector matched). New trigger fires because `1 < 5`.
- Operator intent is verbatim: "do I have N comparables I'd actually rank?" If not, fire recall.
- Per-candidate min_similarity beats mean-of-top-n. Today's `mean(top_n.similarity) < 4.0` is fooled by 3-strong + 2-weak averaging to 4.5; per-candidate counting surfaces the gap.

**Why the force knob:**

- Operators recording cassettes, calibrating thresholds, or running on thin/new corpora want recall to fire deterministically without contriving a sparse retrieval to hit the gate. A `--force-llm-recall` CLI flag (threading to `config.force_llm_recall=True`) is the clean expression.
- OR-combined with the trigger so `enable_llm_recall=False, force_llm_recall=True` still fires — useful when the operator wants to test recall in isolation without committing to trigger-driven recall in production.
- Budget protection: the pipeline-level `Budget` (shared across every LLM call) is the single spend gate. Worst-case per recall call with `max_tokens_recall=4096` against Opus 4.7 is ~$0.42 (≈$0.37 output + ≈$0.05 input); `force_llm_recall=True` doesn't bypass that gate. No per-stage cap config — see File Structure note.

**Behavior change for existing `enable_llm_recall=true` users:**

The unified trigger fires more aggressively than the old `len==0` ∪ `mean<4` ∪ vertical-collapse OR. A query with 3 strong matches in-sector previously stayed quiet (mean=high, vertical-axis OK); under the new trigger it fires (`3 < 5`). This is intentional and matches the documented promise of recall ("fill the slots when retrieval can't"), but it is a deliberate ship — not a stealth shift. Calibration fixtures (`hacken.json`, `splunk_ot.json`, third) must be re-baselined before merge.

**Removed config:** `recall_coverage_axis_threshold` is no longer read by the trigger and is removed in this revision.

**Pick:** unified trigger + `force_llm_recall` knob, OR-combined with `enable_llm_recall && trigger_fired`.

### Opus 4.7 for the recall call

- Pros: lowest hallucination rate on company-name recall in the Anthropic family. Willing to refuse with `[]`. Verification gate L1–L4 is the backstop, not a substitute — model that confidently confabulates company URLs wastes verification cycles.
- Cons: 5–10× per-call cost vs Sonnet. Acceptable because the gate fires rarely, and verified entries cache via the corpus.
- **Pick Opus.** Make it `config.model_recall` so ops can downgrade if budget pressure lands.

### Verifier anchors on `evidence_url` body, not the Wayback homepage snapshot

The first iteration of this design used `WaybackEnricher` on the homepage as the L3 gate. That fails on populations the recall path is *most* needed for: robots.txt-excluded vendors (common for small B2B), non-Western / underrepresented vendors, recently-dead sites IA never indexed, and acquired companies whose homepage URL drifted post-merge. Dropping all of those as "unverified" defeats the purpose.

- **L3 anchor is `safe_get(evidence_url)` body.** The suggestion's name (case-insensitive substring) AND at least one death/struggle keyword must appear. Keywords (terminal): `shutdown`, `shut down`, `closed`, `defunct`, `dissolved`, `bankrupt`, `acquired`, `wound down`, `ceased`. Keywords (distress): `layoffs`, `restructuring`, `struggling`, `missed payroll`, `downsizing`, `troubled`. News articles encode both naturally; that's the citation shape Opus is asked to produce.
- **L4 keeps Wayback as a corroborating signal, not a gate.** If the homepage has a snapshot whose body contains the name, set qdrant payload `verification_tier="wayback_anchored"`. Otherwise `"evidence_only"`. Both tiers are accepted; tier is surfaced on the report and audit-queryable in qdrant.
- **L2 unchanged.** Both URLs must `HEAD < 400`. Squatters/parking pages still need to fail at L3 — they will, because they don't carry the death-keyword signal.

Trade-off: one extra `safe_get` per suggestion. Mitigated because L3 short-circuits early on bad URLs and the suggestion cap is 8.

- Pros: surface the population recall is meant for; news-article evidence is the same artifact a human reviewer would cite.
- Cons: trusts current liveness of the news source; one more network call per suggestion.
- **Pick `evidence_url` anchor + Wayback-as-corroboration.**

### Persisted entries flow through the slop classifier

- Pros: same quality bar as crawler entries. If the verified Wayback body is in fact spam (unlikely but possible — squatted domain), slop classifier catches it.
- Cons: 1 extra Haiku call per recall hit (~$0.0008 each).
- **Pick yes.** Cost is bounded by `recall_max_suggestions_per_pitch=8`. Quality consistency wins.

### Single-pass recall, not iterate-until-clear

- Pros: deterministic budget; deterministic latency; no compounding hallucination risk.
- Cons: borderline pitches that yield zero verified hits on first pass have no second chance.
- **Pick single pass.** Surface `coverage_gap=True` in the report so operators see the miss.

### Reliability rank `6` for `llm_recall`

- LLM recall is two derivations from primary (model weights → JSON → Wayback snapshot). Sits below derived reporting (`tavily_news` rank 4, `crunchbase_csv` rank 2) and above dead-letter (rank 9).
- **Pick `6`.** Conservative; reversible.

### Telecom backfill is its own *plan*, not a task in this one

- Pros of including: reclassifying 7 entries restores correct sector tags that the new strict filter would otherwise still skip.
- Cons of including: there is no existing `slopmortem reclassify --provenance-ids` command. `reclassify_quarantined()` (`slopmortem/corpus/_reclassify.py:85`) only walks the on-disk quarantine and re-runs the slop classifier — it can't update sector tags on already-ingested Qdrant points. Building a real per-entry refacet path is its own ~3-task effort (new corpus helper + new CLI command + tests).
- **Pick: defer to its own dated plan.** Task 8 below records the rationale and the to-do list. Nothing in Tasks 1–7 depends on the backfill landing first; the strict sector filter still works correctly for the 45 entries already tagged `other` for legitimate reasons.

---

### Task 1: Strict sector filter

**Files:**

- Modify: `slopmortem/config.py` (add `strict_sector_filter: bool = False` and `strict_sector_filter_excludes_other: bool = False`)
- Modify: `slopmortem/corpus/_store.py` (extend `Corpus.query()` Protocol)
- Modify: `slopmortem/corpus/_qdrant_store.py` (implement filter in `query()`)
- Modify: `slopmortem/stages/retrieve.py` (thread `config.strict_sector_filter` through)
- New test: `tests/corpus/test_qdrant_sector_filter.py`
- Modified test: `tests/test_config.py`

- [x] **Step 1: Write the failing config test**

Append to `tests/test_config.py`:

```python
def test_strict_sector_filter_defaults() -> None:
    from slopmortem.config import Config
    c = Config()
    assert c.strict_sector_filter is False
    assert c.strict_sector_filter_excludes_other is False
```

- [x] **Step 2: Run test to verify it fails**

`uv run pytest tests/test_config.py::test_strict_sector_filter_defaults -v`
Expected: FAIL — attribute missing.

- [x] **Step 3: Add config keys**

In `slopmortem/config.py`, near the existing retrieval-related fields:

```python
    strict_sector_filter: bool = False
    strict_sector_filter_excludes_other: bool = False
```

- [x] **Step 4: Run config test to confirm pass**

`uv run pytest tests/test_config.py -v`

- [x] **Step 5: Extend `Corpus.query()` Protocol**

In `slopmortem/corpus/_store.py`, add `strict_sector_filter: bool` and `strict_sector_filter_excludes_other: bool` keyword-only parameters to the `query()` method signature. Keep them required (Protocol implementations must accept them).

- [x] **Step 6: Write failing test for the qdrant filter shape**

New file `tests/corpus/test_qdrant_sector_filter.py`. Use a mocked `client.query_points` that captures the `query_filter` argument. Assert:

- `strict_sector_filter=False` → no sector clause in filter.
- `strict_sector_filter=True, sector="crypto_web3"` → filter contains `MatchAny(any=["crypto_web3", "other"])` for `facets.sector`.
- `strict_sector_filter=True, sector="other"` → no sector clause (pitch sector is uninformative).
- `strict_sector_filter=True, strict_sector_filter_excludes_other=True, sector="crypto_web3"` → filter contains `MatchValue(value="crypto_web3")`.

- [x] **Step 7: Run test to verify it fails**

`uv run pytest tests/corpus/test_qdrant_sector_filter.py -v`
Expected: FAIL.

- [x] **Step 8: Implement filter in `_qdrant_store.py`**

In `_qdrant_store.py:query()`, near where `_build_recency_filter` is called (line ~180), add:

```python
def _build_sector_filter(
    pitch_sector: str | None,
    *,
    strict: bool,
    exclude_other: bool,
) -> Filter | None:
    if not strict or not pitch_sector or pitch_sector == "other":
        return None
    if exclude_other:
        match = MatchValue(value=pitch_sector)
    else:
        match = MatchAny(any=[pitch_sector, "other"])
    return Filter(must=[FieldCondition(key="facets.sector", match=match)])
```

AND it with the recency filter:

```python
sector_flt = _build_sector_filter(
    facets.sector,
    strict=strict_sector_filter,
    exclude_other=strict_sector_filter_excludes_other,
)
recency_flt = _build_recency_filter(cutoff_iso=cutoff_iso, strict_deaths=strict_deaths)
query_filter = _and_filters(sector_flt, recency_flt)  # helper that handles None
```

- [x] **Step 9: Run filter test to confirm pass**

`uv run pytest tests/corpus/test_qdrant_sector_filter.py -v`

- [x] **Step 10: Thread config through retrieve stage**

In `slopmortem/stages/retrieve.py:31`, add `strict_sector_filter` and `strict_sector_filter_excludes_other` keyword arguments to `retrieve()`. Pass them to `corpus.query(...)` at line 58.

In `slopmortem/pipeline.py`, where `retrieve(...)` is called (line ~156), pass `strict_sector_filter=config.strict_sector_filter, strict_sector_filter_excludes_other=config.strict_sector_filter_excludes_other`.

- [x] **Step 11: Run full test suite**

`just test`

- [x] **Step 12: Lint + typecheck**

`just lint && just typecheck`

- [x] **Step 13: Commit**

`git add slopmortem/config.py slopmortem/corpus/_store.py slopmortem/corpus/_qdrant_store.py slopmortem/stages/retrieve.py slopmortem/pipeline.py tests/ && git commit -m "retrieve: strict sector filter with MatchAny+other"`

Shipped across commits `fd2dd79` (corpus protocol + qdrant impl), `363c156` (pipeline threading), `f811b7c` (live qdrant test), and polish commits `4c72ff4`, `a3798ba`, `90fb237`. Test lives in `tests/corpus/test_qdrant_store.py::test_strict_sector_filter` (parametrized) rather than the standalone `test_qdrant_sector_filter.py` proposed above — same coverage, more idiomatic placement.

---

### Task 2: Coverage gate

**Files:**

- New: `slopmortem/stages/llm_recall.py` (predicate function only in this task)
- New: `tests/stages/test_coverage_gate.py`
- New: `tests/fixtures/coverage_gate/hacken.json`, `splunk_ot.json`, `<third>.json`

- [x] **Step 1: Build the three calibration fixtures**

Each fixture is a JSON file with three top-level keys: `pitch_sector` (the pitch's facet), `retrieved` (`list[Candidate]` shape — the trigger needs each candidate's `payload.facets.sector`), and `ranked` (`list[ScoredCandidate]` shape, joined to retrieved by id). Plus `expected_gate: bool`:

```json
{
  "pitch_sector": "crypto_web3",
  "retrieved": [
    {
      "canonical_id": "norse_corp",
      "score": 0.78,
      "payload": {
        "name": "Norse Corp",
        "facets": {"sector": "security", "...": "..."},
        "...": "..."
      }
    }
  ],
  "ranked": [
    {
      "candidate_id": "norse_corp",
      "perspective_scores": {
        "business_model": {"score": 4.0, "rationale": "..."},
        "market":         {"score": 5.0, "rationale": "..."},
        "gtm":            {"score": 4.0, "rationale": "..."},
        "stage_scale":    {"score": 4.0, "rationale": "..."}
      },
      "rationale": "..."
    }
  ],
  "expected_gate": true
}
```

The predicate consumes `perspective_scores.mean()` for the per-candidate quality threshold and `retrieved[i].payload.facets.sector` for the in-sector membership check. `candidate_id == canonical_id` (same join semantics as `_join_by_id` at `stages/llm_rerank.py:92`, which `select_top_n_by_similarity` reuses). There is no separate `similarity_score` field on `ScoredCandidate` — the cosine score from retrieval is consumed by the rerank stage and not carried forward.

- `hacken.json`: rebuild from the existing `.slopmortem/runs/20260508T142314Z-...md` run. `pitch_sector="crypto_web3"`. Norse Corp + Carbon Black ranked with `payload.facets.sector="security"` (wrong-vertical noise, the canonical motivator). `expected_gate: true` — qualifying count is 0 because no in-sector candidates pass the quality threshold.
- `splunk_ot.json`: synthetic Splunk-for-industrial-OT pitch. `pitch_sector="security"` (or whichever taxonomy bucket OT defenders sit in). 5+ in-sector candidates with mean perspective ≥ 4.0. `expected_gate: false` — qualifying count ≥ N_synthesize.
- `third.json`: a strict-filter sparse-result case to exercise the new "1 high-quality match" trigger. `pitch_sector="crypto_web3"`, exactly 1 ranked entry with `payload.facets.sector="crypto_web3"` and mean ≥ 4.0. `expected_gate: true` — qualifying count is 1, below N_synthesize=5. This fixture is the regression test for the bug the unified trigger fixes.

- [x] **Step 2: Write the failing tests**

`tests/stages/test_coverage_gate.py`:

```python
import json
from pathlib import Path
import pytest
from slopmortem.models import Candidate, ScoredCandidate
from slopmortem.stages import detect_coverage_gap

FIXTURES = Path(__file__).parent.parent / "fixtures" / "coverage_gate"


def _load(name: str) -> tuple[list[Candidate], list[ScoredCandidate], str, bool]:
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    retrieved = [Candidate.model_validate(item) for item in data["retrieved"]]
    ranked = [ScoredCandidate.model_validate(item) for item in data["ranked"]]
    return retrieved, ranked, data["pitch_sector"], bool(data["expected_gate"])


@pytest.mark.parametrize("name", ["hacken", "splunk_ot", "third"])
def test_calibration_fixture(name: str) -> None:
    retrieved, ranked, pitch_sector, expected = _load(name)
    result = detect_coverage_gap(
        retrieved=retrieved,
        ranked=ranked,
        pitch_sector=pitch_sector,
        min_similarity_score=4.0,
        n_synthesize=5,
    )
    assert result is expected


def test_zero_candidates_fires() -> None:
    assert detect_coverage_gap(
        retrieved=[], ranked=[], pitch_sector="crypto_web3",
        min_similarity_score=4.0, n_synthesize=5,
    ) is True


def test_one_in_sector_high_quality_match_fires() -> None:
    # Strict filter cut to 1 — qualifying_count=1 < N_synthesize=5 — fire.
    # This is the regression test for the bug the unified trigger fixes.
    ...


def test_five_in_sector_high_quality_matches_quiet() -> None:
    # Five candidates, all in pitch sector, all mean >= 4.0 — quiet.
    ...


def test_pitch_sector_other_skips_sector_check() -> None:
    # pitch_sector="other" — count quality-only — five high-quality candidates of any sector → quiet.
    ...


def test_wrong_vertical_noise_fires() -> None:
    # Five candidates with mean >= 6.0 but all in wrong sector → qualifying=0 → fire.
    # Replaces the old vertical-axis-collapse test.
    ...
```

- [x] **Step 3: Run tests to verify failure**

`uv run pytest tests/stages/test_coverage_gate.py -v`
Expected: FAIL — `cannot import name 'detect_coverage_gap'`.

- [x] **Step 4: Implement the predicate**

Create `slopmortem/stages/llm_recall.py` with `detect_coverage_gap()` only (the LLM-call portion lands in Task 3 in the same file).

Note on inputs: `LlmRerankResult.ranked` (`slopmortem/models.py:235`) is `list[ScoredCandidate]`. Each `ScoredCandidate` carries `candidate_id` and `perspective_scores` but **not** the candidate's payload — sector lives on `Candidate.payload.facets.sector` from the retrieve stage. The trigger therefore takes both `retrieved` and `ranked`, joining by `candidate_id == canonical_id` (the same join `_join_by_id` already does at `stages/llm_rerank.py:92`, reused by `select_top_n_by_similarity`). `perspective_scores.mean()` is the four-axis mean used by the existing similarity filter so `config.min_similarity_score` keeps one meaning across the codebase.

```python
from slopmortem.models import Candidate, ScoredCandidate


def detect_coverage_gap(
    *,
    retrieved: list[Candidate],
    ranked: list[ScoredCandidate],
    pitch_sector: str,
    min_similarity_score: float,
    n_synthesize: int,
) -> bool:
    """Fire when fewer than ``n_synthesize`` candidates are both high-quality
    (mean perspective ≥ ``min_similarity_score``) and in-sector.

    A pitch sector of ``"other"`` short-circuits the in-sector check — sector
    is uninformative, so quality alone gates qualifying count.
    """
    by_id: dict[str, Candidate] = {c.canonical_id: c for c in retrieved}
    pitch_sector_unknown = pitch_sector == "other"
    qualifying = 0
    for sc in ranked:
        if sc.perspective_scores.mean() < min_similarity_score:
            continue
        if pitch_sector_unknown:
            qualifying += 1
            continue
        cand = by_id.get(sc.candidate_id)
        if cand is None:
            # Rerank should never emit ids absent from retrieve, but if it
            # does we treat it as a miss rather than crashing the gate.
            continue
        if cand.payload.facets.sector in (pitch_sector, "other"):
            qualifying += 1
    return qualifying < n_synthesize
```

- [x] **Step 5: Run tests to confirm pass**

`uv run pytest tests/stages/test_coverage_gate.py -v`

- [x] **Step 6: Re-export from `stages/__init__.py`**

```python
from slopmortem.stages.llm_recall import detect_coverage_gap as detect_coverage_gap
```

- [x] **Step 7: Lint + typecheck**

`just lint && just typecheck`

- [x] **Step 8: Commit**

`git add slopmortem/stages/llm_recall.py slopmortem/stages/__init__.py tests/stages/test_coverage_gate.py tests/fixtures/coverage_gate/ && git commit -m "stages: detect_coverage_gap predicate + calibration fixtures"`

---

### Task 3: `RecallSuggestion` model + recall stage

**Files:**

- Modify: `slopmortem/models.py` (add `RecallSuggestion`)
- Modify: `slopmortem/config.py` (add recall-related config keys)
- Modify: `slopmortem/stages/llm_recall.py` (extend with `llm_recall()` async function)
- New: `slopmortem/llm/prompts/llm_recall.j2`
- New: `tests/stages/test_llm_recall.py`
- New cassette: `tests/fixtures/cassettes/recall/llm_recall_hacken.yaml`

- [x] **Step 1: Add `RecallSuggestion` + `RecallSuggestionList` models**

In `slopmortem/models.py`:

```python
class RecallSuggestion(BaseModel):
    name: str
    category: str
    status: Literal["dead", "absorbed", "struggling", "bruised"]
    homepage_url: HttpUrl
    failure_year: int = Field(ge=1990, le=2030)
    evidence_url: HttpUrl
    one_liner: str


class RecallSuggestionList(BaseModel):
    """Wrapper for the recall stage's array output, so the schema is a single object.

    Mirrors ``LlmRerankResult``: OpenRouter strict ``json_schema`` mode rejects
    array roots. Wrap the list under ``suggestions`` so ``to_strict_response_schema``
    emits an object schema the API will accept.
    """

    suggestions: list[RecallSuggestion]
```

- [x] **Step 2: Add config keys**

In `slopmortem/config.py`:

```python
    enable_llm_recall: bool = False
    force_llm_recall: bool = False
    model_recall: str = "anthropic/claude-opus-4-7"
    max_tokens_recall: int = Field(default=4096, ge=1)
    recall_max_suggestions_per_pitch: int = Field(default=8, ge=1, le=20)
```

`force_llm_recall=True` fires recall on every query regardless of `enable_llm_recall` and the unified trigger. Use case: cassette recording, eval calibration, thin/new corpora where the trigger would always fire anyway. Spend is gated by the pipeline-level `Budget` shared across every LLM call — there is no per-stage cap.

- [x] **Step 3: Write the prompt template**

`slopmortem/llm/prompts/llm_recall.j2` — Opus prompt: pitch + facets + current top-N → JSON list of `RecallSuggestion`-shaped objects with `[]` on uncertainty. Use the same Jinja `{% block system %}` / `{% block user %}` shape as `title_pre_filter.j2`.

The verifier (Task 4) now anchors on `evidence_url` body, so the prompt must tell Opus that **`evidence_url` is the citation that proves the company failed/struggled — a news article, blog post, court filing, or obituary URL whose body contains the company name AND words describing the failure (shutdown, acquired, layoffs, etc.)**. A homepage or LinkedIn URL is *not* acceptable as `evidence_url`. If Opus cannot produce such a URL, it must omit the suggestion. This is critical: a hallucinated `evidence_url` (or one that just points to a marketing page) means the suggestion will fail L3 silently, wasting an Opus call.

- [x] **Step 4: Write the failing tests**

`tests/stages/test_llm_recall.py`:

```python
@pytest.mark.anyio
async def test_recall_returns_empty_on_uncertain_llm() -> None:
    # Wrapper shape: {"suggestions": []} is the "uncertain" sentinel under strict mode.
    llm = _StubLLM(text='{"suggestions": []}')
    out = await llm_recall(pitch="...", facets=..., current_top_n=[], llm=llm,
                          model="claude-opus-4-7", max_tokens=4096, cap=8)
    assert out == []

@pytest.mark.anyio
async def test_recall_caps_at_max() -> None:
    # Stub returns wrapper with 12 suggestions; assert returned len == 8.
    ...

@pytest.mark.anyio
async def test_recall_drops_invalid_response() -> None:
    # Stub returns malformed JSON or a wrapper that fails ValidationError —
    # llm_recall returns []. (Strict mode + wrapper means we validate the
    # whole response or reject it; partial salvage isn't worth the surface
    # area given the cassette is the real coverage.)
    ...

@pytest.mark.vcr
async def test_recall_cassette_round_trip() -> None:
    # Real Opus call recorded once with the Hacken pitch.
    # In-body skip mirrors `tests/llm/test_openrouter_cassette.py` so the
    # OPENROUTER_API_KEY reminder shows up in the skip message.
    if not CASSETTE_FILE.exists() and not os.environ.get("RECORD"):
        pytest.skip(
            f"no cassette at {CASSETTE_FILE}; rerun with RECORD=1 + OPENROUTER_API_KEY to record"
        )
    ...
```

- [x] **Step 5: Run tests to verify failure**

`uv run pytest tests/stages/test_llm_recall.py -v`

- [x] **Step 6: Implement `llm_recall()`**

Add to `slopmortem/stages/llm_recall.py`:

```python
async def llm_recall(
    *,
    pitch: str,
    facets: Facets,
    current_top_n: list[ScoredCandidate],
    llm: LLMClient,
    model: str,
    max_tokens: int,
    cap: int,
) -> list[RecallSuggestion]:
    blocks = render_blocks("llm_recall", pitch=pitch, facets=facets,
                           current_top_n=current_top_n)
    try:
        result = await llm.complete(
            blocks["user"],
            system=blocks["system"],
            model=model,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "RecallSuggestionList",
                    "schema": to_strict_response_schema(RecallSuggestionList),
                    "strict": True,
                },
            },
            max_tokens=max_tokens,
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.warning("llm_recall: call failed: %r", exc)
        return []
    try:
        wrapper = RecallSuggestionList.model_validate_json(result.text)
    except ValidationError as exc:
        logger.info("llm_recall: dropped invalid response: %r", exc)
        return []
    return wrapper.suggestions[:cap]
```

- [x] **Step 7: Run tests to confirm pass**

`uv run pytest tests/stages/test_llm_recall.py -v` (cassette tests skipped without `RECORD=1`).

- [x] **Step 8: Record the cassette (manual, costs ~$0.05)** — deferred to operator; verified the cassette test skips cleanly without `RECORD=1`.

```
RECORD=1 OPENROUTER_API_KEY=<key> uv run pytest \
  tests/stages/test_llm_recall.py::test_recall_cassette_round_trip -v
```

Inspect the recorded `tests/fixtures/cassettes/recall/llm_recall_hacken.yaml` for the actual Opus output. Verify it includes recognisable Web3 security firms (Hexagate, CipherTrace, Coinfirm, Halborn, Forta, etc.) — if Opus refused with `[]`, the prompt needs strengthening before this lands.

- [x] **Step 9: Lint + typecheck**

`just lint && just typecheck`

- [x] **Step 10: Commit**

`git add slopmortem/models.py slopmortem/config.py slopmortem/llm/prompts/llm_recall.j2 slopmortem/stages/llm_recall.py tests/stages/test_llm_recall.py tests/fixtures/cassettes/recall/ && git commit -m "stages: llm_recall + RecallSuggestion + Opus cassette"`

---

### Task 4: Verifier (L1–L4)

**Files:**

- Modify: `slopmortem/http.py` (add `safe_head`)
- New: `slopmortem/stages/recall_verify.py`
- New: `tests/stages/test_recall_verify.py`
- Modify: `slopmortem/stages/__init__.py` (re-export)

- [ ] **Step 1: Write failing test for `safe_head`**

Append to `tests/test_http.py` (or create if absent):

```python
@pytest.mark.anyio
async def test_safe_head_returns_status_for_200() -> None:
    # Spin up a tiny test server or use respx to mock.
    ...

@pytest.mark.anyio
async def test_safe_head_blocks_ssrf() -> None:
    with pytest.raises(SSRFBlockedError):
        await safe_head("http://169.254.169.254/")  # AWS metadata

@pytest.mark.anyio
async def test_safe_head_raises_on_404() -> None:
    ...
```

- [ ] **Step 2: Implement `safe_head`**

In `slopmortem/http.py`, mirror `safe_get` but use `client.head(...)`. Reuse the existing SSRF guard.

- [ ] **Step 3: Run http tests**

`uv run pytest tests/test_http.py -v`

- [ ] **Step 4: Write verifier failing tests**

`tests/stages/test_recall_verify.py`:

```python
@pytest.mark.anyio
async def test_l2_rejects_404_homepage() -> None:
    # Mock safe_head homepage → 404; assert dropped before evidence GET.
    ...

@pytest.mark.anyio
async def test_l2_rejects_404_evidence() -> None:
    # Mock safe_head evidence → 404; assert dropped before body GET.
    ...

@pytest.mark.anyio
async def test_l3_rejects_evidence_missing_name() -> None:
    # safe_get(evidence_url) body has death keyword but no name → drop.
    ...

@pytest.mark.anyio
async def test_l3_rejects_evidence_missing_death_keyword() -> None:
    # Body has name but no death/struggle keyword → drop.
    ...

@pytest.mark.anyio
async def test_l3_accepts_name_and_keyword_case_insensitive() -> None:
    # Body has "HEXAGATE shutdown its operations" → accept; verify body retained.
    ...

@pytest.mark.anyio
async def test_l4_wayback_present_with_name_sets_anchored_tier() -> None:
    # L3 passes; Wayback returns body containing name → tier=wayback_anchored,
    # markdown_text is the wayback body (richer marketing copy).
    ...

@pytest.mark.anyio
async def test_l4_wayback_absent_keeps_evidence_only_tier() -> None:
    # L3 passes; Wayback returns markdown_text=None → tier=evidence_only;
    # markdown_text is the evidence body. Suggestion still accepted.
    ...

@pytest.mark.anyio
async def test_l4_wayback_present_but_no_name_keeps_evidence_only() -> None:
    # Wayback body exists but name absent (squatter on the URL post-acquisition)
    # → tier=evidence_only; evidence body wins.
    ...

@pytest.mark.anyio
async def test_l4_wayback_raises_does_not_drop() -> None:
    # WaybackEnricher.enrich() raises (transient IA outage); L3 already passed
    # → tier=evidence_only; suggestion accepted.
    ...

@pytest.mark.anyio
async def test_verify_all_via_gather_resilient_isolates_failures() -> None:
    # 3 suggestions, one raises in L3; the other 2 still verify.
    ...
```

- [ ] **Step 5: Implement `recall_verify.py`**

```python
_DEATH_KEYWORDS: Final[frozenset[str]] = frozenset({
    # terminal
    "shutdown", "shut down", "closed", "defunct", "dissolved",
    "bankrupt", "bankruptcy", "acquired", "acquisition",
    "wound down", "ceased", "going out of business",
    # distress
    "layoffs", "layoff", "restructuring", "struggling",
    "missed payroll", "downsizing", "troubled",
})

type VerificationTier = Literal["wayback_anchored", "evidence_only"]


def _body_anchors_name_and_death(name: str, body: str) -> bool:
    haystack = body.lower()
    if name.lower() not in haystack:
        return False
    return any(kw in haystack for kw in _DEATH_KEYWORDS)


async def verify_suggestion(
    suggestion: RecallSuggestion,
    *,
    wayback: WaybackEnricher,
) -> tuple[RawEntry, VerificationTier] | None:
    # L1: schema already enforced by Pydantic.
    # L2: HEAD both URLs. 40s matches the L3 GET budget — a host slow on HEAD
    # is a host we'd time out on for the GET anyway; one budget across both.
    for url in (str(suggestion.homepage_url), str(suggestion.evidence_url)):
        try:
            resp = await safe_head(url, timeout=40.0)
        except (SSRFBlockedError, httpx.HTTPError):
            return None
        if resp.status_code >= HTTP_BAD_REQUEST:
            return None
    # L3: GET evidence_url body — primary anchor.
    # ``safe_get`` returns ``httpx.Response`` (see ``slopmortem/http.py``);
    # mirror the ``WaybackEnricher._fetch`` shape: catch the same exceptions,
    # gate on status_code, then read ``.text``. 40s timeout because some
    # citation hosts (court filings, archived blogs) are sluggish.
    # NOTE: ``safe_get`` does NOT consult robots.txt (unlike
    # ``WaybackEnricher._fetch`` which calls ``respect_robots`` first).
    # Intentional here — recall's reason for existence is to surface vendors
    # whose homepage is robots-blocked or vanished. The evidence URL is a
    # third-party citation (news article, court filing), not the vendor's
    # site, so robots fetch-policy on the *vendor* doesn't apply. Don't
    # "harmonize" by adding a robots check here — it would defeat the L3
    # gate on exactly the population recall targets.
    try:
        resp = await safe_get(str(suggestion.evidence_url), timeout=40.0)
    except (SSRFBlockedError, httpx.HTTPError):
        return None
    if resp.status_code >= HTTP_BAD_REQUEST:
        return None
    evidence_body = resp.text
    if not _body_anchors_name_and_death(suggestion.name, evidence_body):
        return None
    # L4: optional Wayback corroboration. Failures here never drop the suggestion.
    tier: VerificationTier = "evidence_only"
    body = evidence_body
    # ``markdown_text=None`` AND ``raw_html=None`` are load-bearing:
    # ``WaybackEnricher.enrich`` short-circuits if either body is already
    # populated (``wayback.py:112-115``). We need it to actually fetch.
    seed = RawEntry(
        source="llm_recall",
        source_id=_recall_source_id(suggestion),
        url=str(suggestion.homepage_url),
        markdown_text=None,
        raw_html=None,
        fetched_at=datetime.now(UTC),
    )
    try:
        enriched = await wayback.enrich(seed)
    except Exception as exc:
        logger.info("recall_verify: wayback corroboration failed: %r", exc)
        enriched = seed
    if enriched.markdown_text and suggestion.name.lower() in enriched.markdown_text.lower():
        tier = "wayback_anchored"
        body = enriched.markdown_text  # richer than the article body for vector search
    # Persist body via ``markdown_text`` only — ``_entry_summary_text``
    # (`ingest/_helpers.py:94`) already prefers ``markdown_text`` over
    # ``raw_html``, so leaving ``raw_html=None`` is correct and avoids
    # double-extracting.
    final = seed.model_copy(update={"markdown_text": body})
    return final, tier


async def verify_and_persist_all(
    suggestions: list[RecallSuggestion],
    *,
    wayback: WaybackEnricher,
    persist: Callable[[RawEntry, VerificationTier], Awaitable[None]],
    concurrency: int = 3,
) -> list[RawEntry]:
    limiter = anyio.CapacityLimiter(concurrency)

    async def _one(s: RecallSuggestion) -> RawEntry | None:
        async with limiter:
            verified = await verify_suggestion(s, wayback=wayback)
        if verified is None:
            return None
        entry, tier = verified
        await persist(entry, tier)
        return entry

    results = await gather_resilient(*(_one(s) for s in suggestions))
    return [r for r in results if isinstance(r, RawEntry)]
```

Note: `RawEntry` itself does not carry `verification_tier`. The tier rides as a sibling argument into `persist_recall_entry` (Task 5), which threads it through `_write_phase` → `_process_entry` → `_build_payload` and lands on `CandidatePayload.verification_tier` — keeps `RawEntry` stable across non-recall sources, and avoids a parallel qdrant payload-merge channel.

- [ ] **Step 6: Run verifier tests**

`uv run pytest tests/stages/test_recall_verify.py -v`

- [ ] **Step 7: Lint + typecheck**

`just lint && just typecheck`

- [ ] **Step 8: Commit**

`git add slopmortem/http.py slopmortem/stages/recall_verify.py slopmortem/stages/__init__.py tests/test_http.py tests/stages/test_recall_verify.py && git commit -m "stages: recall_verify L1-L4 with safe_head + WaybackEnricher"`

---

### Task 5: Persistence helper

**Files:**

- New: `slopmortem/stages/recall_persist.py`
- Modify: `slopmortem/corpus/sources/_names.py` (`SOURCE_LLM_RECALL`)
- New: `tests/stages/test_recall_persist.py`

- [ ] **Step 1: Add `SOURCE_LLM_RECALL` constant**

```python
# In slopmortem/corpus/sources/_names.py
SOURCE_LLM_RECALL: Final = "llm_recall"
```

- [ ] **Step 2: Write failing tests**

`tests/stages/test_recall_persist.py`:

```python
@pytest.mark.anyio
async def test_persist_writes_to_journal_and_qdrant() -> None:
    # Inject fake journal + fake qdrant; persist; assert one row + one point.
    ...

@pytest.mark.anyio
async def test_persist_idempotent() -> None:
    # Persist same suggestion twice; assert one row + one point (deterministic source_id).
    ...

@pytest.mark.anyio
async def test_persist_deterministic_source_id() -> None:
    # Two suggestions with identical (name, homepage_url) produce same source_id.
    # Different homepage_url → different source_id.
    ...

@pytest.mark.anyio
async def test_persist_writes_verification_tier_to_payload() -> None:
    # tier="wayback_anchored" → CandidatePayload.verification_tier=="wayback_anchored"
    # in the qdrant point payload (model_dump emits the field).
    # tier="evidence_only"   → CandidatePayload.verification_tier=="evidence_only".
    # Non-recall sources (sanity) leave it at the default None.
    ...
```

- [ ] **Step 3: Add `verification_tier` to `CandidatePayload`**

`verification_tier` rides into the qdrant payload via the existing `CandidatePayload` model rather than a side-channel `extra_payload` kwarg. The simpler shape avoids plumbing a new param through `_write_phase` → `_process_entry` → `_build_payload` → `_embed_and_upsert` (four layers) just to merge one optional field onto the qdrant point.

In `slopmortem/models.py:CandidatePayload` (`models.py:183`):

```python
    # None for non-recall sources; set by ``recall_persist`` for source=llm_recall.
    verification_tier: Literal["wayback_anchored", "evidence_only"] | None = None
```

In `slopmortem/ingest/_helpers.py:_build_payload`, add a `verification_tier` kwarg defaulting to `None` and forward it to the `CandidatePayload(...)` construction. Existing callers pass nothing (default applies); the recall persist path passes the tier explicitly.

- [ ] **Step 4: Implement `recall_persist.py`**

The function reuses the existing ingest tail by calling each phase with a one-element batch. The three reusable seams are:

1. `slopmortem.ingest._classify_phase(entries, ...)` (`slopmortem/ingest/_ingest.py:113`) — runs slop classify and returns `keepers: list[tuple[RawEntry, str]]`. Quarantine + journal writes still happen here, so a recall body the slop classifier rejects is dropped exactly the same way a crawler entry would be.
2. `slopmortem.ingest._fan_out._facet_summarize_fanout(keepers, ...)` (`slopmortem/ingest/_fan_out.py:80`) — runs facet extraction + summarize.
3. `slopmortem.ingest._ingest._write_phase(keepers, fanout, ...)` (`slopmortem/ingest/_ingest.py:255`) — entity-resolve, journal write, disk write, qdrant write, `mark_complete`. This is the seam that satisfies the load-bearing invariant CLAUDE.md spells out: terminal-state writes happen in one transaction; `mark_complete` only fires after both Qdrant and disk writes succeed. The `_ingest.py` module docstring (line 2) just points to `architecture.md` and CLAUDE.md — the invariant text itself lives in CLAUDE.md, and the journal short-circuit is the `journal.is_terminal` check at line 142.

To plumb `verification_tier` into the payload without touching `_write_phase`'s 11-arg signature: add a `verification_tier` kwarg to `_process_entry` (`slopmortem/ingest/_journal_writes.py:64`), thread it into the `_build_payload(...)` call there, and add the same kwarg on `_write_phase` so it can pass it through. Existing callers pass nothing (default `None`); the recall path sets it. One field, three signatures, no new payload-merge plumbing in the qdrant adapter.

These are package-private (`_`-prefixed) but stable enough that the recall path can call them directly. If the access-control feels wrong, lift them to `__all__` on `slopmortem/ingest/__init__.py` as `classify_phase` / `facet_summarize_fanout` / `write_phase` in this same task — but do not introduce a new write path that bypasses the journal.

```python
def _recall_source_id(suggestion: RecallSuggestion) -> str:
    return hashlib.sha256(
        f"{suggestion.name}|{suggestion.homepage_url}".encode()
    ).hexdigest()[:16]


async def persist_recall_entry(
    entry: RawEntry,  # body already filled in by the verifier (Task 4)
    tier: Literal["wayback_anchored", "evidence_only"],
    *,
    journal: MergeJournal,
    corpus: Corpus,
    embed_client: EmbeddingClient,
    llm: LLMClient,
    slop_classifier: SlopClassifier,
    sparse_encoder: SparseEncoder,
    config: Config,
    post_mortems_root: Path,
    progress: IngestProgress,
    result: IngestResult,
) -> None:
    keepers = await _classify_phase(
        [entry],
        enrichers=(),  # body already anchored in verifier; don't re-enrich.
        slop_classifier=slop_classifier,
        journal=journal,
        config=config,
        post_mortems_root=post_mortems_root,
        dry_run=False,
        force=False,
        progress=progress,
        result=result,
    )
    if not keepers:
        return  # quarantined or duplicate
    fanout = await _facet_summarize_fanout(keepers, llm=llm, config=config, progress=progress)
    await _write_phase(
        keepers, fanout,
        journal=journal, corpus=corpus, embed_client=embed_client, llm=llm,
        config=config, post_mortems_root=post_mortems_root, force=False,
        sparse_encoder=sparse_encoder, progress=progress, result=result,
        verification_tier=tier,  # forwarded to _build_payload → CandidatePayload field
    )
```

`verification_tier` is the new kwarg added to `_write_phase` and `_process_entry` in Step 3. It defaults to `None`, so the crawler path is unaffected; the recall path sets it. The field lands in qdrant alongside `facets.*` because `CandidatePayload.model_dump()` already serializes every field.

Idempotency falls out of `journal.is_terminal(entry.source, entry.source_id)` short-circuit inside `_classify_phase` (line 142): re-persisting the same recall suggestion produces no second journal row or qdrant point because `_recall_source_id` is deterministic on `(name, homepage_url)`. A re-verification that flips tier `evidence_only → wayback_anchored` is therefore *not* propagated automatically — that's intentional; tier upgrades require a separate re-write tool, which is out of scope here.

- [ ] **Step 5: Run persist tests**

`uv run pytest tests/stages/test_recall_persist.py -v`

- [ ] **Step 6: Add reliability rank entry**

In `slopmortem/ingest/_helpers.py`, extend the `SOURCE_*` import block to pull in `SOURCE_LLM_RECALL`, then extend `_RELIABILITY_RANK` (the dict already keys on the constants, not literals — keep the style consistent so a future rename of the literal value can't silently desync):

```python
    SOURCE_LLM_RECALL: 6,
```

Add `(SOURCE_LLM_RECALL, 6)` to the parametrize set in `tests/ingest/test_reliability_rank.py`.

- [ ] **Step 7: Run reliability test**

`uv run pytest tests/ingest/test_reliability_rank.py -v`

- [ ] **Step 8: Lint + typecheck**

`just lint && just typecheck`

- [ ] **Step 9: Commit**

`git add slopmortem/models.py slopmortem/ingest/_helpers.py slopmortem/ingest/_journal_writes.py slopmortem/ingest/_ingest.py slopmortem/stages/recall_persist.py slopmortem/corpus/sources/_names.py tests/ && git commit -m "stages: recall_persist + verification_tier on CandidatePayload + reliability rank"`

---

### Task 6: Pipeline wiring

**Files:**

- Modify: `slopmortem/pipeline.py` (insert recall branch + `QueryPhase.RECALL`)
- Modify: `slopmortem/models.py` (`PipelineMeta` flags)
- Modify: `slopmortem/render.py` (surface flags in report)
- New: `tests/test_pipeline_recall_fallback.py`

- [ ] **Step 1: Add PipelineMeta flags**

In `slopmortem/models.py`:

```python
class PipelineMeta(BaseModel):
    # ... existing ...
    coverage_gap: bool = False
    recall_used: bool = False
    recall_persisted_count: int = 0
```

- [ ] **Step 2: Add `QueryPhase.RECALL`**

In `slopmortem/pipeline.py`:

```python
class QueryPhase(StrEnum):
    FACET_EXTRACT = "facet_extract"
    RETRIEVE = "retrieve"
    RERANK = "rerank"
    RECALL = "recall"
    SYNTHESIZE = "synthesize"
```

No `NullQueryProgress` change needed — its methods accept any `QueryPhase` value.

- [ ] **Step 3: Write failing E2E test**

`tests/test_pipeline_recall_fallback.py`:

```python
@pytest.mark.anyio
async def test_pipeline_recall_fires_on_zero_candidates() -> None:
    # Fake corpus returns []; fake LLM emits 2 RecallSuggestion;
    # fake verifier accepts 1, rejects 1; assert:
    #   - report.pipeline_meta.coverage_gap is True
    #   - report.pipeline_meta.recall_used is True
    #   - report.pipeline_meta.recall_persisted_count == 1
    #   - report.candidates contains the 1 verified suggestion's synthesis
    ...

@pytest.mark.anyio
async def test_pipeline_recall_disabled_default() -> None:
    # enable_llm_recall=False, force_llm_recall=False; gate not even checked;
    # recall doesn't run; report.coverage_gap=False, recall_used=False.
    ...

@pytest.mark.anyio
async def test_pipeline_recall_trigger_fires_with_enable() -> None:
    # enable_llm_recall=True, force_llm_recall=False; corpus seeded so the
    # unified trigger fires (qualifying_count < N_synthesize); recall runs;
    # report.coverage_gap=True, recall_used=True.
    ...

@pytest.mark.anyio
async def test_pipeline_force_llm_recall_runs_without_enable() -> None:
    # enable_llm_recall=False, force_llm_recall=True; trigger never evaluated
    # (coverage_gap stays False); recall runs anyway because force is set.
    # report.coverage_gap=False, recall_used=True — operator can distinguish
    # forced runs from trigger-driven runs by inspecting the pair.
    ...

@pytest.mark.anyio
async def test_pipeline_force_llm_recall_with_quiet_trigger() -> None:
    # enable_llm_recall=True, force_llm_recall=True, but corpus has plenty
    # of strong in-sector matches so trigger would stay quiet on its own.
    # Force makes recall fire anyway. report.coverage_gap=False, recall_used=True.
    ...

@pytest.mark.anyio
async def test_pipeline_recall_max_one_pass() -> None:
    # Recall runs (either path), persists 1, second rerank still has gap;
    # do NOT recall again — single-pass guarantee from the §Decision.
    ...
```

- [ ] **Step 4: Run test to verify failure**

`uv run pytest tests/test_pipeline_recall_fallback.py -v`

- [ ] **Step 5: Wire the recall branch in `pipeline.py`**

`run_query` (`pipeline.py:102`) does not currently take a `WaybackEnricher` — and `WaybackEnricher` is *not* instantiated anywhere in prod today (only in `tests/sources/test_wayback.py`). The CLI ingest path at `_ingest_cmd.py:378-394` deliberately keeps it out of the enrichers list because of Wayback's HEAD/GET rate-limiting under bulk crawl. The recall path is much lighter — at most `recall_max_suggestions_per_pitch=8` Wayback lookups *only when the gate fires* — so the rate-limit concern that gates the bulk ingest path doesn't gate this one.

The persist path needs `journal`, `slop_classifier`, `post_mortems_root`, plus per-call `IngestProgress` + `IngestResult` for the slop/quarantine bookkeeping inside `_classify_phase`. None of those exist on `run_query` today. Bundle the long-lived deps into a single `RecallDeps` dataclass kwarg so non-recall callers pay nothing; instantiate the per-call progress/result inside the recall branch (they're per-entry counters, not pipeline-shared state, so a fresh pair per persist is correct).

`RecallDeps` lives in `slopmortem/pipeline.py` next to `QueryPhase`:

```python
@dataclass(frozen=True)
class RecallDeps:
    journal: MergeJournal
    slop_classifier: SlopClassifier
    post_mortems_root: Path
    wayback: WaybackEnricher | None = None  # default-instantiated when needed
```

Add `recall_deps: RecallDeps | None = None` to `run_query`. When `should_fire` is True and `recall_deps is None`, raise `RuntimeError("recall enabled but RecallDeps not provided")` so the misconfiguration surfaces at the call site rather than silently no-oping. `sparse_encoder` is also required by the persist tail (`_write_phase: sparse_encoder: SparseEncoder` is non-optional) but is `Optional` on `run_query` — guard it the same way: when `should_fire` and `sparse_encoder is None`, raise `RuntimeError("recall requires sparse_encoder").

After the first `llm_rerank` (line 172, ends just before `select_top_n_by_similarity` at line 184):

```python
from slopmortem.ingest._ports import IngestResult, NullProgress  # in import block

recall_used = False
recall_persisted_count = 0
coverage_gap = False

if config.enable_llm_recall:
    coverage_gap = detect_coverage_gap(
        retrieved=retrieved,
        ranked=reranked.ranked,
        pitch_sector=facets.sector,
        min_similarity_score=config.min_similarity_score,
        n_synthesize=config.N_synthesize,
    )

# OR-combined: trigger-driven OR force-on. force_llm_recall does NOT
# require enable_llm_recall — operators recording cassettes or running
# eval calibration can opt into recall without committing to
# trigger-driven behaviour in production.
should_fire = (config.enable_llm_recall and coverage_gap) or config.force_llm_recall

if should_fire:
    if recall_deps is None:
        raise RuntimeError("recall enabled but RecallDeps not provided")
    if sparse_encoder is None:
        raise RuntimeError("recall requires sparse_encoder")
    progress.start_phase(QueryPhase.RECALL, total=1)
    suggestions = await llm_recall(
        pitch=input_ctx.description,
        facets=facets,
        current_top_n=reranked.ranked[: config.N_synthesize],
        llm=llm,
        model=config.model_recall,
        max_tokens=config.max_tokens_recall,
        cap=config.recall_max_suggestions_per_pitch,
    )
    if suggestions:
        # Lazy default — only instantiated on the recall path; tests inject
        # a fake via RecallDeps.wayback.
        wb = recall_deps.wayback if recall_deps.wayback is not None else WaybackEnricher()

        async def _persist(entry: RawEntry, tier: VerificationTier) -> None:
            # Per-call progress/result: classify_phase mutates these for the
            # one entry we're persisting; nothing else reads them. Fresh pair
            # per call avoids cross-talk if recall fires multiple suggestions
            # concurrently inside verify_and_persist_all.
            await persist_recall_entry(
                entry, tier,
                journal=recall_deps.journal,
                corpus=corpus,
                embed_client=embedding_client,
                llm=llm,
                slop_classifier=recall_deps.slop_classifier,
                sparse_encoder=sparse_encoder,
                config=config,
                post_mortems_root=recall_deps.post_mortems_root,
                progress=NullProgress(),
                result=IngestResult(),
            )

        verified = await verify_and_persist_all(
            suggestions, wayback=wb, persist=_persist,
        )
        recall_persisted_count = len(verified)
        if verified:
            recall_used = True
            # Re-run retrieve + rerank on the augmented corpus.
            retrieved = await retrieve(...)
            reranked = await llm_rerank(retrieved, ...)
    progress.advance_phase(QueryPhase.RECALL)
    progress.end_phase(QueryPhase.RECALL)
```

The `coverage_gap` flag is computed only when `enable_llm_recall=True` because it's a meaningful audit signal *for trigger-driven runs* (the report can show "trigger fired"). When recall fires only because of `force_llm_recall=True`, `coverage_gap` stays `False` — the recall ran on operator demand, not because retrieval was thin. Operators inspecting reports can distinguish the two cases via `recall_used=True && coverage_gap=False`.

Pass `coverage_gap`, `recall_used`, `recall_persisted_count` into `PipelineMeta(...)` at the bottom of the function.

- [ ] **Step 6: Run E2E tests**

`uv run pytest tests/test_pipeline_recall_fallback.py -v`

- [ ] **Step 7: Update render.py**

In `slopmortem/render.py`, add a "Pipeline meta" line for each non-default flag. E.g.:

```
- coverage_gap: True
- recall_used: True
- recall_persisted_count: 1
```

- [ ] **Step 8: Run full test suite**

`just test`

- [ ] **Step 9: Lint + typecheck**

`just lint && just typecheck`

- [ ] **Step 10: Commit**

`git add slopmortem/pipeline.py slopmortem/models.py slopmortem/render.py tests/ && git commit -m "pipeline: wire llm_recall fallback after rerank"`

---

### Task 7: CLI flag + tracing

**Files:**

- Modify: `slopmortem/cli/_query_cmd.py`
- Modify: `slopmortem/tracing/events.py` (add new SpanEvent values)
- New: `tests/test_cli_query.py` (CLI-test files live at the top level — `test_cli_ingest.py`, `test_cli_smoke.py`, etc. There is no `tests/cli/` dir.)

- [ ] **Step 1: Add Typer flag**

In `slopmortem/cli/_query_cmd.py`:

```python
    enable_llm_recall: Annotated[
        bool,
        typer.Option(
            "--enable-llm-recall/--no-llm-recall",
            help=(
                "Enable LLM-based recall fallback when retrieval has no usable "
                "comparables for the pitch's vertical. Verified suggestions are "
                "persisted as source=llm_recall corpus entries for reuse. "
                "Costs ~$0.05-0.15 per call when the gate fires."
            ),
        ),
    ] = False,
    force_llm_recall: Annotated[
        bool,
        typer.Option(
            "--force-llm-recall/--no-force-llm-recall",
            help=(
                "Fire LLM recall on every query regardless of the coverage gate. "
                "Independent of --enable-llm-recall (OR-combined in the pipeline). "
                "Use for cassette recording, eval calibration, or thin/new corpora."
            ),
        ),
    ] = False,
```

`_query_cmd.py:_query` currently calls `load_config()` and uses the result unmodified — there is no overlay mechanism today. Apply the flags via `config.model_copy(update=...)` immediately after `load_config()`:

```python
config = load_config()
config = config.model_copy(update={
    "enable_llm_recall": enable_llm_recall,
    "force_llm_recall": force_llm_recall,
})
```

Pydantic v2's `model_copy(update=...)` skips validators (model_validator(mode="after") doesn't re-run), but neither flag participates in cross-field validation, so this is safe. If a future flag *does* need re-validation, switch to `Config.model_validate({**config.model_dump(), **overrides})`.

- [ ] **Step 2: Add SpanEvent values**

In `slopmortem/tracing/events.py`, add:

```python
RECALL_GATE_FIRED = "recall.gate_fired"
RECALL_SUGGESTIONS_RECEIVED = "recall.suggestions_received"
RECALL_REJECTED_L2 = "recall.rejected_l2"                       # URL HEAD failed
RECALL_REJECTED_L3_NAME_MISSING = "recall.rejected_l3_name"     # evidence body lacks name
RECALL_REJECTED_L3_KEYWORD_MISSING = "recall.rejected_l3_kw"    # evidence body lacks death keyword
RECALL_VERIFIED_WAYBACK_ANCHORED = "recall.verified_wayback"    # L3 + L4 both confirm
RECALL_VERIFIED_EVIDENCE_ONLY = "recall.verified_evidence"      # L3 confirms; L4 absent/silent
RECALL_PERSISTED = "recall.persisted"
```

Emit them at the matching call sites in `pipeline.py` and `recall_verify.py`.

- [ ] **Step 3: Write CLI tests**

```python
def test_enable_llm_recall_flag() -> None:
    # CliRunner invocation with --enable-llm-recall on a synthesized pitch
    # (use stubs or a tiny fake corpus); assert config.enable_llm_recall = True
    # and config.force_llm_recall = False.
    ...

def test_force_llm_recall_flag() -> None:
    # CliRunner invocation with --force-llm-recall *without* --enable-llm-recall;
    # assert config.force_llm_recall = True and config.enable_llm_recall = False
    # — confirms the two flags are independent (OR-combined in pipeline).
    ...

def test_both_recall_flags() -> None:
    # --enable-llm-recall --force-llm-recall both set;
    # assert both reflect True on the resolved Config.
    ...
```

- [ ] **Step 4: Run tests**

`just test`

- [ ] **Step 5: Lint + typecheck**

`just lint && just typecheck`

- [ ] **Step 6: Smoke test (manual, optional, ~$0.10)**

```
OPENROUTER_API_KEY=<key> uv run slopmortem query \
  --enable-llm-recall \
  "$(cat .slopmortem/runs/20260508T142314Z-one-liner-real-time-on-chain-threat-dete.md | head -20)"
```

Verify the rendered report includes `coverage_gap: True` and `recall_used: True`, and at least one synthesized comparable's source resolves to `llm_recall`.

- [ ] **Step 7: Commit**

`git add slopmortem/cli/_query_cmd.py slopmortem/tracing/events.py tests/ && git commit -m "cli: --enable-llm-recall flag + SpanEvents"`

---

### Task 8: Telecom backfill — DEFERRED to its own plan

**Status:** Out of scope here. Track in a follow-up dated plan; nothing in Tasks 1–7 depends on it.

**Why split:** The plan originally framed this as "operational, runnable independently" by invoking `slopmortem reclassify --provenance-ids …`. That command does not exist:

- `slopmortem reclassify` is not a top-level command. The only reclassify path is `slopmortem ingest --reclassify` (`slopmortem/cli/_ingest_cmd.py:71`), which calls `reclassify_quarantined()` (`slopmortem/corpus/_reclassify.py:85`).
- `reclassify_quarantined()` only walks the on-disk **quarantine** directory and re-runs the slop classifier. It cannot target ingested entries by `provenance_id`, does not re-extract facets, and never updates Qdrant payloads.

A real backfill needs a new code path: re-run `extract_facets` (Haiku) on each of the 7 target entries' bodies, then update both the on-disk canonical and the Qdrant payload's `facets.sector`. That's a new corpus operation plus a new CLI surface, not a one-line CLI tweak.

**What to do instead:** when the recall feature is shipping, write `docs/plans/<date>-telecom-backfill.md` covering:

1. New corpus helper `refacet_existing(canonical_ids, *, llm, corpus, journal, post_mortems_root)` that re-runs facet extraction on already-canonicalized bodies and updates the Qdrant payload via `corpus.update_payload(...)` (or equivalent — check `_qdrant_store.py` for the actual seam).
2. New CLI command `slopmortem refacet --filter sector=other,sub_sector=telecommunications` that scrolls Qdrant for matches and pipes them through `refacet_existing`.
3. Tests covering both the helper and the CLI on a 1-entry fake corpus.

The 7 telecom entries can wait — they hurt one out-of-vertical pitch each, and the LLM recall feature itself sidesteps the issue by populating the right comparables on demand.

---

### Polish

- [ ] **Step 1: Run post-implementation polish**

Dispatch the `post-implementation-polish` skill on the diff produced by Tasks 1–8.

- [ ] **Step 2: Address findings, recommit if needed**

Each polish-driven fix lands as its own commit so blame stays useful.

- [ ] **Step 3: Final sweep**

`just lint && just typecheck && just test && just coverage`

Expected: clean, coverage on new modules ≥ existing project floor.

- [ ] **Step 4: Update `docs/architecture.md`**

Add a one-line pointer:

> LLM recall fallback: when retrieval misses a vertical entirely, an Opus call names candidate comparables from training data, verifies them via Wayback, and persists them as `source=llm_recall` corpus entries. See `docs/plans/2026-05-08-llm-recall-fallback.md`.

- [ ] **Step 5: Eval drift check**

Run `just eval` (cassette mode, free). Existing eval pitches don't trigger the gate so cassettes shouldn't shift. If they do, that's a real regression — investigate before merging.

- [ ] **Step 6: Manual end-to-end on Hacken pitch**

```
uv run slopmortem query --enable-llm-recall \
  "$(cat .slopmortem/runs/20260508T142314Z-one-liner-real-time-on-chain-threat-dete.md | head -20)"
```

Compare the output to the existing run. Expect:
- `coverage_gap: True` flag appears in pipeline meta.
- `recall_used: True` if at least one Wayback-anchored comparable was persisted.
- New comparables (Hexagate / CipherTrace / Coinfirm / Halborn / Forta / etc.) appear in the report instead of (or alongside) Norse / Carbon Black.
- `sector=crypto_web3` count in Qdrant has grown from 6 to 6 + N where N is the persisted count.

This run validates the whole pipeline end-to-end on the canonical fixture that motivated the work.
