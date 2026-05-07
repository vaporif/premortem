"""Per-entry journal write ordering.

Load-bearing invariant (CLAUDE.md): mark_complete fires only after both Qdrant
and disk writes succeed. The fixed order is:

    1. journal.upsert_pending
    2. write_raw_atomic
    3. write_canonical_atomic
    4. corpus.delete_chunks_for_canonical
    5. _embed_and_upsert (Qdrant)
    6. journal.mark_complete
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING

from lmnr import Laminar

from slopmortem._time import utcnow_iso
from slopmortem.corpus import (
    CHUNK_STRATEGY_VERSION,
    Section,
    combined_hash,
    combined_text,
    resolve_entity,
    write_canonical_atomic,
    write_raw_atomic,
)
from slopmortem.ingest._fan_out import _embed_and_upsert
from slopmortem.ingest._helpers import (
    _build_payload,
    _reliability_for,
    _skip_key,
    _text_id_for,
)
from slopmortem.llm import prompt_template_sha
from slopmortem.tracing import SpanEvent

if TYPE_CHECKING:
    from pathlib import Path

    from slopmortem.config import Config
    from slopmortem.corpus import MergeJournal
    from slopmortem.ingest._fan_out import _FanoutResult
    from slopmortem.ingest._ports import Corpus, SparseEncoder
    from slopmortem.llm import EmbeddingClient, LLMClient
    from slopmortem.models import RawEntry

__all__ = ["ProcessOutcome", "_process_entry"]

logger = logging.getLogger(__name__)


class ProcessOutcome(StrEnum):
    PROCESSED = "processed"
    SKIPPED = "skipped"
    SKIPPED_EMPTY = "skipped_empty"
    FAILED = "failed"


async def _process_entry(  # noqa: PLR0913 - orchestration density is the contract
    entry: RawEntry,
    *,
    body: str,
    fan: _FanoutResult,
    journal: MergeJournal,
    corpus: Corpus,
    embed_client: EmbeddingClient,
    llm: LLMClient,
    config: Config,
    post_mortems_root: Path,
    slop_score: float,
    force: bool,
    span_events: list[str],
    sparse_encoder: SparseEncoder,
) -> ProcessOutcome:
    """Resolve, write, and journal one entry.

    SKIPPED_EMPTY: zero chunks → skip mark_complete rather than journalling
    a row with no Qdrant points. FAILED: a write raised (today only
    delete_chunks_for_canonical on a re-merge); abort before any upsert so we
    don't shadow prior orphans with a fresh layer.
    """
    name = entry.source_id  # ingest's name extraction is best-effort in v1
    sector = fan.facets.sector
    res = await resolve_entity(
        entry,
        journal=journal,
        embed_client=embed_client,
        name=name,
        sector=sector,
        founding_year=fan.facets.founding_year,
        llm_client=llm,
        haiku_model_id=config.model_facet,
        tiebreaker_max_tokens=config.max_tokens_tiebreaker,
    )
    span_events.extend(res.span_events)
    if res.action in ("alias_blocked", "resolver_flipped"):
        return ProcessOutcome.SKIPPED
    canonical_id = res.canonical_id

    # v1 ingest only knows about this raw section; multi-source merge happens
    # via the read-back path in reconcile.
    section = Section(
        text=body,
        reliability_rank=_reliability_for(entry.source),
        source_id=entry.source_id,
        source=entry.source,
    )
    merged = combined_text([section])
    content_hash = combined_hash([section])
    facet_sha = prompt_template_sha("facet_extract")

    skip_key = _skip_key(
        content_hash=content_hash,
        facet_sha=facet_sha,
        summarize_sha=prompt_template_sha("summarize"),
        haiku_model_id=config.model_facet,
        embed_model_id=config.embed_model_id,
        chunk_strategy=CHUNK_STRATEGY_VERSION,
        taxonomy_version=config.taxonomy_version,
        reliability_rank_version=config.reliability_rank_version,
    )

    existing = await journal.fetch_by_key(canonical_id, entry.source, entry.source_id)
    if not force and existing:
        row = existing[0]
        if row.get("merge_state") == "complete" and row.get("skip_key") == skip_key:
            return ProcessOutcome.SKIPPED

    await journal.upsert_pending(
        canonical_id=canonical_id, source=entry.source, source_id=entry.source_id
    )

    text_id = _text_id_for(canonical_id)
    await write_raw_atomic(
        post_mortems_root,
        text_id,
        entry.source,
        body,
        front_matter={
            "canonical_id": canonical_id,
            "source": entry.source,
            "source_id": entry.source_id,
            "content_hash": content_hash,
            "facet_prompt_hash": facet_sha,
            "embed_model_id": config.embed_model_id,
            "chunk_strategy_version": CHUNK_STRATEGY_VERSION,
            "taxonomy_version": config.taxonomy_version,
        },
    )

    await write_canonical_atomic(
        post_mortems_root,
        text_id,
        merged,
        front_matter={
            "canonical_id": canonical_id,
            "combined_hash": content_hash,
            "skip_key": skip_key,
            "merged_at": utcnow_iso(),
            "source_ids": [f"{entry.source}:{entry.source_id}"],
        },
    )

    # Wipe prior chunks before re-upserting so orphans from a longer prior body
    # don't leak their higher-index chunks into the merged result.
    if existing:
        try:
            await corpus.delete_chunks_for_canonical(canonical_id)
        except Exception as exc:  # noqa: BLE001 - qdrant-client raises many transport/auth/validation shapes; recovery is the same for all.
            # Abort the entry; reconcile picks up the drift on a later pass.
            Laminar.event(
                name=SpanEvent.INGEST_ENTRY_FAILED.value,
                attributes={
                    "canonical_id": canonical_id,
                    "stage": "delete_chunks",
                    "error": str(exc),
                },
            )
            logger.warning(
                "ingest aborted entry: delete_chunks_for_canonical failed for %s: %r",
                canonical_id,
                exc,
            )
            return ProcessOutcome.FAILED
    payload = _build_payload(
        facets=fan.facets,
        summary=fan.summary,
        body=merged,
        slop_score=slop_score,
        sources_seen=[entry.url] if entry.url else [],
        provenance_id=f"{entry.source}:{entry.source_id}",
        text_id=text_id,
        name=name,
        provenance=entry.source,
        synthesized=entry.synthesized,
    )
    chunks_written = await _embed_and_upsert(
        canonical_id=canonical_id,
        body=merged,
        payload=payload,
        corpus=corpus,
        embed_client=embed_client,
        embed_model_id=config.embed_model_id,
        sparse_encoder=sparse_encoder,
    )
    if chunks_written == 0:
        # A "complete" row with zero Qdrant points is silent corpus drift. Leave
        # the row pending and surface the empty chunk count; reconcile catches
        # this class of drift retroactively.
        Laminar.event(
            name=SpanEvent.INGEST_ENTRY_EMPTY_CHUNKS.value,
            attributes={"canonical_id": canonical_id},
        )
        logger.warning(
            "ingest skipped mark_complete: zero chunks for canonical_id=%s",
            canonical_id,
        )
        return ProcessOutcome.SKIPPED_EMPTY

    # mark_complete must run last: skip_key being set is the only signal a
    # later ingest uses to short-circuit this entry.
    await journal.mark_complete(
        canonical_id=canonical_id,
        source=entry.source,
        source_id=entry.source_id,
        skip_key=skip_key,
        merged_at=utcnow_iso(),
        content_hash=content_hash,
    )
    logger.info(
        "ingest: saved %s:%s → canonical=%s chunks=%d body=%d chars",
        entry.source,
        entry.source_id,
        canonical_id,
        chunks_written,
        len(merged),
    )
    return ProcessOutcome.PROCESSED
