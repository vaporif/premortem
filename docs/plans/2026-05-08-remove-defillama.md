# Remove DefiLlama Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or `/team-feature` to implement this plan task-by-task, per the Execution Strategy below. Steps use checkbox (`- [ ]`) syntax — these are **persistent durable state**, not visual decoration. The executor edits the plan file in place: `- [ ]` → `- [x]` the instant a step verifies, before moving on. On resume (new session, crash, takeover), the executor scans existing `- [x]` marks and skips them — these steps are NOT redone. TodoWrite mirrors this state in-session; the plan file is the source of truth across sessions.

**Goal:** Delete the DefiLlama dead-protocol source from the codebase so ingest stops carrying a feature whose enricher chain (Tavily /extract, Wayback) is currently disabled and that emits zero usable entries today.

**Architecture:** Mechanical removal across one source module, one test module, one CLI surface (six flags + handler kwargs + source-registry entry), one reliability-rank entry, one source-id constant, two stale doc files, and one passing reference in a docstring. No runtime behavior changes for the remaining sources (curated, hn_algolia, crunchbase_csv, tavily_news) — DefiLlama is the only thing that goes; reliability ranks for the other sources do not shift because the table is keyed by string, not ordinal.

**Tech Stack:** Python 3.13, `uv`, `pytest` + `pytest-xdist`, `ruff`, `basedpyright`, `typer`, `pydantic` v2.

## Execution Strategy

**Subagents** — default; no spec override. The work is a mechanical removal across ~8 files, naturally sequential because each edit removes a name that the next task expects to be gone. A single subagent per task plus the standard two-stage review fits cleanly.

## Task Dependency Graph

- Task 1 [AFK]: Remove CLI surface — depends on `none`
- Task 2 [AFK]: Remove reliability-rank entry — depends on `Task 1`
- Task 3 [AFK]: Delete DefiLlama source test module — depends on `Task 2`
- Task 4 [AFK]: Delete source module + drop exports + drop source-id — depends on `Task 3`
- Task 5 [AFK]: Drop stray docstring reference — depends on `Task 4`
- Task 6 [AFK]: Delete now-orphaned plan + spec docs — depends on `Task 5`
- Task 7 [AFK]: Full verification (lint, typecheck, tests) — depends on `Task 6`

All tasks are sequential because each removes a name the next task expects gone. False parallelism here just risks half-removed state on disk between commits.

## Agent Assignments

- Task 1: Remove CLI surface → general-purpose (Python)
- Task 2: Remove reliability-rank entry → general-purpose (Python)
- Task 3: Delete DefiLlama source test module → general-purpose (Python)
- Task 4: Delete source module + drop exports + drop source-id → general-purpose (Python)
- Task 5: Drop stray docstring reference → general-purpose (Python)
- Task 6: Delete now-orphaned plan + spec docs → general-purpose
- Task 7: Full verification (lint, typecheck, tests) → general-purpose (Python)
- Polish: post-implementation-polish → general-purpose

## Design notes

**What goes vs what stays.** DefiLlama-specific files are deleted. Historical mentions of "defillama" in unrelated plans (`docs/plans/2026-05-06-hn-yaml-phrases.md`, `2026-05-07-pitch-filler.md`, `2026-05-06-tavily-news-source.md`, `docs/specs/2026-05-06-tavily-news-source-design.md`, `docs/specs/2026-05-08-llm-recall-fallback-design.md`) are left alone — those are dated planning artifacts that mention defillama in passing while documenting other features. The recall-fallback design is draft-status and references `wayback_snapshot_near` from the deleted module; that's accepted as a known stale dependency, not something this removal patches up. Editing them would be revising history.

**Plan + spec docs for the feature itself get deleted** (`docs/plans/2026-05-06-defillama-source.md`, `docs/specs/2026-05-06-defillama-source-design.md`). They document the removed feature and have no other consumer. Auto-selected — no downsides compared to leaving them as orphans. Let me know if you disagree.

**No reliability-rank shift.** Removing `SOURCE_DEFILLAMA: 3` from `_RELIABILITY_RANK` leaves a numeric gap (0,1,2,4) but the table is consulted by `_reliability_for(source)` which does a dict lookup — the integers function as ordering keys, not array indices. Renumbering tavily_news from 4 to 3 would be a behavior change to merge_text section ordering and is out of scope here.

**No reliability_rank_version bump.** That version string is part of the `_skip_key` hash; bumping it would invalidate every prior ingest's skip cache. The set of source strings shrinks but the ranks of all surviving sources stay identical, so existing journal rows stay valid. If you disagree and want a forced re-ingest, that's a separate decision.

**Cassettes.** No `*defillama*` cassette files exist under `tests/fixtures/cassettes/`. Nothing to clean there.

**Config.** `slopmortem.toml` has no `defillama_*` keys. Nothing to clean there.

---

### Task 1: Remove CLI surface

**Files:**
- Modify: `slopmortem/cli/_ingest_cmd.py`

This is the largest single edit: six `--defillama-*` typer options on `ingest_cmd`, the matching keyword args on `_run_ingest`, the kwarg pass-through inside `ingest_cmd`'s `functools.partial`, the `DefiLlamaSource` import, the `_SOURCE_REGISTRY` entry, the `if spec.source_class is DefiLlamaSource:` branch in the `--only-source` block, the `if enable_defillama:` source-construction block, the `defillama` mention in the `--only-source` help string, and the `DefiLlama` mention in the `--limit` help string.

- [x] **Step 1: Remove the `DefiLlamaSource` import**

In `slopmortem/cli/_ingest_cmd.py`, edit lines 38–44:

```python
from slopmortem.corpus.sources import (
    CrunchbaseCsvSource,
    CuratedSource,
    HNAlgoliaSource,
    TavilyNewsSource,
)
```

(Drops the `DefiLlamaSource,` line.)

- [x] **Step 2: Remove the six `--defillama-*` typer options from `ingest_cmd`**

In `slopmortem/cli/_ingest_cmd.py`, delete lines 121–179 inclusive — the entire block of six `Annotated[..., typer.Option(...)]` parameters: `enable_defillama`, `defillama_rps`, `defillama_concurrency`, `defillama_wayback_concurrency`, `defillama_max_emit`, `defillama_shortlist_ceiling_usd`. The block sits between `enable_title_pre_filter: ...` (ends at line 120 with `] = False,`) and `enable_tavily_news: ...` (begins at line 180).

- [x] **Step 3: Remove the matching kwargs in the `functools.partial` call**

In `slopmortem/cli/_ingest_cmd.py`, edit the partial call inside `ingest_cmd` (currently lines 254–278). Drop these six lines:

```python
            enable_defillama=enable_defillama,
            defillama_rps=defillama_rps,
            defillama_concurrency=defillama_concurrency,
            defillama_wayback_concurrency=defillama_wayback_concurrency,
            defillama_max_emit=defillama_max_emit,
            defillama_shortlist_ceiling_usd=defillama_shortlist_ceiling_usd,
```

- [x] **Step 4: Remove the matching keyword-only parameters on `_run_ingest`**

In `slopmortem/cli/_ingest_cmd.py`, in `_run_ingest`'s signature (currently lines 324–348), delete the six lines:

```python
    enable_defillama: bool,
    defillama_rps: float | None,
    defillama_concurrency: int | None,
    defillama_wayback_concurrency: int | None,
    defillama_max_emit: int | None,
    defillama_shortlist_ceiling_usd: float | None,
```

- [x] **Step 5: Remove the `--only-source defillama` enable-flag branch**

In `slopmortem/cli/_ingest_cmd.py`, delete lines 386–387 inside the `if only_source is not None:` block:

```python
        if spec.source_class is DefiLlamaSource:
            enable_defillama = True
```

- [x] **Step 6: Remove the source-construction block**

In `slopmortem/cli/_ingest_cmd.py`, delete lines 414–423 (the whole `if enable_defillama: sources.append(DefiLlamaSource(...))` block). The line directly above is `sources.append(CrunchbaseCsvSource(csv_path=crunchbase_csv))` and the line directly below is `if enable_tavily_news:`.

- [x] **Step 7: Remove the registry entry**

In `slopmortem/cli/_ingest_cmd.py`, edit `_SOURCE_REGISTRY` (currently lines 545–551). Delete the line:

```python
    "defillama": _SourceSpec(source_class=DefiLlamaSource),
```

- [x] **Step 8: Edit the `--only-source` help string**

In `slopmortem/cli/_ingest_cmd.py`, in the `--only-source` typer.Option help (currently lines 226–230), change:

```python
                "Accepts source identifiers (curated, hn_algolia, crunchbase_csv, "
                "defillama, tavily_news)."
```

to:

```python
                "Accepts source identifiers (curated, hn_algolia, crunchbase_csv, "
                "tavily_news)."
```

- [x] **Step 9: Edit the `--limit` help string**

In `slopmortem/cli/_ingest_cmd.py`, in the `--limit` typer.Option help (currently around line 246), change:

```python
                "Source order is curated -> HN -> Crunchbase -> DefiLlama -> TavilyNews. "
```

to:

```python
                "Source order is curated -> HN -> Crunchbase -> TavilyNews. "
```

- [x] **Step 10: Verify imports and CLI options resolve**

Run:

```bash
uv run python -c "from slopmortem.cli._ingest_cmd import ingest_cmd, _run_ingest, _SOURCE_REGISTRY; print(sorted(_SOURCE_REGISTRY))"
```

Expected output (one line):

```
['crunchbase_csv', 'curated', 'hn_algolia', 'tavily_news']
```

- [x] **Step 11: Run typecheck on the CLI module**

Run:

```bash
uv run basedpyright slopmortem/cli/_ingest_cmd.py
```

Expected: no errors. (Warnings unrelated to defillama are OK if they pre-existed; if any new error mentions `defillama`, `DefiLlamaSource`, or any of the six flags, fix it before moving on.)

- [x] **Step 12: Run lint on the CLI module**

Run:

```bash
uv run ruff check slopmortem/cli/_ingest_cmd.py
```

Expected: no errors.

- [x] **Step 13: Commit**

Run:

```bash
git add slopmortem/cli/_ingest_cmd.py
git commit -m "cli: drop defillama ingest flags and source registration"
```

Expected: clean commit, no hook failures.

---

### Task 2: Remove reliability-rank entry

**Files:**
- Modify: `slopmortem/ingest/_helpers.py`
- Modify: `tests/ingest/test_reliability_rank.py`

The reliability rank table is keyed by source-id string and surfaces in `_reliability_for(source)`, which is consulted when the corpus stitches multi-source `merge_text`. Removing the `SOURCE_DEFILLAMA: 3` entry leaves a numeric gap (0,1,2,4) — that's fine because the table is dict-keyed and the integers are ordering keys, not array indices.

- [x] **Step 1: Update the parametrized test first (TDD)**

In `tests/ingest/test_reliability_rank.py`, edit the import block (lines 7–13). Drop the `SOURCE_DEFILLAMA,` line so the import becomes:

```python
from slopmortem.corpus.sources._names import (
    SOURCE_CRUNCHBASE_CSV,
    SOURCE_CURATED,
    SOURCE_HN_ALGOLIA,
    SOURCE_TAVILY_NEWS,
)
```

Then edit the parametrize body (lines 19–25) to drop the `(SOURCE_DEFILLAMA, 3)` row:

```python
@pytest.mark.parametrize(
    ("source", "expected_rank"),
    [
        (SOURCE_CURATED, 0),
        (SOURCE_HN_ALGOLIA, 1),
        (SOURCE_CRUNCHBASE_CSV, 2),
        (SOURCE_TAVILY_NEWS, 4),
    ],
)
```

- [x] **Step 2: Run the test — expect it to fail because `SOURCE_DEFILLAMA` is still imported in `_helpers.py`**

Run:

```bash
uv run pytest tests/ingest/test_reliability_rank.py -v
```

Expected: actually it should still PASS at this point (we removed the failing case but `SOURCE_DEFILLAMA` is still imported in `_helpers.py` and that import succeeds). Confirm green; the goal of the test edit is to align the test with the post-removal reality, not to drive a red-green cycle.

- [x] **Step 3: Remove the import from `_helpers.py`**

In `slopmortem/ingest/_helpers.py`, edit lines 11–17 to drop `SOURCE_DEFILLAMA`:

```python
from slopmortem.corpus.sources._names import (
    SOURCE_CRUNCHBASE_CSV,
    SOURCE_CURATED,
    SOURCE_HN_ALGOLIA,
    SOURCE_TAVILY_NEWS,
)
```

- [x] **Step 4: Remove the rank-table entry**

In `slopmortem/ingest/_helpers.py`, edit `_RELIABILITY_RANK` (currently lines 43–49). Drop the `SOURCE_DEFILLAMA: 3,` row:

```python
_RELIABILITY_RANK: Final[dict[str, int]] = {
    SOURCE_CURATED: 0,
    SOURCE_HN_ALGOLIA: 1,
    SOURCE_CRUNCHBASE_CSV: 2,
    SOURCE_TAVILY_NEWS: 4,
}
```

- [x] **Step 5: Run the test again to confirm green**

Run:

```bash
uv run pytest tests/ingest/test_reliability_rank.py -v
```

Expected: 5 passed (4 known sources + 1 dead-letter case).

- [x] **Step 6: Run typecheck on the helper module**

Run:

```bash
uv run basedpyright slopmortem/ingest/_helpers.py
```

Expected: no errors.

- [x] **Step 7: Commit**

```bash
git add slopmortem/ingest/_helpers.py tests/ingest/test_reliability_rank.py
git commit -m "ingest: drop defillama from reliability rank table"
```

Expected: clean commit.

---

### Task 3: Delete DefiLlama source test module

**Files:**
- Delete: `tests/sources/test_defillama.py`

This file imports `DefiLlamaSource` and `from slopmortem.corpus.sources.defillama import classify_death, wayback_snapshot_near` — both go away in Task 4. Deleting the test module first means Task 4 doesn't need to leave the source module half-deleted to satisfy a transient broken import.

- [x] **Step 1: Delete the test file**

Run:

```bash
rm tests/sources/test_defillama.py
```

- [x] **Step 2: Verify the directory still contains the other source tests**

Run:

```bash
ls tests/sources/
```

Expected: `test_crunchbase_csv.py`, `test_curated.py`, `test_hn_algolia.py`, `test_tavily.py`, `test_tavily_news.py`, `test_wayback.py`, plus `__init__.py` if present, plus possibly `__pycache__/`. The exact set may vary — the assertion is just that `test_defillama.py` is gone and nothing else changed.

- [x] **Step 3: Run the full sources test suite**

Run:

```bash
uv run pytest tests/sources/ -n auto
```

Expected: all remaining source tests pass. Zero `test_defillama` collected.

- [x] **Step 4: Commit**

```bash
git add -A tests/sources/
git commit -m "tests: drop defillama source tests"
```

Expected: clean commit recording one deletion.

---

### Task 4: Delete source module + drop exports + drop source-id

**Files:**
- Delete: `slopmortem/corpus/sources/defillama.py`
- Modify: `slopmortem/corpus/sources/__init__.py`
- Modify: `slopmortem/corpus/sources/_names.py`

By this point nothing imports `DefiLlamaSource` or `SOURCE_DEFILLAMA` (Task 1 cleared the CLI; Task 2 cleared the helper; Task 3 deleted the test). This task removes the source module itself and its two re-export points.

- [x] **Step 1: Delete the source module**

Run:

```bash
rm slopmortem/corpus/sources/defillama.py
```

- [x] **Step 2: Update `slopmortem/corpus/sources/__init__.py`**

Edit so the file becomes exactly:

```python
"""Source adapters and enrichers that produce ``RawEntry`` for ingest."""

from __future__ import annotations

from slopmortem.corpus.sources.base import Enricher as Enricher
from slopmortem.corpus.sources.base import Source as Source
from slopmortem.corpus.sources.crunchbase_csv import CrunchbaseCsvSource as CrunchbaseCsvSource
from slopmortem.corpus.sources.curated import CuratedSource as CuratedSource
from slopmortem.corpus.sources.hn_algolia import HNAlgoliaSource as HNAlgoliaSource
from slopmortem.corpus.sources.tavily import TavilyEnricher as TavilyEnricher
from slopmortem.corpus.sources.tavily_news import TavilyNewsSource as TavilyNewsSource
from slopmortem.corpus.sources.wayback import WaybackEnricher as WaybackEnricher

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

(Drops the `defillama` import line and the `"DefiLlamaSource",` entry from `__all__`.)

- [x] **Step 3: Update `slopmortem/corpus/sources/_names.py`**

Edit so the file becomes exactly:

```python
"""Source-id strings shared by `RawEntry.source`, the reliability table, and the pre-vetted set."""

from __future__ import annotations

from typing import Final

SOURCE_CURATED: Final = "curated"
SOURCE_HN_ALGOLIA: Final = "hn_algolia"
SOURCE_CRUNCHBASE_CSV: Final = "crunchbase_csv"
SOURCE_TAVILY_NEWS: Final = "tavily_news"
```

(Drops the `SOURCE_DEFILLAMA: Final = "defillama"` line.)

- [x] **Step 4: Verify the package imports cleanly**

Run:

```bash
uv run python -c "from slopmortem.corpus.sources import CrunchbaseCsvSource, CuratedSource, HNAlgoliaSource, TavilyEnricher, TavilyNewsSource, WaybackEnricher; print('ok')"
```

Expected output:

```
ok
```

- [x] **Step 5: Verify no orphan references to defillama remain in the package**

Run:

```bash
grep -rn -i "defillama\|defi_llama\|DefiLlama" slopmortem/ tests/
```

Expected: zero output. (If anything matches, finish removing it before moving on.)

- [x] **Step 6: Run typecheck across the whole package**

Run:

```bash
uv run basedpyright slopmortem/
```

Expected: no errors.

- [x] **Step 7: Run lint across the whole package**

Run:

```bash
uv run ruff check slopmortem/ tests/
```

Expected: no errors.

- [x] **Step 8: Commit**

```bash
git add -A slopmortem/corpus/sources/
git commit -m "sources: drop defillama source"
```

Expected: clean commit recording one deletion plus two edits.

---

### Task 5: Drop stray docstring reference

**Files:**
- Modify: `slopmortem/cli/_common.py`

A single line in the `_maybe_setup_logging` docstring still mentions defillama emit lines. With the source gone, nothing emits those lines.

- [x] **Step 1: Edit the docstring**

In `slopmortem/cli/_common.py`, in the `_maybe_setup_logging` docstring (around lines 41–48), change:

```python
    progress (defillama emit lines, tavily fill lines, ingest save lines).
```

to:

```python
    progress (tavily fill lines, ingest save lines).
```

- [x] **Step 2: Verify no further defillama mentions remain**

Run:

```bash
grep -rn -i "defillama" slopmortem/ tests/
```

Expected: zero output.

- [x] **Step 3: Run lint and typecheck**

Run:

```bash
uv run ruff check slopmortem/cli/_common.py && uv run basedpyright slopmortem/cli/_common.py
```

Expected: both clean.

- [x] **Step 4: Commit**

```bash
git add slopmortem/cli/_common.py
git commit -m "cli: drop stale defillama reference from logging docstring"
```

Expected: clean commit.

---

### Task 6: Delete now-orphaned plan + spec docs

**Files:**
- Delete: `docs/plans/2026-05-06-defillama-source.md`
- Delete: `docs/specs/2026-05-06-defillama-source-design.md`

These two documents are dedicated to the now-removed feature. The other dated plans and specs that mention defillama incidentally (`docs/plans/2026-05-06-hn-yaml-phrases.md`, `docs/plans/2026-05-07-pitch-filler.md`, `docs/plans/2026-05-06-tavily-news-source.md`, `docs/specs/2026-05-06-tavily-news-source-design.md`, `docs/specs/2026-05-08-llm-recall-fallback-design.md`) are left alone — they document other features and only mention defillama in passing as historical context.

- [x] **Step 1: Delete the two feature docs**

Run:

```bash
rm docs/plans/2026-05-06-defillama-source.md docs/specs/2026-05-06-defillama-source-design.md
```

- [x] **Step 2: Confirm only incidental mentions remain in `docs/`**

Run:

```bash
grep -rln -i "defillama" docs/
```

Expected: a small set of files (the other dated plans/specs that mention defillama in passing) — `docs/plans/2026-05-06-hn-yaml-phrases.md`, `docs/plans/2026-05-07-pitch-filler.md`, `docs/plans/2026-05-06-tavily-news-source.md`, `docs/specs/2026-05-06-tavily-news-source-design.md`, `docs/specs/2026-05-08-llm-recall-fallback-design.md`, plus this plan itself. No `defillama-source.md` or `defillama-source-design.md` should appear.

- [x] **Step 3: Commit**

```bash
git add -A docs/
git commit -m "docs: drop defillama plan + spec"
```

Expected: clean commit recording two deletions plus this plan's progress edits.

---

### Task 7: Full verification (lint, typecheck, tests)

**Files:**
- (no edits — verification only)

Final gate. Runs the project's standard checks across the whole tree to make sure no defillama-shaped hole has shifted load to a place we did not anticipate.

- [x] **Step 1: Run lint**

Run:

```bash
just lint
```

Expected: clean.

- [x] **Step 2: Run typecheck**

Run:

```bash
just typecheck
```

Expected: clean.

- [x] **Step 3: Run the full test suite**

Run:

```bash
just test
```

Expected: full suite passes. No `test_defillama` collected. No collection errors. Skipped tests for `requires_qdrant` / `slow` are fine — they are unrelated to this change.

- [x] **Step 4: Final defillama sweep**

Run:

```bash
grep -rln -i "defillama\|defi_llama\|DefiLlama" slopmortem/ tests/ justfile slopmortem.toml README.md CLAUDE.md 2>/dev/null
```

Expected: zero output. (Mentions in `docs/` are allowed — those are historical plan/spec artifacts that mention defillama in passing while documenting other features.)

- [x] **Step 5: Show the branch summary**

Run:

```bash
git log --oneline main..HEAD | head -10
```

Expected: the six removal commits from Tasks 1–6 sit on top of the prior `pitch filler` commit, in the order they were made.
