"""End-to-end pipeline tests: full ``run_query`` path with fakes for every dependency.

Covers Task 10 plan steps:
- 10.1 Full pipeline E2E with fake LLM/embedder/Corpus.
- 10.5 Ctrl-C cancel propagates as ``asyncio.CancelledError``.

Tests use ``FakeLLMClient`` (canned ``(prompt_template_sha, model)`` responses)
and ``FakeEmbeddingClient`` (sha256-derived vectors). The Corpus is an
in-memory implementation of `slopmortem.corpus.store.Corpus`; no
Qdrant required.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, cast

import pytest

from conftest import llm_canned_key
from slopmortem.budget import Budget
from slopmortem.config import Config
from slopmortem.llm import FakeEmbeddingClient, FakeLLMClient, FakeResponse, render_prompt
from slopmortem.models import (
    Candidate,
    CandidatePayload,
    Facets,
    InputContext,
    Synthesis,
    TopRisks,
)
from slopmortem.pipeline import (
    QueryPhase,
    cutoff_iso,
    run_query,
)
from slopmortem.stages import synthesize_prompt_kwargs

if TYPE_CHECKING:
    from collections.abc import Mapping

    from slopmortem.llm import CompletionResult

_FACET_MODEL = "test-facet"
_RERANK_MODEL = "test-rerank"
_SYNTH_MODEL = "test-synth"
_CONSOLIDATE_MODEL = "test-consolidate"
_EMBED_MODEL = "text-embedding-3-small"


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
        body=f"{name} was a B2B fintech that ran out of runway.",
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


def _consolidate_payload() -> str:
    """Canned consolidate-risks JSON for the e2e test.

    The synthesis fixture always emits ``candidate_id="acme"`` and lesson
    ``"target larger ACVs"``, so the consolidate input is deterministic.
    """
    return json.dumps(
        {
            "top_risks": [
                {
                    "summary": "target larger ACVs",
                    "applies_because": "pitch sells SMB invoicing — same shape as the comparable.",
                    "raised_by": ["acme"],
                    "severity": "medium",
                }
            ],
            "injection_detected": False,
        }
    )


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
    """Build the FakeLLMClient canned-response map for the full pipeline.

    Renders one entry per (template_sha, model, prompt_hash) the pipeline
    will produce: one facet_extract, one llm_rerank over the ``retrieved``
    set, and one synthesize per candidate in ``top_n``.
    """
    # facet_extract is rendered with `description=ctx.description`.
    facet_prompt = render_prompt("facet_extract", description=ctx.description)
    # llm_rerank is rendered over the retrieved candidates with the parsed
    # facets (post LLM JSON validation including the extra fields the prompt
    # response embeds). Reproduce by parsing the canned facet payload.
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
    # Synthesis runs once per top_n candidate; render each prompt and emit
    # one canned entry per (template, model, prompt_hash). The response's
    # ``candidate_id`` field stays "acme" but the stage's parser doesn't
    # enforce that it matches the request.
    synth_resp = FakeResponse(
        text=_synthesis_payload("acme"), cost_usd=0.01, cache_creation_tokens=10
    )
    for cand in top_n:
        synth_prompt = render_prompt(
            "synthesize", **synthesize_prompt_kwargs(cand, pitch=ctx.description)
        )
        canned[llm_canned_key("synthesize", model=_SYNTH_MODEL, prompt=synth_prompt)] = synth_resp

    # Consolidate sees N copies of the same Synthesis (canned synth always
    # returns candidate_id="acme"); per-candidate dedup collapses to one
    # lesson, candidate_ids list keeps duplicates.
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
    ] = FakeResponse(text=_consolidate_payload(), cost_usd=0.005)
    return canned


@dataclass
class _FakeCorpus:
    """In-memory `Corpus` for pipeline tests; no Qdrant, no fastembed."""

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
    # Force ``enable_tracing=False`` so the test is hermetic. Without this,
    # a developer's ``slopmortem.local.toml`` with ``enable_tracing=true``
    # would leak through ``Config()`` and the ``trace_id`` assertion below
    # would flip whenever a CLI-test sibling initialized Laminar in the
    # same xdist worker.
    cfg = Config()
    return cfg.model_copy(
        update={
            "K_retrieve": k_retrieve,
            "N_synthesize": n_synthesize,
            "model_facet": _FACET_MODEL,
            "model_rerank": _RERANK_MODEL,
            "model_synthesize": _SYNTH_MODEL,
            "model_consolidate": _CONSOLIDATE_MODEL,
            "enable_tracing": False,
        }
    )


class _RecordingQueryProgress:
    """Test stub for `slopmortem.pipeline.QueryProgress`.

    Records every phase event into ``self.events`` as ``("start", phase, total)``,
    ``("advance", phase, n)``, ``("end", phase)``, ``("log", message)``, or
    ``("error", phase, message)`` tuples.
    """

    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []

    def start_phase(self, phase: QueryPhase, total: int) -> None:
        self.events.append(("start", phase, total))

    def advance_phase(self, phase: QueryPhase, n: int = 1) -> None:
        self.events.append(("advance", phase, n))

    def end_phase(self, phase: QueryPhase) -> None:
        self.events.append(("end", phase))

    def set_phase_status(self, phase: QueryPhase, status: str | None) -> None:
        self.events.append(("status", phase, status))

    def log(self, message: str) -> None:
        self.events.append(("log", message))

    def error(self, phase: QueryPhase, message: str) -> None:
        self.events.append(("error", phase, message))


async def test_full_pipeline_with_fake_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the full pipeline end-to-end with fakes; assert the Report shape."""
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

    # Override retrieve's default sparse encoder to avoid loading fastembed.
    monkeypatch.setattr("slopmortem.corpus._embed_sparse.encode", _no_op_sparse_encoder)

    progress = _RecordingQueryProgress()
    report = await run_query(
        ctx,
        llm=fake_llm,
        embedding_client=fake_embed,
        corpus=fake_corpus,
        config=cfg,
        budget=budget,
        progress=progress,
    )

    # Report shape.
    assert report.input == ctx
    assert isinstance(report.candidates, list)
    assert 0 < len(report.candidates) <= cfg.N_synthesize
    assert all(isinstance(s, Synthesis) for s in report.candidates)

    # Top risks: consolidated from the canned synthesis lessons. The fake
    # payload always emits ``candidate_id="acme"`` and lesson
    # ``"target larger ACVs"``, so per-candidate dedup collapses everything
    # and the canned consolidate response returns a single risk.
    assert isinstance(report.top_risks, TopRisks)
    assert len(report.top_risks.risks) == 1
    assert report.top_risks.risks[0].raised_by == ["acme"]
    assert report.top_risks.risks[0].severity == "medium"

    # Pipeline meta.
    meta = report.pipeline_meta
    assert meta.K_retrieve == cfg.K_retrieve
    assert meta.N_synthesize == cfg.N_synthesize
    # FakeLLMClient/FakeEmbeddingClient don't push costs into the budget. We
    # still assert ``cost_usd_total`` reads from ``Budget.spent_usd`` so the
    # pipeline doesn't hand-roll the figure.
    assert meta.cost_usd_total == budget.spent_usd
    assert meta.latency_ms_total >= 0
    assert meta.budget_exceeded is False
    assert meta.trace_id is None  # tracing not initialized in tests
    assert set(meta.models.keys()) == {"facet", "rerank", "synthesize"}
    assert meta.models["facet"] == _FACET_MODEL
    assert meta.models["rerank"] == _RERANK_MODEL
    assert meta.models["synthesize"] == _SYNTH_MODEL

    # Progress hooks were invoked at every stage.
    phases_started = {evt[1] for evt in progress.events if evt[0] == "start"}
    assert phases_started == {
        QueryPhase.FACET_EXTRACT,
        QueryPhase.RETRIEVE,
        QueryPhase.RERANK,
        QueryPhase.SYNTHESIZE,
    }
    # Synthesize must have advanced once per top_n candidate.
    synth_advances = sum(
        cast("int", evt[2])
        for evt in progress.events
        if evt[0] == "advance" and evt[1] == QueryPhase.SYNTHESIZE
    )
    assert synth_advances == cfg.N_synthesize  # under fake fixtures all candidates settle

    # Corpus.query was invoked with the right knobs.
    assert len(fake_corpus.queries) == 1
    q = fake_corpus.queries[0]
    assert q["k_retrieve"] == cfg.K_retrieve
    assert q["strict_deaths"] == cfg.strict_deaths


async def test_run_query_forwards_sparse_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_query forwards sparse_encoder to retrieve(); production fastembed not loaded."""
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

    seen_calls: list[str] = []

    def my_sparse(text: str) -> dict[int, float]:
        seen_calls.append(text)
        return {1: 1.0}

    # Sabotage the lazy fastembed default so the test fails loud if the
    # injected encoder is NOT used.
    def _boom(_t: str) -> dict[int, float]:
        msg = "default sparse encoder must not be invoked"
        raise AssertionError(msg)

    monkeypatch.setattr("slopmortem.corpus._embed_sparse.encode", _boom)

    _ = await run_query(
        ctx,
        llm=fake_llm,
        embedding_client=fake_embed,
        corpus=fake_corpus,
        config=cfg,
        budget=budget,
        sparse_encoder=my_sparse,
    )

    # The injected encoder ran during retrieve(): ctx.description goes through
    # the sparse path verbatim.
    assert seen_calls
    assert ctx.description in seen_calls


async def test_run_query_marks_budget_exceeded_on_llm_overspend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM overspend (settle-side raise) truncates the run cleanly.

    Companion to ``test_run_query_records_budget_exceeded``, which covers the
    embedding/reserve path. The wrapper funnels each FakeLLMClient cost
    through ``budget.settle`` so the first call pushes spent over cap.
    """
    candidates = [_candidate(f"cand-{i}") for i in range(6)]
    cfg = _build_config(k_retrieve=6, n_synthesize=3)
    ctx = InputContext(name="newco", description="A B2B fintech for SMB invoicing")
    canned = _build_canned(
        retrieved=candidates[: cfg.K_retrieve],
        top_n=candidates[: cfg.N_synthesize],
        ctx=ctx,
    )
    fake_embed = FakeEmbeddingClient(model=_EMBED_MODEL)
    fake_corpus = _FakeCorpus(candidates=candidates)
    # Cap sits just under the canned facet cost (0.001), so the first settle
    # crosses the cap.
    budget = Budget(cap_usd=0.0001)

    @dataclass
    class _SettlingFakeLLMClient:
        """FakeLLMClient that routes each completion's cost through budget.settle."""

        inner: FakeLLMClient
        budget: Budget

        async def complete(  # noqa: PLR0913 - mirrors LLMClient.complete signature
            self,
            prompt: str,
            *,
            system: str | None = None,
            tools: list[Any] | None = None,
            model: str | None = None,
            cache: bool = False,
            response_format: dict[str, Any] | None = None,
            extra_body: dict[str, Any] | None = None,
            max_tokens: int | None = None,
            single_tool_call: bool = False,
        ) -> CompletionResult:
            result = await self.inner.complete(
                prompt,
                system=system,
                tools=tools,
                model=model,
                cache=cache,
                response_format=response_format,
                extra_body=extra_body,
                max_tokens=max_tokens,
                single_tool_call=single_tool_call,
            )
            if result.cost_usd > 0.0:
                await self.budget.settle("test:llm", result.cost_usd)
            return result

    settling_llm = _SettlingFakeLLMClient(
        inner=FakeLLMClient(canned=canned, default_model=_SYNTH_MODEL),
        budget=budget,
    )

    monkeypatch.setattr("slopmortem.corpus._embed_sparse.encode", _no_op_sparse_encoder)

    report = await run_query(
        ctx,
        llm=settling_llm,
        embedding_client=fake_embed,
        corpus=fake_corpus,
        config=cfg,
        budget=budget,
    )

    assert report.pipeline_meta.budget_exceeded is True
    assert report.candidates == []
    assert report.top_risks.risks == []
    # The over-cap call's cost still got credited.
    assert report.pipeline_meta.cost_usd_total > budget.cap_usd


async def test_run_query_records_budget_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    """BudgetExceededError mid-run sets ``budget_exceeded=True`` and returns cleanly."""
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
    # Cap at 0.0 so any LLM call's cost reservation exceeds the budget.
    budget = Budget(cap_usd=0.0)

    monkeypatch.setattr("slopmortem.corpus._embed_sparse.encode", _no_op_sparse_encoder)

    # Force extract_facets to raise BudgetExceededError immediately, so the
    # except branch in ``run_query`` runs without needing the embedding
    # client to do real reservation accounting.
    from slopmortem.budget import BudgetExceededError  # noqa: PLC0415

    async def _raise(*_a: object, **_kw: object) -> None:
        msg = "test"
        raise BudgetExceededError(msg)

    monkeypatch.setattr("slopmortem.pipeline.extract_facets", _raise)

    report = await run_query(
        ctx,
        llm=fake_llm,
        embedding_client=fake_embed,
        corpus=fake_corpus,
        config=cfg,
        budget=budget,
    )

    assert report.pipeline_meta.budget_exceeded is True
    assert report.candidates == []


async def test_ctrl_c_cancels_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling the run_query task propagates as ``asyncio.CancelledError``."""
    candidates = [_candidate(f"cand-{i}") for i in range(6)]
    cfg = _build_config(k_retrieve=6, n_synthesize=3)
    ctx = InputContext(name="newco", description="A B2B fintech for SMB invoicing")
    canned = _build_canned(
        retrieved=candidates[: cfg.K_retrieve],
        top_n=candidates[: cfg.N_synthesize],
        ctx=ctx,
    )

    @dataclass
    class _SlowFakeLLMClient:
        """FakeLLMClient that sleeps before each completion so cancel can land."""

        inner: FakeLLMClient

        async def complete(  # noqa: PLR0913 - mirrors LLMClient.complete signature
            self,
            prompt: str,
            *,
            system: str | None = None,
            tools: list[Any] | None = None,
            model: str | None = None,
            cache: bool = False,
            response_format: dict[str, Any] | None = None,
            extra_body: dict[str, Any] | None = None,
            max_tokens: int | None = None,
            single_tool_call: bool = False,
        ) -> CompletionResult:
            await asyncio.sleep(0.5)
            return await self.inner.complete(
                prompt,
                system=system,
                tools=tools,
                model=model,
                cache=cache,
                response_format=response_format,
                extra_body=extra_body,
                max_tokens=max_tokens,
                single_tool_call=single_tool_call,
            )

    slow_llm = _SlowFakeLLMClient(inner=FakeLLMClient(canned=canned, default_model=_SYNTH_MODEL))
    fake_embed = FakeEmbeddingClient(model=_EMBED_MODEL)
    fake_corpus = _FakeCorpus(candidates=candidates)
    budget = Budget(cap_usd=2.0)

    monkeypatch.setattr("slopmortem.corpus._embed_sparse.encode", _no_op_sparse_encoder)

    task = asyncio.create_task(
        run_query(
            ctx,
            llm=slow_llm,
            embedding_client=fake_embed,
            corpus=fake_corpus,
            config=cfg,
            budget=budget,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def testcutoff_iso_none_returns_none() -> None:
    assert cutoff_iso(None) is None


def testcutoff_iso_returns_iso_date_string() -> None:
    out = cutoff_iso(years_filter=5)
    assert out is not None
    # Must be parseable as a date.
    date.fromisoformat(out)


def test_cutoff_iso_handles_leap_year_correctly(monkeypatch: pytest.MonkeyPatch) -> None:
    """relativedelta(years=4) from 2024-02-29 must land on 2020-02-29, not 2020-02-28."""
    from datetime import UTC, datetime  # noqa: PLC0415

    fixed_now = datetime(2024, 2, 29, 12, 0, 0, tzinfo=UTC)

    class _Now:
        @staticmethod
        def now(tz: object) -> datetime:
            return fixed_now

    monkeypatch.setattr("slopmortem.pipeline.datetime", _Now)
    assert cutoff_iso(years_filter=4) == "2020-02-29"


def test_cutoff_iso_none_passthrough() -> None:
    assert cutoff_iso(years_filter=None) is None


async def test_run_query_zero_passes_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """A threshold above every rerank score yields an empty Report.candidates."""
    candidates = [_candidate(f"cand-{i}") for i in range(6)]
    cfg = _build_config(k_retrieve=6, n_synthesize=3).model_copy(
        update={"min_similarity_score": 9.5}  # rerank fixture caps at mean 5.5
    )
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

    report = await run_query(
        ctx,
        llm=fake_llm,
        embedding_client=fake_embed,
        corpus=fake_corpus,
        config=cfg,
        budget=budget,
    )

    assert report.candidates == []
    assert report.top_risks.risks == []
    assert report.pipeline_meta.budget_exceeded is False
    assert report.pipeline_meta.min_similarity_score == 9.5


def _synth_payload_with_scores(
    canonical_id: str, *, bm: float, mk: float, gtm: float, ss: float
) -> str:
    """Synth canned payload with caller-controlled perspective scores."""
    return json.dumps(
        {
            "candidate_id": canonical_id,
            "name": canonical_id,
            "one_liner": "B2B fintech for SMB invoicing.",
            "failure_date": "2023-01-01",
            "lifespan_months": 60,
            "similarity": {
                "business_model": {"score": bm, "rationale": "x"},
                "market": {"score": mk, "rationale": "x"},
                "gtm": {"score": gtm, "rationale": "x"},
                "stage_scale": {"score": ss, "rationale": "x"},
            },
            "why_similar": "Both target SMB invoicing.",
            "where_diverged": "Pitch is web-first; analogue was mobile-only.",
            "failure_causes": ["CAC > LTV"],
            "lessons_for_input": ["target larger ACVs"],
        }
    )


async def test_post_synth_filter_records_drop_on_pipeline_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synth re-scores 2 of 3 candidates below threshold; meta records the drop."""
    candidates = [_candidate(f"cand-{i}") for i in range(3)]
    cfg = _build_config(k_retrieve=3, n_synthesize=3).model_copy(
        update={"min_similarity_score": 4.5}
    )
    ctx = InputContext(name="newco", description="A B2B fintech for SMB invoicing")

    # Build canned per-candidate synth replies: only cand-0 keeps a passing
    # synth mean (5.5); cand-1 / cand-2 fall to 2.0 so the post-synth filter
    # drops them. Rerank still emits 5.5 for all three so they all reach synth.
    parsed_facets = Facets.model_validate_json(_facet_extract_payload())
    facet_prompt = render_prompt("facet_extract", description=ctx.description)
    rerank_prompt = render_prompt(
        "llm_rerank",
        pitch=ctx.description,
        facets=parsed_facets.model_dump(),
        top_n=cfg.N_synthesize,
        candidates=[
            {
                "candidate_id": c.canonical_id,
                "name": c.payload.name,
                "summary": c.payload.summary,
            }
            for c in candidates
        ],
    )
    canned: dict[tuple[str, str, str], FakeResponse | CompletionResult] = {
        llm_canned_key("facet_extract", model=_FACET_MODEL, prompt=facet_prompt): FakeResponse(
            text=_facet_extract_payload(), cost_usd=0.001
        ),
        llm_canned_key("llm_rerank", model=_RERANK_MODEL, prompt=rerank_prompt): FakeResponse(
            text=_rerank_payload([c.canonical_id for c in candidates]), cost_usd=0.005
        ),
    }
    synth_scores: dict[str, tuple[float, float, float, float]] = {
        "cand-0": (7.0, 6.0, 5.0, 4.0),  # mean 5.5 — passes
        "cand-1": (2.0, 2.0, 2.0, 2.0),  # mean 2.0 — dropped
        "cand-2": (2.0, 2.0, 2.0, 2.0),  # mean 2.0 — dropped
    }
    for cand in candidates:
        synth_prompt = render_prompt(
            "synthesize", **synthesize_prompt_kwargs(cand, pitch=ctx.description)
        )
        bm, mk, gtm, ss = synth_scores[cand.canonical_id]
        canned[llm_canned_key("synthesize", model=_SYNTH_MODEL, prompt=synth_prompt)] = (
            FakeResponse(
                text=_synth_payload_with_scores(cand.canonical_id, bm=bm, mk=mk, gtm=gtm, ss=ss),
                cost_usd=0.01,
                cache_creation_tokens=10,
            )
        )
    # Only the surviving synth (cand-0) reaches consolidate.
    consolidate_prompt = render_prompt(
        "consolidate_risks",
        pitch=ctx.description,
        lessons=[
            {
                "candidate_id": "cand-0",
                "candidate_name": "cand-0",
                "lesson": "target larger ACVs",
            }
        ],
        candidate_ids=["cand-0"],
    )
    canned[
        llm_canned_key("consolidate_risks", model=_CONSOLIDATE_MODEL, prompt=consolidate_prompt)
    ] = FakeResponse(
        text=json.dumps(
            {
                "top_risks": [
                    {
                        "summary": "target larger ACVs",
                        "applies_because": "pitch matches.",
                        "raised_by": ["cand-0"],
                        "severity": "medium",
                    }
                ],
                "injection_detected": False,
            }
        ),
        cost_usd=0.005,
    )

    fake_llm = FakeLLMClient(canned=canned, default_model=_SYNTH_MODEL)
    fake_embed = FakeEmbeddingClient(model=_EMBED_MODEL)
    fake_corpus = _FakeCorpus(candidates=candidates)
    budget = Budget(cap_usd=2.0)

    monkeypatch.setattr("slopmortem.corpus._embed_sparse.encode", _no_op_sparse_encoder)

    report = await run_query(
        ctx,
        llm=fake_llm,
        embedding_client=fake_embed,
        corpus=fake_corpus,
        config=cfg,
        budget=budget,
    )

    assert report.pipeline_meta.filtered_post_synth == 2
    assert len(report.candidates) == 1


def test_render_banner_mentions_filter_drops() -> None:
    """An empty candidates Report with post-synth drops renders the 'filtered out' banner."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from slopmortem.models import PipelineMeta, Report  # noqa: PLC0415
    from slopmortem.render import render  # noqa: PLC0415

    report = Report(
        input=InputContext(name="newco", description="A B2B fintech for SMB invoicing"),
        generated_at=datetime.now(UTC),
        candidates=[],
        pipeline_meta=PipelineMeta(
            K_retrieve=6,
            N_synthesize=3,
            min_similarity_score=4.0,
            models={"facet": "f", "rerank": "r", "synthesize": "s"},
            cost_usd_total=0.0,
            latency_ms_total=0,
            trace_id=None,
            budget_remaining_usd=2.0,
            budget_exceeded=False,
            filtered_pre_synth=0,
            filtered_post_synth=3,
        ),
    )
    rendered = render(report)
    assert "filtered out" in rendered.lower()
    assert "min_similarity" in rendered.lower()


async def test_pipeline_logs_drops_at_info(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """min_similarity drops emit an INFO log on the rerank stage."""
    import logging  # noqa: PLC0415

    candidates = [_candidate(f"cand-{i}") for i in range(3)]
    cfg = _build_config(k_retrieve=3, n_synthesize=3).model_copy(
        update={"min_similarity_score": 9.5}  # above any rerank score → all drop pre-synth
    )
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

    with caplog.at_level(logging.INFO, logger="slopmortem.stages.llm_rerank"):
        _ = await run_query(
            ctx,
            llm=fake_llm,
            embedding_client=fake_embed,
            corpus=fake_corpus,
            config=cfg,
            budget=budget,
        )

    assert any("min_similarity dropped" in r.message for r in caplog.records)
