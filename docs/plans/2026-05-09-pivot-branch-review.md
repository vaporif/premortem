# Pivot branch review plan

A structured review brief for a fresh Claude session. The `pivot` branch has diverged ~85 commits / 82 files / +11,279 / -139 lines from `main`, bundling **seven** distinct workstreams. This plan decomposes the review so each area can be assessed in isolation against its own design intent.

## How to use this plan

Read this top-to-bottom once, then work the workstreams in the order listed below. Each workstream has its own intent doc in `docs/plans/` or `docs/specs/` — read that first, then audit the code against it. Don't try to hold the whole branch in your head; it won't fit.

For each workstream, produce findings in the format at the bottom of this file. At the end, answer the two questions in **§ Final verdict** with evidence.

## Scope

```
git diff main..HEAD --stat
# 82 files changed, 11279 insertions(+), 139 deletions(-)
```

Most-recent polish cluster (`3b78969..HEAD`, 5 commits, 7 files, 72 lines) is **already reviewed** in this session — see chat history. Skip re-reviewing those commits unless cross-cutting concerns surface.

## Workstream map

Listed in the order they were merged in. Read intent docs before code.

| # | Workstream | Commits (first..last) | Intent doc |
|---|---|---|---|
| 1 | Defillama removal cleanup | `bb6bba4..cf6965a` | `docs/plans/2026-05-08-remove-defillama.md` |
| 2 | Ingest infra (concurrent slop check, wayback throttle, algolia fixes, logging) | `4c8779e..7aef5e7` | none — read commit messages |
| 3 | Pitch filler enricher + `single_tool_call` enforcement | `801c40d..5eeaa81` | `docs/plans/2026-05-07-pitch-filler.md`, `docs/plans/2026-05-07-pitch-filler-single-tool-call.md` |
| 4 | Title pre-filter enricher | `103e401..3c47e86` | `docs/plans/2026-05-08-title-pre-filter.md` |
| 5 | Skip already-processed entries | `27d7541..f09f73d` | `docs/plans/2026-05-08-skip-already-processed.md` |
| 6 | Strict sector filter | `9d24034..de755b7` | `docs/plans/2026-05-08-strict-sector-filter.md` |
| 7 | LLM recall fallback (largest — ~3000 lines incl. tests) | `c9846e2..45cb78f` | `docs/plans/2026-05-08-llm-recall-fallback.md`, `docs/specs/2026-05-08-llm-recall-fallback-design.md`, `docs/adr/0001-tavily-grounded-recall.md` |

The polish cluster `3b78969..45cb78f` is already-reviewed; treat as ratified.

## Pre-flight checks (run once, before workstream review)

These should all pass cleanly before you spend time on per-workstream review. If any fail, **stop and fix** — the workstream review will mostly redo what they already cover.

```bash
just lint                 # ruff + format check
just typecheck            # basedpyright strict
UV_CACHE_DIR=/tmp/uv-cache uv run lint-imports   # 7/7 contracts kept expected
just test                 # pytest -n auto, offline-friendly (lazy ONNX stub)
just eval                 # cassette-replay eval; budget-bounded
```

`just eval-record` is **off-limits** without explicit user authorization — it costs ~$2 of LLM credit.

If `just test` reports `PermissionError` from the journal write path, that's a sandbox artifact (this Claude Code session hits it); confirm by running outside the sandbox.

## Per-workstream review brief

For each workstream below, the review checklist is:

1. **Read the intent doc** listed in the workstream map.
2. **Read the diff** scoped to that workstream's commits: `git diff <first-commit>^..<last-commit>`.
3. **Walk the new files top-to-bottom** — they're small enough to read in full.
4. **Validate against the load-bearing rules** in `CLAUDE.md` (search "Load-bearing things to not break") that touch this area.
5. **Produce findings** in the format at the bottom of this file.

### Workstream 1 — Defillama removal

Risk: low. This is a deletion. Verify nothing references the source after removal. Net effect on the working tree should be the absence of `slopmortem/corpus/sources/defillama.py` and friends.

Targeted greps (must all return zero hits):
```bash
grep -rn -i 'defillama' --include='*.py' slopmortem/ tests/
grep -rn -i 'defillama' slopmortem.toml slopmortem/corpus/taxonomy.yml
```

If any hits remain, that's the finding.

### Workstream 2 — Ingest infra (slop / wayback / algolia / logging)

Risk: medium. Concurrency changes always merit a careful read. Files to read fully:

- `slopmortem/ingest/_ingest.py` (the +174 line file — but a large fraction is workstream 5/7, separate it carefully via `git log -p`)
- `slopmortem/corpus/sources/wayback.py` (+20)
- `slopmortem/corpus/sources/hn_algolia.py` (+26)

Validate:
- **Concurrent slop check:** does it use `anyio.CapacityLimiter` and `gather_resilient`, per CLAUDE.md? Per-entry failure must log-and-continue, not abort the run.
- **Wayback throttle:** check the rate-limit handling path — there is a memory note at `~/.claude/projects/-Users-vaporif-Repos-slopmortem/memory/feedback_wayback_rate_limit.md` saying "wait-and-retry on Wayback rate-limit signals, don't drop candidates". Verify the code does this, not the lazy alternative.
- **Logging:** stdlib `logging` only; no `print()` in library code (CLI Rich is fine).

### Workstream 3 — Pitch filler + `single_tool_call`

Risk: high. New enricher + new LLM-protocol kwarg + tool-using LLM call.

Files:
- `slopmortem/ingest/_pitch_filler.py` (+175, new)
- `slopmortem/llm/_pitch_filler_tools.py` (+110, new)
- `slopmortem/llm/openrouter.py` (+25) — the `single_tool_call` per-turn `tool_choice` logic
- `slopmortem/llm/client.py` (+1) — protocol kwarg
- `slopmortem/llm/fake.py` (+2) — fake protocol parity
- `slopmortem/llm/prompts/pitch_filler.j2`
- `tests/ingest/test_pitch_filler.py` (+259)
- `tests/llm/test_openrouter_unit.py` (+61)

Validate:
- **`single_tool_call` semantics:** commit `5eeaa81` says it caps turns at 2. Verify the comment matches the code — read `openrouter.py` and confirm a 2-turn ceiling is actually enforced and the rationale (force a tool call on turn 1, force a final answer on turn 2) is what the code does.
- **Tool execution surface:** the pitch-filler tools file is a new place where the LLM can drive code execution. Audit each tool's input handling. Are URLs/IDs validated? Does the SSRF guard from `slopmortem/http.py` cover all outbound HTTP? (`http.py` got +16 lines this branch — read those changes too.)
- **Prompt-injection surface:** does the pitch filler trust LLM-emitted strings as later inputs to anything sensitive (filenames, SQL, URLs)?
- **`SecretStr` discipline:** confirm no `OPENROUTER_API_KEY` lands in span attributes (Laminar `@observe`) or log lines.

### Workstream 4 — Title pre-filter

Risk: medium. Another LLM-driven enricher; pattern is parallel to pitch filler.

Files:
- `slopmortem/ingest/_title_pre_filter.py` (+131, new)
- `slopmortem/llm/prompts/title_pre_filter.j2`
- `slopmortem/models.py` — `RawEntry.title_pre_filter_rejected` field
- CLI auto-enable for `hn_algolia` source
- `tests/ingest/test_title_pre_filter.py` (+169)

Validate:
- **Skip-guard semantics:** commit `edb1b8b` says ingest honors `title_pre_filter_rejected` in skip-guards. Verify a rejected title actually short-circuits before downstream enrichers run — that's where the cost saving lives.
- **Per-entry failure isolation:** if the Haiku call fails, does the entry continue (treated as not-rejected) or get dropped? CLAUDE.md says per-entry failures log-and-continue.
- **Auto-enable wiring:** is the `hn_algolia` auto-enable a config default, a CLI conditional, or both? Single source of truth, please.

### Workstream 5 — Skip already-processed

Risk: medium. Touches the **journal** which CLAUDE.md flags as load-bearing.

Files:
- `slopmortem/corpus/_merge.py` (+35) — new `is_terminal` lookup
- `slopmortem/ingest/_ingest.py` — skip-guard placement
- `tests/corpus/test_merge_journal.py` (+45)

Validate:
- **Terminal-state definition:** `is_terminal` covers `complete` AND `quarantine` (commit `27d7541`). Verify both states are checked, not just `complete`.
- **Skip placement:** the skip must run **before** enrichers (commit `76f2cd1`). Confirm the call site is in the right phase of the ingest pipeline. If the skip is too late, the enrichers run and the cost saving is gone.
- **Don't bypass the journal:** CLAUDE.md says "Don't add a write path that bypasses the journal." Verify no new code writes to qdrant or the disk corpus without journal coordination.

### Workstream 6 — Strict sector filter

Risk: low-medium. Adds a hard server-side qdrant filter.

Files:
- `slopmortem/stages/retrieve.py` (+4)
- `slopmortem/corpus/_qdrant_store.py` (+56) — bulk of the change
- `slopmortem/corpus/_store.py` (+2) — protocol surface
- `slopmortem/config.py` — `strict_sector_filter` knob
- `tests/corpus/test_qdrant_filter.py` (+77, new)

Validate:
- **Hard filter vs. score boost:** is this a `must` filter (drops non-matches) or a soft boost? Plan name says "strict" — confirm in code that it drops, not just down-ranks.
- **Off-by-default:** flag should default to off so existing behavior is preserved without config.
- **`requires_qdrant` marker:** the live test (`f811b7c`) should be marked, per CLAUDE.md.

### Workstream 7 — LLM recall fallback (largest)

Risk: highest. ~3000 lines (incl. tests). New stage cluster wired into the query pipeline. Also writes new corpus entries with `source=llm_recall`, which interacts with the journal.

Files (new):
- `slopmortem/stages/llm_recall.py` (+113) — `detect_coverage_gap`, `llm_recall`
- `slopmortem/stages/recall_verify.py` (+256) — L1-L4 gates incl. Wayback
- `slopmortem/stages/recall_persist.py` (+106)
- `slopmortem/llm/prompts/llm_recall.j2`
- `slopmortem/models.py` — `RecallSuggestion`, `RecallSuggestionList`

Files (modified):
- `slopmortem/pipeline.py` (+226) — wiring after rerank
- `slopmortem/cli/_query_cmd.py` (+81) — `--enable-llm-recall` flag, `_maybe_build_recall_deps`
- `slopmortem/config.py` — recall-related keys

Tests (new):
- `tests/stages/test_llm_recall.py` (+277)
- `tests/stages/test_recall_verify.py` (+470)
- `tests/stages/test_recall_persist.py` (+213)
- `tests/stages/test_coverage_gate.py` (+174)
- `tests/test_pipeline_recall_fallback.py` (+947)
- `tests/test_cli_query.py` (+202)

Validate (in this order — earlier checks gate later ones):

1. **Read the spec:** `docs/specs/2026-05-08-llm-recall-fallback-design.md` and `docs/adr/0001-tavily-grounded-recall.md`. Walk the data flow on paper before reading code.

2. **Coverage-gap predicate:** when does `detect_coverage_gap` fire? With both `--enable-llm-recall` and `--force-llm-recall`? Off by default? Verify the off-default contract that the polish cluster's lazy-construction test guards.

3. **Three-tier entity resolution interaction:** CLAUDE.md flags this as load-bearing. New `source=llm_recall` entries must go through the same registrable-domain → normalized-name+sector → dense-similarity-with-Haiku-tiebreaker pipeline. The cache key must be lex-sorted. Confirm `recall_persist.py` doesn't bypass any of this.

4. **Slop classifier interaction:** the slop classifier quarantines docs above `slop_threshold` to `post_mortems/quarantine/` with no Qdrant point and no journal row. Recall-injected entries must be subject to the same gate. Confirm in `recall_persist.py`.

5. **Wayback verify L1-L4:** `recall_verify.py` runs four gates. For each tier, confirm the failure mode logs the right `SpanEvent` and stage attribute (`head` vs. `get` per `ba2a47e`). Tier values are a `Literal`; ensure no string typo path makes it past basedpyright.

6. **Prompt-injection marker:** CLAUDE.md says synthesis emits `where_diverged == "prompt_injection_attempted"` as a sentinel. Recall introduces a new LLM call (the Opus recall prompt). Does it have an analogous protection? If yes, what's the marker, and where is it asserted? If no, why not — and is the LLM input domain narrow enough that injection isn't reachable?

7. **`source_id` on recall entries:** commit `d13ac0a` says `source_id` is keyed on `homepage_url`. Verify this matches `_recall_source_id`. The lex-sort cache-key concern doesn't apply here (single-URL key) but the canonicalization should.

8. **Lazy ingest:** the polish round confirmed `_maybe_build_recall_deps` short-circuits when both flags are off (`_query_cmd.py:230-231`). Already-reviewed; spot-check only.

9. **Budget short-circuit:** does the recall stage check `BudgetExceededError` and emit `budget_exceeded=True`? The Opus call is the most expensive thing in the pipeline.

10. **Pipeline ordering:** `468a91d` says recall fires "after rerank". Confirm the call site is between rerank and synthesize, and that synthesis sees the augmented candidate set without re-running rerank.

## Cross-cutting checks (do once across the whole branch)

These don't fit any single workstream:

1. **`slopmortem.evals` → prod isolation.** Forbidden direction. Verify:
   ```bash
   grep -rn 'from slopmortem.evals' --include='*.py' slopmortem/ \
     | grep -v '^slopmortem/evals/'
   ```
   Should return zero lines.

2. **No `# type: ignore`** added in this branch:
   ```bash
   git diff main..HEAD -- '*.py' | grep '^+' | grep 'type: ignore'
   ```
   Each surviving suppression needs a one-line justification per CLAUDE.md.

3. **No `print()` in library code** (CLI Rich is fine):
   ```bash
   git diff main..HEAD -- 'slopmortem/**/*.py' \
     | grep '^+' | grep -E '\bprint\(' | grep -v 'slopmortem/cli/'
   ```

4. **No bare `except Exception: pass`:**
   ```bash
   git diff main..HEAD -- '*.py' | grep -B1 '^+\s*pass\s*$' | grep 'except'
   ```

5. **Pinned-model audit.** CLAUDE.md says don't bump pinned models without re-recording cassettes. Diff `slopmortem.toml` for `model_*` keys; if any moved, confirm cassettes were re-recorded in the same branch (look for a regenerated `tests/fixtures/cassettes/...`).

6. **`slopmortem.local.toml` discipline.** Branch should not have edited `slopmortem.toml` for personal tweaks. If `slopmortem.toml` changed, verify each new key is a new feature default, not a personal override.

7. **Journal migrations.** If `_merge.py` schema changed (workstream 5 adds `is_terminal`), confirm a migration exists or the schema is forward-compatible. The journal is checked into the user's working dir — a breaking schema change without a migration means existing users lose data on `git pull`.

## Verification commands the reviewer should run

```bash
# Pre-flight (must all pass before workstream review):
just lint
just typecheck
UV_CACHE_DIR=/tmp/uv-cache uv run lint-imports
just test                                          # may need TMPDIR override outside this sandbox
just eval                                          # cassette-only, no live API

# Diff scoping helpers:
git log --oneline main..HEAD --reverse | less      # walk the branch chronologically
git diff <commit>^..<commit> -- <path>             # per-workstream slice

# Cross-cutting greps: see § Cross-cutting checks above.

# Don't run:
just eval-record                                   # ~$2, off-limits without user OK
```

## Finding format

For each issue, write:

```
[severity] file:line — one-line summary
  Evidence: <quote of the code or assertion>
  Reasoning: <why this is wrong / risky / off-spec>
  Confidence: <high | medium | low>
  Suggested fix: <one sentence>
```

Severities:
- **blocker** — incorrect behavior, data loss, security hole, or violates a CLAUDE.md "load-bearing" rule.
- **major** — likely bug, missing test for a real edge case, perf regression in a hot path.
- **minor** — style, dead code, unclear comment, cosmetic test polish.
- **nit** — pure preference; the author can ignore.

Default to fewer findings, higher signal. Don't pad with style nits when the load-bearing rules haven't all been verified.

## Final verdict

After working all workstreams + cross-cutting checks, answer:

1. **Is the branch safe to merge to `main`?** Yes / No / Conditional. If conditional, list the must-fix-first findings by ID.
2. **What's the smallest set of follow-up work that should land before merge vs. after?** Two lists.

Keep the verdict to under 200 words.

## What's already-reviewed (don't redo)

- The polish cluster `3b78969..HEAD` (5 commits, 7 files, 72 lines) — reviewed in the session that produced this plan. Findings: lazy-contract test sentinel never fires under correct behavior (low); Polish Step 3 in the LLM-recall plan unticked while 1/2/4 are ticked (low). Lint, typecheck, import-linter all green for this cluster.

## Out of scope

- Style preferences not encoded in `CLAUDE.md` or `ruff.toml`.
- Re-litigating decisions documented in the workstream plans / specs / ADRs. Read those, accept the decision, audit the implementation against it.
- The eval recording itself (`just eval-record` is off-limits without user OK).
