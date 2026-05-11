"""End-to-end cassette recording.

Ephemeral Qdrant, recording wrappers, atomic dir swap, Tavily forced off.
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anyio
from anyio import to_thread
from openai import AsyncOpenAI

from slopmortem.budget import Budget
from slopmortem.concurrency import gather_resilient
from slopmortem.evals.qdrant_setup import setup_ephemeral_qdrant
from slopmortem.evals.recording import (
    RecordingEmbeddingClient,
    RecordingLLMClient,
    RecordingSparseEncoder,
)
from slopmortem.evals.recording_progress import (
    NullRecordProgress,
    RecordPhase,
    RecordProgress,
)
from slopmortem.llm import OpenRouterClient, make_embedder
from slopmortem.pipeline import QueryPhase, run_query

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from slopmortem.config import Config
    from slopmortem.llm import CompletionResult
    from slopmortem.models import InputContext


_DEFAULT_MAX_COST_USD = 2.0
_STALE_TMP_SECONDS = 24 * 3600
_DEFAULT_MAX_CONCURRENT_ROWS = 3
# FACET_EXTRACT + RETRIEVE + RERANK; ``N_synthesize`` more get added per row.
_FIXED_TICKS_PER_ROW = 3


@dataclass(frozen=True, slots=True)
class RecordResult:
    rows_total: int
    rows_succeeded: int
    cassettes_written: int
    total_cost_usd: float


def _sweep_stale_recording_dirs(root: Path, *, max_age_seconds: int) -> None:
    """Best-effort: remove ``*.recording`` dirs older than ``max_age_seconds``."""
    if not root.exists():
        return
    cutoff = time.time() - max_age_seconds
    for d in root.glob("**/*.recording"):
        try:
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _atomic_swap(*, tmp_dir: Path, real_dir: Path) -> None:
    """Two-step rename so SIGKILL always leaves ``real_dir`` or ``real_dir.old`` intact."""
    old = real_dir.parent / (real_dir.name + ".old")
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    if real_dir.exists():
        real_dir.replace(old)
    real_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir.replace(real_dir)
    shutil.rmtree(old, ignore_errors=True)


@contextmanager
def _tavily_off(config: Config) -> Generator[Config]:
    """Yield a ``Config`` with both Tavily features off.

    ``enable_tavily_recall_search`` is defensive: recording doesn't wire
    ``RecallDeps`` today, but flipping it stops a future edit from quietly
    threading recall through to live Tavily. Re-record with a
    ``RecordingTavilySearch`` wrapper when an eval row exercises recall.
    """
    yield config.model_copy(
        update={
            "enable_tavily_synthesis": False,
            "enable_tavily_recall_search": False,
        }
    )


async def record_cassettes_for_inputs(  # noqa: PLR0913, PLR0915 — entry point exposes each knob; per-row inline closure intentional
    *,
    inputs: list[InputContext],
    output_dir: Path,
    corpus_fixture_path: Path,
    config: Config,
    qdrant_url: str = "http://localhost:6333",
    max_cost_usd: float = _DEFAULT_MAX_COST_USD,
    progress: RecordProgress | None = None,
    max_concurrent_rows: int = _DEFAULT_MAX_CONCURRENT_ROWS,
) -> RecordResult:
    """Record cassettes for every input under ``output_dir/<scope>/``.

    Rows run under ``CapacityLimiter(max_concurrent_rows)``; the default keeps
    in-flight Sonnet calls under typical OpenRouter per-key limits. Failed
    rows clean their tmp dirs; the first failure re-raises after siblings
    settle. Tavily is forced off.
    """
    # Lazy: runner imports back into this module.
    from slopmortem.corpus import set_query_corpus  # noqa: PLC0415
    from slopmortem.evals.runner import (  # noqa: PLC0415
        _row_id,  # pyright: ignore[reportPrivateUsage]
    )

    await to_thread.run_sync(lambda: output_dir.mkdir(parents=True, exist_ok=True))
    _sweep_stale_recording_dirs(output_dir, max_age_seconds=_STALE_TMP_SECONDS)

    bar: RecordProgress = progress if progress is not None else NullRecordProgress()
    ticks_per_row = _FIXED_TICKS_PER_ROW + config.N_synthesize
    bar.start_phase(RecordPhase.ROWS, total=len(inputs) * ticks_per_row)
    running_cost = 0.0
    rows_succeeded = 0
    cassettes_written = 0

    def _on_cost(delta: float) -> None:
        nonlocal running_cost
        running_cost += delta

    with _tavily_off(config) as cfg:
        async with setup_ephemeral_qdrant(
            corpus_fixture_path,
            qdrant_url=qdrant_url,
        ) as corpus:
            set_query_corpus(corpus)
            # Shared Budget sized to worst-case run total so it never starves
            # a row; per-row caps live on each RecordingLLMClient.
            budget = Budget(
                cap_usd=max(
                    cfg.max_cost_usd_per_query,
                    max_cost_usd * (_FIXED_TICKS_PER_ROW) * len(inputs),
                )
            )
            openrouter_sdk = AsyncOpenAI(
                api_key=cfg.openrouter_api_key.get_secret_value(),
                base_url=cfg.openrouter_base_url,
            )
            llm = OpenRouterClient(
                sdk=openrouter_sdk,
                budget=budget,
                model=cfg.model_synthesize,
            )
            embedder = make_embedder(cfg, budget)

            from slopmortem.corpus._embed_sparse import (  # noqa: PLC0415
                encode as live_sparse,
            )

            limiter = anyio.CapacityLimiter(max_concurrent_rows)

            async def _record_row(ctx: InputContext) -> None:
                nonlocal cassettes_written, rows_succeeded
                async with limiter:
                    # Share dir naming with replay (runner._row_id) so
                    # anonymous inputs land in the same <sha1[:8]> slot.
                    scope_name = _row_id(ctx)
                    real_dir = output_dir / scope_name
                    tmp_dir = (
                        output_dir / f"{scope_name}.{os.getpid()}.{uuid.uuid4().hex}.recording"
                    )
                    tmp_dir.mkdir(parents=True, exist_ok=False)
                    bridge = _AggregateProgressBridge(bar, ticks_per_row=ticks_per_row)
                    try:
                        rec_llm_facet = RecordingLLMClient(
                            inner=llm,
                            out_dir=tmp_dir,
                            stage="facet_extract",
                            model=cfg.model_facet,
                            max_cost_usd=max_cost_usd,
                            on_cost=_on_cost,
                        )
                        rec_llm_rerank = RecordingLLMClient(
                            inner=llm,
                            out_dir=tmp_dir,
                            stage="llm_rerank",
                            model=cfg.model_rerank,
                            max_cost_usd=max_cost_usd,
                            on_cost=_on_cost,
                        )
                        rec_llm_synth = RecordingLLMClient(
                            inner=llm,
                            out_dir=tmp_dir,
                            stage="synthesize",
                            model=cfg.model_synthesize,
                            max_cost_usd=max_cost_usd,
                            on_cost=_on_cost,
                        )
                        routed_llm = _ByModelLLM(
                            {
                                cfg.model_facet: rec_llm_facet,
                                cfg.model_rerank: rec_llm_rerank,
                                cfg.model_synthesize: rec_llm_synth,
                            }
                        )
                        rec_embed = RecordingEmbeddingClient(
                            inner=embedder,
                            out_dir=tmp_dir,
                            on_cost=_on_cost,
                        )
                        rec_sparse = RecordingSparseEncoder(inner=live_sparse, out_dir=tmp_dir)

                        _ = await run_query(
                            ctx,
                            llm=routed_llm,
                            embedding_client=rec_embed,
                            corpus=corpus,
                            config=cfg,
                            budget=budget,
                            sparse_encoder=rec_sparse,
                            progress=bridge,
                        )
                    except Exception as exc:
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                        bar.error(RecordPhase.ROWS, f"{scope_name}: {exc!r}")
                        bridge.top_up()
                        raise
                    else:
                        _atomic_swap(tmp_dir=tmp_dir, real_dir=real_dir)
                        n_cassettes = len(list(real_dir.glob("*.json")))
                        cassettes_written += n_cassettes
                        rows_succeeded += 1
                        bar.log(f"✓ {scope_name} — {n_cassettes} cassettes, ${running_cost:.4f}")
                        bridge.top_up()

            results = await gather_resilient(*(_record_row(ctx) for ctx in inputs))
            for r in results:
                if isinstance(r, BaseException):
                    raise r
    bar.end_phase(RecordPhase.ROWS)
    return RecordResult(
        rows_total=len(inputs),
        rows_succeeded=rows_succeeded,
        cassettes_written=cassettes_written,
        total_cost_usd=running_cost,
    )


class _ByModelLLM:
    """LLM router: dispatch each ``complete()`` to the wrapper for the matching model."""

    def __init__(self, by_model: dict[str, RecordingLLMClient]) -> None:
        self._by_model = by_model

    async def complete(  # noqa: PLR0913 — mirrors LLMClient.complete signature
        self,
        prompt: str,
        *,
        system: str | None = None,
        tools: list[Any] | None = None,  # pyright: ignore[reportExplicitAny]
        model: str | None = None,
        cache: bool = False,
        response_format: dict[str, Any] | None = None,  # pyright: ignore[reportExplicitAny]
        extra_body: dict[str, Any] | None = None,  # pyright: ignore[reportExplicitAny]
        max_tokens: int | None = None,
        single_tool_call: bool = False,
    ) -> CompletionResult:
        if model is None or model not in self._by_model:
            msg = f"no recording wrapper for model={model!r}"
            raise KeyError(msg)
        return await self._by_model[model].complete(
            prompt,
            system=system,
            tools=tools,
            model=model,
            cache=cache,
            response_format=response_format,
            extra_body=extra_body,
            max_tokens=max_tokens,
            single_tool_call=single_tool_call,
        )


class _AggregateProgressBridge:
    """Funnel one row's ``QueryPhase`` advances into the shared ROWS bar.

    Sink total is pre-summed across rows so percentage tracks subtask
    completion. ``set_phase_status`` is a no-op (concurrent rows would race
    the status line). ``top_up`` settles rows that fire fewer SYNTHESIZE
    advances than budgeted.
    """

    def __init__(self, sink: RecordProgress, ticks_per_row: int) -> None:
        self._sink = sink
        self._ticks_per_row = ticks_per_row
        self._ticked = 0

    def start_phase(self, phase: QueryPhase, total: int) -> None:
        del phase, total

    def advance_phase(self, phase: QueryPhase, n: int = 1) -> None:
        del phase
        delta = min(n, self._ticks_per_row - self._ticked)
        if delta <= 0:
            return
        self._ticked += delta
        self._sink.advance_phase(RecordPhase.ROWS, delta)

    def end_phase(self, phase: QueryPhase) -> None:
        del phase

    def set_phase_status(self, phase: QueryPhase, status: str | None) -> None:
        del phase, status

    def log(self, message: str) -> None:
        self._sink.log(message)

    def error(self, phase: QueryPhase, message: str) -> None:
        del phase
        self._sink.error(RecordPhase.ROWS, message)

    def top_up(self) -> None:
        remaining = self._ticks_per_row - self._ticked
        if remaining > 0:
            self._sink.advance_phase(RecordPhase.ROWS, remaining)
            self._ticked = self._ticks_per_row
