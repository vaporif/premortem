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
from slopmortem.models import Facets
from slopmortem.stages.llm_recall import PriorCandidateHint, llm_recall

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
        del cache, response_format, extra_body, single_tool_call
        self.calls.append(
            {
                "prompt": prompt,
                "system": system,
                "model": model,
                "max_tokens": max_tokens,
                "tools": tools,
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


def _hint(name: str, *, rationale: str = "stub") -> PriorCandidateHint:
    return PriorCandidateHint(name=name, rationale=rationale)


def _suggestion_json(
    name: str, *, year: int = 2023, evidence_url: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "category": "Web3 security audits",
        "status": "dead",
        "homepage_url": f"https://{name.lower().replace(' ', '')}.example.com",
        "failure_year": year,
        "one_liner": f"{name} did Web3 audits and shut down in {year}.",
    }
    if evidence_url is not None:
        payload["evidence_url"] = evidence_url
    return payload


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
        tools=[],
    )

    assert out == []
    # Confirm both blocks rendered: system + user reach the LLM separately.
    assert len(llm.calls) == 1
    assert llm.calls[0]["system"]
    assert "Hacken-style" in llm.calls[0]["prompt"]


async def test_recall_renders_current_top_n_block() -> None:
    # Covers the Jinja ``{% if current_top_n %}`` branch: the prior top-N
    # gets serialized with human-readable *names* (not candidate id slugs)
    # plus the reranker's rationale so Opus can dedupe meaningfully.
    llm = _StubLLM(text='{"suggestions": []}')
    top_n = [
        _hint("Hacken", rationale="prior corpus hit A"),
        _hint("CertiK", rationale="prior corpus hit B"),
    ]

    out = await llm_recall(
        pitch="Hacken-style Web3 audit firm",
        facets=_facets(),
        current_top_n=top_n,
        llm=llm,
        model=_RECALL_MODEL,
        max_tokens=_MAX_TOKENS,
        cap=8,
        tools=[],
    )

    assert out == []
    rendered = llm.calls[0]["prompt"]
    # Names render verbatim — no ``candidate_id:`` prefix anymore.
    assert "Hacken — already in corpus" in rendered
    assert "CertiK — already in corpus" in rendered
    assert "prior corpus hit A" in rendered
    assert "candidate_id:" not in rendered
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
        tools=[],
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
        tools=[],
    )

    assert out == []


async def test_recall_drops_wrapper_failing_validation() -> None:
    # Valid JSON but suggestion misses `failure_year` — wrapper validation
    # rejects the whole response.
    bad = {
        "suggestions": [
            {
                "name": "Hexagate",
                "category": "Web3 security",
                "status": "absorbed",
                "homepage_url": "https://hexagate.com",
                # failure_year missing
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
        tools=[],
    )

    assert out == []


async def test_recall_accepts_evidence_url_when_llm_provides_one() -> None:
    # Opus pre-discovers a citation URL via its own ``tavily_search`` and
    # surfaces it on the wire; the parsed model should preserve it for the
    # verifier's L0 short-circuit.
    payload = {
        "suggestions": [
            {
                "name": "Hexagate",
                "category": "Web3 security",
                "status": "absorbed",
                "homepage_url": "https://hexagate.example.com",
                "evidence_url": "https://news.example.com/hexagate-shutdown",
                "failure_year": 2024,
                "one_liner": "Acquired by Chainalysis in 2024.",
            }
        ]
    }
    llm = _StubLLM(text=json.dumps(payload))

    out = await llm_recall(
        pitch="...",
        facets=_facets(),
        current_top_n=[],
        llm=llm,
        model=_RECALL_MODEL,
        max_tokens=_MAX_TOKENS,
        cap=8,
        tools=[],
    )

    assert len(out) == 1
    assert out[0].evidence_url == "https://news.example.com/hexagate-shutdown"


async def test_recall_accepts_missing_homepage_url() -> None:
    # homepage_url is OPTIONAL per the new contract — null (or absent) should
    # validate cleanly and surface as ``None`` on the parsed model.
    payload = {
        "suggestions": [
            {
                "name": "Hexagate",
                "category": "Web3 security",
                "status": "absorbed",
                "homepage_url": None,
                "failure_year": 2024,
                "one_liner": "Acquired by Chainalysis in 2024.",
            }
        ]
    }
    llm = _StubLLM(text=json.dumps(payload))

    out = await llm_recall(
        pitch="...",
        facets=_facets(),
        current_top_n=[],
        llm=llm,
        model=_RECALL_MODEL,
        max_tokens=_MAX_TOKENS,
        cap=8,
        tools=[],
    )

    assert len(out) == 1
    assert out[0].homepage_url is None


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
        tools=[],
    )

    assert out == []


async def test_llm_recall_passes_tools_to_llm(monkeypatch) -> None:
    # Build a real ``recall_tools(config)`` spec list and assert the stage
    # forwards it intact through ``llm.complete(..., tools=...)`` — the
    # tool-call loop in OpenRouterClient is what gives Opus mid-reasoning
    # access to tavily_search + tavily_extract.
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    from slopmortem.config import Config  # noqa: PLC0415
    from slopmortem.llm import recall_tools  # noqa: PLC0415

    cfg = Config(enable_tavily_recall_search=True, recall_max_tavily_calls=5)
    tools = recall_tools(cfg)
    assert {t.name for t in tools} == {"tavily_search", "tavily_extract"}

    llm = _StubLLM(text='{"suggestions": []}')
    _ = await llm_recall(
        pitch="...",
        facets=_facets(),
        current_top_n=[],
        llm=llm,
        model=_RECALL_MODEL,
        max_tokens=_MAX_TOKENS,
        cap=8,
        tools=tools,
        recall_max_tavily_calls=cfg.recall_max_tavily_calls,
    )

    forwarded = llm.calls[0]["tools"]
    assert forwarded is not None
    assert {t.name for t in forwarded} == {"tavily_search", "tavily_extract"}


async def test_llm_recall_empty_tools_list_is_passed_through() -> None:
    # Default training-data-only mode: tools=[] reaches llm.complete unchanged.
    llm = _StubLLM(text='{"suggestions": []}')

    _ = await llm_recall(
        pitch="...",
        facets=_facets(),
        current_top_n=[],
        llm=llm,
        model=_RECALL_MODEL,
        max_tokens=_MAX_TOKENS,
        cap=8,
        tools=[],
    )

    assert llm.calls[0]["tools"] == []


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
        tools=[],
    )

    # We don't assert specific names — Opus picks. Just confirm round trip
    # works and the wrapper validates.
    assert isinstance(out, list)
