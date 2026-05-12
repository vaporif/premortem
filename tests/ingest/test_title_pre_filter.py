"""Unit tests for ``HaikuTitlePreFilter`` — cheap title-only Haiku gate."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from slopmortem.budget import Budget
from slopmortem.ingest import HaikuTitlePreFilter
from slopmortem.llm import OpenRouterCompletionError
from slopmortem.llm.client import CompletionResult
from slopmortem.models import RawEntry

_TEST_MODEL = "anthropic/claude-haiku-4.5"


@dataclass
class _StubLLM:
    text: str = ""
    raise_exc: BaseException | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

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
        self.calls.append(
            {
                "prompt": prompt,
                "system": system,
                "tools": tools,
                "model": model,
                "cache": cache,
                "response_format": response_format,
                "extra_body": extra_body,
                "max_tokens": max_tokens,
                "single_tool_call": single_tool_call,
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return CompletionResult(text=self.text, stop_reason="stop")


def _make_entry(
    *,
    title: str | None = "Lytro is shutting down",
    url: str | None = "https://blog.lytro.com/farewell",
    markdown_text: str | None = None,
    raw_html: str | None = None,
) -> RawEntry:
    return RawEntry(
        source="hn_algolia",
        source_id="42",
        url=url,
        title=title,
        markdown_text=markdown_text,
        raw_html=raw_html,
        fetched_at=datetime.now(UTC),
    )


def _filter(*, llm: _StubLLM) -> HaikuTitlePreFilter:
    return HaikuTitlePreFilter(
        llm=llm,
        model=_TEST_MODEL,
        budget=Budget(cap_usd=1.0),
        max_tokens=16,
    )


@pytest.mark.anyio
async def test_yes_passes_entry_through_unchanged() -> None:
    llm = _StubLLM(text=json.dumps({"decision": "yes"}))
    out = await _filter(llm=llm).enrich(_make_entry())
    assert out.title_pre_filter_rejected is False
    assert len(llm.calls) == 1


@pytest.mark.anyio
async def test_no_sets_rejected_flag() -> None:
    llm = _StubLLM(text=json.dumps({"decision": "no"}))
    out = await _filter(llm=llm).enrich(_make_entry(title="Show HN: my new startup"))
    assert out.title_pre_filter_rejected is True
    assert len(llm.calls) == 1


@pytest.mark.anyio
async def test_skips_when_body_pre_filled() -> None:
    llm = _StubLLM(text=json.dumps({"decision": "no"}))
    out = await _filter(llm=llm).enrich(_make_entry(markdown_text="already enriched"))
    assert out.title_pre_filter_rejected is False
    assert len(llm.calls) == 0


@pytest.mark.anyio
async def test_skips_when_raw_html_pre_filled() -> None:
    """A populated raw_html means a body fetcher already ran; don't re-gate."""
    llm = _StubLLM(text=json.dumps({"decision": "no"}))
    out = await _filter(llm=llm).enrich(_make_entry(raw_html="<p>already fetched</p>"))
    assert out.title_pre_filter_rejected is False
    assert len(llm.calls) == 0


@pytest.mark.anyio
async def test_skips_when_title_missing() -> None:
    llm = _StubLLM(text=json.dumps({"decision": "no"}))
    out = await _filter(llm=llm).enrich(_make_entry(title=None))
    assert out.title_pre_filter_rejected is False
    assert len(llm.calls) == 0


@pytest.mark.anyio
async def test_malformed_json_returns_entry_unchanged() -> None:
    llm = _StubLLM(text="not json at all")
    out = await _filter(llm=llm).enrich(_make_entry())
    assert out.title_pre_filter_rejected is False
    assert len(llm.calls) == 1


@pytest.mark.anyio
async def test_invalid_decision_value_returns_entry_unchanged() -> None:
    llm = _StubLLM(text=json.dumps({"decision": "maybe"}))
    out = await _filter(llm=llm).enrich(_make_entry())
    assert out.title_pre_filter_rejected is False
    assert len(llm.calls) == 1


@pytest.mark.anyio
async def test_budget_exhausted_skips() -> None:
    llm = _StubLLM(text=json.dumps({"decision": "no"}))
    budget = Budget(cap_usd=0.0)  # nothing remaining
    f = HaikuTitlePreFilter(llm=llm, model=_TEST_MODEL, budget=budget, max_tokens=16)
    out = await f.enrich(_make_entry())
    assert out.title_pre_filter_rejected is False
    assert len(llm.calls) == 0


@pytest.mark.anyio
async def test_llm_completion_error_returns_entry_unchanged() -> None:
    llm = _StubLLM(raise_exc=OpenRouterCompletionError("boom", reason="hard_stop"))
    out = await _filter(llm=llm).enrich(_make_entry())
    assert out.title_pre_filter_rejected is False


@pytest.mark.anyio
async def test_budget_exceeded_propagates() -> None:
    from slopmortem.budget import BudgetExceededError  # noqa: PLC0415

    llm = _StubLLM(raise_exc=BudgetExceededError("over"))
    with pytest.raises(BudgetExceededError):
        await _filter(llm=llm).enrich(_make_entry())


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
