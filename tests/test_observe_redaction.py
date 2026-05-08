"""Regression: corpus body strings never appear in Laminar-captured span attributes.

The pipeline runs against fakes (helpers copied inline from
``tests/test_pipeline_e2e.py``; sharing them as a fixture is intentionally out
of scope for this test file). Every fake `Candidate`'s
``payload.body`` carries a sentinel string. After the run, the test scrapes
every span attribute via OpenTelemetry's `InMemorySpanExporter` and
asserts the sentinel is nowhere captured.

Wiring rationale: ``lmnr-python`` does not expose an exporter override on
``Laminar.initialize``. The test initializes Laminar against an unreachable
loopback endpoint, then swaps the underlying ``TracerProvider``'s active span
processor with one backed by ``InMemorySpanExporter`` (replacing
``_active_span_processor`` on ``TracerWrapper.instance._tracer_provider``).
This is the only intercept point exposed by the SDK at this version; if a
public hook lands upstream, this fixture should switch to it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

from lmnr import Laminar
from lmnr.opentelemetry_lib.tracing import TracerWrapper
from opentelemetry.sdk.trace import SynchronousMultiSpanProcessor
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from conftest import llm_canned_key
from slopmortem.budget import Budget
from slopmortem.config import Config
from slopmortem.llm import FakeEmbeddingClient, FakeLLMClient, FakeResponse, render_prompt
from slopmortem.models import Candidate, CandidatePayload, Facets, InputContext
from slopmortem.pipeline import run_query
from slopmortem.stages import synthesize_prompt_kwargs

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pytest

    from slopmortem.llm import CompletionResult

# Inlined fakes (copied verbatim in spirit from tests/test_pipeline_e2e.py;
# extracting a shared fixture is out of scope for this test file).
_FACET_MODEL = "test-facet"
_RERANK_MODEL = "test-rerank"
_SYNTH_MODEL = "test-synth"
_CONSOLIDATE_MODEL = "test-consolidate"
_EMBED_MODEL = "text-embedding-3-small"

_BODY_SENTINEL = "ZZ-CANARY-CORPUS-BODY-DO-NOT-EXFILTRATE-ZZ"


def _facets() -> Facets:
    return Facets(
        sector="fintech",
        business_model="b2b_saas",
        customer_type="smb",
        geography="us",
        monetization="subscription_recurring",
    )


def _payload(*, name: str, canonical_id: str) -> CandidatePayload:
    return CandidatePayload(
        name=name,
        summary=f"{name} was a B2B fintech.",
        body=f"{name} body — {_BODY_SENTINEL} — full corpus content here.",
        facets=_facets(),
        founding_date=date(2018, 1, 1),
        failure_date=date(2023, 1, 1),
        founding_date_unknown=False,
        failure_date_unknown=False,
        provenance="curated_real",
        slop_score=0.0,
        sources=["https://news.ycombinator.com/item?id=" + canonical_id],
        text_id=canonical_id.replace("-", "") + "0123456789",
    )


def _candidate(canonical_id: str, *, score: float = 0.9) -> Candidate:
    return Candidate(
        canonical_id=canonical_id,
        score=score,
        payload=_payload(name=canonical_id, canonical_id=canonical_id),
    )


def _facet_extract_payload() -> str:
    return json.dumps(
        {
            "sector": "fintech",
            "business_model": "b2b_saas",
            "customer_type": "smb",
            "geography": "us",
            "monetization": "subscription_recurring",
            "sub_sector": "smb invoicing",
            "product_type": "saas",
            "price_point": "tiered",
            "founding_year": 2024,
            "failure_year": None,
        }
    )


def _rerank_payload(canonical_ids: list[str]) -> str:
    ranked = [
        {
            "candidate_id": cid,
            "perspective_scores": {
                "business_model": {"score": 7.0, "rationale": "match"},
                "market": {"score": 6.0, "rationale": "match"},
                "gtm": {"score": 5.0, "rationale": "match"},
                "stage_scale": {"score": 4.0, "rationale": "match"},
            },
            "rationale": "ranked",
        }
        for cid in canonical_ids
    ]
    return json.dumps({"ranked": ranked})


def _synthesis_payload(canonical_id: str) -> str:
    return json.dumps(
        {
            "candidate_id": canonical_id,
            "name": canonical_id,
            "one_liner": "B2B fintech for SMB invoicing.",
            "failure_date": "2023-01-01",
            "lifespan_months": 60,
            "similarity": {
                "business_model": {"score": 7.0, "rationale": "both B2B SaaS"},
                "market": {"score": 6.0, "rationale": "SMB overlap"},
                "gtm": {"score": 5.0, "rationale": "outbound sales"},
                "stage_scale": {"score": 4.0, "rationale": "seed stage"},
            },
            "why_similar": "Both target SMB invoicing.",
            "where_diverged": "Pitch is web-first; analogue was mobile-only.",
            "failure_causes": ["CAC > LTV"],
            "lessons_for_input": ["target larger ACVs"],
        }
    )


def _build_canned(
    *,
    retrieved: list[Candidate],
    top_n: list[Candidate],
    ctx: InputContext,
) -> Mapping[tuple[str, str, str], FakeResponse | CompletionResult]:
    """Build the FakeLLMClient canned-response map for the full pipeline."""
    facet_prompt = render_prompt("facet_extract", description=ctx.description)
    parsed_facets = Facets.model_validate_json(_facet_extract_payload())
    rerank_prompt = render_prompt(
        "llm_rerank",
        pitch=ctx.description,
        facets=parsed_facets.model_dump(),
        top_n=len(top_n),
        candidates=[
            {
                "candidate_id": c.canonical_id,
                "name": c.payload.name,
                "summary": c.payload.summary,
            }
            for c in retrieved
        ],
    )
    canned: dict[tuple[str, str, str], FakeResponse | CompletionResult] = {
        llm_canned_key("facet_extract", model=_FACET_MODEL, prompt=facet_prompt): FakeResponse(
            text=_facet_extract_payload(), cost_usd=0.001
        ),
        llm_canned_key("llm_rerank", model=_RERANK_MODEL, prompt=rerank_prompt): FakeResponse(
            text=_rerank_payload([c.canonical_id for c in top_n]), cost_usd=0.005
        ),
    }
    synth_resp = FakeResponse(
        text=_synthesis_payload("acme"), cost_usd=0.01, cache_creation_tokens=10
    )
    for cand in top_n:
        synth_prompt = render_prompt(
            "synthesize", **synthesize_prompt_kwargs(cand, pitch=ctx.description)
        )
        canned[llm_canned_key("synthesize", model=_SYNTH_MODEL, prompt=synth_prompt)] = synth_resp

    # Canned synthesis always emits ``candidate_id="acme"`` and lesson
    # ``"target larger ACVs"``, so the consolidate prompt is deterministic.
    consolidate_prompt = render_prompt(
        "consolidate_risks",
        pitch=ctx.description,
        lessons=[
            {
                "candidate_id": "acme",
                "candidate_name": "acme",
                "lesson": "target larger ACVs",
            }
        ],
        candidate_ids=["acme"] * len(top_n),
    )
    canned[
        llm_canned_key("consolidate_risks", model=_CONSOLIDATE_MODEL, prompt=consolidate_prompt)
    ] = FakeResponse(
        text=json.dumps({"top_risks": [], "injection_detected": False}),
        cost_usd=0.005,
    )
    return canned


@dataclass
class _FakeCorpus:
    """In-memory `Corpus`; mirrors the helper in test_pipeline_e2e.py."""

    candidates: list[Candidate]
    queries: list[dict[str, object]] = field(default_factory=list)

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
        self.queries.append(
            {
                "dense_dim": len(dense),
                "sparse_keys": list(sparse.keys()),
                "facets": facets.model_dump(),
                "cutoff_iso": cutoff_iso,
                "strict_deaths": strict_deaths,
                "k_retrieve": k_retrieve,
                "strict_sector_filter": strict_sector_filter,
                "strict_sector_filter_excludes_other": strict_sector_filter_excludes_other,
            }
        )
        return list(self.candidates[:k_retrieve])

    async def get_post_mortem(self, canonical_id: str) -> str:
        for c in self.candidates:
            if c.canonical_id == canonical_id:
                return c.payload.body
        msg = f"unknown canonical_id {canonical_id!r}"
        raise KeyError(msg)

    async def search_corpus(
        self, q: str, facets: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        del q, facets
        return [
            {
                "canonical_id": c.canonical_id,
                "name": c.payload.name,
                "summary": c.payload.summary,
                "score": c.score,
            }
            for c in self.candidates
        ]


def _no_op_sparse_encoder(_t: str) -> dict[int, float]:
    return {1: 1.0}


def _build_config(*, k_retrieve: int = 6, n_synthesize: int = 3) -> Config:
    cfg = Config()
    return cfg.model_copy(
        update={
            "K_retrieve": k_retrieve,
            "N_synthesize": n_synthesize,
            "model_facet": _FACET_MODEL,
            "model_rerank": _RERANK_MODEL,
            "model_synthesize": _SYNTH_MODEL,
            "model_consolidate": _CONSOLIDATE_MODEL,
        }
    )


async def test_no_corpus_body_in_laminar_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the full pipeline; assert the corpus-body sentinel never reaches a span."""
    candidates = [_candidate(f"cand-{i}") for i in range(6)]
    cfg = _build_config(k_retrieve=6, n_synthesize=3)
    ctx = InputContext(name="newco", description="A B2B fintech for SMB invoicing")
    canned = _build_canned(
        retrieved=candidates[: cfg.K_retrieve],
        top_n=candidates[: cfg.N_synthesize],
        ctx=ctx,
    )
    fake_llm = FakeLLMClient(canned=canned, default_model=_SYNTH_MODEL)
    fake_embed = FakeEmbeddingClient(model=_EMBED_MODEL)
    fake_corpus = _FakeCorpus(candidates=candidates)
    budget = Budget(cap_usd=2.0)

    monkeypatch.setattr("slopmortem.corpus._embed_sparse.encode", _no_op_sparse_encoder)

    # Initialize Laminar against an unreachable loopback endpoint, then swap the
    # active span processor on the underlying TracerProvider with one backed by
    # InMemorySpanExporter. This is the only intercept point exposed by
    # lmnr-python at this version; cf. module docstring.
    # initialize() must sit inside the try: if the processor swap below
    # raises, Laminar would otherwise stay initialized and leak a real
    # trace_id into other tests on the same xdist worker.
    try:
        Laminar.initialize(
            project_api_key="test-key",
            base_url="http://localhost",
            http_port=1,
            grpc_port=1,
            disable_batch=True,
        )
        exporter = InMemorySpanExporter()
        new_multi = SynchronousMultiSpanProcessor()
        new_multi.add_span_processor(SimpleSpanProcessor(exporter))
        tracer_provider = TracerWrapper.instance._tracer_provider
        assert tracer_provider is not None
        tracer_provider._active_span_processor.shutdown()
        tracer_provider._active_span_processor = new_multi

        report = await run_query(
            ctx,
            llm=fake_llm,
            embedding_client=fake_embed,
            corpus=fake_corpus,
            config=cfg,
            budget=budget,
        )
        assert report.candidates  # sanity check: pipeline produced output

        Laminar.flush()
        spans = exporter.get_finished_spans()
        # Sanity check: the three decorated stages emitted spans.
        span_names = {s.name for s in spans}
        assert "stage.facet_extract" in span_names
        assert "stage.retrieve" in span_names
        assert "stage.llm_rerank" in span_names

        captured = json.dumps(
            [{"name": s.name, "attrs": dict(s.attributes or {})} for s in spans],
            default=str,
        )
        assert _BODY_SENTINEL not in captured, "corpus body leaked to Laminar span attributes"
    finally:
        Laminar.shutdown()
