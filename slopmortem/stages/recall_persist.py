"""Route a verified ``RawEntry`` through the existing ingest tail.

The verifier (``stages.recall_verify``) hands off entries that passed L1-L4
gating with ``markdown_text`` already populated. ``persist_recall_entry``
runs them through the same tail crawler entries take — slop classify →
facet+summarize → entity-resolve → journal/disk/qdrant write — so the
load-bearing invariant from CLAUDE.md still holds: ``mark_complete`` only
fires after both Qdrant and disk writes succeed.

The three phase helpers (``classify_phase`` / ``facet_summarize_fanout`` /
``write_phase``) are lifted onto ``slopmortem.ingest``'s public surface for
this one extra caller — the import-linter contract forbids reaching into
``ingest._*`` from sibling packages. Don't introduce a new write path that
bypasses the journal.

``verification_tier`` rides through ``write_phase`` → ``_process_entry`` →
``_build_payload`` → ``CandidatePayload.verification_tier`` and lands in the
qdrant payload via ``model_dump``. ``deathness_verdict`` rides the same
chain alongside it. No side-channel payload merge.
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
    from slopmortem.stages.recall_verify import VerificationTier

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

    Idempotency is delegated to ``classify_phase``: it short-circuits via
    ``journal.is_terminal(entry.source, entry.source_id)`` when the source
    id has already been written. The verifier keys ``source_id`` on
    ``(name, homepage_url)`` so re-verifying the same vendor produces no
    second journal row or qdrant point. A tier upgrade
    (``evidence_only`` → ``wayback_anchored``) is *not* propagated by this
    path — that's intentional; tier upgrades require an explicit re-write
    tool, which is out of scope here.

    ``enrichers=()`` because the verifier already filled ``markdown_text``
    from the evidence body (or a Wayback snapshot when L4 corroborated).
    Re-enriching would clobber that with a second extraction pass.
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
