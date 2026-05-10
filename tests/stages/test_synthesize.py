"""Tests for the ``synthesize`` stage: structured-output round trip and cache-warm pattern."""

from __future__ import annotations

import json
from datetime import date
from typing import Literal

from conftest import llm_canned_key
from slopmortem.config import Config
from slopmortem.llm import FakeLLMClient, FakeResponse, render_prompt
from slopmortem.models import Candidate, CandidatePayload, Facets, InputContext
from slopmortem.stages import synthesize, synthesize_all, synthesize_prompt_kwargs

_DEFAULT_MODEL = "test-synth-model"


def _facets() -> Facets:
    return Facets(
        sector="fintech",
        business_model="b2b_saas",
        customer_type="smb",
        geography="us",
        monetization="subscription_recurring",
    )


def _payload(
    *,
    name: str = "Acme",
    body: str = "Acme was a B2B fintech that ran out of runway.",
    sources: list[str] | None = None,
    source: str | None = None,
    deathness_verdict: Literal["dead", "struggling"] | None = None,
) -> CandidatePayload:
    return CandidatePayload(
        name=name,
        summary="summary",
        body=body,
        facets=_facets(),
        founding_date=date(2018, 1, 1),
        failure_date=date(2023, 1, 1),
        founding_date_unknown=False,
        failure_date_unknown=False,
        provenance="curated_real",
        slop_score=0.0,
        sources=sources if sources is not None else ["https://acme.com/postmortem"],
        text_id="abcdef0123456789",
        source=source,
        deathness_verdict=deathness_verdict,
    )


def _candidate(  # noqa: PLR0913 — test fixture: every kwarg shapes a candidate field
    *,
    canonical_id: str = "acme-corp",
    name: str = "Acme",
    body: str = "Acme was a B2B fintech that ran out of runway.",
    sources: list[str] | None = None,
    source: str | None = None,
    deathness_verdict: Literal["dead", "struggling"] | None = None,
) -> Candidate:
    return Candidate(
        canonical_id=canonical_id,
        score=0.9,
        payload=_payload(
            name=name,
            body=body,
            sources=sources,
            source=source,
            deathness_verdict=deathness_verdict,
        ),
    )


def _ctx() -> InputContext:
    return InputContext(name="newco", description="A B2B fintech for SMB invoicing")


def _synthesis_payload(
    *,
    candidate_id: str = "acme-corp",
    name: str = "Acme",
    where_diverged: str = "New pitch is web-first; Acme was mobile-only.",
) -> str:
    """Build a canned LLM response. failure_date, lifespan_months, and sources
    are derived/passed-through by the pipeline from the candidate payload, so
    the LLM no longer emits them.
    """
    return json.dumps(
        {
            "candidate_id": candidate_id,
            "name": name,
            "one_liner": "B2B fintech for SMB invoicing.",
            "similarity": {
                "business_model": {"score": 7.0, "rationale": "both B2B SaaS"},
                "market": {"score": 6.0, "rationale": "SMB invoicing overlap"},
                "gtm": {"score": 5.0, "rationale": "outbound sales"},
                "stage_scale": {"score": 4.0, "rationale": "seed stage"},
            },
            "why_similar": "Both target SMB invoicing with B2B SaaS pricing.",
            "where_diverged": where_diverged,
            "failure_causes": ["CAC > LTV", "long sales cycles"],
            "lessons_for_input": ["target larger ACVs", "avoid SMB churn traps"],
        }
    )


def _synthesize_canned(
    candidates: list[Candidate],
    ctx: InputContext,
    *,
    text: str,
    cache_creation_tokens: int | None = None,
    model: str = _DEFAULT_MODEL,
) -> dict[tuple[str, str, str], FakeResponse]:
    """Build one canned entry per rendered synthesize prompt for ``candidates``."""
    out: dict[tuple[str, str, str], FakeResponse] = {}
    for cand in candidates:
        rendered = render_prompt(
            "synthesize", **synthesize_prompt_kwargs(cand, pitch=ctx.description)
        )
        out[llm_canned_key("synthesize", model=model, prompt=rendered)] = FakeResponse(
            text=text, cache_creation_tokens=cache_creation_tokens
        )
    return out


async def test_synthesize_returns_filled_synthesis() -> None:
    cand = _candidate()
    payload = _synthesis_payload(candidate_id=cand.canonical_id)
    fake_llm = FakeLLMClient(
        canned=_synthesize_canned([cand], _ctx(), text=payload),
        default_model=_DEFAULT_MODEL,
    )

    s = await synthesize(cand, _ctx(), fake_llm, Config(), model=_DEFAULT_MODEL)

    # Anti-cheerleading guard: the prompt forces a non-empty divergence note.
    assert s.where_diverged.strip() != ""
    assert s.candidate_id == cand.canonical_id
    assert s.similarity.business_model.score >= 0


async def test_synthesize_derives_dates_from_payload_not_llm() -> None:
    """failure_date and lifespan_months come from CandidatePayload, not the LLM body."""
    cand = _candidate()
    # _payload uses founding_date=2018-01-01, failure_date=2023-01-01 -> 60 months.
    fake_llm = FakeLLMClient(
        canned=_synthesize_canned(
            [cand], _ctx(), text=_synthesis_payload(candidate_id=cand.canonical_id)
        ),
        default_model=_DEFAULT_MODEL,
    )

    s = await synthesize(cand, _ctx(), fake_llm, Config(), model=_DEFAULT_MODEL)

    assert s.failure_date == cand.payload.failure_date
    assert s.lifespan_months == 60


async def test_synthesize_sources_pass_through_from_payload() -> None:
    """``Synthesis.sources`` mirrors ``CandidatePayload.sources``; LLM is bypassed."""
    cand = _candidate(sources=["https://en.wikipedia.org/wiki/Acme", "https://example.com/x"])
    fake_llm = FakeLLMClient(
        canned=_synthesize_canned(
            [cand], _ctx(), text=_synthesis_payload(candidate_id=cand.canonical_id)
        ),
        default_model=_DEFAULT_MODEL,
    )

    s = await synthesize(cand, _ctx(), fake_llm, Config(), model=_DEFAULT_MODEL)

    assert s.sources == cand.payload.sources


async def test_synthesize_sources_empty_when_payload_has_none() -> None:
    """Payload with no URL → empty Synthesis.sources, regardless of LLM output."""
    cand = _candidate(sources=[])
    fake_llm = FakeLLMClient(
        canned=_synthesize_canned(
            [cand], _ctx(), text=_synthesis_payload(candidate_id=cand.canonical_id)
        ),
        default_model=_DEFAULT_MODEL,
    )

    s = await synthesize(cand, _ctx(), fake_llm, Config(), model=_DEFAULT_MODEL)

    assert s.sources == []


def test_synthesize_prompt_kwargs_includes_source() -> None:
    """``synthesize_prompt_kwargs`` surfaces ``payload.source`` so the j2 template can branch."""
    cand = _candidate(source="llm_recall")
    kwargs = synthesize_prompt_kwargs(cand, pitch="x")
    assert kwargs["source"] == "llm_recall"


def test_synthesize_prompt_kwargs_source_unknown_when_payload_missing() -> None:
    """``payload.source=None`` becomes ``"unknown"`` in the prompt context."""
    cand = _candidate(source=None)
    kwargs = synthesize_prompt_kwargs(cand, pitch="x")
    assert kwargs["source"] == "unknown"


def test_synthesize_prompt_kwargs_defaults_deathness_status_to_dead() -> None:
    """``payload.deathness_verdict=None`` defaults to ``"dead"`` in the prompt context.

    Pre-Task-3 crunchbase/web rows have no verdict and were already-curated
    post-mortems, so the prompt's post-mortem framing is the right default.
    """
    cand = _candidate(deathness_verdict=None)
    kwargs = synthesize_prompt_kwargs(cand, pitch="x")
    assert kwargs["deathness_status"] == "dead"


def test_synthesize_prompt_kwargs_passes_struggling_verdict() -> None:
    """L5 ``"struggling"`` admits surface in the prompt context as-is."""
    cand = _candidate(deathness_verdict="struggling")
    kwargs = synthesize_prompt_kwargs(cand, pitch="x")
    assert kwargs["deathness_status"] == "struggling"


async def test_synthesize_struggling_candidate_uses_distress_framing() -> None:
    """A candidate with ``deathness_verdict="struggling"`` round-trips into ``Synthesis``.

    Contract: the rendered prompt carries ``deathness_status: struggling`` and
    the prompt's struggling-framing rule, and the resulting ``Synthesis``
    preserves the verdict so the renderer can pick the forward-looking heading.
    """
    cand = _candidate(deathness_verdict="struggling")
    fake_llm = FakeLLMClient(
        canned=_synthesize_canned(
            [cand], _ctx(), text=_synthesis_payload(candidate_id=cand.canonical_id)
        ),
        default_model=_DEFAULT_MODEL,
    )

    s = await synthesize(cand, _ctx(), fake_llm, Config(), model=_DEFAULT_MODEL)

    assert s.deathness_verdict == "struggling"
    rendered_prompt = fake_llm.calls[0].prompt
    assert "deathness_status: struggling" in rendered_prompt
    # The struggling-framing rule must reach the system block so the LLM can
    # condition on it; "currently operating but visibly impaired" is the
    # rule's load-bearing phrase.
    assert "still operating but visibly impaired" in rendered_prompt


async def test_synthesize_dead_candidate_carries_dead_verdict() -> None:
    """``deathness_verdict="dead"`` round-trips and the prompt reflects "dead"."""
    cand = _candidate(deathness_verdict="dead")
    fake_llm = FakeLLMClient(
        canned=_synthesize_canned(
            [cand], _ctx(), text=_synthesis_payload(candidate_id=cand.canonical_id)
        ),
        default_model=_DEFAULT_MODEL,
    )

    s = await synthesize(cand, _ctx(), fake_llm, Config(), model=_DEFAULT_MODEL)

    assert s.deathness_verdict == "dead"
    assert "deathness_status: dead" in fake_llm.calls[0].prompt


async def test_synthesize_legacy_candidate_defaults_verdict_in_prompt() -> None:
    """Legacy crunchbase/web rows (verdict=None) render as ``deathness_status: dead``.

    The ``Synthesis`` itself keeps ``deathness_verdict=None`` so the renderer
    can distinguish an explicit "dead" admit from a legacy untyped row if it
    ever needs to.
    """
    cand = _candidate(deathness_verdict=None)
    fake_llm = FakeLLMClient(
        canned=_synthesize_canned(
            [cand], _ctx(), text=_synthesis_payload(candidate_id=cand.canonical_id)
        ),
        default_model=_DEFAULT_MODEL,
    )

    s = await synthesize(cand, _ctx(), fake_llm, Config(), model=_DEFAULT_MODEL)

    assert s.deathness_verdict is None
    assert "deathness_status: dead" in fake_llm.calls[0].prompt


async def test_synthesize_threads_source_into_synthesis() -> None:
    """``Synthesis.source`` mirrors ``CandidatePayload.source`` so render can flag llm_recall."""
    cand = _candidate(source="llm_recall")
    fake_llm = FakeLLMClient(
        canned=_synthesize_canned(
            [cand], _ctx(), text=_synthesis_payload(candidate_id=cand.canonical_id)
        ),
        default_model=_DEFAULT_MODEL,
    )

    s = await synthesize(cand, _ctx(), fake_llm, Config(), model=_DEFAULT_MODEL)

    assert s.source == "llm_recall"


async def test_synthesize_lifespan_none_when_dates_unknown() -> None:
    """If the payload lacks one of the dates, lifespan_months collapses to None."""
    payload = _payload()
    # CandidatePayload requires both fields typed; flip founding to unknown.
    payload_dict = payload.model_dump()
    payload_dict["founding_date"] = None
    payload_dict["founding_date_unknown"] = True
    cand = Candidate(
        canonical_id="acme-corp",
        score=0.9,
        payload=CandidatePayload.model_validate(payload_dict),
    )
    fake_llm = FakeLLMClient(
        canned=_synthesize_canned(
            [cand], _ctx(), text=_synthesis_payload(candidate_id=cand.canonical_id)
        ),
        default_model=_DEFAULT_MODEL,
    )

    s = await synthesize(cand, _ctx(), fake_llm, Config(), model=_DEFAULT_MODEL)

    assert s.lifespan_months is None
    assert s.failure_date == cand.payload.failure_date  # still pass-through


async def test_synthesize_all_warms_cache_before_gather() -> None:
    """First call asserted as cache-warm; remaining run via asyncio.gather."""
    cands = [_candidate(canonical_id=f"cand-{i}") for i in range(3)]
    payload = _synthesis_payload(candidate_id="cand-0")
    # One canned entry per rendered (template_sha, model, prompt_hash). The
    # 3-tuple lookup is strict, so each candidate's prompt needs its own key.
    fake_llm = FakeLLMClient(
        canned=_synthesize_canned(cands, _ctx(), text=payload, cache_creation_tokens=10),
        default_model=_DEFAULT_MODEL,
    )

    results = await synthesize_all(cands, _ctx(), fake_llm, Config(), model=_DEFAULT_MODEL)

    assert len(results) == len(cands)
    # First call must have observed cache-creation tokens (warm path).
    assert fake_llm.calls[0].cache is True
    assert all(not isinstance(r, Exception) for r in results)


def _synth_with(cid: str, *, bm: float, mk: float, gtm: float, ss: float):
    from slopmortem.models import (  # noqa: PLC0415
        PerspectiveScore,
        SimilarityScores,
        Synthesis,
    )

    return Synthesis(
        candidate_id=cid,
        name=cid,
        one_liner="x",
        failure_date=None,
        lifespan_months=None,
        similarity=SimilarityScores(
            business_model=PerspectiveScore(score=bm, rationale="x"),
            market=PerspectiveScore(score=mk, rationale="x"),
            gtm=PerspectiveScore(score=gtm, rationale="x"),
            stage_scale=PerspectiveScore(score=ss, rationale="x"),
        ),
        why_similar="x",
        where_diverged="x",
        failure_causes=["x"],
        lessons_for_input=["x"],
        sources=[],
    )


def test_drop_below_min_similarity_drops_below_threshold() -> None:
    from slopmortem.stages.synthesize import drop_below_min_similarity  # noqa: PLC0415

    syntheses = [
        _synth_with("strong", bm=7.0, mk=6.0, gtm=5.0, ss=4.0),  # mean = 5.5
        _synth_with("synth_disagreed", bm=2.0, mk=2.0, gtm=1.0, ss=3.0),  # mean = 2.0
    ]
    kept, dropped = drop_below_min_similarity(syntheses, min_similarity=4.0)
    assert [s.candidate_id for s in kept] == ["strong"]
    assert dropped == 1


def test_drop_below_min_similarity_zero_dropped_when_all_pass() -> None:
    from slopmortem.stages.synthesize import drop_below_min_similarity  # noqa: PLC0415

    syntheses = [_synth_with("strong", bm=7.0, mk=6.0, gtm=5.0, ss=4.0)]
    kept, dropped = drop_below_min_similarity(syntheses, min_similarity=4.0)
    assert kept == syntheses
    assert dropped == 0
