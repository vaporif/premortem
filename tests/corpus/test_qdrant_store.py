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


def _make_chunk_with_sector(
    canonical_id: str,
    idx: int,
    sector: str,
    *,
    source: str | None = None,
    dense_value: float | None = None,
) -> _Point:
    """Like ``_make_chunk`` but with a full CandidatePayload — ``query`` validates payloads.

    ``source`` lands in the Qdrant payload as a plain key (CandidatePayload
    ignores extras), so tests can hit source-keyed filters before the field
    is part of the model. ``dense_value`` overrides the per-idx vector when a
    test needs a fixed embedding (e.g. score-parity assertions).
    """
    raw = (idx + 1) * 0.001 if dense_value is None else dense_value
    dense = [float(raw)] * _DIM
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
    if source is not None:
        payload["source"] = source
    return _Point(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"{canonical_id}:{idx}").hex,
        vector={"dense": dense, "sparse": sparse},
        payload=payload,
    )


async def _build_corpus(
    qdrant_client: AsyncQdrantClient,
    tmp_path: Path,
    name: str,
    *,
    facet_boost: float = 0.01,
    recall_score_factor: float = 1.0,
) -> QdrantCorpus:
    if await qdrant_client.collection_exists(name):
        await qdrant_client.delete_collection(name)
    await ensure_collection(qdrant_client, name, dim=_DIM)
    return QdrantCorpus(
        client=qdrant_client,
        collection=name,
        post_mortems_root=tmp_path,
        facet_boost=facet_boost,
        recall_score_factor=recall_score_factor,
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
async def test_llm_recall_score_downweighted(
    qdrant_client: AsyncQdrantClient, tmp_path: Path
) -> None:
    """An llm_recall entry's score scales by ``recall_score_factor`` vs the same query at 1.0."""
    name = "test_recall_downweight"
    factor = 0.5  # exaggerated to keep assertion robust against RRF rounding
    # ``facet_boost=0.0`` isolates the recall term: with the additive boost
    # the observed ratio would be ``(score x factor + boost) / (score + boost)``
    # rather than ``factor``.
    baseline_corpus = await _build_corpus(
        qdrant_client, tmp_path, name, facet_boost=0.0, recall_score_factor=1.0
    )
    try:
        await baseline_corpus.upsert_chunk(
            _make_chunk_with_sector("c:recall", 0, "fintech", source="llm_recall", dense_value=0.5)
        )
        kwargs: dict[str, object] = {
            "dense": [0.5] * _DIM,
            "sparse": {0: 1.0},
            "facets": _facets_with_sector("fintech"),
            "cutoff_iso": None,
            "strict_deaths": False,
            "k_retrieve": 5,
        }
        baseline = await baseline_corpus.query(**kwargs)  # pyright: ignore[reportArgumentType]
        baseline_score = next(c.score for c in baseline if c.canonical_id == "c:recall")
    finally:
        await qdrant_client.delete_collection(name)

    demoted_corpus = await _build_corpus(
        qdrant_client, tmp_path, name, facet_boost=0.0, recall_score_factor=factor
    )
    try:
        await demoted_corpus.upsert_chunk(
            _make_chunk_with_sector("c:recall", 0, "fintech", source="llm_recall", dense_value=0.5)
        )
        demoted = await demoted_corpus.query(**kwargs)  # pyright: ignore[reportArgumentType]
        demoted_score = next(c.score for c in demoted if c.canonical_id == "c:recall")
    finally:
        await qdrant_client.delete_collection(name)

    assert demoted_score == pytest.approx(baseline_score * factor, rel=0.01)


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


class _CapturingResp:
    def __init__(self) -> None:
        self.points: list[object] = []


class _CapturingClient:
    """Records the FormulaQuery passed to ``query_points`` and returns no hits."""

    def __init__(self) -> None:
        self.last_query: object = None

    async def query_points(self, **kwargs: object) -> _CapturingResp:
        self.last_query = kwargs.get("query")
        return _CapturingResp()


def _formula_terms(query: object) -> list[object]:
    # ``FormulaQuery.formula`` is a ``SumExpression``; its ``sum`` field holds
    # the per-term mix that ``QdrantCorpus.query`` builds.
    sum_expr = query.formula  # pyright: ignore[reportAttributeAccessIssue]
    return list(sum_expr.sum)


async def test_recall_score_factor_one_is_neutral(tmp_path: Path) -> None:
    """At factor=1.0 the FormulaQuery has no recall-demote term — same shape as before."""
    client = _CapturingClient()
    corpus = QdrantCorpus(
        client=client,  # pyright: ignore[reportArgumentType]
        collection="x",
        post_mortems_root=tmp_path,
        recall_score_factor=1.0,
    )
    await corpus.query(
        dense=[0.0] * _DIM,
        sparse={0: 1.0},
        facets=_facets_with_sector("fintech"),
        cutoff_iso=None,
        strict_deaths=False,
        k_retrieve=5,
    )
    terms = _formula_terms(client.last_query)
    # Pre-change shape: ``["$score", MultExpression(facet boost)]`` — no recall demote.
    assert terms[0] == "$score"
    assert len(terms) == 2


async def test_recall_score_factor_below_one_appends_demote_term(tmp_path: Path) -> None:
    """At factor<1.0 a third term references ``$score`` and the source filter."""
    from qdrant_client.models import FieldCondition, MultExpression  # noqa: PLC0415

    client = _CapturingClient()
    corpus = QdrantCorpus(
        client=client,  # pyright: ignore[reportArgumentType]
        collection="x",
        post_mortems_root=tmp_path,
        recall_score_factor=0.5,
    )
    await corpus.query(
        dense=[0.0] * _DIM,
        sparse={0: 1.0},
        facets=_facets_with_sector("fintech"),
        cutoff_iso=None,
        strict_deaths=False,
        k_retrieve=5,
    )
    terms = _formula_terms(client.last_query)
    assert len(terms) == 3
    demote = terms[2]
    assert isinstance(demote, MultExpression)
    assert "$score" in demote.mult
    assert pytest.approx(-0.5) in demote.mult  # factor - 1.0
    assert any(isinstance(m, FieldCondition) and m.key == "source" for m in demote.mult)
