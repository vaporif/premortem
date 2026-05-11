"""Pre-flight: prove a persisted recall entry's vector survives top-K retrieve.

Gates the rest of the recall-verifier-hardening plan. If it fails, the
combined-body construction is the MVP bottleneck — Tasks 1-6 won't move the
outcome until the construction's vector matches pitch vectors well enough
to clear ``top_K_retrieve``.

Scope: dense + sparse retrieval only. No rerank assertion — rerank operates
on ``summarize`` output (a separate LLM call inside the persist tail), and
stubbing the rerank LLM produces a tautological assertion. End-to-end rerank
behavior is exercised by ``just eval`` cassettes.

Runs offline: real fastembed (nomic-embed) + real BM25 sparse encoder. First
run downloads the ~550MB ONNX models; marked ``slow`` so default ``just
test`` doesn't trigger it.

Vendor pair: Pebble Technology Corp (smartwatch, dead 2016, Wayback snapshot
of getpebble.com pre-shutdown + Wikipedia article documenting the December
2016 insolvency and Fitbit asset sale). Pitch is a hardware Kickstarter
wearable play; if the recall pipeline can't surface Pebble against this
pitch, no other vendor pair will fare better.

InMemoryCorpus today only implements the write-side ``ingest.Corpus``
protocol (``upsert_chunk`` / ``has_chunks`` / ``delete_chunks_for_canonical``).
This file ships a small ``_RetrievalCorpus`` that adds the read-side
``corpus.Corpus`` ``query`` method backed by cosine similarity over the
upserted chunks, with parent-collapse and the production
``recall_score_factor`` down-weight. Promote into the package only if a
second test wants the same surface.

The combined-body construction below mirrors the shape Task 3 will lift into
``slopmortem/stages/recall_verify._combine_recall_body``. After Task 3
lands, swap the inline block for a direct call to that helper so this test
tracks the production combiner.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from conftest import llm_canned_key
from slopmortem.budget import Budget
from slopmortem.config import Config
from slopmortem.corpus import MergeJournal, extract_clean
from slopmortem.corpus._embed_sparse import encode as bm25_encode
from slopmortem.corpus.sources._names import SOURCE_LLM_RECALL
from slopmortem.ingest import (
    FakeSlopClassifier,
    IngestResult,
    InMemoryCorpus,
    NullProgress,
)
from slopmortem.llm import (
    FakeLLMClient,
    FakeResponse,
    FastEmbedEmbeddingClient,
    render_prompt,
)
from slopmortem.models import Candidate, CandidatePayload, Facets, RawEntry
from slopmortem.stages import extract_facets, persist_recall_entry, retrieve
from slopmortem.stages.recall_verify import _recall_source_id

if TYPE_CHECKING:
    from collections.abc import Mapping

    from slopmortem.models import RecallSuggestion

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "recall"
_HAIKU = "anthropic/claude-haiku-4.5"
_EMBED_MODEL = "nomic-ai/nomic-embed-text-v1.5"


@pytest.fixture(autouse=True)
def _isolate_tldextract_cache(  # pyright: ignore[reportUnusedFunction]
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swap ``tldextract.extract`` for a cache-isolated instance.

    Default ``~/.cache/python-tldextract`` is sandbox-blocked on macOS; the
    persist tail's entity resolver touches tldextract via
    ``slopmortem.corpus._entity_resolution``. ``TLDEXTRACT_CACHE`` env doesn't
    help because tldextract caches its module-level extractor at first call.
    Mirrors the fixture in ``tests/test_pipeline_recall_fallback.py``.
    """
    import tldextract  # noqa: PLC0415

    cache = tmp_path_factory.mktemp("tldextract")
    extractor = tldextract.TLDExtract(cache_dir=str(cache))
    monkeypatch.setattr("slopmortem.corpus._entity_resolution.tldextract.extract", extractor)


def _entry_facets() -> Facets:
    return Facets(
        sector="hardware",
        business_model="hardware_one_time",
        customer_type="consumer",
        geography="us",
        monetization="one_time_purchase",
        founding_year=2012,
        failure_year=2016,
    )


def _pitch_facets() -> Facets:
    return Facets(
        sector="hardware",
        business_model="hardware_one_time",
        customer_type="consumer",
        geography="us",
        monetization="one_time_purchase",
    )


def _combine_recall_body(*, wayback_md: str, news_html: str, evidence_url: str) -> str:
    """Mirror Task 3's ``_combine_recall_body``: Wayback first, then news citation.

    Order matches the plan's "marketing copy section gives synthesis the
    value-prop, news section gives the death narrative" rationale. Replace
    with a direct import once Task 3 lands.
    """
    news = extract_clean(news_html)
    return (
        f"# Vendor description (archived)\n\n{wayback_md}"
        f"\n\n---\n\n# Failure citation\n\n"
        f"Source: {evidence_url}\n"
        f"Status (LLM-suggested): dead (2016)\n\n{news}"
    )


def _suggestion_from_fixture(vendor: dict[str, Any]) -> RecallSuggestion:
    from slopmortem.models import RecallSuggestion  # noqa: PLC0415

    return RecallSuggestion(
        name=str(vendor["name"]),
        category="consumer wearables",
        status="dead",
        homepage_url=str(vendor["homepage_url"]),
        failure_year=int(vendor["failure_year"]),
        one_liner=f"{vendor['name']} filed for insolvency in {vendor['failure_year']}.",
    )


def _entry_for(suggestion: RecallSuggestion, body: str) -> RawEntry:
    return RawEntry(
        source=SOURCE_LLM_RECALL,
        source_id=_recall_source_id(suggestion),
        url=str(suggestion.homepage_url),
        markdown_text=body,
        raw_html=None,
        fetched_at=datetime(2026, 5, 10, tzinfo=UTC),
    )


def _canned_facet_summary(body: str) -> Mapping[tuple[str, str, str], FakeResponse]:
    """Canned (facet_extract, summarize) responses keyed on the production prompt."""
    facet_prompt = render_prompt("facet_extract", description=body)
    summarize_prompt = render_prompt("summarize", body=body, source_id="")
    return {
        llm_canned_key("facet_extract", model=_HAIKU, prompt=facet_prompt): FakeResponse(
            text=_entry_facets().model_dump_json()
        ),
        llm_canned_key("summarize", model=_HAIKU, prompt=summarize_prompt): FakeResponse(
            text=(
                "Pebble shipped Kickstarter-funded e-paper smartwatches; "
                "filed for insolvency December 2016 and sold IP to Fitbit."
            )
        ),
    }


def _canned_pitch_facet(pitch: str) -> Mapping[tuple[str, str, str], FakeResponse]:
    facet_prompt = render_prompt("facet_extract", description=pitch)
    return {
        llm_canned_key("facet_extract", model=_HAIKU, prompt=facet_prompt): FakeResponse(
            text=_pitch_facets().model_dump_json()
        ),
    }


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. fastembed L2-normalizes, so dot suffices, but be defensive."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _sparse_dot(a: dict[int, float], b: dict[int, float]) -> float:
    """Sparse-vector dot product over shared indices."""
    if not a or not b:
        return 0.0
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return sum(v * long.get(k, 0.0) for k, v in short.items())


def _ranks(scores: list[tuple[int, float]]) -> dict[int, int]:
    """Return ``point_idx -> 1-based rank`` ordered by descending score."""
    ordered = sorted(scores, key=lambda kv: -kv[1])
    return {idx: rank for rank, (idx, _) in enumerate(ordered, start=1)}


def _vector_scores(
    *,
    points: list[Any],
    dense: list[float],
    sparse: dict[int, float],
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Per-point cosine and sparse-dot scores; mismatched modalities skip."""
    dense_scores: list[tuple[int, float]] = []
    sparse_scores: list[tuple[int, float]] = []
    for idx, point in enumerate(points):
        vec_dense = point.vector.get("dense")
        vec_sparse = point.vector.get("sparse")
        if isinstance(vec_dense, list):
            dense_scores.append((idx, _cosine(dense, vec_dense)))
        if isinstance(vec_sparse, dict):
            sparse_scores.append((idx, _sparse_dot(sparse, vec_sparse)))
    return dense_scores, sparse_scores


@dataclass
class _RetrievalCorpus(InMemoryCorpus):
    """``InMemoryCorpus`` plus a cosine-similarity ``query`` method.

    Mirrors enough of ``QdrantCorpus`` for Task 0's retrieval-survival check:
    dense+sparse RRF fusion, parent-collapse to ``canonical_id``, and the
    production ``recall_score_factor`` down-weight on ``source==llm_recall``
    rows. No facet boost, no recency filter, no alias graph — those are
    out of scope for "does the combined-body vector clear top_K".
    """

    recall_score_factor: float = 0.9
    rrf_k: int = 60

    def _rrf_per_point(self, *, dense: list[float], sparse: dict[int, float]) -> dict[int, float]:
        dense_scores, sparse_scores = _vector_scores(points=self.points, dense=dense, sparse=sparse)
        dense_rank = _ranks(dense_scores)
        sparse_rank = _ranks(sparse_scores)
        rrf: dict[int, float] = {}
        for idx in range(len(self.points)):
            score = 0.0
            if idx in dense_rank:
                score += 1.0 / (self.rrf_k + dense_rank[idx])
            if idx in sparse_rank:
                score += 1.0 / (self.rrf_k + sparse_rank[idx])
            rrf[idx] = score
        return rrf

    def _collapse_to_parents(
        self, rrf: dict[int, float]
    ) -> list[tuple[str, float, dict[str, Any]]]:
        best: dict[str, tuple[float, dict[str, Any]]] = {}
        for idx, score in rrf.items():
            payload = self.points[idx].payload
            cid = str(payload.get("canonical_id", ""))
            if not cid:
                continue
            adjusted = score
            if self.recall_score_factor < 1.0 and payload.get("source") == SOURCE_LLM_RECALL:
                adjusted = score * self.recall_score_factor
            if cid not in best or adjusted > best[cid][0]:
                best[cid] = (adjusted, dict(payload))
        return [(cid, score, payload) for cid, (score, payload) in best.items()]

    async def query(  # noqa: PLR0913 - Protocol contract dictates the signature
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
        del facets, cutoff_iso, strict_deaths, strict_sector_filter
        del strict_sector_filter_excludes_other
        if not self.points:
            return []
        rrf = self._rrf_per_point(dense=dense, sparse=sparse)
        collapsed = self._collapse_to_parents(rrf)
        ordered = sorted(collapsed, key=lambda triple: -triple[1])[:k_retrieve]
        candidates: list[Candidate] = []
        for cid, score, payload in ordered:
            cleaned = {k: v for k, v in payload.items() if k not in ("canonical_id", "chunk_idx")}
            candidates.append(
                Candidate(
                    canonical_id=cid,
                    score=score,
                    payload=CandidatePayload.model_validate(cleaned),
                )
            )
        return candidates

    async def get_post_mortem(self, canonical_id: str) -> str:
        del canonical_id
        return ""

    async def search_corpus(
        self,
        q: str,
        facets: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        del q, facets
        return []


_NOISE_BODIES: tuple[str, ...] = (
    "Acme Bank API gateway for fintech compliance officers automating SOC2 evidence collection.",
    "Marine biology research vessel chartering platform for academic institutions.",
    "Concrete-curing IoT sensor network for civil engineering project managers.",
    "Insurance claim adjuster mobile app for property and casualty workflows.",
    "Real-estate title transfer escrow software for closing attorneys.",
    "Pediatric oncology clinical trial recruitment marketplace for hospital networks.",
    "Cattle-breed genealogy ledger backed by smart contracts for ranching co-ops.",
    "Mortgage broker CRM with rate-watch alerts for residential lending teams.",
    "Container freight visibility dashboard tracking ocean shipments port-to-port.",
    "Fluid-dynamics simulation cloud for automotive aerodynamics engineers.",
    "Pharmaceutical cold-chain courier network for biologic injectable shipments.",
    "Industrial laundry route optimization software for hospitality linen suppliers.",
    "Synthetic-aperture radar imagery analytics for crop insurance underwriters.",
    "Public-records FOIA request automation for investigative journalists.",
    "Restaurant inventory variance reporting for multi-unit franchise operators.",
    "Aviation maintenance log digitization for commercial fleet operators.",
    "Subsea cable installation crew scheduling for offshore wind developers.",
    "Petrochemical refinery emissions reporting compliance dashboard.",
    "Vintage wine cellar provenance tracking for auction houses.",
    "Commercial real estate cap-rate analytics for institutional investors.",
    "K-12 school bus routing optimizer for district transportation directors.",
    "Wholesale floral supply chain platform connecting growers to florists.",
    "Aquaculture tank water-chemistry monitoring for shrimp farms.",
    "Industrial spray-coating QA imaging for aerospace paint shops.",
    "Funeral home grief counselling scheduling SaaS for licensed therapists.",
    "Construction crane maintenance audit log for OSHA compliance officers.",
    "Bus depot parking allocation software for municipal transit authorities.",
    "Steel mill furnace temperature controller analytics dashboard.",
    "Court reporter transcription accuracy benchmarking platform.",
    "Plumbing apprentice training credential tracker for trade unions.",
    "Vegetable seed germination rate forecasting for commercial growers.",
    "Chemical pesticide drift modeling for state agricultural inspectors.",
    "Forensic accounting software for divorce-settlement asset valuation.",
    "Oil rig drilling fluid composition logging for upstream operators.",
    "Bridge structural fatigue monitoring for state DOT inspection teams.",
    "Hospital sterilization cycle audit reporting for infection-control nurses.",
)


async def _seed_noise(
    corpus: _RetrievalCorpus,
    *,
    embed_client: FastEmbedEmbeddingClient,
) -> None:
    """Seed unrelated short candidate bodies so retrieve has a real top-K cut."""
    from slopmortem.ingest._ports import _Point  # noqa: PLC0415

    embeds = await embed_client.embed(list(_NOISE_BODIES))
    for i, (body, vec) in enumerate(zip(_NOISE_BODIES, embeds.vectors, strict=True)):
        cid = f"noise:{i}"
        sparse = bm25_encode(body)
        payload: CandidatePayload = CandidatePayload(
            name=f"Noise Co {i}",
            summary=body[:80],
            body=body,
            facets=_pitch_facets(),
            founding_date=None,
            failure_date=None,
            founding_date_unknown=True,
            failure_date_unknown=True,
            provenance="curated_real",
            slop_score=0.0,
            sources=[],
            provenance_id=f"noise:{i}",
            text_id=f"noise{i:04d}deadbeef",
            source="curated_real",
        )
        merged_payload: dict[str, object] = {
            **payload.model_dump(mode="json"),
            "canonical_id": cid,
            "chunk_idx": 0,
        }
        point = _Point(
            id=uuid.uuid5(uuid.NAMESPACE_URL, f"{cid}:0").hex,
            vector={"dense": vec, "sparse": sparse},
            payload=merged_payload,
        )
        await corpus.upsert_chunk(point)


@pytest.mark.slow
async def test_recall_entry_lands_in_top_k_after_persist(tmp_path: Path) -> None:
    pitch = (_FIXTURES / "survival_pitch.txt").read_text()
    vendor: dict[str, Any] = json.loads((_FIXTURES / "survival_vendor.json").read_text())
    news_html = (_FIXTURES / "survival_news_body.html").read_text()
    wayback_md = (_FIXTURES / "survival_wayback_body.txt").read_text()

    # Stand-in for the L0 Tavily-discovered citation URL; the body composition
    # only cares about the string for the ``Source:`` header.
    discovered_url = "https://en.wikipedia.org/wiki/Pebble_(watch)"
    body = _combine_recall_body(
        wayback_md=wayback_md,
        news_html=news_html,
        evidence_url=discovered_url,
    )
    suggestion = _suggestion_from_fixture(vendor)
    entry = _entry_for(suggestion, body)

    config = Config(max_cost_usd_per_ingest=100.0, ingest_concurrency=1)
    journal = MergeJournal(tmp_path / "journal.sqlite")
    await journal.init()
    corpus = _RetrievalCorpus(recall_score_factor=config.recall_score_factor)

    embed_client = FastEmbedEmbeddingClient(
        model=_EMBED_MODEL, budget=Budget(0.0), cache_dir=tmp_path / "fastembed"
    )

    # Pre-seed unrelated candidates so the recall entry has to actually win
    # the top-K cut against a noise floor — without them the assertion is
    # trivially true.
    await _seed_noise(corpus, embed_client=embed_client)

    persist_llm = FakeLLMClient(canned=_canned_facet_summary(body), default_model=_HAIKU)
    await persist_recall_entry(
        entry,
        "wayback_anchored",
        journal=journal,
        corpus=corpus,
        embed_client=embed_client,
        llm=persist_llm,
        slop_classifier=FakeSlopClassifier(default_score=0.0),
        sparse_encoder=bm25_encode,
        config=config,
        post_mortems_root=tmp_path / "post_mortems",
        progress=NullProgress(),
        result=IngestResult(),
    )

    # Sanity: at least one chunk for the recall entry made it into the corpus.
    recall_points = [p for p in corpus.points if p.payload.get("source") == SOURCE_LLM_RECALL]
    assert recall_points, "recall entry was not persisted into the in-memory corpus"
    recall_canonicals = {str(p.payload.get("canonical_id")) for p in recall_points}

    pitch_llm = FakeLLMClient(canned=_canned_pitch_facet(pitch), default_model=_HAIKU)
    pitch_facets = await extract_facets(
        pitch, pitch_llm, model=_HAIKU, max_tokens=config.max_tokens_facet
    )

    candidates = await retrieve(
        description=pitch,
        facets=pitch_facets,
        corpus=corpus,
        embedding_client=embed_client,
        cutoff_iso=None,
        strict_deaths=False,
        k_retrieve=config.K_retrieve,
        sparse_encoder=bm25_encode,
        strict_sector_filter=config.strict_sector_filter,
        strict_sector_filter_excludes_other=config.strict_sector_filter_excludes_other,
    )

    # The ingest path stamps ``CandidatePayload.name`` with ``entry.source_id`` —
    # for an LLM-recall entry that's the verifier's ``sha256(name|homepage)[:16]``,
    # not "Pebble". Match on canonical_id (entity-resolution output) instead so
    # the assertion tracks identity rather than a stand-in field.
    retrieved_canonicals = [c.canonical_id for c in candidates]
    matched = any(cid in recall_canonicals for cid in retrieved_canonicals)
    assert matched, (
        f"Recall entry vector not in top-{config.K_retrieve} after persist "
        f"against {len(_NOISE_BODIES)} noise candidates. "
        f"Combined-body construction is the MVP bottleneck — pause Tasks 1-6 "
        f"and revisit body shape (e.g. persist Wayback marketing copy alone, "
        f"keep news body only as payload metadata) before resuming. "
        f"Recall canonical(s): {sorted(recall_canonicals)} "
        f"Top-K canonicals returned: {retrieved_canonicals}"
    )
