# pyright: reportAny=false
"""Top-level ``ingest()`` orchestration.

Pipeline overview: ``docs/architecture.md``; load-bearing invariants: CLAUDE.md.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import anyio
from lmnr import Laminar, observe

from slopmortem.concurrency import gather_resilient
from slopmortem.corpus.sources._names import SOURCE_LLM_RECALL
from slopmortem.ingest._fan_out import (
    _facet_summarize_fanout,
    _FanoutResult,
)
from slopmortem.ingest._helpers import (
    _enrich_pipeline,
    _entry_summary_text,
    _gather_entries,
)
from slopmortem.ingest._journal_writes import (
    ProcessOutcome,
    _process_entry,
)
from slopmortem.ingest._ports import (
    _MAX_RECORDED_ERRORS,  # pyright: ignore[reportPrivateUsage]  -- intra-package private boundary
    IngestPhase,
    IngestResult,
    NullProgress,
)
from slopmortem.ingest._slop_gate import (
    _effective_slop_threshold,
    _quarantine,
    classify_one,
)
from slopmortem.ingest._warm_cache import cache_read_ratio_event, cache_warm
from slopmortem.tracing import SpanEvent, git_sha, mint_run_id

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from typing import Literal

    from slopmortem.budget import Budget
    from slopmortem.config import Config
    from slopmortem.corpus import MergeJournal
    from slopmortem.corpus.sources import Enricher, Source
    from slopmortem.ingest._ports import (
        Corpus,
        IngestProgress,
        SlopClassifier,
        SparseEncoder,
    )
    from slopmortem.llm import EmbeddingClient, LLMClient
    from slopmortem.models import RawEntry

__all__ = ["_classify_phase", "_write_phase", "ingest"]

logger = logging.getLogger(__name__)

# Lower bound on borderline-event emission. Scores under 0.4 trip too readily
# on clean docs (TechCrunch obits, Wayback marketing copy, founder blogs are
# all routinely <0.3) and would drown the trace in non-actionable noise. The
# tunable region for ``recall_slop_threshold`` sits between 0.4 and 0.85.
_BORDERLINE_LOWER = 0.4


def _emit_collected_events(result: IngestResult) -> None:
    if not Laminar.is_initialized():
        return
    for name in result.span_events:
        Laminar.event(name=name)


def _record_error(result: IngestResult, entry_label: str, exc: BaseException) -> None:
    """Record per-entry failures on the parent ingest span.

    Without this, swallowed per-entry exceptions only show up in stderr; the
    parent ingest span returns OK and ``INGEST_ENTRY_FAILED`` carries no payload.
    """
    if not Laminar.is_initialized():
        return
    idx = result.errors
    if idx >= _MAX_RECORDED_ERRORS:
        Laminar.set_span_attributes({"errors.truncated_count": idx - _MAX_RECORDED_ERRORS + 1})
        return
    Laminar.set_span_attributes(
        {
            f"errors.{idx}.entry": entry_label,
            f"errors.{idx}.exception_type": type(exc).__name__,
            f"errors.{idx}.message": str(exc)[:500],
        }
    )


def _record_entry_failure(  # noqa: PLR0913 - one seam, every dep
    *,
    result: IngestResult,
    progress: IngestProgress,
    phase: IngestPhase,
    entry: RawEntry,
    exc: BaseException,
    message: str,
) -> None:
    """Record an entry-level failure.

    Caller still owns ``progress.advance_phase`` and ``continue``.
    """
    label = f"{entry.source}:{entry.source_id}"
    logger.warning("ingest: %s", message)
    progress.error(phase, message)
    _record_error(result, label, exc)
    result.errors += 1
    result.span_events.append(SpanEvent.INGEST_ENTRY_FAILED.value)


async def _classify_phase(  # noqa: PLR0913, C901 - one phase, every dep + branch at this seam
    entries: Sequence[RawEntry],
    *,
    enrichers: Sequence[Enricher],
    slop_classifier: SlopClassifier,
    journal: MergeJournal,
    config: Config,
    post_mortems_root: Path,
    dry_run: bool,
    force: bool,
    progress: IngestProgress,
    result: IngestResult,
    skip_slop: bool = False,
) -> list[tuple[RawEntry, str]]:
    """Enrich → extract body → slop-gate; return survivors as ``(entry, body)``.

    Per-entry failures log and continue (only the run-level orchestrator can
    short-circuit). Mutates ``result`` counters and ``result.span_events``;
    ``gather_resilient`` preserves input order so survivors line up with
    ``entries``.
    """
    progress.start_phase(IngestPhase.CLASSIFY, total=len(entries))
    limiter = anyio.CapacityLimiter(config.ingest_concurrency)

    async def _one(entry: RawEntry) -> tuple[RawEntry, str] | None:
        async with limiter:
            if not force and await journal.is_terminal(entry.source, entry.source_id):
                logger.info(
                    "ingest: skipped %s:%s — already processed (use --force to re-run)",
                    entry.source,
                    entry.source_id,
                )
                result.skipped += 1
                progress.advance_phase(IngestPhase.CLASSIFY)
                return None
            result.seen += 1
            try:
                enriched = await _enrich_pipeline(entry, enrichers)
            except Exception as exc:  # noqa: BLE001 - per-entry isolation.
                _record_entry_failure(
                    result=result,
                    progress=progress,
                    phase=IngestPhase.CLASSIFY,
                    entry=entry,
                    exc=exc,
                    message=f"enricher failed for {entry.source_id}: {exc!r}",
                )
                progress.advance_phase(IngestPhase.CLASSIFY)
                return None

            if enriched.title_pre_filter_rejected:
                logger.info(
                    "ingest: title pre-filter rejected %s:%s title=%r",
                    entry.source,
                    entry.source_id,
                    entry.title,
                )
                result.skipped += 1
                progress.advance_phase(IngestPhase.CLASSIFY)
                return None

            body = _entry_summary_text(enriched, max_tokens=config.max_doc_tokens)
            if not body:
                logger.info(
                    "ingest: skipped %s:%s — empty body url=%s",
                    entry.source,
                    entry.source_id,
                    entry.url,
                )
                result.skipped += 1
                progress.advance_phase(IngestPhase.CLASSIFY)
                return None

            if skip_slop:
                # L5 deathness is the stricter gate for recall entries; slop
                # was tuned on a different body shape (raw crawler output, no
                # combined Wayback + news citation), so running it here risks
                # false-quarantining L5-verified rows.
                slop_score = 0.0
            else:
                slop_score = await classify_one(
                    entry=enriched,
                    body=body,
                    slop_classifier=slop_classifier,
                    on_error=lambda exc: progress.error(
                        IngestPhase.CLASSIFY, f"slop classifier failed: {exc}"
                    ),
                )

            effective_threshold = _effective_slop_threshold(enriched, config)
            # Borderline band only fires for llm_recall — that's the source the
            # override knob targets, and the score-vs-threshold gap is the data
            # we need to retune it.
            if (
                enriched.source == SOURCE_LLM_RECALL
                and _BORDERLINE_LOWER <= slop_score < effective_threshold
                and Laminar.is_initialized()
            ):
                Laminar.event(
                    name=SpanEvent.RECALL_SLOP_BORDERLINE.value,
                    attributes={
                        "slop_score": f"{slop_score:.3f}",
                        "effective_threshold": f"{effective_threshold:.3f}",
                    },
                )

            if slop_score > effective_threshold:
                if not dry_run:
                    await _quarantine(
                        journal=journal,
                        entry=enriched,
                        body=body,
                        slop_score=slop_score,
                        post_mortems_root=post_mortems_root,
                    )
                logger.info(
                    "ingest: quarantined %s:%s slop=%.2f body=%d chars url=%s",
                    entry.source,
                    entry.source_id,
                    slop_score,
                    len(body),
                    entry.url,
                )
                result.quarantined += 1
                result.span_events.append(SpanEvent.SLOP_QUARANTINED.value)
                progress.advance_phase(IngestPhase.CLASSIFY)
                return None

            preview = body[:160].replace("\n", " ")
            logger.info(
                "ingest: kept %s:%s slop=%.2f body=%d chars url=%s preview=%r",
                entry.source,
                entry.source_id,
                slop_score,
                len(body),
                entry.url,
                preview,
            )
            progress.advance_phase(IngestPhase.CLASSIFY)
            return enriched, body

    classified = await gather_resilient(*(_one(e) for e in entries))
    keepers: list[tuple[RawEntry, str]] = []
    for entry, item in zip(entries, classified, strict=True):
        if isinstance(item, Exception):
            # ``_one`` already routes Exceptions through ``_record_entry_failure``
            # for known per-entry shapes. Anything reaching here is unexpected
            # (e.g. ``_quarantine`` disk/journal write blew up) — log it but
            # don't abort the run, and don't double-count ``errors``.
            logger.warning(
                "ingest classify: unhandled exception for %s:%s: %r",
                entry.source,
                entry.source_id,
                item,
            )
            continue
        if item is not None:
            keepers.append(item)

    progress.end_phase(IngestPhase.CLASSIFY)
    return keepers


async def _write_phase(  # noqa: PLR0913 - one phase, every dep at this seam
    keepers: Sequence[tuple[RawEntry, str]],
    fanout: Sequence[_FanoutResult | Exception],
    *,
    journal: MergeJournal,
    corpus: Corpus,
    embed_client: EmbeddingClient,
    llm: LLMClient,
    config: Config,
    post_mortems_root: Path,
    force: bool,
    sparse_encoder: SparseEncoder,
    progress: IngestProgress,
    result: IngestResult,
    verification_tier: Literal["wayback_anchored", "evidence_only"] | None = None,
    deathness_verdict: Literal["dead", "struggling"] | None = None,
) -> None:
    """Per-entry: resolve → journal → disk → qdrant → mark_complete.

    Per-entry failures isolate; ``BaseException`` (cancel/system-exit)
    re-raises after surfacing which entry triggered it.
    """
    progress.start_phase(IngestPhase.WRITE, total=len(keepers))
    for (entry, body), fan in zip(keepers, fanout, strict=True):
        if isinstance(fan, Exception):
            # Phase=FAN_OUT because that's where the failure happened, even
            # though we surface it during the WRITE loop.
            _record_entry_failure(
                result=result,
                progress=progress,
                phase=IngestPhase.FAN_OUT,
                entry=entry,
                exc=fan,
                message=f"fan-out failed for {entry.source}:{entry.source_id}: {fan!r}",
            )
            progress.advance_phase(IngestPhase.WRITE)
            continue
        try:
            outcome = await _process_entry(
                entry,
                body=body,
                fan=fan,
                journal=journal,
                corpus=corpus,
                embed_client=embed_client,
                llm=llm,
                config=config,
                post_mortems_root=post_mortems_root,
                slop_score=0.0,  # already slop-filtered upstream
                force=force,
                span_events=result.span_events,
                sparse_encoder=sparse_encoder,
                verification_tier=verification_tier,
                deathness_verdict=deathness_verdict,
            )
        except Exception as exc:  # noqa: BLE001 - per-entry isolation; run continues.
            _record_entry_failure(
                result=result,
                progress=progress,
                phase=IngestPhase.WRITE,
                entry=entry,
                exc=exc,
                message=f"write phase failed for {entry.source}:{entry.source_id}: {exc!r}",
            )
            progress.advance_phase(IngestPhase.WRITE)
            continue
        except BaseException as exc:
            # CancelledError / SystemExit slip past ``except Exception``. Surface
            # which entry and what type via progress before re-raising so the
            # run terminates loud rather than silent.
            progress.error(
                IngestPhase.WRITE,
                f"FATAL {type(exc).__name__} on {entry.source}:{entry.source_id}: {exc}",
            )
            raise
        match outcome:
            case ProcessOutcome.PROCESSED:
                result.processed += 1
            case ProcessOutcome.SKIPPED:
                result.skipped += 1
            case ProcessOutcome.SKIPPED_EMPTY:
                result.skipped_empty += 1
            case ProcessOutcome.FAILED:
                # ``_process_entry`` already logged the underlying cause at
                # warning level (e.g. delete_chunks transport error). Surface
                # the entry on progress + trace at parity with the exception
                # path; the synthetic exc is a placeholder so the helper's
                # contract stays narrow.
                _record_entry_failure(
                    result=result,
                    progress=progress,
                    phase=IngestPhase.WRITE,
                    entry=entry,
                    exc=RuntimeError("process_entry returned FAILED; see prior warning"),
                    message=(
                        f"write phase failed for {entry.source}:{entry.source_id} (FAILED outcome)"
                    ),
                )
                result.failed += 1
        progress.advance_phase(IngestPhase.WRITE)
    progress.end_phase(IngestPhase.WRITE)


@observe(
    name="ingest",
    ignore_inputs=[
        "sources",
        "enrichers",
        "journal",
        "corpus",
        "llm",
        "embed_client",
        "budget",
        "slop_classifier",
        "sparse_encoder",
        "progress",
    ],
)
async def ingest(  # noqa: PLR0913 - orchestration takes every dependency.
    *,
    sources: Sequence[Source],
    enrichers: Sequence[Enricher],
    journal: MergeJournal,
    corpus: Corpus,
    llm: LLMClient,
    embed_client: EmbeddingClient,
    budget: Budget,  # noqa: ARG001 - consumed by LLM/embed clients at construction time
    slop_classifier: SlopClassifier,
    config: Config,
    post_mortems_root: Path,
    dry_run: bool = False,
    force: bool = False,
    sparse_encoder: SparseEncoder | None = None,
    limit: int | None = None,
    progress: IngestProgress | None = None,
) -> IngestResult:
    """Per-entry/source failures log and continue; only budget exhaustion truncates the run.

    ``sparse_encoder=None`` lazy-loads the fastembed model; tests pass a no-op
    stub to dodge the ~150 MB ONNX download.
    """
    result = IngestResult(dry_run=dry_run)
    progress = progress or NullProgress()

    if Laminar.is_initialized():
        Laminar.set_span_attributes(
            {
                "run.id": mint_run_id(),
                "run.kind": "ingest",
                "run.git_sha": git_sha() or "",
                "run.dry_run": dry_run,
                "run.force": force,
                "run.limit": limit if limit is not None else 0,
                "config.taxonomy_version": config.taxonomy_version,
                "config.reliability_rank_version": config.reliability_rank_version,
                "config.slop_threshold": config.slop_threshold,
                "config.model_facet": config.model_facet,
                "config.model_summarize": config.model_summarize,
            }
        )

    if sparse_encoder is None:
        from slopmortem.corpus._embed_sparse import encode as _encode_sparse  # noqa: PLC0415

        sparse_encoder = _encode_sparse

    # ``total=None`` when --limit isn't set: count isn't known up front, so
    # let the bar pulse instead of lying with 0.
    progress.start_phase(IngestPhase.GATHER, total=limit)
    entries, source_failures = await _gather_entries(
        sources, span_events=result.span_events, limit=limit, progress=progress
    )
    progress.end_phase(IngestPhase.GATHER)
    progress.log(f"gathered {len(entries)} entries from {len(sources)} sources")
    result.source_failures = source_failures

    keepers = await _classify_phase(
        entries,
        enrichers=enrichers,
        slop_classifier=slop_classifier,
        journal=journal,
        config=config,
        post_mortems_root=post_mortems_root,
        dry_run=dry_run,
        force=force,
        progress=progress,
        result=result,
    )
    quarantined = result.quarantined
    skipped = result.skipped
    progress.log(f"classified: {len(keepers)} kept, {quarantined} quarantined, {skipped} skipped")

    if dry_run:
        result.would_process = len(keepers)
        _emit_collected_events(result)
        return result

    if not keepers:
        _emit_collected_events(result)
        return result

    progress.start_phase(IngestPhase.CACHE_WARM, total=1)
    warmed, warm_creation, warm_events = await cache_warm(
        llm=llm,
        model=config.model_summarize,
        seed_text=keepers[0][1][:1000],
        max_tokens=config.max_tokens_summarize,
    )
    progress.advance_phase(IngestPhase.CACHE_WARM)
    progress.end_phase(IngestPhase.CACHE_WARM)
    result.cache_warmed = warmed
    result.cache_creation_tokens_warm = warm_creation
    result.span_events.extend(warm_events)

    progress.start_phase(IngestPhase.FAN_OUT, total=len(keepers))
    fanout = await _facet_summarize_fanout(keepers, llm=llm, config=config, progress=progress)
    progress.end_phase(IngestPhase.FAN_OUT)

    ratio_event = cache_read_ratio_event(
        [r for r in fanout if isinstance(r, _FanoutResult)],
        threshold=config.cache_read_ratio_threshold,
        probe_n=config.cache_read_ratio_probe_n,
    )
    if ratio_event:
        result.span_events.append(ratio_event)

    await _write_phase(
        keepers,
        fanout,
        journal=journal,
        corpus=corpus,
        embed_client=embed_client,
        llm=llm,
        config=config,
        post_mortems_root=post_mortems_root,
        force=force,
        sparse_encoder=sparse_encoder,
        progress=progress,
        result=result,
    )

    _emit_collected_events(result)
    return result
