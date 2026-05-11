# Pivot Branch Cleanup — Tier 1 + Tier 2 Fixes

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or `/team-feature` to implement this plan task-by-task, per the Execution Strategy below. Steps use checkbox (`- [ ]`) syntax — these are **persistent durable state**, not visual decoration. The executor edits the plan file in place: `- [ ]` → `- [x]` the instant a step verifies, before moving on. On resume (new session, crash, takeover), the executor scans existing `- [x]` marks and skips them — these steps are NOT redone. TodoWrite mirrors this state in-session; the plan file is the source of truth across sessions.

**Goal:** Land the defensive fixes and dead-code cleanups from the `pivot` branch review — one concern per commit, no cassette re-records.

**Architecture:** Ten small, file-isolated edits across the ingest journal-write path, models, CLI, and tracing helpers. All edits are tracing guards, exhaustiveness defenses, dead-code deletions, or validator-tightening that the existing test suite covers. No user-visible behavior changes.

**Out of scope:** The recall post-synth threshold question (originally Task 1) is a **design choice, not a bug** — the pre-synth and post-synth filters score different signals (rerank score vs. synthesis score). Aligning them changes which judge wins for recall-path entries, which is a separate decision that needs ADR/owner sign-off before any code change. Track it elsewhere.

**Tech Stack:** Python 3.13, `anyio`, Pydantic v2, `typer`, `basedpyright` strict, `pytest -n auto` with cassette-replay fixtures.

## Execution Strategy

**Subagents** — default, no spec override. The tasks are independent enough to dispatch in parallel but small enough that a single subagent per task with the project's standard verify loop (`just test && just typecheck && just lint`) is simpler than coordinating a team. Sequential ordering chosen to keep `git log` readable and to let each fix land with its own commit per the project's "one concern per commit when feasible" convention.

**Defense-first execution order.** Trivial fixes first, then the helper-signature rework (Task 3), then the schema-gated collapse (Task 6) last so the gate fires after the no-risk work has already landed. Task 6 may force its own PR if the JSON schema diff is non-empty — keeping it at the end means everything else has already merged.

**Commit hygiene.** Each task lists specific paths to stage. **Do not `git add -A`** — the verify loop or polish step can leave stray edits (e.g. tooling caches, lockfile bumps) in adjacent files; staging by path keeps each commit honest to its one concern.

## Task Dependency Graph

Execute in this order (task numbers are stable labels, not the execution sequence):

- Task 2 [AFK]: depends on `none` → batch 1 (Laminar guard, journal writes)
- Task 4 [AFK]: depends on `Task 2` → batch 2 (exhaustiveness via `assert_never`)
- Task 5 [AFK]: depends on `Task 4` → batch 3 (injection-marker contract test)
- Task 7 [AFK]: depends on `Task 5` → batch 4 (honest URL narrowing)
- Task 8 [AFK]: depends on `Task 7` → batch 5 (drop `_set_corpus` alias)
- Task 9 [AFK]: depends on `Task 8` → batch 6 (collapse `_SourceSpec`)
- Task 10 [AFK]: depends on `Task 9` → batch 7 (`Literal` CLI validation)
- Task 11 [AFK]: depends on `Task 10` → batch 8 (single `TAVILY_EXTRACT_URL`)
- Task 3 [AFK]: depends on `Task 11` → batch 9 (surface `ProcessOutcome.FAILED`)
- Task 6 [AFK]: depends on `Task 3` → batch 10 (collapse `_LLMConsolidatedRisk`; schema-gated, may split PR)
- Polish [AFK]: depends on `Task 6` → batch 11

Rationale: Tasks 2/4/5/7-11 are pure defense or dead-code drops with no cassette impact. Task 3 reworks a helper signature, so it lands once the trivial fixes are in. Task 6 may trigger a cassette re-record and exits the plan to its own PR if so.

## Agent Assignments

- Task 2: Guard `Laminar.event()` in journal writes → python-development:python-pro (Python)
- Task 3: Surface `ProcessOutcome.FAILED` in ingest → python-development:python-pro (Python)
- Task 4: Exhaustiveness assertion on status query match → python-development:python-pro (Python)
- Task 5: Bind `_INJECTION_MARKER` to `SpanEvent` → python-development:python-pro (Python)
- Task 6: Collapse `_LLMConsolidatedRisk` into `ConsolidatedRisk` → python-development:python-pro (Python)
- Task 7: Remove dead-fallback narrowing in pitch_filler URL → python-development:python-pro (Python)
- Task 8: Drop `_set_corpus` alias → python-development:python-pro (Python)
- Task 9: Collapse `_SourceSpec` single-field dataclass → python-development:python-pro (Python)
- Task 10: Validate `tavily_news_search_depth` with `Literal` → python-development:python-pro (Python)
- Task 11: Delete duplicate `TAVILY_EXTRACT_URL` → python-development:python-pro (Python)
- Polish: post-implementation-polish → python-development:python-pro

---

### Task 1 — REMOVED

Originally framed as a bug: "post-synth filter at `pipeline.py:527-528` ignores the lowered recall threshold." On closer reading the premise is wrong — the pre-synth filter (`select_top_n_by_similarity`) scores against `perspective_scores.mean()` from the LLM **reranker**, while the post-synth filter (`drop_below_min_similarity`) scores against `similarity.mean()` from the **synthesizer**'s own output. Two independent judges, two filters. The current code lets the deep judge (synthesizer, full-body read) overrule the cheap judge (reranker, title+snippet) even on recall-path entries. Whether the recall-lowered floor should propagate to the deep-judge gate is a **design choice**, not a defect, and it changes user-visible report content. Track separately (issue/ADR) with owner sign-off — do not change in this cleanup PR.

Task number 1 retained as a label for traceability; nothing to execute here.

---

### Task 2: Guard `Laminar.event()` in journal writes

`_journal_writes.py:109, 188, 229` call `Laminar.event()` without the `Laminar.is_initialized()` guard that every other site in the codebase uses (see `pipeline.py`, `_ingest.py`, `synthesize.py`, `recall_verify.py`, `consolidate_risks.py`). When `LAMINAR_PROJECT_API_KEY` is unset, the unguarded calls may raise; the `:109` arm is followed by `return ProcessOutcome.SKIPPED` at `:114`, so a raise inside `Laminar.event` would skip the `alias_blocked`/`resolver_flipped` short-circuit and leak the entry into the rest of the function.

**Files:**
- Modify: `slopmortem/ingest/_journal_writes.py:109, 188, 229`

**Pros:** Matches the established pattern. No behavior change in traced runs. Removes one of two reasons a non-traced ingest could crash.
**Cons:** None — pure defensive consistency.

- [x] **Step 1: Wrap each call in the existing guard**

At each of the three sites, change:

```python
Laminar.event(
    name=...,
    attributes=...,
)
```

to:

```python
if Laminar.is_initialized():
    Laminar.event(
        name=...,
        attributes=...,
    )
```

If a small helper already lives near the top of `_journal_writes.py` for guarded emits, prefer reusing it; otherwise inline the guard at each site. Do not introduce a new helper unless one already exists in the file — `synthesize.py:75-77` has its own `_emit_event`, but module-private helpers in this codebase are not shared across modules.

- [x] **Step 2: Verify tests + typecheck + lint**

Run: `just test && just typecheck && just lint`
Expected: all pass.

- [x] **Step 3: Commit**

```bash
git add slopmortem/ingest/_journal_writes.py
git commit -m "fix: guard Laminar.event() in journal writes"
```

---

### Task 3: Surface `ProcessOutcome.FAILED` in ingest

The match arm at `_ingest.py:371-372` increments `result.failed` and does nothing else: no `_record_entry_failure`, no `progress.error`, no span event. The entry vanishes from the trace and the progress bar. Contrast the `except Exception` arm just above which already calls `_record_entry_failure`. `ProcessOutcome.FAILED` is currently returned from one path (`_journal_writes.py:201`, after a `delete_chunks_for_canonical` failure), so the silent counter is the only signal something broke for that class of error.

**Files:**
- Modify: `slopmortem/ingest/_ingest.py:371-372`
- Test: `tests/ingest/test_orchestration.py` (file already exists per the diff stat)

**Pros:** Brings the FAILED outcome to parity with the exception path. Recovers the entry identity in trace and UI for the delete_chunks failure mode.
**Cons:** `_record_entry_failure` (defined earlier in `_ingest.py`) is signed for the exception path — it takes `result`, `progress`, `phase`, `entry`, `exc`, `message`. The FAILED outcome has no `exc` because the failure was logged-and-converted inside `_process_entry`. Two options: (a) synthesize a placeholder `RuntimeError("delete_chunks failed; see prior log")` and pass it as `exc`, or (b) widen `_record_entry_failure` to accept `exc: BaseException | None`. Pick (a) — it's the smaller diff and keeps the helper's contract intact.

- [ ] **Step 1: Write the failing test**

Add to `tests/ingest/test_orchestration.py`:

```python
async def test_failed_outcome_records_entry_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When _process_entry returns ProcessOutcome.FAILED, the outer loop must
    record the failure on the progress bar (same contract as the exception
    path). Silent counter increment is not enough."""
    progress = _RecordingProgress()
    entry = _make_entry(source="hn_algolia", source_id="123")
    # Patch _process_entry to return FAILED without raising.
    monkeypatch.setattr(
        "slopmortem.ingest._ingest._process_entry",
        _AsyncReturn(ProcessOutcome.FAILED),
    )

    result = IngestResult()
    await _run_write_phase(entries=[entry], result=result, progress=progress)

    assert result.failed == 1
    assert progress.errors, "progress.error must be called for FAILED outcome"
    assert any(entry.source_id in msg for msg in progress.errors)
```

`_RecordingProgress`, `_make_entry`, `_AsyncReturn`, and `_run_write_phase` may not exist verbatim — use the closest equivalents already in this test file. If none exist, build them from the patterns used by neighboring tests in the same file.

- [ ] **Step 2: Run the test to verify it fails**

Run: `just test tests/ingest/test_orchestration.py::test_failed_outcome_records_entry_failure -v`
Expected: FAIL with `assert progress.errors` (empty list).

- [ ] **Step 3: Apply the fix**

In `slopmortem/ingest/_ingest.py:371-372`, change:

```python
case ProcessOutcome.FAILED:
    result.failed += 1
```

to:

```python
case ProcessOutcome.FAILED:
    _record_entry_failure(
        result=result,
        progress=progress,
        phase=IngestPhase.WRITE,
        entry=entry,
        exc=RuntimeError("process_entry returned FAILED; see prior warning"),
        message=f"write phase failed for {entry.source}:{entry.source_id} (FAILED outcome)",
    )
```

`_record_entry_failure` already increments `result.failed` (verify by reading its body before the edit — if it doesn't, keep the `result.failed += 1`). The synthetic `RuntimeError` is a placeholder so we don't widen the helper's signature for a single caller; the real reason was already logged at warning level inside `_process_entry` (`_journal_writes.py:196-200`). Do not add a new helper.

- [ ] **Step 4: Run the test to verify it passes**

Run: `just test tests/ingest/test_orchestration.py::test_failed_outcome_records_entry_failure -v`
Expected: PASS.

- [ ] **Step 5: Full verify**

Run: `just test && just typecheck && just lint`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add slopmortem/ingest/_ingest.py tests/ingest/test_orchestration.py
git commit -m "fix: surface ProcessOutcome.FAILED in ingest progress + trace"
```

---

### Task 4: Exhaustiveness assertion on status query match

The `match` in `_build_status_shaped_query` (`recall_verify.py:481-491`) covers all four values of the current `RecallSuggestion.status` literal (`"dead"`, `"absorbed"`, `"struggling"`, `"bruised"`), but has no `case _` arm. The declared return type is `str`; Python returns implicit `None` for any future status value, which then gets concatenated into a URL. basedpyright's exhaustiveness check passes today only because every literal value is covered — add a fifth value and the missing-return goes unnoticed until runtime.

**Files:**
- Modify: `slopmortem/stages/recall_verify.py:481-491`

**Pros:** Future-proofs the function against status taxonomy growth. `assert_never` is idiomatic exhaustiveness in modern Python — basedpyright understands it and will emit a **type error at the point of incompleteness** the moment a new literal is added to `RecallSuggestion.status`, instead of a runtime-only `AssertionError`.
**Cons:** None — `assert_never` is in stdlib `typing` since 3.11, this project is 3.13+.

- [x] **Step 1: Add the exhaustiveness check**

Add `from typing import assert_never` to the imports if not already present.

In `slopmortem/stages/recall_verify.py:481-491`, append after the `"struggling" | "bruised"` arm:

```python
        case _:
            assert_never(suggestion.status)
```

Do not add a runtime `AssertionError` arm — `assert_never` already raises at runtime if reached (defensive) and, more importantly, makes basedpyright fail the build the moment the literal grows without this arm being updated.

- [x] **Step 2: Verify**

Run: `just test && just typecheck && just lint`
Expected: all pass. `basedpyright` should remain green; if it reports `assert_never` as unreachable, the match was already exhaustive — that's the design.

- [x] **Step 3: Commit**

```bash
git add slopmortem/stages/recall_verify.py
git commit -m "harden: exhaustiveness on _build_status_shaped_query match"
```

---

### Task 5: Contract test pinning `_INJECTION_MARKER` to prompt + SpanEvent

`synthesize.py:72` defines `_INJECTION_MARKER = "prompt_injection_attempted"`; `tracing/events.py:11` defines `SpanEvent.PROMPT_INJECTION_ATTEMPTED = "prompt_injection_attempted"`; `slopmortem/llm/prompts/synthesize.j2:16` instructs the LLM to emit the same literal in `where_diverged`. **Three independent copies of one load-bearing string** — CLAUDE.md flags this as not-to-break and says "Don't normalize the marker string away in prompts or post-processing."

The earlier draft of this task proposed binding `_INJECTION_MARKER = SpanEvent.PROMPT_INJECTION_ATTEMPTED.value`. That collapses code↔tracer drift but **adds** a new failure mode: someone renames the SpanEvent value to clean up the enum, `_INJECTION_MARKER` silently follows, the prompt template still emits `"prompt_injection_attempted"`, and injection detection breaks with no test failure. The real drift risk is **prompt ↔ code**, not code ↔ tracer.

Replace the binding idea with a contract test that pins all three copies to the same literal at test time.

**Files:**
- Create: `tests/stages/test_injection_marker_contract.py`
- No production code changes.

**Pros:** Catches any drift between prompt template, `_INJECTION_MARKER`, and `SpanEvent` at test time — at least one of the three must move for detection to silently break, and the test fails the moment any does.
**Cons:** Pure test addition; no behavior change.

- [x] **Step 1: Add the contract test**

Create `tests/stages/test_injection_marker_contract.py`:

```python
"""Pin the three independent copies of the injection-marker literal to the same string.

Drift between the prompt template, the synthesize-stage post-processor, and the
SpanEvent enum silently breaks injection detection — there is no other test
that fails when only one of the three is renamed.
"""

from pathlib import Path

from slopmortem.stages.synthesize import _INJECTION_MARKER
from slopmortem.tracing.events import SpanEvent

_PROMPT_PATH = Path(__file__).resolve().parents[2] / "slopmortem" / "llm" / "prompts" / "synthesize.j2"


def test_injection_marker_matches_span_event() -> None:
    assert _INJECTION_MARKER == SpanEvent.PROMPT_INJECTION_ATTEMPTED.value


def test_injection_marker_present_in_prompt_template() -> None:
    prompt_text = _PROMPT_PATH.read_text(encoding="utf-8")
    assert _INJECTION_MARKER in prompt_text, (
        f"synthesize.j2 must instruct the LLM to emit the literal "
        f"{_INJECTION_MARKER!r} in where_diverged; not found in prompt body"
    )
```

If the prompt path differs from what's above, locate it with `git grep -l "synthesize.j2"` and adjust.

- [x] **Step 2: Run the contract test**

Run: `just test tests/stages/test_injection_marker_contract.py -v`
Expected: both tests pass against current code.

- [x] **Step 3: Full verify**

Run: `just test && just typecheck && just lint`
Expected: all pass.

- [x] **Step 4: Commit**

```bash
git add tests/stages/test_injection_marker_contract.py
git commit -m "harden: contract test pins _INJECTION_MARKER to prompt + SpanEvent"
```

---

### Task 6: Collapse `_LLMConsolidatedRisk` into `ConsolidatedRisk`

`models.py:389-393` defines `_LLMConsolidatedRisk` with the same four fields as `ConsolidatedRisk` at `models.py:362-379`. `consolidate_risks.py:108-119` walks `parsed.top_risks` and rebuilds each `_LLMConsolidatedRisk` into a `ConsolidatedRisk`. The mapping is not pure 1:1 — it filters `raised_by` against `valid_ids` and drops entries with no surviving IDs — so the mapping loop must stay, but the duplicate model can go: `LLMTopRisksConsolidation` can wrap `ConsolidatedRisk` directly.

**Files:**
- Modify: `slopmortem/models.py:389-400`
- Modify: `slopmortem/stages/consolidate_risks.py:108-119` (only if the loop references `_LLMConsolidatedRisk` directly)

**Pros:** One schema, no drift risk when a field is added.
**Cons:** `LLMTopRisksConsolidation` is passed to `to_strict_response_schema` for OpenRouter structured output — the generated JSON schema may use the field model's class name in `definitions`. If so, the schema string changes and `prompt_template_sha` for the `consolidate_risks` prompt may differ, requiring cassette re-record. Verify the schema string before and after; if it differs, this task moves to its own PR with `just eval-record` rerun.

- [ ] **Step 1: Capture the current schema for diffing**

Run:

```bash
uv run python -c "
from slopmortem.llm.tools import to_strict_response_schema
from slopmortem.models import LLMTopRisksConsolidation
import json
print(json.dumps(to_strict_response_schema(LLMTopRisksConsolidation), sort_keys=True))
" > /tmp/schema_before.json
```

- [ ] **Step 2: Apply the model collapse**

In `slopmortem/models.py:389-400`, delete the `_LLMConsolidatedRisk` definition and change `LLMTopRisksConsolidation.top_risks` to reference `ConsolidatedRisk`:

```python
class LLMTopRisksConsolidation(BaseModel):
    """LLM-facing wrapper for the consolidate-risks stage's JSON output."""

    top_risks: list[ConsolidatedRisk] = Field(default_factory=list)
    injection_detected: bool = False
```

In `slopmortem/stages/consolidate_risks.py:108-119`, the loop still walks `parsed.top_risks` and rebuilds with `valid_ids` filtering — that logic stays. Only the type annotation on the loop variable changes (it's now `ConsolidatedRisk` directly). No behavioral change.

- [ ] **Step 3: Diff the schema**

Run:

```bash
uv run python -c "
from slopmortem.llm.tools import to_strict_response_schema
from slopmortem.models import LLMTopRisksConsolidation
import json
print(json.dumps(to_strict_response_schema(LLMTopRisksConsolidation), sort_keys=True))
" > /tmp/schema_after.json
diff /tmp/schema_before.json /tmp/schema_after.json
```

Expected: empty diff. If the diff is non-empty, **stop** — the JSON schema changed and cassettes will mismatch. Revert this task and surface it to the user: needs its own PR with `just eval-record`.

- [ ] **Step 4: Verify**

Run: `just test && just typecheck && just lint`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add slopmortem/models.py slopmortem/stages/consolidate_risks.py
git commit -m "cleanup: collapse _LLMConsolidatedRisk into ConsolidatedRisk"
```

---

### Task 7: Remove dead-fallback narrowing in pitch_filler URL

`_pitch_filler.py:99-100` reads:

```python
# ``url`` is non-empty here per the skip-guard above; narrow for the typechecker.
url = entry.url or ""
```

`_should_skip` at `_pitch_filler.py:75-93` returns `True` when `not entry.url` (via the `if not entry.url` branch — confirm by reading the function), so by line 100 `entry.url` is guaranteed truthy. The `or ""` is a lie: it pretends the `None` case is possible and silently swaps in an empty string if the guard is ever loosened. Use an `assert` so basedpyright narrows correctly and a regression bites at runtime.

**Files:**
- Modify: `slopmortem/ingest/_pitch_filler.py:99-100`

**Pros:** Honest narrowing. A future loosening of `_should_skip` surfaces immediately rather than producing an empty-domain pitch filler call.
**Cons:** `assert` is stripped under `python -O`, which the project doesn't use — fine here, but worth a note. If the team prefers a runtime-loud narrowing, use `if entry.url is None: raise AssertionError(...)`.

- [x] **Step 1: Replace the dead fallback**

In `slopmortem/ingest/_pitch_filler.py:99-100`, change:

```python
# ``url`` is non-empty here per the skip-guard above; narrow for the typechecker.
url = entry.url or ""
```

to:

```python
# ``_should_skip`` guarantees a non-empty URL at this point.
assert entry.url, "pitch filler reached enrich() with empty entry.url"
url = entry.url
```

- [x] **Step 2: Verify**

Run: `just test && just typecheck && just lint`
Expected: all pass.

- [x] **Step 3: Commit**

```bash
git add slopmortem/ingest/_pitch_filler.py
git commit -m "cleanup: honest URL narrowing in pitch_filler"
```

---

### Task 8: Drop `_set_corpus` alias

`_tools_impl.py:95-102` defines `_set_corpus(c)` and `set_query_corpus(c)`; the second calls the first and adds no logic. Both appear in `__all__`. Collapse to one public name.

**Files:**
- Modify: `slopmortem/corpus/_tools_impl.py:95-102`
- Modify: `slopmortem/corpus/_tools_impl.py` `__all__` block (top of file)
- Modify: `tests/test_synthesis_tools.py:20,94` — imports and calls `_set_corpus` directly. Rewrite to `set_query_corpus`.
- Check: `git grep '_set_corpus\b' slopmortem/ tests/` — any other callers?

**Pros:** One name for one function.
**Cons:** None.

- [x] **Step 1: Confirm and enumerate all callers of `_set_corpus`**

Run: `git grep -n '_set_corpus' slopmortem/ tests/`
Expected: matches inside `_tools_impl.py` AND `tests/test_synthesis_tools.py` (the test file imports and calls the private name). Update the test file in this task — switch the import and the call site to `set_query_corpus`.

- [x] **Step 2: Inline the body and delete the alias**

In `slopmortem/corpus/_tools_impl.py:95-102`, change:

```python
def _set_corpus(c: Corpus) -> None:
    global _corpus  # noqa: PLW0603 — the module-level binding is the public init surface
    _corpus = c


def set_query_corpus(c: Corpus) -> None:
    """Public re-export of ``_set_corpus`` so callers don't reach past the ``corpus`` façade."""
    _set_corpus(c)
```

to:

```python
def set_query_corpus(c: Corpus) -> None:
    """Wire the module-level corpus reference that the LLM tool callables read."""
    global _corpus  # noqa: PLW0603 — the module-level binding is the public init surface
    _corpus = c
```

Remove `_set_corpus` from `__all__` if it appears there.

- [x] **Step 3: Verify**

Run: `just test && just typecheck && just lint`
Expected: all pass.

- [x] **Step 4: Commit**

```bash
git add slopmortem/corpus/_tools_impl.py tests/test_synthesis_tools.py
git commit -m "cleanup: drop _set_corpus alias"
```

---

### Task 9: Collapse `_SourceSpec` single-field dataclass

`cli/_ingest_cmd.py:455-465` wraps a single `source_class: type[Source]` in a frozen dataclass. The registry is `dict[str, _SourceSpec]` and every access reads `spec.source_class`. Collapse to `dict[str, type[Source]]`.

**Files:**
- Modify: `slopmortem/cli/_ingest_cmd.py:455-465`
- Modify: every `spec.source_class` site in the same file (`git grep -n 'source_class' slopmortem/cli/_ingest_cmd.py`)

**Pros:** Removes ceremony. `_SourceSpec` adds zero over `type[Source]`.
**Cons:** If a second field (`enable_flag`, `default_kwargs`) is coming, the dataclass becomes useful — but per CLAUDE.md, design for today, not hypothetical futures.

- [x] **Step 1: Find all `_SourceSpec` / `source_class` references**

Run: `git grep -n '_SourceSpec\|source_class' slopmortem/cli/_ingest_cmd.py`

- [x] **Step 2: Replace the dataclass with a bare type alias**

In `slopmortem/cli/_ingest_cmd.py:455-465`, delete the `_SourceSpec` dataclass definition and change:

```python
_SOURCE_REGISTRY: dict[str, _SourceSpec] = {
    "curated": _SourceSpec(source_class=CuratedSource),
    "hn_algolia": _SourceSpec(source_class=HNAlgoliaSource),
    "crunchbase_csv": _SourceSpec(source_class=CrunchbaseCsvSource),
    "tavily_news": _SourceSpec(source_class=TavilyNewsSource),
}
```

to:

```python
_SOURCE_REGISTRY: dict[str, type[Source]] = {
    "curated": CuratedSource,
    "hn_algolia": HNAlgoliaSource,
    "crunchbase_csv": CrunchbaseCsvSource,
    "tavily_news": TavilyNewsSource,
}
```

Replace every `spec.source_class` lookup elsewhere in the file with the registry value directly (e.g. `_SOURCE_REGISTRY[name]` instead of `_SOURCE_REGISTRY[name].source_class`).

- [x] **Step 3: Verify**

Run: `just test && just typecheck && just lint`
Expected: all pass.

- [x] **Step 4: Commit**

```bash
git add slopmortem/cli/_ingest_cmd.py
git commit -m "cleanup: drop _SourceSpec single-field wrap"
```

---

### Task 10: Validate `tavily_news_search_depth` with `Literal`

`cli/_ingest_cmd.py:155-164` declares the option as `str | None` and the help text says `basic`/`advanced` (closed set of two). A typo like `--tavily-news-search-depth baisic` passes through to `TavilyNewsSource` and silently falls back to the default. Fail at the CLI boundary.

**Files:**
- Modify: `slopmortem/cli/_ingest_cmd.py:155-164`

**Pros:** Typos fail immediately with a clear message instead of producing silently wrong runs.
**Cons:** `typer.Choice` and `Literal`-typed options behave slightly differently in Typer's help rendering; pick one that matches the project's existing CLI style. Grep for `Literal[` in `cli/` to check prior art.

- [x] **Step 1: Check existing patterns**

Run: `git grep -n 'Literal\[' slopmortem/cli/`
Use whichever pattern the rest of the CLI uses (Typer supports `Literal["a", "b"]` natively in 0.12+).

- [x] **Step 2: Tighten the type**

In `slopmortem/cli/_ingest_cmd.py:155-164`, change:

```python
tavily_news_search_depth: Annotated[
    str | None,
    typer.Option(
        "--tavily-news-search-depth",
        help=(
            "Override search_depth for the Tavily news source: "
            "basic (1 credit) or advanced (2)."
        ),
    ),
] = None,
```

to:

```python
tavily_news_search_depth: Annotated[
    Literal["basic", "advanced"] | None,
    typer.Option(
        "--tavily-news-search-depth",
        help=(
            "Override search_depth for the Tavily news source: "
            "basic (1 credit) or advanced (2)."
        ),
    ),
] = None,
```

Add `from typing import Literal` to imports if not already there.

If Typer's version in this project doesn't support `Literal` for options (it should — check `uv tree | grep typer`), fall back to `typer.Option(... click_type=click.Choice(["basic","advanced"]))` and update the type to `str | None`.

- [x] **Step 3: Verify with a deliberate typo**

Run: `uv run slopmortem ingest --tavily-news-search-depth baisic --help-only 2>&1 || true`

(or whatever the dry-run invocation is — find the equivalent by checking how other CLI tests in `tests/test_cli_ingest.py` exercise invalid options).

Expected: Typer rejects the value before the command body runs.

- [x] **Step 4: Full verify**

Run: `just test && just typecheck && just lint`
Expected: all pass.

- [x] **Step 5: Commit**

```bash
git add slopmortem/cli/_ingest_cmd.py
git commit -m "harden: validate --tavily-news-search-depth at CLI boundary"
```

---

### Task 11: Delete duplicate `TAVILY_EXTRACT_URL`

`corpus/tavily.py:30` and `corpus/_tools_impl.py:32` both define `TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"` independently. `sources/tavily.py` imports the copy from `_tools_impl`. Drop the duplicate and import from `corpus/tavily.py` everywhere.

**Files:**
- Modify: `slopmortem/corpus/_tools_impl.py:32` (delete the constant)
- Modify: `slopmortem/corpus/sources/tavily.py` (re-point the import)
- Audit: `git grep -n 'TAVILY_EXTRACT_URL' slopmortem/`

**Pros:** One source of truth for the URL.
**Cons:** None.

- [x] **Step 1: Find every reference**

Run: `git grep -n 'TAVILY_EXTRACT_URL' slopmortem/ tests/`

- [x] **Step 2: Delete the duplicate**

In `slopmortem/corpus/_tools_impl.py`, delete the line at `:32`:

```python
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
```

- [x] **Step 3: Re-point any imports that came from `_tools_impl`**

For every file that imports `TAVILY_EXTRACT_URL` from `slopmortem.corpus._tools_impl`, change the source to `slopmortem.corpus.tavily`. Most likely just `sources/tavily.py`.

- [x] **Step 4: Verify**

Run: `just test && just typecheck && just lint`
Expected: all pass.

- [x] **Step 5: Commit**

```bash
git add slopmortem/corpus/_tools_impl.py slopmortem/corpus/sources/tavily.py
git commit -m "cleanup: single source for TAVILY_EXTRACT_URL"
```

---

### Polish: post-implementation polish pass

Once Tasks 2–11 land, run the standard polish pass: three review rounds with fixes, idiomatic-code pass, `/cleanup`, comment humanization. Catches anything the per-task verify loop missed and strips any AI breadcrumbs left in the diff.

- [ ] **Step 1: Dispatch the polish skill**

Use the `post-implementation-polish` skill on the commits in this plan (the ten cleanup commits, not the whole branch). Constrain scope to the files this plan touched, and **review the polish diff before letting it commit** — polish/cleanup passes can revert intentional choices (e.g., the synthetic `RuntimeError` in Task 3, the `assert_never` arm in Task 4, the marker comment in Task 5).

- [ ] **Step 2: Final full-suite run**

Run: `just test && just typecheck && just lint`
Expected: all pass. If polish surfaced any new findings worth applying, they land as separate commits.

- [ ] **Step 3: Confirm no cassette files moved**

Run: `git status tests/fixtures/cassettes/`
Expected: clean. If a cassette moved, the LLM schema or prompt SHA shifted unexpectedly — bisect to find the offending task and re-evaluate before merging.
