"""Retrieve stage: embed and delegate to `Corpus.query`.

The hybrid-retrieval contract (FormulaQuery + RRF + recency-branch filter +
collapse-to-parents + alias-graph dedup) lives in
`slopmortem.corpus._qdrant_store.QdrantCorpus.query`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lmnr import Laminar, observe

if TYPE_CHECKING:
    from collections.abc import Callable

    from slopmortem.corpus import Corpus
    from slopmortem.llm import EmbeddingClient
    from slopmortem.models import Candidate, Facets

type SparseEncoder = Callable[[str], dict[int, float]]


# ``ignore_output=True`` drops the auto-captured Candidate output; a redacted
# ``(canonical_id, score, name, facets, slop_score)`` projection gets re-attached
# below. ``ignore_inputs=["corpus"]`` keeps the corpus handle out of the trace
# (test fakes inline their candidate set; production stores serialize the
# client handle, neither of which is useful trace content). Body bytes never
# cross the trace boundary.
@observe(name="stage.retrieve", ignore_output=True, ignore_inputs=["corpus"])
async def retrieve(  # noqa: PLR0913 — every dependency is required at the call site
    *,
    description: str,
    facets: Facets,
    corpus: Corpus,
    embedding_client: EmbeddingClient,
    cutoff_iso: str | None,
    strict_deaths: bool,
    k_retrieve: int,
    sparse_encoder: SparseEncoder | None = None,
    strict_sector_filter: bool = False,
    strict_sector_filter_excludes_other: bool = False,
) -> list[Candidate]:
    """Retrieve dense+sparse candidates for *description* from *corpus*.

    Both dense and sparse queries come from *description* verbatim — no HyDE
    expansion, rerank slack absorbs the modality gap.

    ``sparse_encoder=None`` lazy-loads the fastembed model on first call;
    tests pass a no-op stub to dodge the ~150 MB ONNX download.
    """
    if sparse_encoder is None:
        from slopmortem.corpus._embed_sparse import encode as _default_encode  # noqa: PLC0415

        sparse_encoder = _default_encode

    embed_result = await embedding_client.embed([description])
    [dense] = embed_result.vectors
    sparse = sparse_encoder(description)
    candidates = await corpus.query(
        dense=dense,
        sparse=sparse,
        facets=facets,
        cutoff_iso=cutoff_iso,
        strict_deaths=strict_deaths,
        k_retrieve=k_retrieve,
        strict_sector_filter=strict_sector_filter,
        strict_sector_filter_excludes_other=strict_sector_filter_excludes_other,
    )
    Laminar.set_span_attributes(
        {
            "candidates": [
                {
                    "canonical_id": c.canonical_id,
                    "score": c.score,
                    "name": c.payload.name,
                    "facets": c.payload.facets.model_dump(),
                    "slop_score": c.payload.slop_score,
                }
                for c in candidates
            ],
        }
    )
    return candidates
