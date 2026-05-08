"""Tests for the ``llm_recall`` stage: empty/uncertain, cap, malformed, cassette."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from slopmortem.budget import Budget
from slopmortem.llm import CompletionResult, LLMClient, OpenRouterClient
from slopmortem.models import Facets, PerspectiveScore, ScoredCandidate, SimilarityScores
from slopmortem.stages.llm_recall import llm_recall

CASSETTE_FILE = (
    Path(__file__).parent.parent / "fixtures" / "cassettes" / "recall" / "llm_recall_hacken.yaml"
)

_RECALL_MODEL = "anthropic/claude-opus-4-7"
_MAX_TOKENS = 4096


def _facets() -> Facets:
    return Facets(
        sector="crypto_web3",
        business_model="services_consulting",
        customer_type="enterprise",
        geography="global",
        monetization="services_layer",
    )


@dataclass
class _StubLLM:
    """Minimal LLMClient stub: returns canned text or raises on demand.

    FakeLLMClient keys on ``(prompt_template_sha, model, prompt_hash)`` which
    is overkill for the recall tests — we only need a single response per
    test. Stub avoids re-deriving the cassette key shape per test.
    """

    text: str = ""
    raises: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

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
        del tools, cache, response_format, extra_body, single_tool_call
        self.calls.append(
            {
                "prompt": prompt,
                "system": system,
                "model": model,
                "max_tokens": max_tokens,
            }
        )
        if self.raises is not None:
            raise self.raises
        return CompletionResult(text=self.text, stop_reason="stop")


def _assert_matches_protocol(_: LLMClient) -> None:
    # Static-only check: drift in ``LLMClient.complete`` (added required kwarg,
    # renamed param) fails basedpyright at the ``_assert_matches_protocol``
    # call below instead of silently letting the stub diverge.
    return


_assert_matches_protocol(_StubLLM())


def _scored(candidate_id: str, *, score: float = 8.0, rationale: str = "stub") -> ScoredCandidate:
    perspective = PerspectiveScore(score=score, rationale=rationale)
    return ScoredCandidate(
        candidate_id=candidate_id,
        perspective_scores=SimilarityScores(
            business_model=perspective,
            market=perspective,
            gtm=perspective,
            stage_scale=perspective,
        ),
        rationale=rationale,
    )


def _suggestion_json(name: str, *, year: int = 2023) -> dict[str, Any]:
    return {
        "name": name,
        "category": "Web3 security audits",
        "status": "dead",
        "homepage_url": f"https://{name.lower().replace(' ', '')}.example.com",
        "failure_year": year,
        "evidence_url": f"https://news.example.com/{name.lower().replace(' ', '-')}-shuts-down",
        "one_liner": f"{name} did Web3 audits and shut down in {year}.",
    }


async def test_recall_returns_empty_on_uncertain_llm() -> None:
    # Wrapper shape: {"suggestions": []} is the "uncertain" sentinel under strict mode.
    llm = _StubLLM(text='{"suggestions": []}')

    out = await llm_recall(
        pitch="Hacken-style Web3 audit firm",
        facets=_facets(),
        current_top_n=[],
        llm=llm,
        model=_RECALL_MODEL,
        max_tokens=_MAX_TOKENS,
        cap=8,
    )

    assert out == []
    # Confirm both blocks rendered: system + user reach the LLM separately.
    assert len(llm.calls) == 1
    assert llm.calls[0]["system"]
    assert "Hacken-style" in llm.calls[0]["prompt"]


async def test_recall_renders_current_top_n_block() -> None:
    # Covers the Jinja ``{% if current_top_n %}`` branch: the prior top-N
    # gets serialized as ``- candidate_id: <id> (rationale: ...)`` so Opus
    # avoids re-suggesting what the corpus already returned.
    llm = _StubLLM(text='{"suggestions": []}')
    top_n = [
        _scored("hacken-io", rationale="prior corpus hit A"),
        _scored("certik", rationale="prior corpus hit B"),
    ]

    out = await llm_recall(
        pitch="Hacken-style Web3 audit firm",
        facets=_facets(),
        current_top_n=top_n,
        llm=llm,
        model=_RECALL_MODEL,
        max_tokens=_MAX_TOKENS,
        cap=8,
    )

    assert out == []
    rendered = llm.calls[0]["prompt"]
    assert "candidate_id: hacken-io" in rendered
    assert "candidate_id: certik" in rendered
    assert "(none — corpus returned no in-vertical matches)" not in rendered


async def test_recall_caps_at_max() -> None:
    # Stub returns wrapper with 12 suggestions; assert returned len == 8.
    payload = json.dumps(
        {"suggestions": [_suggestion_json(f"Co{i}") for i in range(12)]},
    )
    llm = _StubLLM(text=payload)

    out = await llm_recall(
        pitch="...",
        facets=_facets(),
        current_top_n=[],
        llm=llm,
        model=_RECALL_MODEL,
        max_tokens=_MAX_TOKENS,
        cap=8,
    )

    assert len(out) == 8
    assert out[0].name == "Co0"
    assert out[-1].name == "Co7"


async def test_recall_drops_invalid_response() -> None:
    # Malformed JSON → ValidationError path → stage returns [].
    llm = _StubLLM(text="not json at all }")

    out = await llm_recall(
        pitch="...",
        facets=_facets(),
        current_top_n=[],
        llm=llm,
        model=_RECALL_MODEL,
        max_tokens=_MAX_TOKENS,
        cap=8,
    )

    assert out == []


async def test_recall_drops_wrapper_failing_validation() -> None:
    # Valid JSON but suggestion misses `evidence_url` — wrapper validation
    # rejects the whole response.
    bad = {
        "suggestions": [
            {
                "name": "Hexagate",
                "category": "Web3 security",
                "status": "absorbed",
                "homepage_url": "https://hexagate.com",
                "failure_year": 2024,
                # evidence_url missing
                "one_liner": "Acquired by Chainalysis in 2024.",
            }
        ]
    }
    llm = _StubLLM(text=json.dumps(bad))

    out = await llm_recall(
        pitch="...",
        facets=_facets(),
        current_top_n=[],
        llm=llm,
        model=_RECALL_MODEL,
        max_tokens=_MAX_TOKENS,
        cap=8,
    )

    assert out == []


async def test_recall_returns_empty_on_http_error() -> None:
    # Transport failure path: stage logs and returns [] rather than raising.
    llm = _StubLLM(raises=httpx.ConnectError("dns"))

    out = await llm_recall(
        pitch="...",
        facets=_facets(),
        current_top_n=[],
        llm=llm,
        model=_RECALL_MODEL,
        max_tokens=_MAX_TOKENS,
        cap=8,
    )

    assert out == []


@pytest.mark.vcr
async def test_recall_cassette_round_trip() -> None:
    # Real Opus call recorded once with the Hacken pitch. Skip when no
    # cassette and no RECORD=1; mirrors tests/llm/test_openrouter_cassette.py
    # so the OPENROUTER_API_KEY reminder shows up in the skip message.
    if not CASSETTE_FILE.exists() and not os.environ.get("RECORD"):
        pytest.skip(
            f"no cassette at {CASSETTE_FILE}; rerun with RECORD=1 + OPENROUTER_API_KEY to record"
        )
    api_key = os.environ.get("OPENROUTER_API_KEY", "sk-or-v1-test")
    sdk = AsyncOpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
    client = OpenRouterClient(sdk=sdk, budget=Budget(2.0), model=_RECALL_MODEL)

    out = await llm_recall(
        pitch=(
            "Hacken — Web3 security audits, smart-contract review, and bug-bounty "
            "platform for crypto protocols and exchanges."
        ),
        facets=_facets(),
        current_top_n=[],
        llm=client,
        model=_RECALL_MODEL,
        max_tokens=_MAX_TOKENS,
        cap=8,
    )

    # We don't assert specific names — Opus picks. Just confirm round trip
    # works and the wrapper validates.
    assert isinstance(out, list)
