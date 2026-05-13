"""Pipeline orchestration. All side-effecting deps injected; CLI wires them up.

``BudgetExceededError`` truncates the run and returns a partial ``Report``
with ``budget_exceeded=True``. Per-candidate synthesis failures don't abort;
``synthesize_all`` returns them as exception entries which we drop before
populating ``Report.candidates``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

import anyio
from dateutil.relativedelta import relativedelta
from lmnr import Laminar, observe

from slopmortem.budget import BudgetExceededError
from slopmortem.concurrency import gather_resilient
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

if TYPE_CHECKING:
    from pathlib import Path

    from slopmortem.budget import Budget
    from slopmortem.config import Config
    from slopmortem.corpus import Corpus, MergeJournal
    from slopmortem.ingest import Corpus as IngestCorpus
    from slopmortem.ingest import SlopClassifier
    from slopmortem.llm import EmbeddingClient, LLMClient
    from slopmortem.models import (
        Candidate,
        Facets,
        InputContext,
        LlmRerankResult,
    )
    from slopmortem.stages import SparseEncoder


logger = logging.getLogger(__name__)


class QueryPhase(StrEnum):
    """Phase keys used by `QueryProgress`."""

    FACET_EXTRACT = "facet_extract"
    RETRIEVE = "retrieve"
    RERANK = "rerank"
    RECALL = "recall"
    SYNTHESIZE = "synthesize"


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


@runtime_checkable
class QueryProgress(Protocol):
    """Phase-level progress hooks for ``slopmortem query``.

    ``NullQueryProgress`` keeps the orchestrator UI-library-free; the CLI
    wires a Rich implementation.
    """

    def start_phase(self, phase: QueryPhase, total: int) -> None: ...
    def advance_phase(self, phase: QueryPhase, n: int = 1) -> None: ...
    def end_phase(self, phase: QueryPhase) -> None: ...
    def set_phase_status(self, phase: QueryPhase, status: str | None) -> None: ...
    def log(self, message: str) -> None: ...
    def error(self, phase: QueryPhase, message: str) -> None: ...


class NullQueryProgress:
    """No-op `QueryProgress` for when no display surface is attached."""

    def start_phase(self, phase: QueryPhase, total: int) -> None: ...
    def advance_phase(self, phase: QueryPhase, n: int = 1) -> None: ...
    def end_phase(self, phase: QueryPhase) -> None: ...
    def set_phase_status(self, phase: QueryPhase, status: str | None) -> None: ...
    def log(self, message: str) -> None: ...
    def error(self, phase: QueryPhase, message: str) -> None: ...


def cutoff_iso(years_filter: int | None) -> str | None:
    """Compute the ISO date cutoff for ``years_filter``.

    Floor to ``date()``: retrieve takes ``YYYY-MM-DD``, not timestamps.
    """
    if years_filter is None:
        return None
    return (datetime.now(UTC) - relativedelta(years=years_filter)).date().isoformat()


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


def _current_trace_id(*, enable_tracing: bool) -> str | None:
    # Gated on enable_tracing because @observe can still mint an OTel trace id
    # via TracerWrapper after Laminar.shutdown(), leaking a trace_id into runs
    # the user never asked to trace.
    if not enable_tracing or not Laminar.is_initialized():
        return None
    tid = Laminar.get_trace_id()
    return str(tid) if tid is not None else None


@dataclass(frozen=True)
class _RecallOutcome:
    retrieved: list[Candidate]
    reranked: LlmRerankResult
    persisted_count: int
    used: bool


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
    # Pre-extraction the OLD verify_and_persist_all released the verify limiter
    # before awaiting persist, so up to `recall_max_suggestions_per_pitch` (8)
    # Qdrant upserts + journal writes + slop-classify LLM hops fanned out
    # unbounded. CapacityLimiter(3) caps that fan-out on the query critical path;
    # matches the verify-side ceiling so the persist tail can't blow past it.
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


@observe(
    name="query",
    ignore_inputs=["llm", "embedding_client", "corpus", "budget", "progress"],
)
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
    """Run the query pipeline end-to-end and assemble the ``Report``.

    Per-candidate synthesis exceptions drop silently. ``BudgetExceededError``
    truncates the run and surfaces as ``pipeline_meta.budget_exceeded=True``.

    Provide ``recall_deps`` so the coverage-gap predicate can fire. Without
    them, predicate-driven recall logs and no-ops (test path);
    ``force_llm_recall=True`` raises ``RuntimeError`` so operator opt-in
    surfaces misconfig.
    """
    t0 = time.monotonic()
    successes: list[Synthesis] = []
    top_risks = TopRisks()
    budget_exceeded = False
    filtered_pre_synth = 0
    filtered_post_synth = 0
    coverage_gap = False
    recall_used = False
    recall_persisted_count = 0

    if Laminar.is_initialized():
        Laminar.set_span_attributes(
            {
                "run.id": mint_run_id(),
                "run.kind": "query",
                "run.git_sha": git_sha() or "",
                "config.taxonomy_version": config.taxonomy_version,
                "config.K_retrieve": config.K_retrieve,
                "config.N_synthesize": config.N_synthesize,
                "config.min_similarity_score": config.min_similarity_score,
                "config.min_similarity_score_after_recall": (
                    config.min_similarity_score_after_recall
                ),
                "config.strict_deaths": config.strict_deaths,
                "config.model_facet": config.model_facet,
                "config.model_rerank": config.model_rerank,
                "config.model_synthesize": config.model_synthesize,
            }
        )

    progress = progress if progress is not None else NullQueryProgress()
    try:
        progress.start_phase(QueryPhase.FACET_EXTRACT, total=1)
        facets = await extract_facets(
            input_ctx.description,
            llm,
            model=config.model_facet,
            max_tokens=config.max_tokens_facet,
        )
        progress.advance_phase(QueryPhase.FACET_EXTRACT)
        progress.end_phase(QueryPhase.FACET_EXTRACT)

        progress.start_phase(QueryPhase.RETRIEVE, total=1)
        cutoff = cutoff_iso(input_ctx.years_filter)
        retrieved = await retrieve(
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
        progress.advance_phase(QueryPhase.RETRIEVE)
        progress.end_phase(QueryPhase.RETRIEVE)

        progress.start_phase(QueryPhase.RERANK, total=1)
        reranked = await llm_rerank(
            retrieved,
            input_ctx.description,
            facets,
            llm,
            config,
            model=config.model_rerank,
            max_tokens=config.max_tokens_rerank,
        )
        progress.advance_phase(QueryPhase.RERANK)
        progress.end_phase(QueryPhase.RERANK)

        gap_result = compute_coverage_gap(
            retrieved=retrieved,
            ranked=reranked.ranked,
            pitch_sector=facets.sector,
            min_similarity_score=config.min_similarity_score,
            n_synthesize=config.N_synthesize,
        )
        coverage_gap = gap_result.gap

        # GAP_SCORE fires every query so eval can sweep predicate thresholds
        # against historical traces. GATE_FIRED stays predicate-driven only.
        # Attributes are stringified for OTLP.
        if Laminar.is_initialized():
            Laminar.event(
                name=str(SpanEvent.RECALL_GAP_SCORE),
                attributes={
                    "qualifying": str(gap_result.qualifying),
                    "required": str(gap_result.required),
                    "pitch_sector": facets.sector,
                },
            )

        # OR-combined: predicate-driven OR force-on. ``force_llm_recall`` lets
        # operators recording cassettes or running eval calibration fire recall
        # on every query regardless of the predicate.
        should_fire = coverage_gap or config.force_llm_recall

        if coverage_gap and Laminar.is_initialized():
            Laminar.event(name=str(SpanEvent.RECALL_GATE_FIRED))

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

        # When recall persisted >=1 entry, the corpus is by definition thin for
        # this pitch — the corpus-normal floor would re-filter out the very
        # entries we just verified and persisted. Lower bar trades fidelity
        # for a non-empty report; verifier already vetted the candidates.
        synthesis_threshold = (
            config.min_similarity_score_after_recall
            if recall_persisted_count > 0
            else config.min_similarity_score
        )
        top_n, filtered_pre_synth = select_top_n_by_similarity(
            retrieved=retrieved,
            ranked=reranked.ranked,
            min_similarity=synthesis_threshold,
            n_synthesize=config.N_synthesize,
        )

        progress.start_phase(QueryPhase.SYNTHESIZE, total=len(top_n))
        # First synthesize call runs alone to warm Anthropic's prompt cache before
        # the fan-out (see synthesize_all). Surface it on the bar so users don't
        # read the 0/N as "stuck".
        progress.set_phase_status(QueryPhase.SYNTHESIZE, "warming prompt cache")
        warmup_cleared = False

        def _on_candidate_done(exc: BaseException | None) -> None:
            nonlocal warmup_cleared
            if not warmup_cleared:
                progress.set_phase_status(QueryPhase.SYNTHESIZE, None)
                warmup_cleared = True
            if exc is not None:
                progress.error(QueryPhase.SYNTHESIZE, f"{type(exc).__name__}: {exc}")
            progress.advance_phase(QueryPhase.SYNTHESIZE)

        synth_results = await synthesize_all(
            top_n,
            input_ctx,
            llm,
            config,
            model=config.model_synthesize,
            max_tokens=config.max_tokens_synthesize,
            on_candidate_done=_on_candidate_done,
        )
        successes = [s for s in synth_results if isinstance(s, Synthesis)]
        # Mirror the pre-synth floor: when recall persisted, the synthesizer's
        # re-scored similarity is judged against the same lowered bar the
        # rerank-side filter used. Otherwise the recall branch pays for
        # synthesis on candidates it already vetted only for the post-synth
        # drop to re-apply the corpus-normal floor and discard them.
        successes, filtered_post_synth = drop_below_min_similarity(
            successes, min_similarity=synthesis_threshold
        )
        # Inside the try so a budget-exceeded run falls through to the default
        # empty TopRisks instead of consolidating a partial set.
        top_risks = await consolidate_risks(
            successes,
            pitch=input_ctx.description,
            llm=llm,
            config=config,
            model=config.model_consolidate,
            max_tokens=config.max_tokens_consolidate,
        )
        progress.end_phase(QueryPhase.SYNTHESIZE)
    except BudgetExceededError:
        budget_exceeded = True
        if Laminar.is_initialized():
            Laminar.event(name=str(SpanEvent.BUDGET_EXCEEDED))

    return Report(
        input=input_ctx,
        generated_at=datetime.now(UTC),
        candidates=successes,
        top_risks=top_risks,
        pipeline_meta=PipelineMeta(
            K_retrieve=config.K_retrieve,
            N_synthesize=config.N_synthesize,
            min_similarity_score=config.min_similarity_score,
            models={
                "facet": config.model_facet,
                "rerank": config.model_rerank,
                "synthesize": config.model_synthesize,
            },
            cost_usd_total=budget.spent_usd,
            latency_ms_total=int((time.monotonic() - t0) * 1000),
            trace_id=_current_trace_id(enable_tracing=config.enable_tracing),
            budget_remaining_usd=budget.remaining,
            budget_exceeded=budget_exceeded,
            filtered_pre_synth=filtered_pre_synth,
            filtered_post_synth=filtered_post_synth,
            coverage_gap=coverage_gap,
            recall_used=recall_used,
            recall_persisted_count=recall_persisted_count,
        ),
    )
