# LLM Recall Fallback — Design

**Status:** draft
**Date:** 2026-05-08
**Branch context:** `defillama` (current). Sibling work alongside the existing
crawler-based sources (`docs/specs/2026-05-06-defillama-source-design.md`,
`2026-05-06-tavily-news-source-design.md`).

## Goal

Add a fallback recall stage that fires **only when the existing corpus has
no usable comparables for the pitch's vertical** — e.g. the Hacken/Extractor
pitch (vendor-side Web3 security tooling) where retrieval surfaces general
cyber-threat-intel deaths (Norse Corp, Carbon Black) but no Web3-native
vendor analogs because none of the existing crawlers (HN-Algolia,
Tavily news, DefiLlama, Crunchbase CSV, curated YAML) ingest documents
about Hexagate, CipherTrace, Coinfirm, Halborn, etc.

The recall stage asks Claude Opus to name candidate comparables from its
training data, **verifies each one against the live web** (URL HEAD →
Wayback anchor → content match), and **persists verified hits as regular
`RawEntry` rows** with `source=llm_recall`. Subsequent pitches in the same
vertical retrieve those entries through normal Qdrant vector search — the
**cache is the corpus itself**, no separate cache layer.

**Non-goal: replace crawlers.** This is a coverage-gap filler for niche
B2B verticals (chip IP, web3 security tooling, automated DD/research SaaS,
exotic on-chain options) where structured public failure feeds don't
exist. Crawlers stay primary for protocol-side (DefiLlama) and broad
consumer/SaaS deaths (HN-Algolia, Tavily news).

**Hallucination is the central risk.** Without verification, this stage
poisons the corpus permanently. The verification gate (L1–L4 below) is
load-bearing: if it can't anchor a suggestion to a real document, the
suggestion is dropped, not weakened. Net new false positives going into
the corpus must round to zero.

## Architecture

```
pitch
  │
  ▼
facet extract ──► retrieve ──► rerank
  │                              │
  │                              ▼
  │                    ┌── coverage gate ──┐
  │                    │                   │
  │                    ▼                   ▼
  │              coverage ok          coverage gap
  │                    │                   │
  │                    │                   ▼
  │                    │           llm_recall (new stage)
  │                    │                   │
  │                    │                   ▼
  │                    │              verify+anchor
  │                    │                   │
  │                    │                   ▼
  │                    │       persist as corpus entries
  │                    │           (journal + qdrant)
  │                    │                   │
  │                    │                   ▼
  │                    │             retrieve_again
  │                    │                   │
  │                    │                   ▼
  │                    │               rerank_again
  │                    │                   │
  │                    ▼                   ▼
  │               synthesize  ◄────────────┘
  │                    │
  │                    ▼
  │             consolidate_risks
```

The fallback runs **at most once per query**: if the second rerank still
fails the coverage gate, synthesize proceeds with whatever it has and the
report flags `pipeline_meta.coverage_gap=True`. No infinite loop.

## Tech Stack

Python 3.13, `anyio`, `httpx` (via `safe_get`), `pydantic` v2 (for
`RawEntry` and the new `RecallSuggestion` model), `basedpyright` strict.
Reuses `slopmortem.corpus.sources.defillama.wayback_snapshot_near` for
L3 anchoring. Reuses existing `LLMClient`, `Budget`, slop classifier, and
Qdrant store — no new infra.

## Execution Strategy

**Subagents** — three sequential tasks. Task 2 imports models from Task 1;
Task 3 wires Tasks 1+2 into the pipeline. They cannot run in parallel.

## Task Dependency Graph

- Task 1 [AFK]: Coverage gate + `RecallSuggestion` model + Opus prompt + cassette → depends on `none` → batch 1
- Task 2 [AFK]: Verifier (L1–L4) + persistence helper + tests → depends on `Task 1` → batch 2
- Task 3 [AFK]: Pipeline wiring + CLI flag + budget accounting + eval cassette regen → depends on `Task 1, Task 2` → batch 3

## Agent Assignments

- Task 1: Coverage gate + recall stage prompt → python-development:python-pro
- Task 2: Verifier + persistence → python-development:python-pro
- Task 3: Pipeline wiring + CLI + budget → python-development:python-pro
- Polish: post-implementation-polish → general-purpose

---

## File Structure

**New:**

- `slopmortem/stages/llm_recall.py` — coverage-gate predicate, Opus
  call, schema-validated `list[RecallSuggestion]` output. Pure-functional
  shape: takes facets + reranked candidates, returns suggestions.
- `slopmortem/stages/recall_verify.py` — L1–L4 verification helpers.
  Takes a `RecallSuggestion`, returns `RawEntry | None`. Reuses
  `wayback_snapshot_near`, `safe_get`, `safe_head` (new helper in
  `slopmortem/http.py`, see below).
- `slopmortem/stages/recall_persist.py` — writes verified entries
  through the existing journal + Qdrant write path used by ingest.
  No new tables; same columns as a regular `RawEntry`.

**Modified:**

- `slopmortem/corpus/sources/_names.py` — add
  `SOURCE_LLM_RECALL: Final = "llm_recall"`. Used in
  `RawEntry.source`, the `_RELIABILITY_RANK` table, and grep targets
  for purge.
- `slopmortem/stages/__init__.py` — re-export the three new stage
  callables (`detect_coverage_gap`, `llm_recall`, `verify_and_persist`).
- `slopmortem/pipeline.py` — insert the fallback branch after the
  first `llm_rerank` call. New `QueryPhase.RECALL` enum value and
  matching `progress` hooks.
- `slopmortem/models.py` — add `RecallSuggestion` (LLM output shape)
  and `PipelineMeta.coverage_gap: bool = False`,
  `PipelineMeta.recall_used: bool = False`,
  `PipelineMeta.recall_persisted_count: int = 0` for telemetry.
- `slopmortem/config.py` — add `enable_llm_recall: bool = False`
  (opt-in), `model_recall: str = "anthropic/claude-opus-4-7"`,
  `max_tokens_recall: int = 4096`,
  `recall_max_suggestions_per_pitch: int = 8`,
  `recall_coverage_axis_threshold: float = 5.0`,
  `recall_budget_usd_cap: float = 0.50`.
- `slopmortem/http.py` — add `safe_head(url, timeout)` helper.
  Mirrors `safe_get`'s SSRF guard but issues `HEAD`. Needed for L2.
- `slopmortem/ingest/_helpers.py` — extend `_RELIABILITY_RANK`
  with `llm_recall → 6`. Slots **below** `tavily_news` (rank 4)
  because LLM recall is two steps removed from primary data
  (training corpus + Wayback).
- `slopmortem/cli/_query_cmd.py` — add `--enable-llm-recall` flag;
  default off. Enabling without `OPENROUTER_API_KEY` already errors
  via existing client init.
- `slopmortem/corpus/_reclassify.py` — extend the existing
  `--purge-source` mechanism to accept `llm_recall` so a bad recall
  batch can be removed without nuking the whole corpus.

**New tests:**

- `tests/stages/test_coverage_gate.py` — pure-fn tests: gate fires
  at `0` candidates, fires when top-N mean axis < threshold, does
  not fire on the Hacken-Norse-Carbon-Black case **only after we
  treat it as the canonical "wrong vertical" fixture** (see
  Calibration below).
- `tests/stages/test_llm_recall.py` — `FakeLLMClient`-based tests
  that the prompt produces schema-valid suggestions, cap respected,
  cassette round-trip with a real Opus call recorded once for the
  Hacken pitch.
- `tests/stages/test_recall_verify.py` — verifier behavior: L2
  rejects 404, L3 rejects no-snapshot, L4 rejects name-not-in-page.
  Mocked HTTP layer.
- `tests/stages/test_recall_persist.py` — persisted entry has
  `source=llm_recall`, deterministic `source_id`, idempotent
  re-write (running twice with same suggestion → one row, not two).
- `tests/test_pipeline_recall_fallback.py` — end-to-end with a
  fake corpus that returns 0 candidates → recall fires → fake LLM
  emits two suggestions → fake verifier accepts one and rejects
  one → second rerank includes the accepted one → synthesize sees it.

**Modified tests:**

- `tests/ingest/test_reliability_rank.py` — add
  `(SOURCE_LLM_RECALL, 6)` parametrize case.
- `tests/test_pipeline.py` — add a `coverage_gap=True` /
  `recall_used=False` assertion when the gate fires but
  `enable_llm_recall=False`.

---

## Coverage gate

Triggered after the first `llm_rerank` call on the original retrieved set.
Fires if **any** of:

1. `len(retrieved) == 0` — empty candidate set.
2. `mean(top_n.similarity_score) < config.min_similarity_score` —
   already a tuned floor (4.0 in the run header).
3. **Vertical-axis collapse** — across the top-N rerank rows,
   `mean(market_axis) < recall_coverage_axis_threshold` AND
   `mean(business_model_axis) < recall_coverage_axis_threshold`.
   Default threshold `5.0`. The Hacken run scored
   `business_model: 4.0/6.0` and `market: 5.0/3.0` — borderline by
   design. Operators can tune via config.

The gate is a pure function. Inputs: list of reranked candidates +
similarity scores + per-axis scores. Output: `bool`. No I/O.

**Calibration.** The existing Hacken/Extractor run (the one that
prompted this whole conversation) is the canonical fixture. It's a
**true positive for the gate** — corpus has no Web3 vendor analogs and
the user explicitly flagged the result as too generic. Threshold tuning
should keep that fixture firing the gate while not firing on pitches
where Norse/Carbon-Black-tier matches are actually correct (e.g.
"competitor to Splunk in industrial OT"). One fixture is too few; spec
mandates at least three calibration fixtures land in
`tests/fixtures/coverage_gate/` before this ships.

## Recall stage (`llm_recall.py`)

Single Opus call. Prompt skeleton:

```
You are filling a coverage gap in a startup post-mortem corpus.

The pitch:
<pitch markdown>

Extracted facets:
<facets json>

The corpus retrieved these comparables, but they are vertically
mismatched (low market/business-model similarity):
<top-N reranked, name + one-line description + axis scores>

Name up to N dead, struggling, absorbed-into-irrelevance, or quietly
discontinued companies in the SAME vertical as the pitch — i.e.
direct or near-direct competitors that failed. Output JSON only:

[{"name": "...",
  "category": "...",
  "status": "dead|absorbed|struggling|bruised",
  "homepage_url": "https://...",
  "failure_year": 2023,
  "evidence_url": "https://...",
  "one_liner": "..."}]

Hard rules:
- If you don't know a real example, output [] — DO NOT INVENT.
- homepage_url must be the company's actual primary domain.
- evidence_url must be a real article, blog post, or press release
  that documents the death/absorption.
- Do not include companies that are alive and well today.
- Do not duplicate any name from the corpus list above.
```

Output is parsed against the `RecallSuggestion` Pydantic model with
strict validation. Schema violations → drop the row, log at WARN.

**Model:** `anthropic/claude-opus-4-7`. Justification:

| Model | Hallucination behavior on company-name recall | Marginal cost vs. Sonnet | Notes |
|---|---|---|---|
| `claude-opus-4-7` | Most willing to refuse / output `[]` when unsure | ~5–10× Sonnet on the recall call | The whole point of this stage; cache amortizes cost. |
| `claude-sonnet-4-6` | Lower refusal rate, more confident-when-wrong | baseline | Acceptable fallback for budget-constrained ops. |
| `claude-haiku-4-5` | Confabulates URLs in structured JSON readily | ~1/30 Opus | Rejected — verification catches but wastes cycles. |

Configurable via `config.model_recall` so the choice is reversible.

**Cap:** `recall_max_suggestions_per_pitch=8` enforced both in the
prompt ("up to 8") and post-parse. Eight is a heuristic — covers
typical "name 3–5 real ones, hedge with 2–3 borderline" without
flooding the verifier with marginal cases.

## Verification (`recall_verify.py`)

Layered, fail-fast. Each suggestion runs L1 → L2 → L3 → L4. Any layer
failing → drop the suggestion, log at INFO with which layer rejected.

| Layer | Check | Cost | Reject if |
|---|---|---|---|
| **L1 — schema** | Pydantic model validation | free | missing/wrong types |
| **L2 — URL HEAD** | `safe_head(homepage_url)` and `safe_head(evidence_url)` with 5s timeout | 1 HTTP HEAD each (cheap, ~50–200ms) | 4xx/5xx, DNS NX, SSRF blocked |
| **L3 — Wayback anchor** | `wayback_snapshot_near(homepage_url, failure_year)` | 1 Wayback CDX call + ~1 fetch | No snapshot in ±180d window |
| **L4 — content match** | Fetch the snapshot; assert canonical-name appears in plain-text body | 1 HTTP GET | Name not present (case-insensitive substring after stripping diacritics) |

L4 is the strongest hallucination check: real company name + real homepage
URL + real Wayback snapshot **whose body contains the name** is
~indistinguishable from a curated entry. The known failure mode is
"squatted domain that pretends to be the company" — rare enough to accept
as residual risk.

**No L5 cross-model verification.** Same-vendor cross-check shares
training-data leakage; cross-vendor (Llama, Gemini) adds an OpenRouter
dependency without commensurate uplift over L4. Defer until a real
miss-class lands.

**Verification fan-out** uses `gather_resilient` with an
`anyio.CapacityLimiter(3)` — Wayback rejects bursts, and we're
verifying at most 8 suggestions, so 3 in flight is the sweet spot.

## Persistence (`recall_persist.py`)

Each verified suggestion produces one `RawEntry`:

```python
RawEntry(
    source=SOURCE_LLM_RECALL,
    source_id=hashlib.sha256(
        f"{name}|{homepage_url}".encode()
    ).hexdigest()[:16],
    url=wayback_snapshot_url,           # the L3-resolved snapshot
    raw_html=None,
    markdown_text=wayback_snapshot_text, # body fetched in L4, reused
    fetched_at=datetime.now(UTC),
)
```

The entry then goes through the **same ingest tail** that crawler entries
go through:

- slop classifier (treats it like any other doc; nothing special)
- facet extract / summarize
- entity resolve
- journal write (terminal-state, transactional, per the load-bearing
  comment in `slopmortem/ingest.py`)
- Qdrant point write

Idempotency: `source_id` is deterministic from `(name, homepage_url)`,
so re-running the same recall on the same pitch produces the same row
key. The journal's existing UPSERT semantics handle the dedup.

## Pipeline wiring

In `pipeline.py`, between the first `llm_rerank` and `select_top_n`:

```python
reranked = await llm_rerank(retrieved, ...)

recall_used = False
recall_persisted_count = 0
coverage_gap = False
if config.enable_llm_recall:
    coverage_gap = detect_coverage_gap(
        retrieved=retrieved,
        ranked=reranked.ranked,
        config=config,
    )
    if coverage_gap:
        progress.start_phase(QueryPhase.RECALL, total=1)
        suggestions = await llm_recall(
            pitch=input_ctx.description,
            facets=facets,
            current_top_n=reranked.ranked[: config.N_synthesize],
            llm=llm,
            model=config.model_recall,
            max_tokens=config.max_tokens_recall,
            cap=config.recall_max_suggestions_per_pitch,
            budget_cap_usd=config.recall_budget_usd_cap,
        )
        verified = await verify_and_persist(
            suggestions,
            corpus=corpus,
            slop_classifier=...,
            embedding_client=embedding_client,
        )
        recall_persisted_count = len(verified)
        if verified:
            recall_used = True
            retrieved = await retrieve(...)        # rerun
            reranked = await llm_rerank(retrieved, ...)  # rerun
        progress.end_phase(QueryPhase.RECALL)
```

The second `retrieve` + `rerank` runs on the augmented corpus. **Only
once.** No while-loop. If the gate still fires, the report flags
`coverage_gap=True` and synthesize runs on what it has.

`pipeline_meta.recall_used`, `pipeline_meta.recall_persisted_count`,
and `pipeline_meta.coverage_gap` join the existing pipeline meta block
in the rendered report so operators can see which queries triggered
the fallback.

## Budget accounting

The recall call goes through the existing `LLMClient` and counts against
the run's budget normally. `config.recall_budget_usd_cap` (default
`$0.50`) is a **per-call ceiling**, separate from the run-level budget,
to prevent a single Opus call from blowing past the run budget on a
huge prompt. If the recall call would exceed the cap, fall back to
Sonnet for that one call (logged at WARN). Verification HTTP traffic is
free (Wayback + HEAD).

## CLI surface

### `--enable-llm-recall`

Boolean opt-in. Default `False`. When set:

- Asserts `OPENROUTER_API_KEY` (already required by existing flow).
- Activates the coverage gate + recall branch in `pipeline.py`.
- No effect when the gate doesn't fire.

The default `just query` stays bit-identical. Eval cassettes don't
shift unless a fixture pitch trips the gate, in which case the recall
call gets its own cassette key.

No per-stage CLI knobs (cap, model, threshold) — those tune via config
or env. CLI surface stays minimal.

## Tracing

`llm_recall` is wrapped:

```python
@observe(
    name="recall.llm",
    ignore_inputs=["llm"],
    ignore_output=False,  # the JSON list is small and useful for audit
)
```

Custom span events (via `slopmortem.tracing.events.SpanEvent`):

| Event | When |
|---|---|
| `RECALL_GATE_FIRED` | Coverage gate returns `True` |
| `RECALL_SUGGESTIONS_RECEIVED` | Opus returned N suggestions |
| `RECALL_VERIFIED` | One suggestion passed L1–L4 |
| `RECALL_REJECTED_L2` / `RECALL_REJECTED_L3` / `RECALL_REJECTED_L4` | Verification dropped a suggestion (per-layer) |
| `RECALL_PERSISTED` | Entry written to journal + Qdrant |

Verification span attributes never include the suggestion's body (could
be long, sensitive in an enterprise pitch). Just URLs and counts.

## Testing

| Test | What it asserts |
|---|---|
| `test_coverage_gate_fires_on_zero` | Empty candidate set → gate `True` |
| `test_coverage_gate_fires_on_low_axis` | Top-N mean market+business_model < 5.0 → gate `True` |
| `test_coverage_gate_quiet_on_good_match` | Top-N mean axes ≥ 5.0 → gate `False` |
| `test_coverage_gate_calibration_fixtures` | Three canonical pitches: Hacken (fire), Splunk-OT (don't fire), placeholder (TBD) |
| `test_recall_prompt_emits_empty_on_uncertain` | `FakeLLMClient` configured with "I don't know" canned response → parses as `[]` |
| `test_recall_caps_at_max` | LLM emits 12 suggestions → only 8 returned |
| `test_recall_cassette_round_trip` | One real Opus call recorded for the Hacken pitch (requires `RECORD=1`); replayed in CI |
| `test_verify_l2_rejects_404` | Mock HEAD returns 404 → suggestion dropped |
| `test_verify_l3_rejects_no_wayback` | Mock `wayback_snapshot_near` returns `None` → dropped |
| `test_verify_l4_rejects_name_absent` | Mock snapshot text doesn't contain canonical name → dropped |
| `test_verify_l4_accepts_name_present` | Snapshot text contains name → entry produced |
| `test_persist_idempotent` | Run persistence twice with same suggestion → one journal row, one Qdrant point |
| `test_persist_purgeable` | After persist, `--purge-source llm_recall` removes the entry |
| `test_pipeline_recall_end_to_end` | Coverage gap → recall fires → 1 verified, 1 rejected → second rerank includes the verified one |
| `test_pipeline_recall_disabled_default` | `enable_llm_recall=False` (default) → gate may detect gap but recall doesn't run; report flags `coverage_gap=True`, `recall_used=False` |
| `test_pipeline_recall_max_one_pass` | After recall + retry, gate still firing → no second recall; flags `coverage_gap=True`, `recall_used=True` |
| `test_reliability_rank_llm_recall` | `(SOURCE_LLM_RECALL, 6)` |

Cassettes live under `tests/fixtures/cassettes/recall/`.

## Pros and Cons of Key Decisions

**Recall stage runs after rerank vs. parallel-with-retrieve:**

- Pros of after rerank: rerank's per-axis scores are the only reliable
  signal that retrieval missed the vertical. Pre-rerank we'd be guessing
  off raw embedding similarity.
- Pros of parallel: lower latency on miss cases (recall + retrieve run
  concurrently).
- Cons of parallel: spends Opus credits even when retrieval would have
  produced strong candidates — defeats the gating logic.
- **Pick after rerank.** Latency penalty on miss cases (~3–5s for the
  recall call) is small relative to the multi-minute synthesize fan-out.

**Persist verified entries to the corpus vs. ephemeral pass-through:**

- Pros of persist: cache-via-corpus, no separate cache infrastructure;
  same retrieval path; future pitches benefit; auditable.
- Pros of ephemeral: zero risk of hallucinated entries leaking into
  unrelated future runs.
- Cons of persist: a slip in verification poisons the corpus
  permanently — until `--purge-source llm_recall`.
- **Pick persist.** Verification gate is the load-bearing assumption;
  if it's strong enough to use the entry once, it's strong enough to
  use it again. Cache-via-corpus is the elegance argument and matches
  how curated entries already work.

**Vertical-axis-collapse threshold default 5.0 vs. 4.0 vs. 6.0:**

- Pros of 5.0: the Hacken fixture (`business_model: 4.0`, `market:
  5.0/3.0` — averages ~4.0) fires the gate; clearly-good matches
  (axes 6+ across the board) don't.
- Pros of 4.0: lower false-positive rate on edge cases.
- Pros of 6.0: higher recall on borderline misses, but doubles Opus
  calls.
- Cons of 5.0: anchored to one fixture; needs the calibration corpus
  before this is real.
- **Pick 5.0 with mandatory three-fixture calibration before merge.**
  Tuneable via config so ops can adjust without a deploy.

**Opus 4.7 vs. Sonnet 4.6 for the recall call:**

- Pros of Opus: lowest hallucination rate on company-name recall;
  willing to refuse with `[]`.
- Pros of Sonnet: 5–10× cheaper; matches existing rerank/synth model.
- Cons of Opus: cost at the per-call level.
- **Pick Opus.** This is the one stage where confident-when-wrong is
  catastrophic; the verifier is a backstop, not a substitute. Cost
  amortizes via cache-via-corpus.

**Single fallback pass vs. iterate until coverage gate clears:**

- Pros of iterate: more chances to find a verified comparable.
- Pros of single pass: deterministic budget; deterministic latency;
  no compounding hallucination risk.
- Cons of single pass: a borderline pitch where one pass yields zero
  verified suggestions has no backup.
- **Pick single pass.** Budget and determinism win; flag
  `coverage_gap=True` so operators see the miss.

**Reliability rank `6` vs. `2` vs. `9`:**

- Pros of `6`: LLM recall is two derivations from primary — model
  weights → prose → Wayback snapshot. Sits below derived reporting
  (Tavily news rank 4) and above dead-letter (rank 9).
- Pros of `2`: verification gate is strict; final entry is
  indistinguishable from curated.
- Pros of `9`: paranoia; treat as last-resort.
- **Pick `6`.** Conservative. The verification gate is strong but the
  source primitive is "model knowledge", which deserves explicit
  ranking penalty in any future rerank tiebreaker. Reversible if
  empirics show it's overcautious.

**Schema-only verification (skip L4 content match) vs. full L1–L4:**

- Pros of skip-L4: ~30% latency reduction (no body fetch); simpler.
- Pros of L4: catches the "right URL, wrong company" failure mode
  that L2+L3 don't (URL exists, Wayback has it, but it's a different
  company that lived at that domain).
- Cons of L4: cost is ~1 GET per suggestion; Wayback flakiness adds
  failure modes.
- **Pick L4.** The failure mode it catches is exactly the kind of
  high-confidence hallucination that survives weaker checks. Latency
  cost is bounded by the per-pitch suggestion cap.

**Per-pitch cap of 8 suggestions vs. 5 vs. 15:**

- Pros of 8: enough headroom for "3 strong, 3 borderline, 2 hedge"
  without flooding the verifier; latency stays bounded
  (~3–5s prompt + 8 × ~2s verify ≈ 20s worst case).
- Pros of 5: tighter bound on bad-data ingestion if verifier slips.
- Pros of 15: maximize recall.
- Cons of 8: arbitrary midpoint; no calibration data yet.
- **Pick 8.** Tunable via config; revisit after calibration runs.

---

## Out of Scope

- **Dynamic gate tuning.** Per-vertical thresholds (different floors
  for crypto vs. semiconductor pitches) would require classifying the
  pitch's vertical first. Defer; one global threshold + manual config
  is enough until empirics demand otherwise.

- **Cross-model verification (L5).** Adding a second-vendor
  (Llama/Gemini) confirmation step adds OpenRouter routing complexity
  and a second cassette family. The L4 content-match check has no
  known evasion mode that L5 would catch independently. Revisit if a
  real false-positive lands in the corpus.

- **Multi-pass recall.** If the first recall pass yields zero
  verified entries, a second pass with a different prompt could
  recover. Defer; the marginal latency and budget cost outweigh the
  rare second-pass save.

- **LLM-recall-driven curation queue.** A natural extension is a
  reviewer surface where humans approve/reject borderline
  suggestions before persistence. Not built now — the verification
  gate is doing the gatekeeping. Revisit if the false-positive rate
  in the persisted entries crosses 5% over a calibration window.

- **Re-recording eval cassettes for non-fallback pitches.** Existing
  eval pitches don't trigger the gate (they're well-covered). If a
  future eval pitch trips the gate, that cassette gains a recall
  call entry — accept that as part of the cassette diff at that
  point.

- **Updating `docs/architecture.md`.** Add a one-line pointer once
  this lands. Full rewrite isn't warranted.

- **Generic "external knowledge fallback" abstraction.** A plugin
  surface that lets operators swap in Tavily-search or a
  GraphRAG-style knowledge base instead of LLM recall is tempting
  but premature. The current shape is purpose-built for the
  hallucination/verification tradeoff; generalizing it before a
  second use case exists would over-engineer.

- **Halborn-style vendor-side curated YAML.** This spec deliberately
  picks the LLM+verify path over hand-curating Web3 security
  vendors. Curated remains available; this doesn't preclude it.
  But the user's explicit constraint — "I don't want curated, I
  want a source" — drove the spec toward a feed-shaped primitive,
  and LLM recall + verification is the closest feed-shaped
  primitive available given the empirical absence of structured
  public failure data for these verticals.
