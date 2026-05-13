"""Route a verified ``RawEntry`` through the existing ingest tail.

The verifier hands off entries that passed L1-L4 with ``markdown_text``
populated. ``persist_recall_entry`` runs them through the same crawler tail
(slop classify → facet+summarize → entity-resolve → journal/disk/qdrant
write), preserving CLAUDE.md's invariant: ``mark_complete`` only fires
after both Qdrant and disk writes succeed.

The three phase helpers are lifted onto ``slopmortem.ingest``'s public
surface for this one extra caller; import-linter forbids reaching into
``ingest._*`` from sibling packages. Don't add a write path that bypasses
the journal.

``verification_tier`` and ``deathness_verdict`` ride through ``write_phase``
→ ``_process_entry`` → ``_build_payload`` → ``CandidatePayload`` into the
qdrant payload via ``model_dump``. No side-channel payload merge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from slopmortem.ingest import classify_phase, facet_summarize_fanout, write_phase

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Literal

    from slopmortem.config import Config
    from slopmortem.corpus import MergeJournal
    from slopmortem.ingest import (
        Corpus,
        IngestProgress,
        IngestResult,
        SlopClassifier,
        SparseEncoder,
    )
    from slopmortem.llm import EmbeddingClient, LLMClient
    from slopmortem.models import RawEntry
    from slopmortem.recall import VerificationTier

__all__ = ["persist_recall_entry"]


async def persist_recall_entry(  # noqa: PLR0913 - mirrors the ingest tail's dep set
    entry: RawEntry,
    tier: VerificationTier,
    *,
    deathness_verdict: Literal["dead", "struggling"] | None = None,
    journal: MergeJournal,
    corpus: Corpus,
    embed_client: EmbeddingClient,
    llm: LLMClient,
    slop_classifier: SlopClassifier,
    sparse_encoder: SparseEncoder,
    config: Config,
    post_mortems_root: Path,
    progress: IngestProgress,
    result: IngestResult,
) -> None:
    """Persist one verified recall ``entry`` through the ingest tail.

    Idempotency comes from ``classify_phase``'s
    ``journal.is_terminal(entry.source, entry.source_id)`` check. The verifier
    keys ``source_id`` on ``(name, homepage_url)`` so re-verifying the same
    vendor produces no second journal row. A tier upgrade
    (``evidence_only`` → ``wayback_anchored``) is *not* propagated here;
    tier upgrades need an explicit re-write tool, out of scope.

    ``enrichers=()`` because the verifier already filled ``markdown_text``;
    re-enriching would clobber it with a second extraction pass.
    """
    keepers = await classify_phase(
        [entry],
        enrichers=(),
        slop_classifier=slop_classifier,
        journal=journal,
        config=config,
        post_mortems_root=post_mortems_root,
        dry_run=False,
        force=False,
        progress=progress,
        result=result,
        # L5 deathness is the stricter gate; slop tuned on a different body shape.
        skip_slop=True,
    )
    if not keepers:
        # Quarantined by slop classifier, duplicate per the journal, or empty body.
        return
    fanout = await facet_summarize_fanout(keepers, llm=llm, config=config, progress=progress)
    await write_phase(
        keepers,
        fanout,
        journal=journal,
        corpus=corpus,
        embed_client=embed_client,
        llm=llm,
        config=config,
        post_mortems_root=post_mortems_root,
        force=False,
        sparse_encoder=sparse_encoder,
        progress=progress,
        result=result,
        verification_tier=tier,
        deathness_verdict=deathness_verdict,
    )
