"""Tests for the L3 Tavily ``/extract`` fallback in ``stages.recall_verify``.

The fallback fires when direct GET on a citation URL either 4xx's (Medium
returns 403 to our user-agent, etc.) or returns a body too short to admit
(decrypt.co's Next.js SPA shell extracts to ~50 chars). On any other drop
shape — anchor failures — extract won't change what's in the body, so the
verifier short-circuits to today's drop behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from slopmortem.llm.client import CompletionResult
from slopmortem.models import RawEntry, RecallSuggestion
from slopmortem.stages import recall_verify as _rv
from slopmortem.stages.recall_verify import (
    DeathnessConfig,
    verify_suggestion,
)
from slopmortem.tracing import SpanEvent
from tests.stages.test_recall_search_head import FakeTavilyExtract

if TYPE_CHECKING:
    import pytest


_DEATHNESS_PASS = '{"verdict": "dead", "confidence": 0.95, "evidence_quote": "shut down"}'  # noqa: S105 - JSON literal, not a credential
_DEATHNESS = DeathnessConfig(
    model="test-haiku",
    max_tokens=128,
    min_confidence=0.7,
    struggling_min_confidence=0.85,
)

_FILLER_SENTENCE = (
    "The board cited prolonged headwinds, falling renewal rates, and a stalled "
    "fundraising process as the proximate causes. Customers were notified by "
    "email and given ninety days to migrate. Vendors and contractors were "
    "instructed to file claims through the trustee. "
)


def _article_html(lead: str) -> str:
    """Build a ``<main><article>`` body that trafilatura keeps as main content."""
    return (
        "<html><body><main><article><p>"
        + lead
        + " "
        + (_FILLER_SENTENCE * 5)
        + "</p></article></main></body></html>"
    )


def _plain_body(lead: str) -> str:
    """Build a long-enough plain-text body suitable for Tavily's ``raw_content``.

    The L3 fallback re-runs ``_l3_classify`` on the extract output, which calls
    ``extract_clean``. trafilatura accepts plain text wrapped in minimal HTML;
    the filler keeps the body above the 500-char floor without changing the
    keyword surface.
    """
    return _article_html(lead)


@dataclass
class _FakeLLM:
    """Minimal LLM stub; returns ``default`` or raises on missing fixture."""

    default: str | None = None
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
            {"prompt": prompt, "system": system, "model": model, "max_tokens": max_tokens}
        )
        if self.default is not None:
            return CompletionResult(text=self.default, stop_reason="stop")
        msg = "L5 LLM was reached but a prior gate should have rejected first"
        raise AssertionError(msg)


class _FakeWayback:
    """Pass-through enricher: returns the seed entry unchanged."""

    async def enrich(self, entry: RawEntry) -> RawEntry:
        return entry


class _FakeResp:
    def __init__(self, *, status: int = 200, text: str = "") -> None:
        self.status_code = status
        self.text = text


def _patch_http(
    monkeypatch: pytest.MonkeyPatch,
    *,
    head_responses: dict[str, _FakeResp | BaseException],
    get_responses: dict[str, _FakeResp | BaseException],
) -> None:
    async def fake_head(url: str, **_kw: object) -> _FakeResp:
        if url not in head_responses:
            msg = f"unexpected HEAD: {url}"
            raise AssertionError(msg)
        item = head_responses[url]
        if isinstance(item, BaseException):
            raise item
        return item

    async def fake_get(url: str, **_kw: object) -> _FakeResp:
        if url not in get_responses:
            msg = f"unexpected GET: {url}"
            raise AssertionError(msg)
        item = get_responses[url]
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr("slopmortem.stages.recall_verify.safe_head", fake_head)
    monkeypatch.setattr("slopmortem.stages.recall_verify.safe_get", fake_get)


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[SpanEvent, dict[str, str]]]:
    events: list[tuple[SpanEvent, dict[str, str]]] = []

    def stub(event: SpanEvent, attributes: dict[str, str] | None = None) -> None:
        events.append((event, dict(attributes) if attributes else {}))

    monkeypatch.setattr(_rv, "_emit_event", stub)
    return events


def _suggestion(name: str = "Nomad") -> RecallSuggestion:
    slug = name.lower().replace(" ", "-")
    return RecallSuggestion(
        name=name,
        category="Web3 bridge",
        status="dead",
        homepage_url=f"https://{slug}.test/",
        failure_year=2024,
        one_liner=f"{name} bridge wound down in 2024.",
    )


def _discovered(name: str = "Nomad") -> str:
    slug = name.lower().replace(" ", "-")
    return f"https://medium.example/{slug}-post-mortem"


async def test_l3_extract_fallback_recovers_on_l2_get_4xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Medium 403 on GET → Tavily extract returns a real body → admit + emit recovery event."""
    events = _capture_events(monkeypatch)
    sug = _suggestion()
    discovered = _discovered(sug.name)
    extracted_body = _plain_body("Nomad Bridge shut down operations following the 2022 hack.")
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=403)},
        get_responses={discovered: _FakeResp(status=403)},
    )
    extract = FakeTavilyExtract(default=extracted_body)
    out = await verify_suggestion(
        sug,
        discovered_urls=[discovered],
        wayback=_FakeWayback(),
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        extract=extract,
        deathness=_DEATHNESS,
    )
    assert out is not None
    assert extract.calls == [discovered]
    recovery = [e for e in events if e[0] is SpanEvent.RECALL_L3_EXTRACT_FALLBACK_RECOVERED]
    assert recovery == [
        (SpanEvent.RECALL_L3_EXTRACT_FALLBACK_RECOVERED, {"reason": "l2_get_4xx"}),
    ]
    # The L2 drop event must NOT fire when the fallback recovered.
    assert not any(e[0] is SpanEvent.RECALL_REJECTED_L2 for e in events)


async def test_l3_extract_fallback_recovers_on_body_too_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPA shell → trafilatura returns ~0 chars → Tavily extract renders the article."""
    events = _capture_events(monkeypatch)
    sug = _suggestion("Chainalysis")
    discovered = _discovered(sug.name)
    # 200 OK with an empty SPA shell — extract_clean returns "" (below floor).
    spa_shell = '<html><body><div id="root"></div></body></html>'
    extracted_body = _plain_body(
        "Chainalysis CEO on leave during company restructuring after layoffs across the firm."
    )
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=spa_shell)},
    )
    extract = FakeTavilyExtract(default=extracted_body)
    out = await verify_suggestion(
        sug,
        discovered_urls=[discovered],
        wayback=_FakeWayback(),
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        extract=extract,
        deathness=_DEATHNESS,
    )
    assert out is not None
    assert extract.calls == [discovered]
    # Primary drop event MUST fire too (trace consumers join on it) and the
    # recovery event records that the fallback rescued the candidate anyway.
    assert (SpanEvent.RECALL_REJECTED_L3_BODY_TOO_SHORT, {}) in events
    assert (
        SpanEvent.RECALL_L3_EXTRACT_FALLBACK_RECOVERED,
        {"reason": "l3_body_too_short"},
    ) in events


async def test_l3_extract_fallback_drops_when_extract_also_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET 403 + extract returns "" → drop. Original L2 event fires; no recovery event."""
    events = _capture_events(monkeypatch)
    sug = _suggestion()
    discovered = _discovered(sug.name)
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=403)},
        get_responses={discovered: _FakeResp(status=403)},
    )
    extract = FakeTavilyExtract(default="")
    out = await verify_suggestion(
        sug,
        discovered_urls=[discovered],
        wayback=_FakeWayback(),
        llm=_FakeLLM(),  # blocker: L5 must not run
        extract=extract,
        deathness=_DEATHNESS,
    )
    assert out is None
    assert extract.calls == [discovered]
    assert not any(e[0] is SpanEvent.RECALL_L3_EXTRACT_FALLBACK_RECOVERED for e in events)
    assert any(e[0] is SpanEvent.RECALL_REJECTED_L2 for e in events)


async def test_l3_extract_fallback_drops_when_extract_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET 403 + extract raises ``httpx.ConnectError`` → drop. No recovery event."""
    events = _capture_events(monkeypatch)
    sug = _suggestion()
    discovered = _discovered(sug.name)
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=403)},
        get_responses={discovered: _FakeResp(status=403)},
    )
    extract = FakeTavilyExtract(default=httpx.ConnectError("boom"))
    out = await verify_suggestion(
        sug,
        discovered_urls=[discovered],
        wayback=_FakeWayback(),
        llm=_FakeLLM(),
        extract=extract,
        deathness=_DEATHNESS,
    )
    assert out is None
    assert extract.calls == [discovered]
    assert not any(e[0] is SpanEvent.RECALL_L3_EXTRACT_FALLBACK_RECOVERED for e in events)
    assert any(e[0] is SpanEvent.RECALL_REJECTED_L2 for e in events)


async def test_l3_no_extract_fallback_on_name_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real article body lacks the suggestion's name → drop; extract not called.

    Extract returning a rendered version of the same article wouldn't change
    which words appear in the body, so the fallback short-circuits for anchor
    failures.
    """
    events = _capture_events(monkeypatch)
    sug = _suggestion()
    discovered = _discovered(sug.name)
    body = _article_html("A small bridge protocol quietly shutdown last week.")
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=body)},
    )
    extract = FakeTavilyExtract(default="should not be called")
    out = await verify_suggestion(
        sug,
        discovered_urls=[discovered],
        wayback=_FakeWayback(),
        llm=_FakeLLM(),
        extract=extract,
        deathness=_DEATHNESS,
    )
    assert out is None
    assert extract.calls == []
    assert any(e[0] is SpanEvent.RECALL_REJECTED_L3_NAME_MISSING for e in events)
    assert not any(e[0] is SpanEvent.RECALL_L3_EXTRACT_FALLBACK_RECOVERED for e in events)
