"""Unit tests for ``HaikuPitchFiller`` — the LLM-driven enricher for URL-only stubs."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from slopmortem.budget import Budget
from slopmortem.ingest import HaikuPitchFiller
from slopmortem.llm.client import CompletionResult
from slopmortem.models import RawEntry

_TEST_MODEL = "anthropic/claude-haiku-4.5"


@dataclass
class _StubLLM:
    """Records call kwargs and returns a canned response.

    Avoids ``FakeLLMClient``'s ``(template_sha, model, prompt_hash)`` keying so
    each test can pre-build its canned text without computing hashes.
    """

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
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return CompletionResult(text=self.text, stop_reason="stop")


def _make_entry(
    *,
    title: str | None = "Lytro is shutting down",
    url: str | None = "https://blog.lytro.com/farewell-from-lytro",
    markdown_text: str | None = None,
    raw_html: str | None = None,
) -> RawEntry:
    return RawEntry(
        source="hn_algolia",
        source_id="42",
        url=url,
        title=title,
        raw_html=raw_html,
        markdown_text=markdown_text,
        fetched_at=datetime.now(UTC),
    )


def _filler(*, llm: _StubLLM, budget: Budget | None = None) -> HaikuPitchFiller:
    return HaikuPitchFiller(
        llm=llm,
        model=_TEST_MODEL,
        budget=budget or Budget(cap_usd=1.0),
        max_tokens=1500,
        max_chars_per_result=2500,
    )


def _high_response(pitch: str = "## Lytro\n\nLight-field cameras...") -> str:
    return json.dumps(
        {
            "pitch_markdown": pitch,
            "confidence": "high",
            "source_urls": ["https://example.com/post-mortem"],
            "entity_match_reason": "Founder post on the original blog domain.",
        }
    )


@pytest.mark.anyio
async def test_high_confidence_populates_markdown_and_sets_synthesized() -> None:
    llm = _StubLLM(text=_high_response("## Lytro\n\nThe pitch body."))
    out = await _filler(llm=llm).enrich(_make_entry())
    assert out.markdown_text == "## Lytro\n\nThe pitch body."
    assert out.synthesized is True
    assert len(llm.calls) == 1


@pytest.mark.anyio
async def test_low_confidence_returns_entry_untouched() -> None:
    llm = _StubLLM(
        text=json.dumps(
            {
                "pitch_markdown": "",
                "confidence": "low",
                "source_urls": [],
                "entity_match_reason": "Could not disambiguate the entity.",
            }
        )
    )
    entry = _make_entry()
    out = await _filler(llm=llm).enrich(entry)
    assert out is entry or (out.markdown_text is None and out.synthesized is False)
    assert len(llm.calls) == 1


@pytest.mark.anyio
async def test_medium_confidence_returns_entry_untouched() -> None:
    """The gate is high-only — medium is not accepted."""
    llm = _StubLLM(
        text=json.dumps(
            {
                "pitch_markdown": "## maybe lytro",
                "confidence": "medium",
                "source_urls": ["https://example.com"],
                "entity_match_reason": "Some signal but not airtight.",
            }
        )
    )
    out = await _filler(llm=llm).enrich(_make_entry())
    assert out.markdown_text is None
    assert out.synthesized is False


@pytest.mark.anyio
async def test_prefilled_markdown_text_skips_llm_call() -> None:
    llm = _StubLLM(text=_high_response())
    entry = _make_entry(markdown_text="already has a body")
    out = await _filler(llm=llm).enrich(entry)
    assert out is entry
    assert llm.calls == []


@pytest.mark.anyio
async def test_prefilled_raw_html_skips_llm_call() -> None:
    llm = _StubLLM(text=_high_response())
    entry = _make_entry(raw_html="<html>body</html>")
    out = await _filler(llm=llm).enrich(entry)
    assert out is entry
    assert llm.calls == []


@pytest.mark.anyio
async def test_missing_title_skips_llm_call() -> None:
    llm = _StubLLM(text=_high_response())
    entry = _make_entry(title=None)
    out = await _filler(llm=llm).enrich(entry)
    assert out is entry
    assert llm.calls == []


@pytest.mark.anyio
async def test_missing_url_skips_llm_call() -> None:
    llm = _StubLLM(text=_high_response())
    entry = _make_entry(url=None)
    out = await _filler(llm=llm).enrich(entry)
    assert out is entry
    assert llm.calls == []


@pytest.mark.anyio
async def test_malformed_json_logs_warning_and_returns_entry_untouched(
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm = _StubLLM(text="not even json")
    entry = _make_entry()
    with caplog.at_level(logging.WARNING, logger="slopmortem.ingest._pitch_filler"):
        out = await _filler(llm=llm).enrich(entry)
    assert out.markdown_text is None
    assert out.synthesized is False
    assert any("malformed output" in rec.message for rec in caplog.records)


@pytest.mark.anyio
async def test_budget_exhausted_skips_without_calling_llm() -> None:
    llm = _StubLLM(text=_high_response())
    budget = Budget(cap_usd=1.0)
    budget.spent_usd = 1.0  # exhausted
    out = await _filler(llm=llm, budget=budget).enrich(_make_entry())
    assert out.markdown_text is None
    assert llm.calls == []


@pytest.mark.anyio
async def test_passes_prompt_template_sha_in_extra_body() -> None:
    """Cache invalidation depends on the template SHA travelling on every call."""
    llm = _StubLLM(text=_high_response())
    await _filler(llm=llm).enrich(_make_entry())
    extra = llm.calls[0]["extra_body"]
    assert isinstance(extra, dict)
    assert "prompt_template_sha" in extra
    assert isinstance(extra["prompt_template_sha"], str)
    assert len(extra["prompt_template_sha"]) == 16


@pytest.mark.anyio
async def test_passes_tavily_search_tool() -> None:
    llm = _StubLLM(text=_high_response())
    await _filler(llm=llm).enrich(_make_entry())
    tools = llm.calls[0]["tools"]
    assert isinstance(tools, list)
    assert len(tools) == 1
    assert tools[0].name == "tavily_search"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_filler_construction_does_not_render_prompt() -> None:
    """Construction is cheap — rendering only happens on enrich()."""
    HaikuPitchFiller(
        llm=_StubLLM(),
        model=_TEST_MODEL,
        budget=Budget(cap_usd=1.0),
    )
