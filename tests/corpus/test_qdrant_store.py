"""Live-Qdrant tests for ``QdrantCorpus.delete_chunks_for_canonical``."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from slopmortem.corpus import QdrantCorpus, ensure_collection
from slopmortem.ingest import _Point
from slopmortem.llm import EMBED_DIMS
from slopmortem.models import Facets

if TYPE_CHECKING:
    from pathlib import Path

    from qdrant_client import AsyncQdrantClient

_DIM = EMBED_DIMS["text-embedding-3-small"]


def _make_chunk(canonical_id: str, idx: int) -> _Point:
    # Distinct dense vectors per chunk so Qdrant indexes them as separate
    # points; the sparse half is required by the hybrid collection schema.
    dense = [float((idx + 1) * 0.001)] * _DIM
    sparse: dict[int, float] = {idx: 1.0}
    return _Point(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"{canonical_id}:{idx}").hex,
        vector={"dense": dense, "sparse": sparse},
        payload={"canonical_id": canonical_id, "chunk_idx": idx},
    )


def _facets_with_sector(sector: str) -> Facets:
    """Build ``Facets`` with the closed-set taxonomy values from ``taxonomy.yml``."""
    return Facets(
        sector=sector,
        business_model="b2b_saas",
        customer_type="enterprise",
        geography="us",
        monetization="subscription_recurring",
    )


def _make_chunk_with_sector(canonical_id: str, idx: int, sector: str) -> _Point:
    """Like ``_make_chunk`` but with a full CandidatePayload — ``query`` validates payloads."""
    dense = [float((idx + 1) * 0.001)] * _DIM
    sparse: dict[int, float] = {idx: 1.0}
    payload: dict[str, object] = {
        "canonical_id": canonical_id,
        "chunk_idx": idx,
        "name": f"chunk-{canonical_id}",
        "summary": "fixture",
        "body": "fixture body",
        "facets": _facets_with_sector(sector).model_dump(),
        "founding_date": None,
        "failure_date": None,
        "founding_date_unknown": True,
        "failure_date_unknown": True,
        "provenance": "curated_real",
        "slop_score": 0.0,
        "sources": [],
        "provenance_id": "",
        "text_id": canonical_id.replace(":", "_"),
    }
    return _Point(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"{canonical_id}:{idx}").hex,
        vector={"dense": dense, "sparse": sparse},
        payload=payload,
    )


async def _build_corpus(
    qdrant_client: AsyncQdrantClient,
    tmp_path: Path,
    name: str,
) -> QdrantCorpus:
    if await qdrant_client.collection_exists(name):
        await qdrant_client.delete_collection(name)
    await ensure_collection(qdrant_client, name, dim=_DIM)
    return QdrantCorpus(
        client=qdrant_client,
        collection=name,
        post_mortems_root=tmp_path,
    )


@pytest.mark.requires_qdrant
async def test_delete_chunks_for_canonical_removes_matching_points(
    qdrant_client: AsyncQdrantClient, tmp_path: Path
) -> None:
    name = "test_delete_chunks_match"
    corpus = await _build_corpus(qdrant_client, tmp_path, name)
    try:
        canonical_id = "test:abc123"
        other = "test:other"
        for idx in range(3):
            await corpus.upsert_chunk(_make_chunk(canonical_id, idx))
        await corpus.upsert_chunk(_make_chunk(other, 0))

        await corpus.delete_chunks_for_canonical(canonical_id)

        # No `get_chunks` accessor exists; verify via scroll + filter.
        from qdrant_client.http.models import (  # noqa: PLC0415
            FieldCondition,
            Filter,
            MatchValue,
        )

        matched, _ = await qdrant_client.scroll(
            collection_name=name,
            scroll_filter=Filter(
                must=[FieldCondition(key="canonical_id", match=MatchValue(value=canonical_id))]
            ),
            limit=10,
        )
        assert matched == []
        other_matched, _ = await qdrant_client.scroll(
            collection_name=name,
            scroll_filter=Filter(
                must=[FieldCondition(key="canonical_id", match=MatchValue(value=other))]
            ),
            limit=10,
        )
        assert len(other_matched) == 1
    finally:
        await qdrant_client.delete_collection(name)


@pytest.mark.requires_qdrant
async def test_delete_chunks_idempotent_when_no_points(
    qdrant_client: AsyncQdrantClient, tmp_path: Path
) -> None:
    name = "test_delete_chunks_idempotent"
    corpus = await _build_corpus(qdrant_client, tmp_path, name)
    try:
        # Must not raise even though no points exist for this canonical_id.
        await corpus.delete_chunks_for_canonical("nonexistent:id")
    finally:
        await qdrant_client.delete_collection(name)


@pytest.mark.requires_qdrant
@pytest.mark.parametrize(
    ("excludes_other", "name", "expected"),
    [
        (False, "test_strict_sector_default", {"crypto_web3", "other"}),
        (True, "test_strict_sector_exclude_other", {"crypto_web3"}),
    ],
    ids=["keeps_other", "excludes_other"],
)
async def test_strict_sector_filter(
    qdrant_client: AsyncQdrantClient,
    tmp_path: Path,
    excludes_other: bool,  # noqa: FBT001 — pytest parametrize positional
    name: str,
    expected: set[str],
) -> None:
    corpus = await _build_corpus(qdrant_client, tmp_path, name)
    try:
        await corpus.upsert_chunk(_make_chunk_with_sector("c:web3", 0, "crypto_web3"))
        await corpus.upsert_chunk(_make_chunk_with_sector("c:other", 1, "other"))
        await corpus.upsert_chunk(_make_chunk_with_sector("c:fin", 2, "fintech"))

        candidates = await corpus.query(
            dense=[0.001] * _DIM,
            sparse={0: 1.0},
            facets=_facets_with_sector("crypto_web3"),
            cutoff_iso=None,
            strict_deaths=False,
            k_retrieve=10,
            strict_sector_filter=True,
            strict_sector_filter_excludes_other=excludes_other,
        )
        sectors = {c.payload.facets.sector for c in candidates}
        assert sectors == expected
    finally:
        await qdrant_client.delete_collection(name)
