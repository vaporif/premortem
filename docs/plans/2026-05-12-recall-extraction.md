# Recall Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or `/team-feature` to implement this plan task-by-task, per the Execution Strategy below. Steps use checkbox (`- [ ]`) syntax — these are **persistent durable state**, not visual decoration. The executor edits the plan file in place: `- [ ]` → `- [x]` the instant a step verifies, before moving on. On resume (new session, crash, takeover), the executor scans existing `- [x]` marks and skips them — these steps are NOT redone. TodoWrite mirrors this state in-session; the plan file is the source of truth across sessions.

**Goal:** Lift recall out of `pipeline.py` into `slopmortem/recall/` so callers can find-and-verify similar dead startups through one async function with a 4-dep surface, without touching the journal, slop classifier, sparse encoder, embedding client, or Qdrant corpus.

**Architecture:** New `slopmortem/recall/` package owns brainstorm + L0–L5 verify; persistence, coverage-gap predicate, and re-`retrieve`/re-`llm_rerank` stay in the pipeline. The pipeline composes `recall(...)` → `gather_resilient(_persist_one, ...)` → re-retrieve. `compute_coverage_gap` moves to `stages/coverage_gap.py`. `PriorCandidateHint` travels with brainstorm; `VerifiedEntry` (the new return record) carries `(entry, tier, verdict)` so persistence stays pipeline-side without losing information.

**Tech Stack:** Python 3.13, anyio, Pydantic v2, OpenRouter via `slopmortem.llm`, Laminar `@observe` tracing, pytest + pytest-xdist.

## Execution Strategy

**Subagents.** This is a mechanical refactor with one cohesive change across recall files, the pipeline, and tests. Splitting it across parallel agents would create merge contention on `pipeline.py` and the test layout. The dependency graph is fundamentally sequential.

1 task, AFK, no parallelism. Reason: file moves and the pipeline reshape share too many files to parallelise; a single agent doing the whole cut is faster than coordinating disjoint slices.

## Task Dependency Graph

- Task 1 [AFK]: depends on `none` → only batch (end-to-end vertical slice)

## Agent Assignments

- Task 1: Extract recall package → python-development:python-pro (Python)
- Polish: post-implementation-polish → python-development:python-pro (Python)

---

### Task 1: Extract recall package

This task is one cohesive refactor performed end-to-end. Steps are bite-sized; each step's verification gate is shown explicitly. Do not skip ahead — type errors and import-linter failures cascade fast in this codebase.

**Files:**
- Create: `slopmortem/recall/__init__.py`
- Create: `slopmortem/recall/_brainstorm.py` (moved from `slopmortem/stages/llm_recall.py`, minus `compute_coverage_gap`)
- Create: `slopmortem/recall/_verify.py` (moved from `slopmortem/stages/recall_verify.py`, persist callback dropped)
- Create: `slopmortem/recall/_models.py`
- Create: `slopmortem/recall/death_keywords.yml` (moved from `slopmortem/stages/death_keywords.yml`)
- Create: `slopmortem/recall/fake.py`
- Create: `slopmortem/stages/coverage_gap.py` (carved out of `slopmortem/stages/llm_recall.py`)
- Delete: `slopmortem/stages/llm_recall.py`
- Delete: `slopmortem/stages/recall_verify.py`
- Delete: `slopmortem/stages/death_keywords.yml`
- Modify: `slopmortem/stages/__init__.py`
- Modify: `slopmortem/stages/recall_persist.py` (one `TYPE_CHECKING` import line update)
- Modify: `slopmortem/pipeline.py`
- Modify: `slopmortem/deps.py` (one `TYPE_CHECKING` import line update)
- Modify: `slopmortem/corpus/sources/__init__.py` (re-export `SOURCE_LLM_RECALL`)
- Modify: `.importlinter`
- Create: `tests/recall/__init__.py`
- Create: `tests/recall/conftest.py`
- Create: `tests/recall/test_brainstorm.py` (moved from `tests/stages/test_llm_recall.py`)
- Create: `tests/recall/test_verify.py` (moved from `tests/stages/test_recall_verify.py`)
- Create: `tests/recall/test_verify_l2_l4_bprime.py` (moved from `tests/stages/test_recall_verify_l2_l4_bprime.py`)
- Create: `tests/recall/test_verify_l3_hygiene.py` (moved from `tests/stages/test_recall_verify_l3_hygiene.py`)
- Create: `tests/recall/test_verify_l5_tristate.py` (moved from `tests/stages/test_recall_verify_l5_tristate.py`)
- Create: `tests/recall/test_l3_extract_fallback.py` (moved from `tests/stages/test_recall_l3_extract_fallback.py`)
- Create: `tests/recall/test_search_head.py` (moved from `tests/stages/test_recall_search_head.py`)
- Create: `tests/recall/test_search_preflight.py` (moved from `tests/stages/test_recall_search_preflight.py`)
- Create: `tests/recall/test_recall_entrypoint.py`
- Create: `tests/recall/test_fake_recaller.py`
- Create: `tests/stages/test_coverage_gap.py` (moved from `tests/stages/test_coverage_gate.py` if it tests `compute_coverage_gap` alone — see Step 30)
- Modify: `tests/test_pipeline_recall_fallback.py` (add persist-failure-isolation test, rewrite monkeypatch paths)
- Modify: `tests/stages/test_recall_persist.py` (import path update)
- Modify: `tests/stages/test_recall_persist_gap_closures.py` (import path update **plus** `RecallDeps` import split into `RecallDeps` + `PersistDeps`)
- Modify: `tests/stages/test_recall_persist_dedup_event.py` (import path update if applicable)
- Modify: `tests/stages/test_recall_retrieval_survival.py` (import path update)
- Modify: `tests/recall/test_verify.py` post-move (rewrite `verify_and_persist_all(..., persist=...)` → `verify_all(...)` and assert against returned `list[VerifiedEntry]` instead of a captured `persisted` list — see Step 31b)

#### Phase A — Public surface promotions and package skeleton

- [x] **Step 1: Promote `SOURCE_LLM_RECALL` to the public surface of `slopmortem.corpus.sources`**

Open `slopmortem/corpus/sources/__init__.py` and add the re-export plus `__all__` entry.

```python
from slopmortem.corpus.sources._names import SOURCE_LLM_RECALL as SOURCE_LLM_RECALL
```

Add `"SOURCE_LLM_RECALL"` to `__all__` (insert it in alphabetical order between `"HNAlgoliaSource"` and `"Source"`).

- [x] **Step 2: Run lint + typecheck after the promotion**

Run: `just lint && just typecheck`
Expected: PASS. If `import-linter` complains, recheck that the import line targets `_names` not the package init.

- [x] **Step 3: Create the recall package skeleton**

Create `slopmortem/recall/__init__.py` with the following body. The actual `recall(...)` implementation and `FakeRecaller` get filled in in later steps — for now, the file just establishes the package and prepares the re-export site.

```python
"""LLM-recall subsystem: find similar dead startups and decide which ones are real.

Public surface: ``recall(pitch, *, facets=None, prior_hints=None, deps, config)`` —
one async function, four runtime deps, one config record. Persistence,
re-retrieval, and the coverage-gap predicate live pipeline-side; this package
is read-only with respect to the corpus.
"""

from __future__ import annotations
```

- [x] **Step 4: Verify the empty package imports**

Run: `uv run python -c "import slopmortem.recall"`
Expected: no output, exit 0.

#### Phase B — Move the verifier

- [x] **Step 5: Move `death_keywords.yml` into the recall package**

Run: `git mv slopmortem/stages/death_keywords.yml slopmortem/recall/death_keywords.yml`
Expected: file moved; `git status` shows the rename.

- [x] **Step 6: Move `recall_verify.py` to `recall/_verify.py`**

Run: `git mv slopmortem/stages/recall_verify.py slopmortem/recall/_verify.py`

- [x] **Step 7: Update the death-keywords path inside `_verify.py`**

The constant `_DEATH_KEYWORDS_PATH` at line 116 (`slopmortem/recall/_verify.py:116` after the move) is `Path(__file__).parent / "death_keywords.yml"`. Because the YAML moved with the file, no edit is needed here, but verify by inspection that `__file__` resolves to `slopmortem/recall/_verify.py` so `parent / "death_keywords.yml"` is correct. No code change in this step — just confirm.

- [x] **Step 8: Switch the private corpus import to the public re-export**

In `slopmortem/recall/_verify.py`, change:

```python
from slopmortem.corpus.sources._names import SOURCE_LLM_RECALL
```

to:

```python
from slopmortem.corpus.sources import SOURCE_LLM_RECALL
```

- [x] **Step 9: Drop `verify_and_persist_all`'s persist callback, rename to `verify_all`, change return type to `list[VerifiedEntry]`**

In `slopmortem/recall/_verify.py`, the function at lines 922-969 needs the following surgery. Replace the existing `verify_and_persist_all` (including the `@observe` decorator) with this body. `VerifiedEntry` is added in the next step; do this step and the next as a pair before running typecheck.

```python
@dataclass(frozen=True, slots=True)
class VerifiedEntry:
    """One suggestion that passed L0–L5.

    Carries the triple persistence needs: the seed ``RawEntry``, the
    Wayback tier label, and the L5 admit verdict (``"dead"`` | ``"struggling"``).
    Persistence lives pipeline-side and reads all three fields.
    """

    entry: RawEntry
    tier: VerificationTier
    verdict: Literal["dead", "struggling"]


@observe(
    name="stage.recall_verify",
    ignore_inputs=["suggestions", "wayback", "llm", "tavily_search", "extract"],
    ignore_output=True,
)
async def verify_all(  # noqa: PLR0913 - leaf helper; recall fan-out takes every dep at this seam
    suggestions: list[RecallSuggestion],
    *,
    wayback: Enricher,
    llm: LLMClient,
    tavily_search: TavilySearchFn,
    extract: ExtractFn,
    tavily_recall_max_results: int,
    deathness: DeathnessConfig,
    concurrency: int = _DEFAULT_CONCURRENCY,
) -> list[VerifiedEntry]:
    """Verify each suggestion under a capacity limiter; return accepted entries.

    ``gather_resilient`` isolates per-suggestion failures so a transient
    outage on one citation host doesn't poison the batch. ``_search_for_evidence``
    runs inside the same ``_one`` closure so a single 403 host doesn't kill
    siblings. Returned list contains only suggestions that passed L0-L5.
    """
    limiter = anyio.CapacityLimiter(concurrency)

    async def _one(s: RecallSuggestion) -> VerifiedEntry | None:
        async with limiter:
            discovered = await _search_for_evidence(
                s, tavily_search=tavily_search, limit=tavily_recall_max_results
            )
            if not discovered:
                return None
            verified = await verify_suggestion(
                s,
                discovered_urls=discovered,
                wayback=wayback,
                llm=llm,
                extract=extract,
                deathness=deathness,
            )
        if verified is None:
            return None
        entry, tier, verdict = verified
        return VerifiedEntry(entry=entry, tier=tier, verdict=verdict)

    results = await gather_resilient(*(_one(s) for s in suggestions))
    return [r for r in results if isinstance(r, VerifiedEntry)]
```

`Callable` and `Awaitable` are no longer used in the body after dropping `persist`; remove them from the `TYPE_CHECKING` block at the top of the file (lines 73-74) if no other callsite needs them. Re-check by grepping the file.

Add `from typing import Literal` at the top of the module if not already present — `VerifiedEntry.verdict` uses it directly instead of leaking the private `_AdmitVerdict` alias through the public surface (see spec lines 130-135).

- [x] **Step 10: Run typecheck to confirm `verify_all` is self-consistent**

Run: `just typecheck`
Expected: **errors at the consumer sites are expected here.** `pipeline.py` still imports `verify_and_persist_all` from `slopmortem.stages` and `stages/__init__.py` still re-exports it from the moved path — both resolve cleanly only after Steps 18 and 21. The load-bearing assertion at this gate is that errors are *only* in those known files (`pipeline.py`, `stages/__init__.py`, `stages/recall_persist.py`, `tests/stages/test_recall_*`). Errors inside `recall/_verify.py` itself (especially unused `Callable`/`Awaitable` imports) are real — fix immediately. Treat as a clean gate once the consumer errors are isolated.

#### Phase C — Move the brainstorm half and split coverage-gap

- [x] **Step 11: Move `llm_recall.py` to `recall/_brainstorm.py`**

Run: `git mv slopmortem/stages/llm_recall.py slopmortem/recall/_brainstorm.py`

- [x] **Step 12: Carve `compute_coverage_gap` and `CoverageGapResult` out into `stages/coverage_gap.py`**

Create `slopmortem/stages/coverage_gap.py` with the lifted definitions. Match the existing module docstring style.

```python
"""Coverage-gap predicate: decides whether the recall branch should fire.

Pipeline reads ``compute_coverage_gap`` to gate the recall call. The
``RECALL_GAP_SCORE`` and ``RECALL_GAP_SCORE_AFTER`` events that ride
on its output stay in ``pipeline.py``; this module is pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slopmortem.models import Candidate, ScoredCandidate


@dataclass(frozen=True)
class CoverageGapResult:
    """Coverage-gap counts plus the fire/don't-fire decision.

    ``qualifying``/``required`` are emitted on every query so eval can sweep
    predicate thresholds against historical traces.
    """

    qualifying: int
    required: int

    @property
    def gap(self) -> bool:
        return self.qualifying < self.required


def compute_coverage_gap(
    *,
    retrieved: list[Candidate],
    ranked: list[ScoredCandidate],
    pitch_sector: str,
    min_similarity_score: float,
    n_synthesize: int,
) -> CoverageGapResult:
    """Score the retrieve+rerank result against the LLM-recall predicate.

    Counts candidates that are both high-quality (mean perspective ≥
    ``min_similarity_score``) and in-sector (own sector or ``"other"``).
    Fewer than ``n_synthesize`` qualifying → gap.

    ``pitch_sector == "other"`` short-circuits the in-sector check: sector
    is uninformative there, so quality alone gates the count.
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
            continue
        if cand.payload.facets.sector in (pitch_sector, "other"):
            qualifying += 1
    return CoverageGapResult(qualifying=qualifying, required=n_synthesize)
```

- [x] **Step 13: Delete `CoverageGapResult` and `compute_coverage_gap` from `recall/_brainstorm.py`**

In `slopmortem/recall/_brainstorm.py`, remove lines 35-84 (the `@dataclass CoverageGapResult` block and `compute_coverage_gap` function — they're now in `stages/coverage_gap.py`). Keep `PriorCandidateHint`, `logger`, and `llm_recall`. The module docstring at line 1 should be updated:

Change:
```python
"""LLM-recall fallback: coverage-gap predicate, plus the recall stage call."""
```

to:
```python
"""LLM brainstorm half of the recall subsystem: asks Opus for comparable failures."""
```

#### Phase D — Internal models module and entrypoint

- [x] **Step 14: Create `slopmortem/recall/_models.py` with `RecallDeps`, `RecallConfig`, `VerifiedEntry` re-export**

Create the file. `RecallDeps` and `RecallConfig` are new; `VerifiedEntry` was defined in `_verify.py` in Step 9 — re-export it here so `_models.py` is the single import site for records that the package's public surface exposes.

```python
"""Record types for the recall subsystem.

``VerifiedEntry`` is re-exported from ``_verify`` so the package surface
has a single import site for records the public ``recall()`` returns.
``RecallDeps`` / ``RecallConfig`` exist here so the recall package never
imports ``slopmortem.config`` — the pipeline builds these from the global
``Config`` at the call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from slopmortem.recall._verify import VerifiedEntry as VerifiedEntry

if TYPE_CHECKING:
    from slopmortem.corpus.sources import Enricher
    from slopmortem.llm import LLMClient
    from slopmortem.models import ToolSpec
    from slopmortem.recall._verify import DeathnessConfig, ExtractFn, TavilySearchFn


@dataclass(frozen=True)
class RecallDeps:
    """Runtime deps recall cannot default from ``RecallConfig``.

    ``wayback=None`` lets eval / CLI / replay callers skip the
    ``WaybackEnricher()`` construction; ``recall()`` lazy-defaults it.
    """

    llm: LLMClient
    tavily_search: TavilySearchFn
    extract: ExtractFn
    wayback: Enricher | None = None


@dataclass(frozen=True)
class RecallConfig:
    """All knobs the recall subsystem reads.

    Names are local to this record — the pipeline-side ``_recall_config_from``
    maps from the global ``Config``. Do NOT add ``suggestion_cap`` or
    ``max_tavily_calls`` to global ``Config``; they exist only here.

    ``tools=[]`` is the canonical "tools disabled" state — neither
    ``enable_tavily_recall_search`` nor ``recall_max_tavily_calls`` survives
    as a separate field on this record. Build ``tools`` via
    ``slopmortem.llm.recall_tools(config)`` at the call site.

    ``model_facet`` and ``max_tokens_facet`` are consumed **only** when
    ``recall()`` is called with ``facets=None`` (eval / CLI / replay).
    The pipeline hot path always passes pre-extracted facets, so these
    fields are dead weight on production traffic. Don't prune them — the
    standalone ``facets=None`` branch is the package's external surface.
    """

    model_facet: str
    max_tokens_facet: int
    model_recall: str
    max_tokens_recall: int
    suggestion_cap: int
    tools: list[ToolSpec]
    max_tavily_calls: int
    tavily_max_results: int
    deathness: DeathnessConfig
```

- [x] **Step 15: Implement `recall(...)` in `slopmortem/recall/__init__.py`**

Replace the contents of `slopmortem/recall/__init__.py` with the full public surface, including the entrypoint. `extract_facets` is imported lazily inside the function body to avoid a cycle with `stages` (which today re-exports recall items).

```python
"""LLM-recall subsystem: find similar dead startups and decide which ones are real.

Public surface: ``recall(pitch, *, facets=None, prior_hints=None, deps, config)`` —
one async function, four runtime deps, one config record. Persistence,
re-retrieval, and the coverage-gap predicate live pipeline-side; this package
is read-only with respect to the corpus.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lmnr import Laminar

from slopmortem.corpus.sources import WaybackEnricher
from slopmortem.recall._brainstorm import PriorCandidateHint as PriorCandidateHint
from slopmortem.recall._brainstorm import llm_recall
from slopmortem.recall._models import RecallConfig as RecallConfig
from slopmortem.recall._models import RecallDeps as RecallDeps
from slopmortem.recall._verify import DeathnessConfig as DeathnessConfig
from slopmortem.recall._verify import VerificationTier as VerificationTier
from slopmortem.recall._verify import VerifiedEntry as VerifiedEntry
from slopmortem.recall._verify import verify_all
from slopmortem.recall.fake import FakeRecaller as FakeRecaller
from slopmortem.tracing import SpanEvent

if TYPE_CHECKING:
    from slopmortem.models import Facets


__all__ = [
    "DeathnessConfig",
    "FakeRecaller",
    "PriorCandidateHint",
    "RecallConfig",
    "RecallDeps",
    "VerificationTier",
    "VerifiedEntry",
    "recall",
]


async def recall(
    pitch: str,
    *,
    facets: Facets | None = None,
    prior_hints: list[PriorCandidateHint] | None = None,
    deps: RecallDeps,
    config: RecallConfig,
) -> list[VerifiedEntry]:
    """Find similar failed startups for ``pitch`` and decide which ones are real.

    ``facets=None`` extracts facets internally via Haiku (one extra call per
    fire on that path). Pipeline callers pass the value they already
    extracted upstream and skip the extra call. ``prior_hints=None`` is
    equivalent to ``[]`` — the prompt template renders its "(none — corpus
    returned no in-vertical matches)" branch.

    Returns ``[]`` when brainstorm produced no suggestions, every L0–L5
    drop fired, or transport failures isolated all candidates. The function
    never raises for per-suggestion failures.
    """
    from slopmortem.stages.facet_extract import extract_facets  # noqa: PLC0415 - lazy: keeps slopmortem.recall outside `stages-leaf` contract source_modules (see .importlinter)

    if facets is None:
        facets = await extract_facets(
            pitch,
            deps.llm,
            model=config.model_facet,
            max_tokens=config.max_tokens_facet,
        )

    suggestions = await llm_recall(
        pitch=pitch,
        facets=facets,
        current_top_n=list(prior_hints) if prior_hints is not None else [],
        llm=deps.llm,
        model=config.model_recall,
        max_tokens=config.max_tokens_recall,
        cap=config.suggestion_cap,
        tools=config.tools,
        recall_max_tavily_calls=config.max_tavily_calls,
    )
    if Laminar.is_initialized():
        Laminar.event(
            name=str(SpanEvent.RECALL_SUGGESTIONS_RECEIVED),
            attributes={"count": len(suggestions)},
        )
    if not suggestions:
        return []

    wayback = deps.wayback if deps.wayback is not None else WaybackEnricher()
    return await verify_all(
        suggestions,
        wayback=wayback,
        llm=deps.llm,
        tavily_search=deps.tavily_search,
        extract=deps.extract,
        tavily_recall_max_results=config.tavily_max_results,
        deathness=config.deathness,
    )
```

- [x] **Step 16: Create `slopmortem/recall/fake.py` with `FakeRecaller`**

`FakeRecaller` is a callable matching the `recall(...)` signature for tests that don't care about verifier internals.

```python
"""Fake recall callable for tests that exercise the pipeline seam without driving L0–L5.

Returns a pre-baked list of ``VerifiedEntry`` records and records the
invocation for assertion. Pipeline tests checking dedup / floor / re-retrieve
inject one of these instead of wiring four fakes (LLM + Tavily search +
extract + Wayback) end-to-end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slopmortem.models import Facets
    from slopmortem.recall._brainstorm import PriorCandidateHint
    from slopmortem.recall._models import RecallConfig, RecallDeps
    from slopmortem.recall._verify import VerifiedEntry


@dataclass
class _RecallCall:
    pitch: str
    facets: Facets | None
    prior_hints: list[PriorCandidateHint] | None


@dataclass
class FakeRecaller:
    """Callable shaped like ``recall(...)``. Returns ``verified`` and records calls."""

    verified: list[VerifiedEntry] = field(default_factory=list)
    calls: list[_RecallCall] = field(default_factory=list)

    async def __call__(
        self,
        pitch: str,
        *,
        facets: Facets | None = None,
        prior_hints: list[PriorCandidateHint] | None = None,
        deps: RecallDeps,  # noqa: ARG002 - present for signature parity
        config: RecallConfig,  # noqa: ARG002 - present for signature parity
    ) -> list[VerifiedEntry]:
        self.calls.append(_RecallCall(pitch=pitch, facets=facets, prior_hints=prior_hints))
        return list(self.verified)
```

- [x] **Step 17: Run typecheck on the recall package**

Run: `just typecheck`
Expected: **the new `slopmortem.recall.*` modules typecheck clean in isolation;** consumer-side errors in `pipeline.py` and `stages/__init__.py` persist until Step 18+21 land. Verify by inspecting that no new error originates inside `slopmortem/recall/`. If a circular import fires, the lazy `extract_facets` import in `recall/__init__.py` was likely flipped to a top-level import; restore it.

#### Phase E — `stages/__init__.py` re-exports cleanup

- [x] **Step 18: Remove recall-internal re-exports from `stages/__init__.py` and add the coverage-gap re-export**

Replace `slopmortem/stages/__init__.py` with the trimmed version. `verify_and_persist_all` and `verify_suggestion` are gone; `compute_coverage_gap` now comes from `stages.coverage_gap`; `llm_recall` and `VerificationTier` move out of `stages` entirely.

```python
"""Pipeline stages: facet_extract, retrieve, llm_rerank, synthesize, consolidate_risks."""

from __future__ import annotations

from slopmortem.stages.consolidate_risks import consolidate_risks as consolidate_risks
from slopmortem.stages.coverage_gap import CoverageGapResult as CoverageGapResult
from slopmortem.stages.coverage_gap import compute_coverage_gap as compute_coverage_gap
from slopmortem.stages.facet_extract import extract_facets as extract_facets
from slopmortem.stages.llm_rerank import (
    llm_rerank as llm_rerank,
)
from slopmortem.stages.llm_rerank import (
    select_top_n_by_similarity as select_top_n_by_similarity,
)
from slopmortem.stages.recall_persist import persist_recall_entry as persist_recall_entry
from slopmortem.stages.retrieve import (
    SparseEncoder as SparseEncoder,
)
from slopmortem.stages.retrieve import (
    retrieve as retrieve,
)
from slopmortem.stages.synthesize import (
    drop_below_min_similarity as drop_below_min_similarity,
)
from slopmortem.stages.synthesize import (
    synthesize as synthesize,
)
from slopmortem.stages.synthesize import (
    synthesize_all as synthesize_all,
)
from slopmortem.stages.synthesize import (
    synthesize_prompt_kwargs as synthesize_prompt_kwargs,
)

__all__ = [
    "CoverageGapResult",
    "SparseEncoder",
    "compute_coverage_gap",
    "consolidate_risks",
    "drop_below_min_similarity",
    "extract_facets",
    "llm_rerank",
    "persist_recall_entry",
    "retrieve",
    "select_top_n_by_similarity",
    "synthesize",
    "synthesize_all",
    "synthesize_prompt_kwargs",
]
```

- [x] **Step 19: Update `slopmortem/stages/recall_persist.py` `TYPE_CHECKING` import**

In the `if TYPE_CHECKING:` block (around line 40), change:

```python
from slopmortem.stages.recall_verify import VerificationTier
```

to:

```python
from slopmortem.recall import VerificationTier
```

- [x] **Step 20: Run typecheck to confirm the stages reshuffle holds**

Run: `just typecheck`
Expected: **errors localised to `pipeline.py` only.** Steps 18 and 19 cleaned every other consumer; `pipeline.py`'s imports get rewritten in Step 21. Anything still pointing at `slopmortem.stages.recall_verify` or `slopmortem.stages.llm_recall` from a file other than `pipeline.py` is a miss — fix before moving on.

#### Phase F — Pipeline reshape

- [x] **Step 21: Replace `pipeline.RecallDeps` with the narrow recall deps + new `PersistDeps`**

Open `slopmortem/pipeline.py`. Replace the imports (lines 22-41) and the `RecallDeps` dataclass (lines 77-99) with the new shape. The recall import comes through `slopmortem.recall`; `PersistDeps` is pipeline-local and carries the journal / slop classifier / post_mortems_root previously on `RecallDeps`.

Replace lines 22-41:

```python
from slopmortem.budget import BudgetExceededError
from slopmortem.ingest import IngestResult, NullProgress
from slopmortem.llm import recall_tools
from slopmortem.models import PipelineMeta, Report, Synthesis, TopRisks
from slopmortem.recall import (
    DeathnessConfig,
    PriorCandidateHint,
    RecallConfig,
    RecallDeps,
    VerifiedEntry,
    recall,
)
from slopmortem.stages import (
    compute_coverage_gap,
    consolidate_risks,
    drop_below_min_similarity,
    extract_facets,
    llm_rerank,
    persist_recall_entry,
    retrieve,
    select_top_n_by_similarity,
    synthesize_all,
)
from slopmortem.tracing import SpanEvent, git_sha, mint_run_id
```

Replace lines 77-99 (the `RecallDeps` dataclass) with:

```python
@dataclass(frozen=True)
class PersistDeps:
    """Long-lived deps the recall persist tail needs.

    Split from ``RecallDeps`` so the recall subsystem doesn't see the
    journal, slop classifier, or post_mortems_root. The pipeline raises
    ``RuntimeError`` when recall fires without both ``RecallDeps`` and
    ``PersistDeps``.
    """

    journal: MergeJournal
    slop_classifier: SlopClassifier
    post_mortems_root: Path
```

`RecallDeps` is imported directly from `slopmortem.recall` above — no
backwards-compatibility alias. Steps 27, 32, and 33 rewrite every prior
caller's import (`cli/_query_cmd.py`, `tests/test_pipeline_recall_fallback.py`,
`tests/stages/test_recall_persist_gap_closures.py`) to source `RecallDeps`
from `slopmortem.recall` directly.

Add `Path`, `MergeJournal`, and `SlopClassifier` to the `TYPE_CHECKING` block (they're already there for the old `RecallDeps`; keep them).

- [x] **Step 22: Add the `_recall_config_from` builder to `pipeline.py`**

Insert the builder near the other module-level helpers (after `cutoff_iso`, around line 137):

```python
def _recall_config_from(config: Config) -> RecallConfig:
    """Project the global ``Config`` onto the recall subsystem's narrow surface.

    ``recall_tools(config)`` collapses ``enable_tavily_recall_search`` and
    ``recall_max_tavily_calls`` into the canonical ``tools=[]`` "disabled"
    state at this seam; the recall package never imports global ``Config``.
    """
    return RecallConfig(
        model_facet=config.model_facet,
        max_tokens_facet=config.max_tokens_facet,
        model_recall=config.model_recall,
        max_tokens_recall=config.max_tokens_recall,
        suggestion_cap=config.recall_max_suggestions_per_pitch,
        tools=recall_tools(config),
        max_tavily_calls=config.recall_max_tavily_calls,
        tavily_max_results=config.tavily_recall_max_results,
        deathness=DeathnessConfig(
            model=config.model_recall_deathness,
            max_tokens=config.max_tokens_recall_deathness,
            min_confidence=config.recall_deathness_min_confidence,
            struggling_min_confidence=config.recall_struggling_min_confidence,
        ),
    )
```

- [x] **Step 23: Replace `_run_recall_branch` with the slim composition**

Replace the entire `_run_recall_branch` function (lines 157-307 of the original `pipeline.py`) with:

```python
async def _run_recall_branch(  # noqa: PLR0913 - leaf helper; every dep flows through from run_query
    *,
    input_ctx: InputContext,
    facets: Facets,
    retrieved: list[Candidate],
    reranked: LlmRerankResult,
    cutoff: str | None,
    llm: LLMClient,
    embedding_client: EmbeddingClient,
    corpus: Corpus,
    config: Config,
    sparse_encoder: SparseEncoder,
    recall_deps: RecallDeps,
    persist_deps: PersistDeps,
) -> _RecallOutcome:
    """Recall fallback: ``recall(...)`` → persist (concurrent, isolated) → re-retrieve / re-rerank.

    Runs at most once per query. Caller owns the ``QueryProgress`` hooks.
    """
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
        deps=recall_deps,
        config=_recall_config_from(config),
    )
    if not verified:
        return _RecallOutcome(retrieved=retrieved, reranked=reranked, persisted_count=0, used=False)

    ingest_corpus = cast("IngestCorpus", corpus)
    # CapacityLimiter(3) mirrors today's verify-time concurrency ceiling.
    # `recall_max_suggestions_per_pitch` defaults to 8; without the limiter,
    # up to 8 Qdrant upserts + journal writes + slop-classify LLM hops fan
    # out concurrently on the query critical path. The spec at lines 305-311
    # called this "out of scope"; this plan reinstates the cap because the
    # default config is already above the safe-parallel ceiling.
    #
    # Concurrency profile vs. today: today's `verify_and_persist_all` ran
    # verify+persist inside one limiter per suggestion, so persist-of-N could
    # overlap verify-of-N+1. After the extraction the two phases run
    # sequentially — `verify_all` drains fully before `gather_resilient` over
    # `_persist_one` begins. Worst-case in-flight count stays at 3; wall-clock
    # has a minor regression on multi-suggestion queries. Acceptable: persist
    # is the cheaper phase, and the simpler control flow is worth it.
    persist_limiter = anyio.CapacityLimiter(3)

    async def _persist_one(v: VerifiedEntry) -> bool:
        async with persist_limiter:
            try:
                await persist_recall_entry(
                    v.entry,
                    v.tier,
                    deathness_verdict=v.verdict,
                    journal=persist_deps.journal,
                    corpus=ingest_corpus,
                    embed_client=embedding_client,
                    llm=llm,
                    slop_classifier=persist_deps.slop_classifier,
                    sparse_encoder=sparse_encoder,
                    config=config,
                    post_mortems_root=persist_deps.post_mortems_root,
                    progress=NullProgress(),
                    result=IngestResult(),
                )
            except Exception as exc:  # noqa: BLE001 - per-suggestion isolation; mirrors today's gather_resilient drop
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
    if persisted_count == 0:
        return _RecallOutcome(retrieved=retrieved, reranked=reranked, persisted_count=0, used=False)

    new_retrieved = await retrieve(
        description=input_ctx.description,
        facets=facets,
        corpus=corpus,
        embedding_client=embedding_client,
        cutoff_iso=cutoff,
        strict_deaths=config.strict_deaths,
        k_retrieve=config.K_retrieve,
        sparse_encoder=sparse_encoder,
        strict_sector_filter=config.strict_sector_filter,
        strict_sector_filter_excludes_other=config.strict_sector_filter_excludes_other,
    )
    new_reranked = await llm_rerank(
        new_retrieved,
        input_ctx.description,
        facets,
        llm,
        config,
        model=config.model_rerank,
        max_tokens=config.max_tokens_rerank,
    )
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
    return _RecallOutcome(
        retrieved=new_retrieved,
        reranked=new_reranked,
        persisted_count=persisted_count,
        used=True,
    )
```

Note `gather_resilient` and `anyio` need to be importable here. Add:

```python
import anyio

from slopmortem.concurrency import gather_resilient
```

near the other top-level imports.

- [x] **Step 24: Update `run_query`'s signature and force-mode guard to take both dep records**

The `run_query` signature (line 314) currently ends with:

```python
async def run_query(  # noqa: PLR0913, C901, PLR0915 - orchestration: every phase + recall branch lands inline
    input_ctx: InputContext,
    *,
    llm: LLMClient,
    embedding_client: EmbeddingClient,
    corpus: Corpus,
    config: Config,
    budget: Budget,
    progress: QueryProgress | None = None,
    sparse_encoder: SparseEncoder | None = None,
    recall_deps: RecallDeps | None = None,
) -> Report:
```

Insert `persist_deps: PersistDeps | None = None,` immediately after `recall_deps`. Resulting signature:

```python
async def run_query(  # noqa: PLR0913, C901, PLR0915 - orchestration: every phase + recall branch lands inline
    input_ctx: InputContext,
    *,
    llm: LLMClient,
    embedding_client: EmbeddingClient,
    corpus: Corpus,
    config: Config,
    budget: Budget,
    progress: QueryProgress | None = None,
    sparse_encoder: SparseEncoder | None = None,
    recall_deps: RecallDeps | None = None,
    persist_deps: PersistDeps | None = None,
) -> Report:
```

All current callers pass kwargs (no positional past `input_ctx`), so the insertion order is purely cosmetic — the convention is to mirror the `recall_deps` placement. Non-CLI callsites that today don't pass `recall_deps` (and so won't pass `persist_deps` either) keep working unchanged: `slopmortem/cli/_replay_cmd.py:70`, `slopmortem/evals/runner.py:230`, `slopmortem/evals/runner.py:270`, `slopmortem/evals/recording_helper.py:222`. Step 27 covers the one CLI callsite that *does* need both records (`slopmortem/cli/_query_cmd.py:162`).

Update the force-flag guard (lines 438-447) to check each independently so the error names the missing record:

```python
        if should_fire and (recall_deps is None or persist_deps is None):
            missing = "RecallDeps" if recall_deps is None else "PersistDeps"
            if config.force_llm_recall:
                msg = f"force_llm_recall=True but {missing} not provided"
                raise RuntimeError(msg)
            logger.info("coverage_gap fired but %s not provided; skipping recall", missing)
            should_fire = False

        if should_fire and recall_deps is not None and persist_deps is not None:
            if sparse_encoder is None:
                msg = "recall requires sparse_encoder"
                raise RuntimeError(msg)
            progress.start_phase(QueryPhase.RECALL, total=1)
            outcome = await _run_recall_branch(
                input_ctx=input_ctx,
                facets=facets,
                retrieved=retrieved,
                reranked=reranked,
                cutoff=cutoff,
                llm=llm,
                embedding_client=embedding_client,
                corpus=corpus,
                config=config,
                sparse_encoder=sparse_encoder,
                recall_deps=recall_deps,
                persist_deps=persist_deps,
            )
            retrieved = outcome.retrieved
            reranked = outcome.reranked
            recall_persisted_count = outcome.persisted_count
            recall_used = outcome.used
            progress.advance_phase(QueryPhase.RECALL)
            progress.end_phase(QueryPhase.RECALL)
```

The error message format changes from a single static string to `f"force_llm_recall=True but {missing} not provided"` so the two failure modes are distinguishable. The existing test at `tests/test_pipeline_recall_fallback.py:967` greps the literal `"force_llm_recall=True but RecallDeps not provided"` — the new format still matches that substring when `recall_deps is None`, so the existing test passes unchanged. The new symmetric test (Step 32) greps for `"force_llm_recall=True but PersistDeps not provided"`.

- [x] **Step 25: Run typecheck on `pipeline.py`**

Run: `just typecheck`
Expected: PASS. Errors mentioning `WaybackEnricher` import unused mean it's no longer needed in `pipeline.py` (recall handles the lazy default internally) — remove the import.

#### Phase G — `deps.py` and CLI wiring

- [x] **Step 26: Update the `TYPE_CHECKING` import in `slopmortem/deps.py`**

Around line 28, change:

```python
from slopmortem.stages.recall_verify import ExtractFn, TavilySearchFn
```

to:

```python
from slopmortem.recall._verify import ExtractFn, TavilySearchFn
```

If the test suite later complains that `ExtractFn` / `TavilySearchFn` should live at `slopmortem.recall.__init__` for the public surface, add a re-export — but only on demand; keep the surface minimal for now.

- [x] **Step 27: Update CLI wiring to pass `persist_deps` alongside `recall_deps`**

Grep for `RecallDeps(` and `run_query(` across all production callers:

Run: `grep -rn "RecallDeps(\|run_query(" slopmortem/cli/ slopmortem/evals/`

The construction site is `slopmortem/cli/_query_cmd.py:156` (via `_build_recall_deps`). Split that helper into two constructions — a `RecallDeps(...)` carrying `llm`, `tavily_search`, `extract`, `wayback`, and a `PersistDeps(...)` carrying `journal`, `slop_classifier`, `post_mortems_root`. Update the `run_query(...)` call at line 162 to pass both records.

The other `run_query(...)` callsites do NOT need updates:

- `slopmortem/cli/_replay_cmd.py:70` — replay path doesn't fire recall; passes no `recall_deps`. Default `persist_deps=None` is correct.
- `slopmortem/evals/runner.py:230, 270` — eval doesn't fire recall today; same default-None applies.
- `slopmortem/evals/recording_helper.py:222` — same.

Also update `slopmortem/cli/_query_cmd.py:34` to drop the `RecallDeps` import from `slopmortem.pipeline` and import it from `slopmortem.recall` instead (the pipeline no longer re-exports it). Add `PersistDeps` to the pipeline import.

The fields move 1:1 — no value transformation. Verify by inspection that no field is dropped.

- [x] **Step 28: Run typecheck on the CLI**

Run: `just typecheck`
Expected: PASS. Any reference to `RecallDeps.journal` / `.slop_classifier` / `.post_mortems_root` is now on `PersistDeps`.

#### Phase H — Test migration

- [x] **Step 29: Create `tests/recall/` skeleton**

Create `tests/recall/__init__.py` (empty file). Create `tests/recall/conftest.py` as a placeholder — move any fixtures from `tests/stages/conftest.py` that only test verifier/brainstorm internals here. If `tests/stages/conftest.py` has shared fixtures used by both verifier tests and other stage tests, leave them in place and import from the stages conftest if needed (pytest auto-discovers parent conftests, so usually nothing is required).

```python
"""Shared fixtures for tests under ``tests/recall/`` — package-local to keep
verifier internals from leaking into ``tests/stages/`` fixtures."""

from __future__ import annotations
```

- [x] **Step 30: Move verifier and brainstorm tests into `tests/recall/`**

Run each `git mv` so history follows:

```
git mv tests/stages/test_recall_verify.py tests/recall/test_verify.py
git mv tests/stages/test_recall_verify_l2_l4_bprime.py tests/recall/test_verify_l2_l4_bprime.py
git mv tests/stages/test_recall_verify_l3_hygiene.py tests/recall/test_verify_l3_hygiene.py
git mv tests/stages/test_recall_verify_l5_tristate.py tests/recall/test_verify_l5_tristate.py
git mv tests/stages/test_recall_l3_extract_fallback.py tests/recall/test_l3_extract_fallback.py
git mv tests/stages/test_recall_search_head.py tests/recall/test_search_head.py
git mv tests/stages/test_recall_search_preflight.py tests/recall/test_search_preflight.py
git mv tests/stages/test_llm_recall.py tests/recall/test_brainstorm.py
git mv tests/stages/test_coverage_gate.py tests/stages/test_coverage_gap.py
```

The coverage-gap test stays in `tests/stages/` because the predicate lives there now; only the filename gets a friendlier rename.

- [x] **Step 31: Rewrite import paths in the moved test files**

In each moved file under `tests/recall/`, run a find+replace from `slopmortem.stages.recall_verify` → `slopmortem.recall._verify` and from `slopmortem.stages.llm_recall` → `slopmortem.recall._brainstorm`. Confirm by:

```
grep -rn "slopmortem.stages.recall_verify\|slopmortem.stages.llm_recall" tests/
```

Expected: matches only in `tests/test_pipeline_recall_fallback.py` (Step 32 handles that) and `tests/stages/test_recall_persist*.py` (Step 33 handles those). Anything in `tests/recall/` is a miss — fix it.

- [x] **Step 31b: Rewrite `verify_and_persist_all(..., persist=...)` callsites in `tests/recall/test_verify.py`**

The renamed `verify_all(...)` (Step 9) takes no `persist` callback and returns `list[VerifiedEntry]` instead of `list[RawEntry]`. The moved file `tests/recall/test_verify.py` (was `tests/stages/test_recall_verify.py`) has two callsites at the pre-move lines `605, 608, 649, 652` that pass `persist=typed_persist`, and one test name asserting old semantics:

1. `test_verify_all_via_gather_resilient_isolates_failures` (pre-move line 563): drives three suggestions, one mid-verify raises `ValueError`, asserts sibling-isolation at the verifier seam. Rewrite to:
   - call `verify_all(...)` without `persist=`
   - assert the returned `list[VerifiedEntry]` has two elements (the surviving siblings), not three
   - drop the `persisted` accumulator

2. `test_verify_skips_persist_for_dropped_suggestions` (pre-move line 628): the persist callback no longer exists. Reframe as `test_verify_drops_rejected_suggestions_from_returned_list` — assert L0/L2/L4/L5-drops are absent from the returned `list[VerifiedEntry]`. The "doesn't call persist" claim moves to a pipeline-level concern (Step 40 covers persist isolation; pipeline only calls `_persist_one` for entries that survived `verify_all`).

Grep to enumerate all rewrites needed:

```
grep -n "verify_and_persist_all\|persist=" tests/recall/test_verify.py
```

Expected: zero `verify_and_persist_all` matches after this step; zero `persist=` matches inside `verify_*` calls.

- [x] **Step 32: Rewrite monkeypatch and import paths in `tests/test_pipeline_recall_fallback.py`**

Two surgical edits.

First, change the imports at line 36 and the section above:

```python
from slopmortem.pipeline import RecallDeps, run_query
```

becomes:

```python
from slopmortem.pipeline import PersistDeps, run_query
from slopmortem.recall import RecallDeps
```

Second, the monkeypatch paths at lines 467-468:

```python
    monkeypatch.setattr("slopmortem.stages.recall_verify.safe_head", fake_head)
    monkeypatch.setattr("slopmortem.stages.recall_verify.safe_get", fake_get)
```

become:

```python
    monkeypatch.setattr("slopmortem.recall._verify.safe_head", fake_head)
    monkeypatch.setattr("slopmortem.recall._verify.safe_get", fake_get)
```

Third, the `_make_recall_deps` helper around line 646 currently builds the old `RecallDeps` carrying all six fields. Split it into two helpers:

```python
async def _make_recall_deps(...) -> RecallDeps:
    ...
    return RecallDeps(
        tavily_search=...,
        extract=...,
        wayback=...,
        llm=...,
    )


def _make_persist_deps(tmp_path: Path) -> PersistDeps:
    ...
    return PersistDeps(
        journal=...,
        slop_classifier=...,
        post_mortems_root=...,
    )
```

Every callsite that previously passed `recall_deps=...` to `run_query` now also passes `persist_deps=_make_persist_deps(tmp_path)`. Grep:

```
grep -n "run_query(" tests/test_pipeline_recall_fallback.py
```

Update each call.

The error-path test at line 942 (`test_pipeline_recall_raises_when_recall_deps_missing`) passes `recall_deps=None` to `run_query` and expects the `RuntimeError`. Add a sibling test for the new symmetric failure mode:

```python
async def test_pipeline_force_llm_recall_raises_when_persist_deps_missing(
    tmp_path: Path,
) -> None:
    """``force_llm_recall=True`` with ``persist_deps=None`` raises so misconfig surfaces.

    Mirrors the existing ``recall_deps=None`` guard. The two records split
    apart in the recall extraction; both must be present for recall to fire.
    """
    cfg = _build_config(k_retrieve=6, n_synthesize=3, force_llm_recall=True)
    # ... build the same minimal scaffold as test_pipeline_recall_raises_when_recall_deps_missing,
    # only with recall_deps=<populated> and persist_deps=None
    with pytest.raises(RuntimeError, match="force_llm_recall=True but PersistDeps not provided"):
        await run_query(
            input_ctx,
            llm=...,
            embedding_client=...,
            corpus=...,
            config=cfg,
            budget=...,
            sparse_encoder=...,
            recall_deps=<populated RecallDeps>,
            persist_deps=None,
        )
```

Copy the scaffolding from `test_pipeline_recall_raises_when_recall_deps_missing` (lines 942-978) verbatim and flip which dep is `None`.

- [x] **Step 33: Rewrite import paths in `tests/stages/test_recall_persist*.py` and `test_recall_retrieval_survival.py`**

Three files import from `slopmortem.stages.recall_verify`:

- `tests/stages/test_recall_persist.py:21` → `from slopmortem.recall import VerificationTier` and `from slopmortem.recall._verify import _recall_source_id`
- `tests/stages/test_recall_persist_gap_closures.py:37` → `from slopmortem.recall._verify import _recall_source_id`; plus the monkeypatches at lines 523-524 → `slopmortem.recall._verify.safe_head` / `safe_get`
- `tests/stages/test_recall_retrieval_survival.py:69` → `from slopmortem.recall._verify import _recall_source_id`

Also: `tests/stages/test_recall_persist_gap_closures.py:34` imports `RecallDeps` from `slopmortem.pipeline` (today the only `RecallDeps` symbol). After Step 21 splits the record, this file needs:

```python
from slopmortem.pipeline import PersistDeps, run_query
from slopmortem.recall import RecallDeps
```

and its `_make_recall_deps`-equivalent helper at lines 647-650 needs the same split as Step 32's `_make_recall_deps` / `_make_persist_deps` pair. Every `run_query(...)` callsite in this file that passes `recall_deps=` must also pass `persist_deps=<populated>`. Grep the file for `run_query(` and `RecallDeps(` to enumerate. Without this update, Step 21's import-side `RecallDeps` (sourced from `slopmortem.recall`) and this file's pipeline-side `RecallDeps` import collide on the same name with incompatible shapes — the file must commit to the new four-field record everywhere.

Grep to confirm:

```
grep -rn "slopmortem.stages.recall_verify\|slopmortem.stages.llm_recall" tests/
```

Expected after this step: zero matches.

- [x] **Step 34: Run the full test suite**

Run: `just test`
Expected: PASS. If a test fires `NoCannedResponseError`, the prompt didn't change; the cassette key may have shifted because facet extraction is now eager-extracted at the recall seam. Per the spec (Cassette stability), pipeline-driven recordings reuse the existing key. Failures here are real bugs — investigate the test, do not re-record.

#### Phase I — New tests for the recall entrypoint and `FakeRecaller`

- [x] **Step 35: Add a failing test for `recall()` accepting pre-extracted facets**

Create `tests/recall/test_recall_entrypoint.py` and add the first failing test. Pattern: when `facets` is passed in, `recall()` does NOT call the facet-extract LLM route.

```python
"""Tests for the ``recall(...)`` public entrypoint composition.

These are entrypoint-level tests — verifier internals are covered in
``test_verify*.py`` and brainstorm internals in ``test_brainstorm.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from slopmortem.llm import FakeLLMClient, FakeResponse
from slopmortem.recall import (
    DeathnessConfig,
    PriorCandidateHint,
    RecallConfig,
    RecallDeps,
    recall,
)

if TYPE_CHECKING:
    from slopmortem.models import Facets

# Fixtures (Facets stub, FakeLLMClient canned routes, fake tavily/extract/wayback,
# minimal RecallConfig) — copy the shape used in tests/recall/test_verify.py.
# Keep one helper that returns ``(deps, config)`` so each test reads cleanly.


async def test_recall_passes_pre_extracted_facets_through_without_re_extracting(
    facets_stub: Facets,
) -> None:
    """When ``facets=`` is passed, ``recall`` skips the internal facet extract."""
    llm = FakeLLMClient(canned={}, default_model="recall-model")
    # Intentionally do NOT seed a facet-extract response: if recall calls facet
    # extract here, FakeLLMClient raises NoCannedResponseError.
    # ... seed only the recall + deathness routes
    deps = RecallDeps(llm=llm, tavily_search=..., extract=..., wayback=...)
    config = RecallConfig(...)
    out = await recall("a pitch", facets=facets_stub, deps=deps, config=config)
    assert out == []  # empty brainstorm result is fine for this assertion
```

This is the smallest meaningful test. Run it and confirm it FAILS for the right reason (recall not implemented or import error).

Run: `pytest tests/recall/test_recall_entrypoint.py::test_recall_passes_pre_extracted_facets_through_without_re_extracting -v`
Expected: FAIL with import or call-shape error first, then PASS once you flesh out the fixtures.

- [x] **Step 36: Make the test pass, then add the symmetric "facets=None triggers facet extraction" test**

Flesh out the fixtures in `tests/recall/test_recall_entrypoint.py`. Once the first test passes, add:

```python
async def test_recall_extracts_facets_when_none_passed(
    facet_extract_canned: dict[tuple[str, str], FakeResponse],
) -> None:
    """``facets=None`` triggers one extra Haiku call to extract facets internally."""
    llm = FakeLLMClient(canned=facet_extract_canned, default_model="recall-model")
    # ... seed facet_extract response + brainstorm response
    deps = RecallDeps(llm=llm, ...)
    config = RecallConfig(model_facet="facet-model", ...)
    out = await recall("a pitch", facets=None, deps=deps, config=config)
    # Assert facet route was hit at least once
    assert any(call.model == "facet-model" for call in llm.calls)
```

Run: `pytest tests/recall/test_recall_entrypoint.py -v`
Expected: PASS.

- [x] **Step 37: Add test for `prior_hints=None` rendering the empty-hints prompt branch**

The recall prompt at `slopmortem/llm/prompts/llm_recall.j2:37-44` renders `current_top_n` inline. The empty list branch already exists for queries where retrieve returns nothing. Assert that `prior_hints=None` produces the same rendered prompt as `prior_hints=[]`.

```python
async def test_recall_treats_none_prior_hints_as_empty(...) -> None:
    """``prior_hints=None`` renders the prompt template's empty-hints branch.

    Equivalent to ``prior_hints=[]`` — both produce the
    "(none — corpus returned no in-vertical matches)" string.
    """
    # Compare the prompt body the LLM saw between two recall() calls,
    # one with prior_hints=None and one with prior_hints=[].
    # Both should produce the same `messages[*].content`.
```

Run: `pytest tests/recall/test_recall_entrypoint.py -v`
Expected: PASS.

- [x] **Step 38: Add tests for the three short-circuit paths**

In `tests/recall/test_recall_entrypoint.py`, add:

- `test_recall_returns_empty_when_brainstorm_returns_no_suggestions` — seed the brainstorm route to return `{"suggestions": []}`; assert `recall()` returns `[]` and the verifier is not invoked.
- `test_recall_returns_empty_when_verifier_drops_all` — seed brainstorm with one suggestion, seed L0 Tavily with zero hits; assert `[]`.
- `test_recall_isolates_per_suggestion_transport_failures` — two suggestions; one's Tavily search raises `httpx.HTTPError`; the other succeeds end-to-end. Assert the returned list has exactly the one success.

Each test is ~15-30 lines of fixture setup plus a few asserts.

Run: `pytest tests/recall/test_recall_entrypoint.py -v`
Expected: PASS.

- [x] **Step 39: Add `FakeRecaller` tests**

Create `tests/recall/test_fake_recaller.py`:

```python
"""Tests for ``FakeRecaller`` — the test-time stand-in for ``recall()``."""

from __future__ import annotations

import pytest

from slopmortem.recall import (
    DeathnessConfig,
    FakeRecaller,
    RecallConfig,
    RecallDeps,
    VerifiedEntry,
)


def _stub_verified_entry() -> VerifiedEntry:
    # Construct a minimal VerifiedEntry with a stub RawEntry, tier, verdict
    ...


async def test_fake_recaller_returns_seeded_verified_list() -> None:
    seed = [_stub_verified_entry()]
    fake = FakeRecaller(verified=seed)
    out = await fake("pitch", deps=..., config=...)
    assert out == seed


async def test_fake_recaller_records_invocations() -> None:
    fake = FakeRecaller(verified=[])
    await fake("pitch-1", facets=None, prior_hints=None, deps=..., config=...)
    await fake("pitch-2", facets=<stub>, prior_hints=[<hint>], deps=..., config=...)
    assert [c.pitch for c in fake.calls] == ["pitch-1", "pitch-2"]
    assert fake.calls[1].prior_hints == [<hint>]


async def test_fake_recaller_returned_list_is_a_copy() -> None:
    """Mutating the returned list must not mutate ``fake.verified``."""
    seed = [_stub_verified_entry()]
    fake = FakeRecaller(verified=seed)
    out = await fake("pitch", deps=..., config=...)
    out.append(_stub_verified_entry())
    assert len(fake.verified) == 1
```

Run: `pytest tests/recall/test_fake_recaller.py -v`
Expected: PASS.

- [x] **Step 40: Add a failing test for pipeline `_persist_one` failure isolation (uses `FakeRecaller`)**

In `tests/test_pipeline_recall_fallback.py`, add a new test that drives `run_query` with two suggestions, configures `persist_recall_entry` to raise on the first and succeed on the second, and asserts that `pipeline_meta.recall_persisted_count == 1`. The test injects a `FakeRecaller` via `monkeypatch.setattr("slopmortem.pipeline.recall", fake_recaller)` so it doesn't have to seed brainstorm + L0-L5 cassettes; this is the one production-shaped wiring of `FakeRecaller` that justifies shipping the fake. Use `monkeypatch.setattr` against `slopmortem.pipeline.persist_recall_entry` (the import target in `pipeline.py`) for the flaky-persist seam.

```python
async def test_pipeline_persist_failure_isolates_per_suggestion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One ``persist_recall_entry`` raising must not abort sibling persists.

    Mirrors the per-suggestion isolation that ``verify_and_persist_all``
    used to provide internally. After the recall extraction, the same
    invariant is enforced by ``_persist_one`` in ``pipeline._run_recall_branch``.
    """
    call_count = {"n": 0}

    async def flaky_persist(*args: object, **kwargs: object) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated persist failure")
        # Succeed for the second call.
        return None

    monkeypatch.setattr("slopmortem.pipeline.persist_recall_entry", flaky_persist)

    # Inject FakeRecaller so we skip brainstorm + L0-L5 cassette setup.
    # The fake returns two pre-baked VerifiedEntry records — exactly the seam
    # `_run_recall_branch` consumes when it fans out persist.
    fake_recaller = FakeRecaller(
        verified=[_stub_verified_entry("a"), _stub_verified_entry("b")],
    )
    monkeypatch.setattr("slopmortem.pipeline.recall", fake_recaller)

    report = await run_query(
        # ... usual scaffolding: input_ctx, llm (FakeLLMClient with synth route),
        # embedding_client (fake), corpus (fake), config (recall enabled),
        # budget, sparse_encoder, recall_deps=_make_recall_deps(tmp_path),
        # persist_deps=_make_persist_deps(tmp_path)
    )
    assert report.pipeline_meta.recall_persisted_count == 1
    assert report.pipeline_meta.recall_used is True
    # FakeRecaller saw exactly one call with the expected pitch + prior_hints shape
    assert len(fake_recaller.calls) == 1
```

`_stub_verified_entry(name)` returns a minimal `VerifiedEntry` with a stub `RawEntry`, `tier="canonical"`, `verdict="dead"` — copy the shape from `tests/recall/test_fake_recaller.py`'s helper (Step 39) so both files share the same fixture style.

Step 40 is intentionally test-first per TDD — flip it green by validating that Step 23's `_persist_one` `try/except` returns False on failure.

Run: `pytest tests/test_pipeline_recall_fallback.py::test_pipeline_persist_failure_isolates_per_suggestion -v`
Expected: PASS (Step 23 already implemented the isolation behaviour). This is also the test that makes `FakeRecaller` (Step 16) production-shaped infrastructure rather than test-only-self-test.

#### Phase J — `.importlinter` contract update

- [x] **Step 41: Add a `recall-private` contract entry to `.importlinter` and extend existing leaf contracts to govern the recall package**

Recall is a new top-level package and needs its own contract so private modules `_verify` and `_brainstorm` stay package-internal. Append to `.importlinter`:

```
[importlinter:contract:recall-private]
name = recall._* is private — outside imports go through __init__
type = forbidden
allow_indirect_imports = True
source_modules =
    slopmortem.cli
    slopmortem.ingest
    slopmortem.pipeline
    slopmortem.stages
    slopmortem.evals
    slopmortem.corpus
    slopmortem.llm
    slopmortem.tracing
forbidden_modules =
    slopmortem.recall._brainstorm
    slopmortem.recall._models
    slopmortem.recall._verify
```

Then add `slopmortem.recall` to the `source_modules` of the existing leaf contracts so the new package is also subject to "no private reaches into other packages":

- `corpus-leaf` (around line 4-37): add `slopmortem.recall` to `source_modules`. Recall imports `Enricher`, `WaybackEnricher`, and `SOURCE_LLM_RECALL` from `corpus.sources` (public surface, fine) and `extract_clean` from `slopmortem.corpus` (public, fine). Listing it in `source_modules` guards against future drift into `corpus._*`.
- `llm-leaf`: add `slopmortem.recall` — recall uses `LLMClient`, `recall_tools`, `ToolSpec` from the public `slopmortem.llm` surface only.
- `sources-leaf`: add `slopmortem.recall` — recall uses `corpus.sources` only via its public `__init__.py`.
- `tracing-leaf`: add `slopmortem.recall` — recall emits `Laminar.event(...)` via the public `slopmortem.tracing.SpanEvent` surface and `lmnr.Laminar` directly.

**Don't** add `slopmortem.recall` to `stages-leaf` source modules. The recall package has one stages import — the lazy `from slopmortem.stages.facet_extract import extract_facets` inside `recall()` (Step 15). `stages-leaf` forbids `stages.facet_extract` from listed `source_modules`; leaving `slopmortem.recall` out means the lazy import is permitted regardless of `allow_indirect_imports` behaviour. The alternative (eager top-level import + adding `slopmortem.recall` to `stages-leaf` `source_modules` + an `ignore_imports` exemption) is strictly more config to maintain for no gain. Document the rationale on the import line itself in Step 15 so a future contributor doesn't "fix" it eagerly.

Verify by running:

Run: `just lint`
Expected: PASS. If `corpus-leaf` flags `slopmortem.recall -> slopmortem.corpus.sources`, the access is through public surface and the contract config is too strict — but in practice this should already pass because the contract only forbids reaches into `corpus._*` private modules. The `TYPE_CHECKING` import in `slopmortem/deps.py` (Step 26 retargets it to `slopmortem.recall._verify`) does NOT need an `ignore_imports` entry — `slopmortem.deps` is not in the `recall-private` contract's `source_modules`, so the rule doesn't fire on it. If a future refactor adds `slopmortem.deps` to that contract's `source_modules`, add this then:

```
ignore_imports =
    slopmortem.deps -> slopmortem.recall._verify
```

`ExtractFn` / `TavilySearchFn` are recall-leaf type aliases that `deps.py` references at TYPE_CHECKING time only.

#### Phase K — Cleanup, verification, and the final gauntlet

- [x] **Step 42: Confirm no stale `verify_and_persist_all` references remain**

Run: `grep -rn "verify_and_persist_all" slopmortem/ tests/`
Expected: zero matches. Anything found is a leftover from the rename.

- [x] **Step 43: Confirm no stale `stages.llm_recall` or `stages.recall_verify` references remain**

Run: `grep -rn "stages.llm_recall\|stages.recall_verify" slopmortem/ tests/ docs/`
Expected: matches only in `docs/specs/` and `docs/plans/` (historical). Anything under `slopmortem/` or `tests/` is a miss.

- [x] **Step 44: Run lint**

Run: `just lint`
Expected: PASS. Common misses: unused `Callable`/`Awaitable` imports in `_verify.py`, dead `WaybackEnricher` import in `pipeline.py`, `PLR0913` reactivated on a function that crossed the 5-arg threshold during the reshape.

- [x] **Step 45: Run typecheck**

Run: `just typecheck`
Expected: PASS. basedpyright is strict — if a `Facets | None` narrowing in `recall()` flags, add an `assert facets is not None` after the eager-extract path (or restructure the conditional so the type narrows on its own).

- [x] **Step 46: Run the full test suite**

Run: `just test`
Expected: PASS.

- [x] **Step 47: Run eval to confirm the offline cassette baseline is byte-identical**

Run: `just eval`
Expected: PASS, with no diff against the existing baseline.

**This gate is structural-only for recall.** The eval cassettes contain `embed`, `facet_extract`, and `synthesize` exchanges; there are no `llm_recall` / `recall_verify` / `recall_deathness` recordings. Eval never fires the recall branch on its current pitches, so "no diff" proves only that the non-recall pipeline is unchanged — it proves nothing about recall correctness. The load-bearing signal for the refactor is **Step 46 (`just test`)**, which exercises every recall code path via the moved/new unit tests plus the `tests/test_pipeline_recall_fallback.py` integration suite. Do not treat Step 47 as recall-coverage evidence.

- [x] **Step 48: Confirm trace-shape changes are documented in commit body**

Stage all the changes and inspect the diff before committing. The commit body needs to call out:

1. `RECALL_PERSISTED` reparents from `stage.recall_verify` → `query` root span.
2. Pipeline's `RecallDeps` split into `recall.RecallDeps` (4 fields) + `pipeline.PersistDeps` (3 fields).
3. `verify_and_persist_all` → `verify_all` + return shape `list[RawEntry]` → `list[VerifiedEntry]`.
4. `stages.compute_coverage_gap` re-exported from `stages.coverage_gap` (was `stages.llm_recall`).
5. `SOURCE_LLM_RECALL` promoted to `corpus.sources` public surface.

This is for the commit message only — no code change in this step.

Run: `git status && git diff --stat`
Expected: a coherent diff covering only the files in the **Files:** list at the top of this task.

- [x] **Step 49: Commit the refactor**

Per CLAUDE.md commit style — terse, no `Co-Authored-By`.

Run:
```
git add -A
git commit -m "$(cat <<'EOF'
extract recall package

- slopmortem.recall: recall(), VerifiedEntry, RecallDeps, RecallConfig,
  FakeRecaller, PriorCandidateHint, DeathnessConfig public; _verify/_brainstorm
  private
- stages.compute_coverage_gap moves to stages/coverage_gap.py
- pipeline: RecallDeps splits into recall.RecallDeps + pipeline.PersistDeps;
  _run_recall_branch composes recall() + gather_resilient(_persist_one)
- verify_and_persist_all → verify_all; persist callback dropped; returns
  list[VerifiedEntry]
- SOURCE_LLM_RECALL promoted to slopmortem.corpus.sources public surface
- RECALL_PERSISTED event now nests under the query root span (was
  stage.recall_verify) — counts/attributes unchanged
EOF
)"
```

Expected: commit succeeds; hooks pass.

---

## Behaviour invariants this plan preserves

These are load-bearing properties the spec calls out. Verify in the diff before claiming done:

| Property | Mechanism | Verified by |
|---|---|---|
| Persistence concurrency | `gather_resilient(*(_persist_one(v) for v in verified))` fans out the persist calls under `anyio.CapacityLimiter(3)` — matches today's verifier-side ceiling. `recall_max_suggestions_per_pitch=8` (default) would otherwise burst 8 Qdrant+journal+slop-classify ops onto the query critical path. | Step 23 code; Step 40 test |
| Per-suggestion persist isolation | `_persist_one` wraps in `try/except`, returns `bool`; one persist raising does not abort siblings | Step 23 code; Step 40 test |
| `persisted_count` counts what landed | `sum(1 for r in persist_results if r is True)` | Step 23 code |
| `recall_used = persisted_count > 0` | Mirrors today's `_RecallOutcome.used = bool(verified after persist)` | Step 23 code |
| `force_llm_recall=True` raises on missing deps | Both `recall_deps` and `persist_deps` checked at the gate; error message names the missing record (`"… RecallDeps not provided"` or `"… PersistDeps not provided"`) so operators can distinguish failure modes | Step 24 code; the original test at `test_pipeline_recall_raises_when_recall_deps_missing` (still matches `"… RecallDeps not provided"`) plus the new symmetric test from Step 32 (matches `"… PersistDeps not provided"`) |
| `RECALL_PERSISTED` reparents to `query` root | Persist runs pipeline-side; the `@observe` decorator on `stage.recall_verify` no longer wraps it | Step 23 code; intentional trace-shape change documented in commit body |
| `RECALL_SUGGESTIONS_RECEIVED` count semantics | Pipeline emits with `count=len(verified)` after `recall()` returns; today emits with `count=len(suggestions)` after brainstorm but pre-verify. **This is a counting change** — see "Note on `RECALL_SUGGESTIONS_RECEIVED`" below |
| Cassette `prompt_hash` stability | `prior_hints` is passed through, so the recall prompt's `current_top_n` block renders identically on the production path | Step 47 eval |
| L0 search inside `_one()` closure | `_search_for_evidence` stays inside the per-suggestion `gather_resilient` leaf | Step 9 code |

### Note on `RECALL_SUGGESTIONS_RECEIVED`

The original `pipeline._run_recall_branch:198-201` emits `RECALL_SUGGESTIONS_RECEIVED` with `count=len(suggestions)` *between* brainstorm and verify. The refactor moves brainstorm inside `recall()`, so the emission moves with it: Step 15's `recall()` body emits the event right after `llm_recall(...)` returns, preserving the event's pre-verify meaning (`count=len(suggestions)`, Opus-emitted brainstorm cardinality). The pipeline's `_run_recall_branch` no longer emits this event — Step 23 omits it.

**Parent span unchanged.** The event was previously emitted inside `_run_recall_branch`, which has no `@observe` wrapper of its own — it nests under `run_query`. After the move, the event fires inside `recall()`, which also has no `@observe` wrapper, so it nests under whatever called `recall()`, which is `_run_recall_branch` and ultimately `run_query` again. Same root span; same `count`. Dashboards keyed on either are unaffected.

Cleaner package boundary was the alternative (drop the event from recall, repurpose the pipeline-side emission to count `len(verified)`) but it would silently change the event's semantics — dashboards keyed on this event would see post-L5 survivor counts (~0-3) instead of pre-verify brainstorm counts (~8), violating the spec's "no behaviour change" mandate. The recall package already imports `lmnr` via the `@observe` decorators on `stage.llm_recall` and `stage.recall_verify`, so direct `Laminar.event(...)` use is not a new dependency.

## Risks (carried verbatim from spec, with mitigations cross-referenced)

| Risk | Severity | Where in this plan |
|---|---|---|
| Persistence concurrency regression | Med | Step 23 keeps `gather_resilient`; Step 40 test pins isolation |
| `RECALL_PERSISTED` reparenting | Med | Step 48 commit body documents the trace-shape change |
| Persist failure isolation lost | Med | Step 23 `_persist_one` `try/except`; Step 40 test |
| Eval cassette continuity | Low | Step 47 eval gate (necessary but not sufficient per spec) |
| Eval baseline parity overstated | Low | Step 46 `just test` is the load-bearing signal |
| `recall_used` / `persisted_count` semantics | Low | Step 23 explicit `sum(...) > 0` |
| Pipeline test churn | Med | Steps 32-33 enumerate the file list; Step 43 grep gate confirms no leftovers |
| Import-linter contract | Low | Step 41 adds the `recall-private` contract; Step 8 fixes the only pre-existing private reach |
| `force_llm_recall` guard wiring | Low | Step 24 checks both `recall_deps` and `persist_deps`; Step 32 adds the symmetric test |

## Out of scope (do not expand into during this task)

- Reworking the L0–L5 ladder
- Changing the coverage-gap predicate (inputs/outputs/event names stay identical)
- Eval cassette re-recording
- Exposing recall as a Typer subcommand
- Adding `suggestion_cap` / `max_tavily_calls` to global `Config` (they live only on `RecallConfig`)
- Promoting `ExtractFn` / `TavilySearchFn` to `slopmortem.recall.__init__` public surface (deferred until a test or user genuinely needs it)
