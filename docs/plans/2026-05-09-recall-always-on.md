# 2026-05-09 — LLM-recall fallback: always-on

## Why

The LLM-recall fallback (added per `2026-05-08-llm-recall-fallback.md`) shipped opt-in (`enable_llm_recall=False`). In practice every "no candidates clear the floor" run on a thin-corpus vertical (e.g. Web3 on-chain security) bottoms out at the empty-state banner and exit 1, when the recall predicate would have fired had the flag been on. The flag added a footgun without buying anything: callers either want the fallback (production) or want it explicitly off-AND-replaced with `force_llm_recall` (cassette/eval).

## What changes

- `Config.enable_llm_recall` is removed. The pipeline gates the recall branch on `coverage_gap` alone (predicate already in `slopmortem/stages/llm_recall.py:23`, unchanged).
- `Config.force_llm_recall` stays — still useful for cassette recording and eval calibration where you want recall every run regardless of the gate.
- `slopmortem query` drops `--enable-llm-recall/--no-llm-recall`. `--force-llm-recall/--no-force-llm-recall` stays.
- The CLI always builds `RecallDeps` (`MergeJournal` + `HaikuSlopClassifier`) for non-debug queries. Cold-start cost is dominated by sqlite open + table check, well under 50ms; cheap enough to pay every query so the recall branch can fire when the predicate trips.

## What stays

- The predicate threshold (`survivors < N_synthesize`, with the in-sector requirement when `pitch_sector != "other"`). User confirmed: "below 5" maps to N_synthesize.
- `min_similarity_score = 4.0` cutoff. Recall changes the candidate pool; rerank+select still runs after, and the empty-state banner still fires when even recall can't find anything that clears the floor.
- `force_llm_recall=True` still requires `RecallDeps`; pipeline raises if missing (eval misconfig surfaces loudly).

## Files touched

- `slopmortem/config.py` — drop `enable_llm_recall` field; rewrite `force_llm_recall` comment.
- `slopmortem/pipeline.py` — remove `if config.enable_llm_recall:` guard around `detect_coverage_gap`; `should_fire = coverage_gap or config.force_llm_recall`; update `run_query` docstring.
- `slopmortem/cli/_query_cmd.py` — remove `--enable-llm-recall/--no-llm-recall` Typer option and the `model_copy({"enable_llm_recall": ...})` line; rename `_maybe_build_recall_deps` to `_build_recall_deps` and call unconditionally (still skipped on `--debug-retrieve` since that path returns before).
- `tests/test_cli_query.py` — drop `test_enable_llm_recall_flag`; rewrite `test_default_no_flags_skips_recall` to assert `recall_deps` is now non-None on the default path.
- `tests/test_pipeline_recall_fallback.py` — drop `enable_llm_recall` arg from `_build_config` helper; tests for the gate-disabled path move to "coverage_gap=False ⇒ no recall".
- `docs/architecture.md` — flip the "opt-in, off by default" line on `4b. LLM recall fallback`.
- `docs/plans/2026-05-08-llm-recall-fallback.md` — append a note pointing to this plan.

## Cost note

The recall LLM call is ~$0.05–0.15 *per fire*. Predicate fires when survivors < N_synthesize (rare on a healthy vertical, common on a thin/wrong vertical). The per-query budget cap (`max_cost_usd_per_query=2.00`) still bounds total spend.

## Out of scope

- Adding a separate recall-cost cap: budget already caps total query spend; a stage-specific cap would add config without tightening the upper bound.
- Caching recall results across queries: the recall branch already persists verified suggestions to Qdrant as `source=llm_recall`, so subsequent queries see them via normal retrieval.
- Telemetry rename: `RECALL_GATE_FIRED` SpanEvent semantics are unchanged (predicate fires); only the precondition was the flag, which is gone.
