# Recall Extraction — Design

**Status:** draft
**Date:** 2026-05-12
**Branch context:** `main` after `Pivot (#45)`. Builds on the LLM-recall fallback shipped in
`docs/specs/2026-05-08-llm-recall-fallback-design.md` and its follow-up plans
(`2026-05-09-recall-always-on.md`, `2026-05-09-recall-fallback-improvements.md`,
`2026-05-10-recall-verifier-hardening.md`, `2026-05-11-recall-search-then-verify.md`).

## Goal

Lift the recall step out of the query pipeline into a self-contained subsystem
that **finds similar startups and decides which ones are correct** behind one
narrow entrypoint. Reusers (eval harness, ad-hoc CLI, replay tooling) should be
able to call recall without touching the journal, slop classifier, sparse
encoder, embedding client, or Qdrant corpus.

Today, "recall" is a verb the pipeline conjugates inline:
`pipeline._run_recall_branch` directly assembles `llm_recall` → `verify_and_persist_all`
→ `persist_recall_entry` and immediately re-runs `retrieve` + `llm_rerank`. The
recall surface depends on every long-lived dependency in the application, even
though most of them only matter for the write-back path.

After this change, recall is a noun — a small package with one async function,
four runtime dependencies, and a configuration record. Persistence,
re-retrieval, and the coverage-gap predicate stay in the pipeline.

**Non-goal:** changing recall's *behaviour*. The L0–L5 ladder, deathness
thresholds, prompt templates, span event *names* and *attributes*, and
cassette keys are unchanged. This is a packaging refactor, not a logic
refactor. One trace-shape change is unavoidable and documented under
"Tracing parity": `RECALL_PERSISTED` reparents from `stage.recall_verify`
to the `query` root when persistence moves pipeline-side.

## Pros & cons (extraction overall)

**Pros**
- Single entrypoint, four-dep surface. `FakeRecaller` collapses the
  brainstorm + verifier fake stack for *verifier-internal* tests (today
  ~4 of the 12 tests in `tests/test_pipeline_recall_fallback.py`). Pipeline
  tests that exercise the predicate gate, post-recall floor, or persist
  tail still need the corpus / journal / embed / sparse fakes — see
  "Testing strategy".
- Eval, replay, and ad-hoc tooling can use recall without wiring up the
  journal, slop classifier, sparse encoder, or corpus.
- Pipeline shrinks; the "what does recall do" question has one answer in
  one package.

**Cons**
- Duplicate facet extraction at runtime when callers do not pre-pass facets.
  Bounded by the optional `facets=` kwarg the pipeline uses on the hot path.
- Coverage-gap predicate is split from brainstorm — they were co-located
  today. Reader must look in two places to see the recall gate end-to-end.
- `RECALL_PERSISTED` span event reparents from `stage.recall_verify` to the
  `query` root span (see "Tracing parity"). Counts and attributes unchanged;
  trace-join queries that pivot on the parent span name will need updating.
- One more package boundary to maintain. Worth it for reuse, but it is
  real overhead.

**Why we chose this:** the pain today is shape, not behaviour. The recall
branch is the only place in the codebase that needs every long-lived
dependency; lifting it out cuts the pipeline's surface area roughly in half
and makes the verifier independently exercisable.

## Surface

```python
# slopmortem/recall/__init__.py

async def recall(
    pitch: str,
    *,
    facets: Facets | None = None,
    prior_hints: list[PriorCandidateHint] | None = None,
    deps: RecallDeps,
    config: RecallConfig,
) -> list[VerifiedEntry]: ...
```

- `pitch` — the user's startup description. The only required input.
- `facets` — optional pre-extracted facets. Pipeline passes the value it
  already computed for retrieve/rerank to avoid a duplicate Haiku call;
  eval / CLI / replay callers pass `None` and recall extracts facets itself.
- `prior_hints` — optional dedup hints (name + reranker rationale per
  already-covered candidate). Pipeline computes these from
  `reranked.ranked[:N_synthesize]` joined back to `retrieved` and passes
  them through so Opus doesn't waste an L0–L5 walk re-suggesting names
  the corpus already covers. `None` (or `[]`) renders the prompt's
  "(none — corpus returned no in-vertical matches)" branch — the same
  shape the prompt sees today when retrieve returns empty. `PriorCandidateHint`
  is re-exported from `slopmortem.recall` so the pipeline can build the
  list without importing recall internals.
- `deps` — runtime dependencies that cannot be defaulted from config:
  the LLM client, the two Tavily callables (search + extract), and the
  Wayback enricher.
- `config` — model names, max-tokens, caps, the deathness gate bundle.

The function returns a list of `VerifiedEntry` records — one per suggestion
that passed L0–L5. Empty list on no-evidence, hallucination, or transport
failures. The function never raises for per-suggestion failures; only
unexpected programming errors (e.g. invalid config) propagate.

### Records

```python
@dataclass(frozen=True)
class RecallDeps:
    llm: LLMClient
    tavily_search: TavilySearchFn
    extract: ExtractFn
    # Optional so eval / CLI / replay callers don't have to construct one.
    # `recall()` falls back to `WaybackEnricher()` lazily when None.
    wayback: Enricher | None = None

@dataclass(frozen=True)
class RecallConfig:
    # facet extraction (used only when facets=None at call time)
    model_facet: str
    max_tokens_facet: int
    # brainstorm
    model_recall: str
    max_tokens_recall: int
    suggestion_cap: int          # was: Config.recall_max_suggestions_per_pitch
    tools: list[ToolSpec]        # see note below — `[]` is the documented "tools disabled" state
    max_tavily_calls: int        # was: Config.recall_max_tavily_calls; surfaced in the brainstorm prompt
    # verifier
    tavily_max_results: int      # was: Config.tavily_recall_max_results
    deathness: DeathnessConfig

@dataclass(frozen=True)
class VerifiedEntry:
    entry: RawEntry
    tier: VerificationTier
    verdict: Literal["dead", "struggling"]
```

The field names listed alongside `was: …` are renames local to
`RecallConfig` — the package surface uses shorter names than the global
`Config` to keep the record readable. The pipeline-side
`_recall_config_from(config)` builder owns the mapping. **Don't** add
`suggestion_cap` or `max_tavily_calls` to global `Config`; they exist only
on this record.

`RecallConfig` exists so the recall package does not import `slopmortem.config`.
The pipeline builds a `RecallConfig` from the global `Config` at the call site;
test code builds one inline. This is the same split `DeathnessConfig` already
uses inside `stages/recall_verify.py`.

**The `tools` field collapses two global flags.** Today `recall_tools(config)`
(`slopmortem/llm/tools.py:151-178`) reads both `enable_tavily_recall_search`
and `recall_max_tavily_calls`, returning `[]` when either is off or the cap
is 0. The recall package treats `tools=[]` as the canonical "tools disabled"
state — neither flag survives as a separate `RecallConfig` field. The
pipeline-side `_recall_config_from(config)` calls `recall_tools(config)` once
and stores the result on `RecallConfig.tools`. Test code building
`RecallConfig` inline either imports `recall_tools` or supplies a hand-rolled
`[ToolSpec]` list. Document this in the `tools` docstring on `RecallConfig`
so a future reader doesn't add a redundant boolean.

## Package layout

```
slopmortem/recall/
  __init__.py        # public re-exports: recall, VerifiedEntry, RecallDeps,
                     # RecallConfig, PriorCandidateHint, FakeRecaller
  _brainstorm.py     # was stages/llm_recall.py (minus compute_coverage_gap).
                     # Carries PriorCandidateHint with it.
  _verify.py         # was stages/recall_verify.py (no persist callback;
                     # _search_for_evidence stays inside _one()'s fan-out)
  _models.py         # RecallSuggestion, VerifiedEntry, VerificationTier,
                     # RecallContext private helpers
  death_keywords.yml # was stages/death_keywords.yml; loaded via Path(__file__).parent
  fake.py            # FakeRecaller for tests / cassette-backed eval
```

The new package is read-only with respect to the corpus. It uses
`slopmortem.corpus` only for the `Enricher` protocol (public on
`corpus.sources`) and `extract_clean` (public on `slopmortem.corpus`,
imported today at `stages/recall_verify.py:60` via
`from slopmortem.corpus import extract_clean`).
`TavilySearchFn` and `ExtractFn` are recall-local type aliases — they
currently live in `stages/recall_verify.py:84,90` and travel with the
file into `recall/_verify.py`. Concrete callables for the Tavily
wrappers are constructed at the pipeline seam via
`slopmortem.deps.build_tavily_recall_{search,extract}` and passed
through `RecallDeps`.

**One pre-existing private reach needs a decision before the move.**
`stages/recall_verify.py:61` does
`from slopmortem.corpus.sources._names import SOURCE_LLM_RECALL`. That
import travels with `_verify.py` into the recall package and would put
the new package in violation of "no reach into `corpus.sources._*`".
Promote `SOURCE_LLM_RECALL` to `slopmortem.corpus.sources`'s public
`__all__` (one-line export) as part of this refactor — it's a stable
identifier and other call sites can be cleaned up incrementally.

### What moves out of `slopmortem/stages/`

| File today | New home | Why |
|---|---|---|
| `stages/llm_recall.py` (brainstorm half) | `recall/_brainstorm.py` | Owns LLM call. `PriorCandidateHint` follows it; the recall package re-exports the model. |
| `stages/llm_recall.py` (`compute_coverage_gap`) | `stages/coverage_gap.py` | Pipeline-side predicate, not recall-internal. No leading underscore — every other `stages/` module is unprefixed. |
| `stages/recall_verify.py` | `recall/_verify.py` | The L0–L5 ladder is half of "find + decide". |
| `stages/recall_persist.py` | unchanged | Persistence stays in `stages/`; pipeline calls it after recall returns. |

`stages/death_keywords.yml` follows `_verify.py` into `recall/`. The file is
loaded relative to the verifier; the move is path-local.

### What stays in the pipeline

- `compute_coverage_gap` — the predicate that gates the recall call.
- `persist_recall_entry` — the journal + Qdrant write-back.
- Re-`retrieve` and re-`llm_rerank` after persistence, plus the
  `RECALL_GAP_SCORE_AFTER` span event that mirrors the pre-recall shape.
- `RecallDeps` — same name, narrowed contents. The current
  `pipeline.RecallDeps` (journal, slop_classifier, post_mortems_root,
  tavily_search, extract, wayback) splits into `recall.RecallDeps`
  (tavily_search, extract, wayback, llm) and `PersistDeps` (journal,
  slop_classifier, post_mortems_root, corpus, embedding_client,
  sparse_encoder, llm — yes the LLM threads through to both records;
  ``llm`` is the slop-classifier substrate inside `classify_phase`).
  Per-call `progress=NullProgress()` and `result=IngestResult()` continue
  to be constructed inline in the pipeline persist loop (see sketch below);
  they're not record-resident because nothing reads them outside the call.

## Pipeline-side reshape

Before:

```python
outcome = await _run_recall_branch(
    input_ctx=input_ctx, facets=facets, retrieved=retrieved, reranked=reranked,
    cutoff=cutoff, llm=llm, embedding_client=embedding_client, corpus=corpus,
    config=config, sparse_encoder=sparse_encoder, recall_deps=recall_deps,
)
```

After (sketch):

```python
# Pipeline still computes hints — recall() accepts them so dedup behaviour
# is preserved on the production hot path. Eval / CLI callers omit the kwarg.
by_id: dict[str, Candidate] = {c.canonical_id: c for c in retrieved}
prior_hints = [
    PriorCandidateHint(name=by_id[sc.candidate_id].payload.name, rationale=sc.rationale)
    for sc in reranked.ranked[: config.N_synthesize]
    if sc.candidate_id in by_id
]

verified = await recall(
    input_ctx.description,
    facets=facets,
    prior_hints=prior_hints,
    deps=RecallDeps(
        llm=llm,
        tavily_search=recall_deps.tavily_search,
        extract=recall_deps.extract,
        wayback=recall_deps.wayback,  # recall() lazy-defaults to WaybackEnricher() when None
    ),
    config=_recall_config_from(config),
)

# Concurrent + failure-isolated persistence. Today verify_and_persist_all
# fans persist out through gather_resilient with a CapacityLimiter(3), and
# a per-suggestion failure doesn't poison the batch. Preserve both
# properties: concurrent fan-out + isolated failures + count only what
# actually landed.
async def _persist_one(v: VerifiedEntry) -> bool:
    try:
        await persist_recall_entry(
            v.entry, v.tier,
            deathness_verdict=v.verdict,
            journal=recall_deps.journal,
            corpus=cast("IngestCorpus", corpus),
            embed_client=embedding_client,
            llm=llm,
            slop_classifier=recall_deps.slop_classifier,
            sparse_encoder=sparse_encoder,
            config=config,
            post_mortems_root=recall_deps.post_mortems_root,
            progress=NullProgress(),
            result=IngestResult(),
        )
    except Exception as exc:  # noqa: BLE001 - per-suggestion isolation
        logger.warning("persist_recall_entry failed for %r: %r", v.entry.title, exc)
        return False
    if Laminar.is_initialized():
        Laminar.event(
            name=str(SpanEvent.RECALL_PERSISTED),
            attributes={"tier": v.tier, "deathness_verdict": v.verdict},
        )
    return True

persist_results = await gather_resilient(*(_persist_one(v) for v in verified))
persisted_count = sum(1 for r in persist_results if r is True)

if persisted_count > 0:
    new_retrieved = await retrieve(...)
    new_reranked = await llm_rerank(...)
    # GAP_SCORE_AFTER event unchanged
```

Two behavioural invariants this sketch preserves explicitly:

1. **Concurrency.** Today persist runs inside `verify_and_persist_all`'s
   `gather_resilient` under a `CapacityLimiter(3)`. The new shape keeps
   `gather_resilient` (per-suggestion failure isolation) and lets the LLM
   client's existing connection pool act as the natural rate-limit. If
   `recall_max_suggestions_per_pitch` ever rises above the safe-parallel
   ceiling for Qdrant, wrap the loop in a `CapacityLimiter(3)` to match
   today's bound — out of scope here.
2. **`persisted_count` counts what landed, not what was verified.** Today
   `_one()` doesn't try/except around `await persist(...)` — a persist
   exception propagates out of `_one`, `gather_resilient` absorbs it, and
   the trailing `isinstance(r, RawEntry)` filter (`recall_verify.py:969`)
   drops the non-`RawEntry` sentinel so the returned list reflects
   committed writes. The new `_persist_one` shape makes this explicit with
   a try/except returning a `bool`; `persisted_count` then sums only `True`
   returns. Observable count is preserved; the mechanism is now
   pipeline-local rather than relying on gather-then-filter inside the
   verifier. `recall_persisted_count` reported on `pipeline_meta` keeps
   its "actually persisted" meaning.

`recall_used = persisted_count > 0` matches the spirit of today's
`_RecallOutcome.used = bool(verified after persist)` — if every persist
fails, recall didn't usefully contribute to this query.

## Behaviour changes (one wart, scoped)

1. **Facet extraction inside recall** when `facets=None`. Adds one Haiku
   call per recall fire on that path. The pipeline always passes the facets
   it already extracted upstream, so production paths are unchanged. Eval,
   CLI, and replay callers pay the extra call; cassette key is identical to
   the pipeline's facet call, so canned responses are reused — no re-record.

Dedup hints (`prior_hints` / `current_top_n`) are *not* dropped: the
`recall()` entrypoint accepts them as an optional kwarg, the pipeline
passes the same hints it computes today, and the prompt template behaviour
is unchanged on the hot path. Eval / CLI / replay callers pass `None` and
get the empty-hints branch — that branch already exists in the template
today for queries where retrieve returns nothing, so the prompt shape is
not new.

**Cassette stability.** The recall prompt template
(`slopmortem/llm/prompts/llm_recall.j2:37-44`) renders `current_top_n`
inline. Pipeline-driven recordings keep producing the same `prompt_hash`
they do today because the same hints are passed through. An audit of
`tests/fixtures/cassettes/` on 2026-05-12 found zero recorded
`stage.llm_recall` interactions anyway (only the Tavily preflight YAML
lives under `cassettes/recall/`), so even a hypothetical prompt drift
would not break anything on disk. Anyone with un-committed `.live.yaml`
cassettes should record on `main` *before* picking this branch up.

The facet change does not alter the recall report contents or the
pipeline meta.

**Tracing parity.** Every span (`stage.llm_recall`, `stage.recall_verify`)
and event keeps its current *name* and *attributes*. Decorators travel
with the files, so `stage.llm_recall` and `stage.recall_verify` parent
their existing children (`RECALL_SUGGESTIONS_RECEIVED`,
`RECALL_VERIFIED_*`, `RECALL_REJECTED_*`, `RECALL_L0_*`,
`RECALL_L3_EXTRACT_FALLBACK_RECOVERED`) without change.
`RECALL_GAP_SCORE`, `RECALL_GAP_SCORE_AFTER`, and `RECALL_GATE_FIRED`
fire from the pipeline today and keep firing from the pipeline.

**One unavoidable reparent: `RECALL_PERSISTED`.** Today the persist
callback runs inside `_one()` *inside* the `@observe(name="stage.recall_verify")`
fan-out, so `RECALL_PERSISTED` events nest under `stage.recall_verify`.
After this refactor persistence runs pipeline-side, so the event nests
under the `query` root span instead. Counts and attributes (`tier`,
`deathness_verdict`) are unchanged; any trace-join query that pivots on
"events under `stage.recall_verify`" needs to widen to "events on the
`query` trace" for `RECALL_PERSISTED`. No dashboards in-repo depend on
this; flag for the operator review at landing.

## Testing strategy

- **Existing tests of `llm_recall` and `recall_verify` follow the files**
  into `tests/recall/`. Import paths update; assertions do not.
- **`tests/test_pipeline_recall_fallback.py` mostly stays in place.** The
  file has 12 async tests across four clusters: predicate-gate
  (`coverage_gap`/`GAP_SCORE`/`GATE_FIRED`), force-flag (`force_llm_recall`
  bypass and `RecallDeps`-missing error), single-pass guarantee, and
  post-recall floor (`min_similarity_score_after_recall` pre- and
  post-synth). Predicate-gate and floor tests stay pipeline-side because
  they exercise the seam, not recall internals — those tests still need
  the corpus/journal/embed/sparse fakes. Only the per-suggestion
  happy-path assertions migrate alongside `_verify.py`. Plan for ~8 tests
  staying, ~4 moving.
- **Monkeypatch paths update.** Tests like
  `tests/test_pipeline_recall_fallback.py:466-467` patch
  `slopmortem.stages.recall_verify.safe_head` / `safe_get`. After the move
  those become `slopmortem.recall._verify.safe_head` / `safe_get`. Grep
  for `slopmortem.stages.recall_verify` and `slopmortem.stages.llm_recall`
  across `tests/` and rewrite as part of the refactor PR.
- **New tests for `recall(...)` top-level** cover: facets passed vs.
  facets extracted internally, `prior_hints=None` vs. populated,
  empty-suggestions short circuit, verifier-drops-all short circuit,
  mixed-verdict admit/drop, transport failures isolated per suggestion.
- **`FakeRecaller`** lives in `recall/fake.py`. Returns a configurable
  list of `VerifiedEntry` records. Pipeline tests that don't care about
  recall internals (e.g. floor / dedup / re-retrieve assertions) inject it
  via the new boundary — this is the test-simplification win. Predicate
  and gate tests still drive the pipeline with the predicate inputs they
  use today.
- **Cassettes** in `tests/fixtures/cassettes/recall/` continue to work —
  prompt templates and model names are unchanged, so the
  `(template_sha, model, prompt_hash)` keys are stable.

## Out of scope

- Reworking the L0–L5 ladder. Hardening tickets (`2026-05-10-recall-verifier-hardening.md`,
  `2026-05-11-recall-search-then-verify.md`) own that surface; this spec
  preserves their changes verbatim.
- Changing the coverage-gap predicate. It moves files but its inputs,
  outputs, and event names stay identical.
- Eval cassette re-recording. The facet call inside recall reuses the
  pipeline's cassette key; re-record is only required when prompts or
  models actually change, which this spec does not. **Caveat:** the
  in-repo eval baseline does not exercise recall today —
  `tests/fixtures/cassettes/evals/*/` contains only `embed/facet_extract/
  synthesize` JSON, no `llm_recall` / `recall_verify` / `recall_deathness`
  recordings. "Eval baseline does not move" is therefore trivially true
  for this refactor, but it does not validate behaviour-preservation on
  the recall path. The pipeline test suite carries that load; adding a
  recall-firing eval fixture is a worthwhile follow-up but out of scope here.
- Exposing recall as a Typer subcommand. Possible follow-up once the
  package boundary exists; not a blocker for the extraction itself.

## Implementation outline

Single vertical slice. Mechanical refactor; the diff is large but the
behaviour-bearing surface is small.

1. Create `slopmortem/recall/` package skeleton with re-exports.
   Promote `SOURCE_LLM_RECALL` to the public surface of
   `slopmortem.corpus.sources` (one-line `__all__` addition) so
   `_verify.py` doesn't carry a private import after the move.
2. Move `stages/recall_verify.py` → `recall/_verify.py` (carry
   `death_keywords.yml` alongside — path-local). Drop the `persist`
   callback parameter from `verify_and_persist_all`; rename to
   `verify_all`. Change the return shape from `list[RawEntry]` to
   `list[VerifiedEntry]`: today `_one()` (`recall_verify.py:947-966`)
   discards `(tier, verdict)` after calling `persist` and yields the
   bare entry, so the per-suggestion path has to be reshaped to surface
   the full triple instead of just the entry. **`_search_for_evidence`
   stays inside `_one()`'s closure**, not at recall-level: per-suggestion
   isolation (a single 403 host shouldn't kill the batch) depends on
   the search-then-verify pair living inside the same `gather_resilient`
   leaf. Mechanical reshape, but more than a parameter rename.
3. Move the brainstorm half of `stages/llm_recall.py` → `recall/_brainstorm.py`.
   `PriorCandidateHint` travels with it; re-export from
   `slopmortem.recall.__init__` so the pipeline can build the list without
   importing private modules. Move `compute_coverage_gap` into a new
   `stages/coverage_gap.py` (no underscore prefix — matches existing
   `stages/` naming).
4. Add `recall(...)` entrypoint in `recall/__init__.py` that composes
   facet extraction (when `facets=None`), `llm_recall` (passing
   `prior_hints` through, defaulting to `[]` when `None`), and `verify_all`.
   Lazy-default `RecallDeps.wayback` to `WaybackEnricher()` inside
   `recall()` so the seam doesn't need the `or WaybackEnricher()` it does
   today.
5. Add `FakeRecaller` in `recall/fake.py`.
6. Update `slopmortem/pipeline.py`: shrink `_run_recall_branch` to compute
   `prior_hints`, call `recall(...)`, then run `gather_resilient` over the
   per-suggestion `_persist_one` closures (failure-isolated, concurrent)
   and re-`retrieve` / re-`llm_rerank`. Split `pipeline.RecallDeps` into
   the narrow `recall.RecallDeps` (4 fields) plus a pipeline-side
   `PersistDeps` (7 fields — see "What stays in the pipeline"). The
   `force_llm_recall` guard at `pipeline.py:443-445` now checks the
   pipeline-side composite (`recall_deps is None or persist_deps is None`)
   so explicit operator opt-in still surfaces misconfig.
7. Move tests; rewrite monkeypatch paths from
   `slopmortem.stages.recall_verify` → `slopmortem.recall._verify` and
   `slopmortem.stages.llm_recall` → `slopmortem.recall._brainstorm`. Add
   tests for the new entrypoint, `FakeRecaller`, and the pipeline's
   `_persist_one` failure-isolation behaviour (one persist raising must
   not abort siblings; `persisted_count` reflects committed writes).
8. Run `just lint`, `just typecheck`, `just test`, `just eval` to confirm
   parity. Eval baseline does not exercise recall (see "Out of scope")
   so it should be byte-identical; the load-bearing parity signal is
   `just test`.

## Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Persistence concurrency regression | Med | Pipeline's `_persist_one` loop runs under `gather_resilient` (see sketch). Preserves today's concurrent fan-out and per-suggestion failure isolation. **Don't** rewrite to a serial `for v in verified: await persist(...)` loop — at `recall_max_suggestions_per_pitch=8` that would serialize up to 8 ingest tails (each ~1-3s) on the query critical path. |
| `RECALL_PERSISTED` reparenting | Med | Documented under "Tracing parity". Event nests under the `query` root instead of `stage.recall_verify` because persist is pipeline-side. Counts and attributes unchanged. Operator review at landing: confirm no Laminar dashboard query pivots on the old parent. |
| Persist failure isolation lost | Med | `_persist_one` wraps `persist_recall_entry` in `try/except` and returns a bool; `gather_resilient` collects them; `persisted_count` counts successes. Same shape as today's `verify_and_persist_all` per-suggestion isolation. |
| Eval cassette continuity | Low | Dedup hints are *not* dropped (passed through via `recall(prior_hints=...)`), so the pipeline-side `prompt_hash` is unchanged. The internal facet call reuses the pipeline's `(template_sha, model, prompt_hash)` key on the `facets=None` path. Audit 2026-05-12: `tests/fixtures/cassettes/` carries zero `stage.llm_recall` recordings anyway. |
| Eval baseline "parity" overstated | Low | Eval doesn't fire recall today (no `llm_recall` / `recall_verify` cassettes under `evals/`). The signal that matters is `just test`; eval being byte-identical is necessary but not sufficient. Documented under "Out of scope". |
| `recall_used` / `persisted_count` semantics drift | Low | `persisted_count = sum(1 for r in persist_results if r is True)`; `recall_used = persisted_count > 0`. Matches today's "useful contribution" semantics (verified entries actually landed in the corpus). |
| Pipeline test churn | Med | ~9 of 13 tests in `test_pipeline_recall_fallback.py` stay (predicate, force, floor, error paths); ~4 verifier-internal tests follow `_verify.py`. Monkeypatch paths (`slopmortem.stages.recall_verify.safe_*`) update to `slopmortem.recall._verify.*` — grep `tests/` and rewrite. Net work is bounded but not zero. |
| Import-linter contract | Low | Recall package depends only on `slopmortem.llm`, `slopmortem.models`, `slopmortem.http`, `slopmortem.concurrency`, `slopmortem.corpus` (for the `Enricher` protocol on `corpus.sources` and `extract_clean` on `slopmortem.corpus`), and `slopmortem.tracing`. The one private reach in today's `recall_verify.py:61` (`from slopmortem.corpus.sources._names import SOURCE_LLM_RECALL`) is resolved by promoting `SOURCE_LLM_RECALL` to `corpus.sources`'s public surface as part of Step 1. No reach into `slopmortem.ingest._*`. |
| `force_llm_recall` guard wiring | Low | After the dep split, the guard checks both `recall_deps` and `persist_deps` for `None` so force-mode without either still raises `RuntimeError("force_llm_recall=True but RecallDeps not provided")`. Documented under "Implementation outline" step 6. |

## Execution Strategy

**Subagents.** This is a mechanical refactor with one cohesive change
across recall files, the pipeline, and tests. Splitting it across
parallel agents would create merge contention on `pipeline.py` and the
test layout. The dependency graph is fundamentally sequential.

1 task, AFK, no parallelism. Reason: file moves and the pipeline reshape
share too many files to parallelise; a single agent doing the whole cut
is faster than coordinating disjoint slices.

## Task Dependency Graph

- **Task 1 — Extract recall package** (`AFK`, predecessors: `none`):
  end-to-end vertical slice. Create package, move files, drop persist
  callback, add `recall(...)` entrypoint, reshape pipeline branch, move
  tests, add new tests, confirm `just lint && just typecheck && just
  test && just eval` parity.

## Agent Assignments

```
Task 1: Extract recall package  → python-development:python-pro   (Python)
Polish:                         → python-development:python-pro   (Python)
```
