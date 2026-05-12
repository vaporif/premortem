"""Tests for the ``recall(...)`` public entrypoint composition.

These are entrypoint-level tests — verifier internals are covered in
``test_verify*.py`` and brainstorm internals in ``test_brainstorm.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from slopmortem.llm import CompletionResult
from slopmortem.models import Facets
from slopmortem.recall import (
    DeathnessConfig,
    RecallConfig,
    RecallDeps,
    recall,
)

if TYPE_CHECKING:
    from slopmortem.corpus.tavily import TavilyHit


_RECALL_MODEL = "recall-model"
_FACET_MODEL = "facet-model"
_DEATHNESS_MODEL = "deathness-model"


def _facets() -> Facets:
    return Facets(
        sector="crypto_web3",
        business_model="services_consulting",
        customer_type="enterprise",
        geography="global",
        monetization="services_layer",
    )


@dataclass
class _RoutedLLM:
    """Routes ``complete`` by ``model`` to a canned text response.

    A stub keyed on ``model`` keeps tests below the FakeLLMClient
    ``(template_sha, model, prompt_hash)`` ceremony.
    """

    responses: dict[str, str]
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
        del cache, response_format, single_tool_call
        self.calls.append(
            {
                "prompt": prompt,
                "system": system,
                "tools": tools,
                "model": model,
                "extra_body": extra_body,
                "max_tokens": max_tokens,
            }
        )
        text = self.responses.get(model or "")
        if text is None:
            msg = f"no canned response for model {model!r}"
            raise RuntimeError(msg)
        return CompletionResult(text=text, stop_reason="stop")


@dataclass
class _FakeTavilySearch:
    hits: list[TavilyHit] = field(default_factory=list)
    raises_for: set[str] = field(default_factory=set)

    async def __call__(self, query: str, limit: int) -> list[TavilyHit]:
        del limit
        if any(token in query for token in self.raises_for):
            msg = "simulated tavily failure"
            raise httpx.HTTPError(msg)
        return list(self.hits)


@dataclass
class _FakeExtract:
    body: str = ""

    async def __call__(self, url: str) -> str:
        del url
        return self.body


@dataclass
class _FakeWayback:
    async def enrich(self, entry: Any) -> Any:
        return entry


def _empty_brainstorm() -> str:
    return json.dumps({"suggestions": []})


def _facets_response() -> str:
    payload = _facets().model_dump()
    return json.dumps(payload)


def _config(suggestion_cap: int = 8) -> RecallConfig:
    return RecallConfig(
        model_facet=_FACET_MODEL,
        max_tokens_facet=512,
        model_recall=_RECALL_MODEL,
        max_tokens_recall=2048,
        suggestion_cap=suggestion_cap,
        tools=[],
        max_tavily_calls=2,
        tavily_max_results=5,
        deathness=DeathnessConfig(
            model=_DEATHNESS_MODEL,
            max_tokens=256,
            min_confidence=0.7,
            struggling_min_confidence=0.85,
        ),
    )


def _deps(llm: _RoutedLLM) -> RecallDeps:
    return RecallDeps(
        llm=llm,
        tavily_search=_FakeTavilySearch(),
        extract=_FakeExtract(),
        wayback=_FakeWayback(),
    )


async def test_recall_passes_pre_extracted_facets_through_without_re_extracting() -> None:
    """When ``facets=`` is passed, ``recall`` skips the internal facet extract."""
    # Intentionally only seed the recall route. If facet extract fires, the
    # stub raises RuntimeError on the missing model.
    llm = _RoutedLLM(responses={_RECALL_MODEL: _empty_brainstorm()})
    out = await recall(
        "a pitch",
        facets=_facets(),
        deps=_deps(llm),
        config=_config(),
    )
    assert out == []
    assert all(call["model"] != _FACET_MODEL for call in llm.calls)


async def test_recall_extracts_facets_when_none_passed() -> None:
    """``facets=None`` triggers one extra Haiku call to extract facets internally."""
    llm = _RoutedLLM(
        responses={
            _FACET_MODEL: _facets_response(),
            _RECALL_MODEL: _empty_brainstorm(),
        }
    )
    out = await recall("a pitch", facets=None, deps=_deps(llm), config=_config())
    assert out == []
    assert any(call["model"] == _FACET_MODEL for call in llm.calls)


async def test_recall_treats_none_prior_hints_as_empty() -> None:
    """``prior_hints=None`` renders the prompt template's empty-hints branch.

    Equivalent to ``prior_hints=[]`` — both produce the
    "(none — corpus returned no in-vertical matches)" string.
    """
    llm_none = _RoutedLLM(responses={_RECALL_MODEL: _empty_brainstorm()})
    _ = await recall(
        "a pitch",
        facets=_facets(),
        prior_hints=None,
        deps=_deps(llm_none),
        config=_config(),
    )

    llm_empty = _RoutedLLM(responses={_RECALL_MODEL: _empty_brainstorm()})
    _ = await recall(
        "a pitch",
        facets=_facets(),
        prior_hints=[],
        deps=_deps(llm_empty),
        config=_config(),
    )

    recall_calls_none = [c for c in llm_none.calls if c["model"] == _RECALL_MODEL]
    recall_calls_empty = [c for c in llm_empty.calls if c["model"] == _RECALL_MODEL]
    assert len(recall_calls_none) == 1
    assert len(recall_calls_empty) == 1
    assert recall_calls_none[0]["prompt"] == recall_calls_empty[0]["prompt"]


async def test_recall_returns_empty_when_brainstorm_returns_no_suggestions() -> None:
    """Brainstorm returned no suggestions — recall short-circuits before verify."""
    llm = _RoutedLLM(responses={_RECALL_MODEL: _empty_brainstorm()})
    deps = _deps(llm)
    out = await recall("a pitch", facets=_facets(), deps=deps, config=_config())
    assert out == []
    # No L5 deathness route was set, so a call to it would fail. Asserting
    # absence here pins that verify_all is not invoked when suggestions = [].
    assert all(c["model"] != _DEATHNESS_MODEL for c in llm.calls)


async def test_recall_returns_empty_when_verifier_drops_all() -> None:
    """One suggestion, but L0 tavily returns nothing → suggestion dropped pre-L2."""
    payload = {
        "suggestions": [
            {
                "name": "DropMe",
                "category": "Fintech",
                "status": "dead",
                "homepage_url": "https://dropme.example.com",
                "evidence_url": None,
                "first_year_active": 2018,
                "last_year_active": 2022,
                "ceased_year": 2022,
                "what_they_built": "B2B invoicing tool.",
                "why_failed": "ran out of runway.",
                "ai_confidence": 0.9,
            }
        ]
    }
    llm = _RoutedLLM(responses={_RECALL_MODEL: json.dumps(payload)})
    # FakeTavilySearch with empty hits drops at L0.
    out = await recall("a pitch", facets=_facets(), deps=_deps(llm), config=_config())
    assert out == []


async def test_recall_isolates_per_suggestion_transport_failures() -> None:
    """Two suggestions; one's Tavily search raises; the other still drops at L0.

    Both come back empty because the fake tavily has no hits anyway, but the
    httpx.HTTPError raised for the first suggestion must not abort the second
    — proven by ``out`` being a list (not the raise propagating out).
    """
    payload = {
        "suggestions": [
            {
                "name": "Raiser",
                "category": "Fintech",
                "status": "dead",
                "homepage_url": "https://raiser.example.com",
                "evidence_url": None,
                "first_year_active": 2018,
                "last_year_active": 2022,
                "ceased_year": 2022,
                "what_they_built": "B2B invoicing tool.",
                "why_failed": "ran out of runway.",
                "ai_confidence": 0.9,
            },
            {
                "name": "Quiet",
                "category": "Fintech",
                "status": "dead",
                "homepage_url": "https://quiet.example.com",
                "evidence_url": None,
                "first_year_active": 2018,
                "last_year_active": 2022,
                "ceased_year": 2022,
                "what_they_built": "Another B2B invoicing tool.",
                "why_failed": "no traction.",
                "ai_confidence": 0.9,
            },
        ]
    }
    llm = _RoutedLLM(responses={_RECALL_MODEL: json.dumps(payload)})
    deps = RecallDeps(
        llm=llm,
        tavily_search=_FakeTavilySearch(hits=[], raises_for={"Raiser"}),
        extract=_FakeExtract(),
        wayback=_FakeWayback(),
    )
    out = await recall("a pitch", facets=_facets(), deps=deps, config=_config())
    # Both suggestions return nothing: Raiser raises L0, Quiet has no hits.
    # Critical: the exception did NOT propagate up.
    assert out == []
