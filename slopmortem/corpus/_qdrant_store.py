# pyright: reportAny=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportIndexIssue=false, reportOptionalSubscript=false
"""Qdrant-backed corpus store: collection bootstrap, read methods, and chunk upsert.

Vendor-SDK boundary; per-file pyright silences match
``slopmortem/llm/openai_embeddings.py``.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

import anyio
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchAny,
    MatchValue,
    Modifier,
    SparseIndexParams,
    SparseVectorParams,
    VectorParams,
)

from slopmortem.corpus._alias_graph import collapse_alias_components
from slopmortem.corpus._disk import read_canonical
from slopmortem.corpus._paths import safe_path
from slopmortem.models import Candidate, CandidatePayload

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from qdrant_client import AsyncQdrantClient

    from slopmortem.models import AliasEdge, Facets


async def ensure_collection(client: AsyncQdrantClient, name: str, *, dim: int) -> None:
    """Create a hybrid (dense + sparse) collection or verify existing dim matches.

    On dim mismatch the recovery is to drop ``data/qdrant/`` and re-ingest.
    """
    if await client.collection_exists(name):
        info = await client.get_collection(name)
        existing = info.config.params.vectors["dense"].size
        if existing != dim:
            msg = (
                f"dim mismatch: collection {name!r} has dim={existing} but config "
                f"wants dim={dim}. Drop data/qdrant/ and re-ingest."
            )
            raise ValueError(msg)
        return
    await client.create_collection(
        collection_name=name,
        vectors_config={"dense": VectorParams(size=dim, distance=Distance.COSINE)},
        sparse_vectors_config={
            "sparse": SparseVectorParams(
                index=SparseIndexParams(on_disk=False),
                modifier=Modifier.IDF,
            )
        },
    )


async def _gather_alias_edges(
    candidates: list[Candidate],
    fetch: Callable[[str], Awaitable[list[AliasEdge]]],
) -> list[AliasEdge]:
    edge_lists: list[list[AliasEdge]] = [[] for _ in candidates]

    async def _collect(idx: int, cid: str) -> None:
        edge_lists[idx] = await fetch(cid)

    async with anyio.create_task_group() as tg:
        for i, c in enumerate(candidates):
            tg.start_soon(_collect, i, c.canonical_id)

    return [e for sub in edge_lists for e in sub]


class QdrantCorpus:
    """Live ``Corpus`` impl: vectors + small payload in Qdrant, full canonical body on disk."""

    def __init__(  # noqa: PLR0913 — orchestration knobs are public construction surface
        self,
        *,
        client: AsyncQdrantClient,
        collection: str,
        post_mortems_root: Path,
        facet_boost: float = 0.01,
        rrf_k: int = 60,
        recall_score_factor: float = 1.0,
        fetch_aliases: Callable[[str], Awaitable[list[AliasEdge]]] | None = None,
    ) -> None:
        # ``facet_boost=0.01`` lifts ~0.04 max for a 4-facet match against an
        # RRF ceiling of ~0.033. ``rrf_k=60`` matches Qdrant's server default.
        # ``recall_score_factor=1.0`` is a no-op default so existing callers
        # (and tests) keep their pre-change scoring; production wires the
        # config value through ``build_deps``. ``fetch_aliases=None`` no-ops
        # the alias-graph dedup pass for tests that don't seed an aliases table.
        self._client = client
        self._collection = collection
        self._root = post_mortems_root
        self._facet_boost = facet_boost
        self._rrf_k = rrf_k
        self._recall_score_factor = recall_score_factor
        self._fetch_aliases = fetch_aliases

    async def query(  # noqa: PLR0913, C901 — Protocol method signature; one branch per orchestration knob
        self,
        *,
        dense: list[float],
        sparse: dict[int, float],
        facets: Facets,
        cutoff_iso: str | None,
        strict_deaths: bool,
        k_retrieve: int,
        strict_sector_filter: bool = False,
        strict_sector_filter_excludes_other: bool = False,
    ) -> list[Candidate]:
        """Hybrid retrieve top-K candidates with FormulaQuery facet boost.

        Inner ``Prefetch`` does dense+sparse RRF fusion; outer ``FormulaQuery``
        adds a per-facet boost on top of ``$score`` (``"other"`` values skip).
        Over-fetches chunks at ``k_retrieve * 4`` so the in-Python parent
        collapse + alias-component dedup have room before truncating.
        ``strict_deaths=True`` narrows the recency filter to "known failure_date
        >= cutoff_iso" (branch A only).
        """
        from qdrant_client.models import (  # noqa: PLC0415 — keep top-level imports lean
            FieldCondition,
            Filter,
            FormulaQuery,
            Fusion,
            FusionQuery,
            MatchValue,
            MultExpression,
            Prefetch,
            SparseVector,
            SumExpression,
        )

        inner = Prefetch(
            prefetch=[
                Prefetch(query=dense, using="dense", limit=k_retrieve * 2),
                Prefetch(
                    query=SparseVector(
                        indices=list(sparse),
                        values=list(sparse.values()),
                    ),
                    using="sparse",
                    limit=k_retrieve * 2,
                ),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            limit=k_retrieve * 2,
        )

        # Only closed-taxonomy facets participate in the boost; free-form
        # fields and ``"other"`` values stay out.
        boost_must: list[Any] = []  # pyright: ignore[reportExplicitAny]
        for fname in ("sector", "business_model", "customer_type", "geography", "monetization"):
            val = getattr(facets, fname)
            if val == "other":
                continue
            boost_must.append(FieldCondition(key=f"facets.{fname}", match=MatchValue(value=val)))

        # An empty Filter matches every doc (1.0 on every candidate), so drop
        # the Mult term when no facets boost — otherwise every score inflates
        # uniformly.
        formula_terms: list[Any] = ["$score"]  # pyright: ignore[reportExplicitAny]
        if boost_must:
            formula_terms.append(MultExpression(mult=[self._facet_boost, Filter(must=boost_must)]))
        # Soft demote on llm_recall: the term resolves to ``$score x (factor-1)``
        # when source matches and to 0 otherwise, so the outer Sum yields
        # ``$score x factor`` for recall rows and ``$score`` for everything else.
        # Skipped at factor=1.0 to keep the formula identical to the pre-change
        # shape for callers that opt out.
        if self._recall_score_factor < 1.0:
            formula_terms.append(
                MultExpression(
                    mult=[
                        "$score",
                        self._recall_score_factor - 1.0,
                        FieldCondition(key="source", match=MatchValue(value="llm_recall")),
                    ]
                )
            )
        formula = FormulaQuery(formula=SumExpression(sum=formula_terms))

        query_filter = _and_filters(
            _build_recency_filter(cutoff_iso=cutoff_iso, strict_deaths=strict_deaths),
            _build_sector_filter(
                sector=facets.sector,
                strict=strict_sector_filter,
                exclude_other=strict_sector_filter_excludes_other,
            ),
        )

        # TODO(prod): chunk over-fetch may under-fill long post-mortems (#25).
        # ``k_retrieve * 4`` assumes ~4 chunks per parent on average. Long
        # post-mortems can chunk into many more, silently under-filling the
        # parent set after collapse. Measure ``len(best)`` vs ``k_retrieve``
        # against a real corpus and bump the multiplier (or switch to a
        # parent-aware fetcher) before going live.
        resp = await self._client.query_points(
            collection_name=self._collection,
            prefetch=inner,
            query=formula,
            query_filter=query_filter,
            limit=k_retrieve * 4,
            with_payload=True,
            with_vectors=False,
        )

        # Qdrant's RRF+FormulaQuery doesn't promise stable order, and that
        # non-determinism would leak into the rerank prompt hash and into
        # which chunk's payload wins per parent.
        points = sorted(
            resp.points,
            key=lambda h: (
                -float(h.score),
                str((h.payload or {}).get("canonical_id", "")),
                int((h.payload or {}).get("chunk_idx", 0)),
            ),
        )
        best: dict[str, tuple[float, dict[str, Any]]] = {}  # pyright: ignore[reportExplicitAny]
        for hit in points:
            payload = dict(hit.payload or {})
            cid = str(payload.get("canonical_id", ""))
            if not cid:
                continue
            score = float(hit.score)
            if cid not in best or score > best[cid][0]:
                best[cid] = (score, payload)

        # TODO(perf): cache pydantic-validated payloads if hot (#26). Keyed on
        # (canonical_id, chunk_idx); payloads are immutable post-ingest so the
        # cache is safe. Skip until measurements justify it.
        ordered = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)
        candidates: list[Candidate] = []
        for cid, (score, payload) in ordered:
            try:
                cp = _payload_dict_to_candidate_payload(payload)
            except Exception as exc:  # noqa: BLE001 — per-doc isolation; we log and continue
                logger.warning("qdrant_query: dropped malformed payload for %r: %r", cid, exc)
                continue
            candidates.append(Candidate(canonical_id=cid, score=score, payload=cp))

        # Known limitation: only fetches edges for canonicals in the top-K, so
        # alias chains longer than one hop where a mid-chain node was pruned
        # upstream stay un-collapsed. See README "Known limitations".
        if self._fetch_aliases is not None and candidates:
            edges = await _gather_alias_edges(candidates, self._fetch_aliases)
            candidates = collapse_alias_components(candidates, edges)

        return candidates[:k_retrieve]

    async def get_post_mortem(self, canonical_id: str) -> str:
        text_id = hashlib.sha256(canonical_id.encode("utf-8")).hexdigest()[:16]
        return read_canonical(self._root, text_id)

    async def search_corpus(
        self,
        q: str,
        facets: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:  # pyright: ignore[reportExplicitAny]  # Protocol surface
        """Scroll-based search; v1 lacks full-text relevance (lands with FormulaQuery in #7)."""
        from qdrant_client.models import (  # noqa: PLC0415 — keep import surface lean
            FieldCondition,
            Filter,
            MatchText,
            MatchValue,
        )

        must: list[Any] = [  # pyright: ignore[reportExplicitAny]
            FieldCondition(key="body", match=MatchText(text=q)),
        ]
        if facets:
            must.extend(
                FieldCondition(key=f"facets.{key}", match=MatchValue(value=value))
                for key, value in facets.items()
            )
        flt = Filter(must=must)
        records, _next = await self._client.scroll(
            collection_name=self._collection,
            scroll_filter=flt,
            limit=10,
            with_payload=True,
            with_vectors=False,
        )
        out: list[dict[str, Any]] = []  # pyright: ignore[reportExplicitAny]
        for rec in records:
            payload = dict(rec.payload or {})
            payload["_point_id"] = rec.id
            out.append(payload)
        return out

    async def has_chunks(self, canonical_id: str) -> bool:
        records, _ = await self._client.scroll(
            collection_name=self._collection,
            scroll_filter=Filter(
                must=[
                    FieldCondition(
                        key="canonical_id",
                        match=MatchValue(value=canonical_id),
                    )
                ]
            ),
            limit=1,
            with_payload=False,
            with_vectors=False,
        )
        return bool(records)

    async def delete_chunks_for_canonical(self, canonical_id: str) -> None:
        """Idempotent. Transport/auth failures propagate to the caller."""
        selector = FilterSelector(
            filter=Filter(
                must=[
                    FieldCondition(
                        key="canonical_id",
                        match=MatchValue(value=canonical_id),
                    )
                ]
            )
        )
        await self._client.delete(
            collection_name=self._collection,
            points_selector=selector,
        )

    async def upsert_chunk(self, point: Any) -> None:  # pyright: ignore[reportExplicitAny]
        # qdrant-client's PointsList rejects arbitrary dataclasses, so build a
        # real PointStruct from the ingest._Point.
        from qdrant_client.models import PointStruct, SparseVector  # noqa: PLC0415

        sparse = point.vector["sparse"]
        struct = PointStruct(
            id=point.id,
            vector={
                "dense": point.vector["dense"],
                "sparse": SparseVector(
                    indices=list(sparse),
                    values=list(sparse.values()),
                ),
            },
            payload=point.payload,
        )
        await self._client.upsert(
            collection_name=self._collection,
            points=[struct],
        )


def canonical_path_for(post_mortems_root: Path, canonical_id: str) -> Path:
    text_id = hashlib.sha256(canonical_id.encode("utf-8")).hexdigest()[:16]
    return safe_path(post_mortems_root, kind="canonical", text_id=text_id)


def _and_filters(*filters: Filter | None) -> Filter | None:
    """AND-combine zero or more optional ``Filter``s, dropping ``None``s.

    Nests (``Filter(must=[a, b])``) instead of merging ``must`` lists because
    ``_build_recency_filter`` returns ``Filter(should=[…])`` when
    ``strict_deaths=False`` — its ``must`` is ``None`` and clause-merging
    would silently drop the ``should`` branches. The outer ``must=[…]``
    wrapper AND-combines correctly regardless of clause shape.
    """
    present = [f for f in filters if f is not None]
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    return Filter(must=list(present))


def _build_sector_filter(*, sector: str, strict: bool, exclude_other: bool) -> Filter | None:
    """Hard payload filter on ``facets.sector``. ``None`` = no narrowing.

    ``strict=False`` is the legacy path: sector only contributes as a soft
    boost, so this returns ``None``. ``strict=True`` pins the pitch's sector
    at retrieve time. ``exclude_other=True`` drops the ``"other"`` safety
    valve — only set it after auditing the corpus's ``"other"`` bucket and
    reclassifying misclassifications.

    Returns ``None`` when ``pitch.sector == "other"`` regardless of flags:
    the pitch sector is uninformative, and filtering on ``"other"`` would
    either match misclassification noise or (with ``exclude_other=True``)
    return nothing.
    """
    if not strict or sector == "other":
        return None

    if exclude_other:
        cond = FieldCondition(key="facets.sector", match=MatchValue(value=sector))
    else:
        cond = FieldCondition(key="facets.sector", match=MatchAny(any=[sector, "other"]))
    return Filter(must=[cond])


def _build_recency_filter(*, cutoff_iso: str | None, strict_deaths: bool) -> Any:  # pyright: ignore[reportExplicitAny]
    # Branches A/B/C use derived ``failure_date_unknown`` /
    # ``founding_date_unknown`` boolean payloads instead of IsNullCondition
    # (qdrant#5148, documented slow under indexed payloads).
    if cutoff_iso is None:
        return None

    from qdrant_client.models import (  # noqa: PLC0415
        DatetimeRange,
        FieldCondition,
        Filter,
        MatchValue,
    )

    # ``DatetimeRange.gte``'s stub is strict ``datetime | date | None`` even
    # though runtime accepts ISO strings — parse once to keep the rest narrow.
    cutoff_dt = datetime.fromisoformat(cutoff_iso)

    branch_a_must: list[Any] = [  # pyright: ignore[reportExplicitAny]
        FieldCondition(key="failure_date_unknown", match=MatchValue(value=False)),
        FieldCondition(key="failure_date", range=DatetimeRange(gte=cutoff_dt)),
    ]
    if strict_deaths:
        return Filter(must=branch_a_must)

    branch_b_must: list[Any] = [  # pyright: ignore[reportExplicitAny]
        FieldCondition(key="failure_date_unknown", match=MatchValue(value=True)),
        FieldCondition(key="founding_date_unknown", match=MatchValue(value=False)),
        FieldCondition(key="founding_date", range=DatetimeRange(gte=cutoff_dt)),
    ]
    branch_c_must: list[Any] = [  # pyright: ignore[reportExplicitAny]
        FieldCondition(key="failure_date_unknown", match=MatchValue(value=True)),
        FieldCondition(key="founding_date_unknown", match=MatchValue(value=True)),
    ]
    return Filter(
        should=[
            Filter(must=branch_a_must),
            Filter(must=branch_b_must),
            Filter(must=branch_c_must),
        ]
    )


def _payload_dict_to_candidate_payload(payload: dict[str, Any]) -> CandidatePayload:  # pyright: ignore[reportExplicitAny]
    """Drop Qdrant-only keys so ``extra="forbid"`` won't trip if the model later turns strict.

    Strips ``canonical_id`` and ``chunk_idx``.
    """
    cleaned = {k: v for k, v in payload.items() if k not in ("canonical_id", "chunk_idx")}
    return CandidatePayload.model_validate(cleaned)
