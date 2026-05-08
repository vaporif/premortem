"""Pipeline orchestration. All side-effecting deps injected; CLI wires them up.

``BudgetExceededError`` truncates the run and returns a partial
`Report` with ``budget_exceeded=True``. Per-candidate synthesis
failures don't abort — ``synthesize_all`` returns them as exception entries
which we drop before populating ``Report.candidates``.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from dateutil.relativedelta import relativedelta
from lmnr import Laminar, observe

from slopmortem.budget import BudgetExceededError
from slopmortem.models import PipelineMeta, Report, Synthesis, TopRisks
from slopmortem.stages import (
    consolidate_risks,
    drop_below_min_similarity,
    extract_facets,
    llm_rerank,
    retrieve,
    select_top_n_by_similarity,
    synthesize_all,
)
from slopmortem.tracing import SpanEvent, git_sha, mint_run_id

if TYPE_CHECKING:
    from slopmortem.budget import Budget
    from slopmortem.config import Config
    from slopmortem.corpus import Corpus
    from slopmortem.llm import EmbeddingClient, LLMClient
    from slopmortem.models import InputContext
    from slopmortem.stages import SparseEncoder


class QueryPhase(StrEnum):
    """Phase keys used by `QueryProgress`."""

    FACET_EXTRACT = "facet_extract"
    RETRIEVE = "retrieve"
    RERANK = "rerank"
    SYNTHESIZE = "synthesize"


@runtime_checkable
class QueryProgress(Protocol):
    """Phase-level progress hooks for ``slopmortem query``.

    The default `NullQueryProgress` keeps the orchestrator decoupled
    from any UI library; the CLI wires a Rich implementation.
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
    """Compute the ISO date cutoff for *years_filter*.

    Floor to ``date()`` keeps the cutoff stable across the query's hour;
    retrieve takes dates (``YYYY-MM-DD``), not timestamps.
    """
    if years_filter is None:
        return None
    return (datetime.now(UTC) - relativedelta(years=years_filter)).date().isoformat()


def _current_trace_id(*, enable_tracing: bool) -> str | None:
    # Gated on enable_tracing because @observe can still mint an OTel trace id
    # via TracerWrapper after Laminar.shutdown(), leaking a trace_id into runs
    # the user never asked to trace.
    if not enable_tracing or not Laminar.is_initialized():
        return None
    tid = Laminar.get_trace_id()
    return str(tid) if tid is not None else None


@observe(
    name="query",
    ignore_inputs=["llm", "embedding_client", "corpus", "budget", "progress"],
)
async def run_query(  # noqa: PLR0913 - every dep is required wiring at the call site
    input_ctx: InputContext,
    *,
    llm: LLMClient,
    embedding_client: EmbeddingClient,
    corpus: Corpus,
    config: Config,
    budget: Budget,
    progress: QueryProgress | None = None,
    sparse_encoder: SparseEncoder | None = None,
) -> Report:
    """Run the query pipeline end-to-end and assemble the `Report`.

    Per-candidate synthesis exceptions are dropped silently; ``BudgetExceededError``
    truncates the run and surfaces as ``pipeline_meta.budget_exceeded=True``.
    """
    t0 = time.monotonic()
    successes: list[Synthesis] = []
    top_risks = TopRisks()
    budget_exceeded = False
    filtered_pre_synth = 0
    filtered_post_synth = 0

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

        top_n, filtered_pre_synth = select_top_n_by_similarity(
            retrieved=retrieved,
            ranked=reranked.ranked,
            min_similarity=config.min_similarity_score,
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
        successes, filtered_post_synth = drop_below_min_similarity(
            successes, min_similarity=config.min_similarity_score
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
        ),
    )
