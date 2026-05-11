"""L3 hygiene tests for ``stages.recall_verify``.

Cover the four failure/admit modes Task 1 hardens:
- body shorter than the extract-clean floor (paywall stub) → drop
- article name only mentioned in sidebar/related copy → drop
- substring matches inside unrelated words (``enclosed``/``disclosed``) → drop
- expanded vocabulary (``shuttered``, ``Chapter 11``, ``wind down``) → admit
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from slopmortem.llm.client import CompletionResult
from slopmortem.models import RawEntry, RecallSuggestion
from slopmortem.stages.recall_verify import (
    DeathnessConfig,
    verify_suggestion,
)
from tests.stages.test_recall_search_head import FakeTavilyExtract

if TYPE_CHECKING:
    import pytest


_DEATHNESS_PASS = '{"verdict": "dead", "confidence": 0.95, "evidence_quote": "shuttered"}'  # noqa: S105 - JSON literal, not a credential
_DEATHNESS = DeathnessConfig(
    model="test-haiku",
    max_tokens=128,
    min_confidence=0.7,
    struggling_min_confidence=0.85,
)
# Default extract fake: returns "" so any L3 fallback call drops without
# recovering. These tests don't exercise the extract path; the fake exists
# to satisfy the required ``extract=`` kwarg.
_NEVER_EXTRACT = FakeTavilyExtract()

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "recall"
PAYWALL_HTML = (_FIXTURES / "paywall_stub.html").read_text()
SIDEBAR_HTML = (_FIXTURES / "sidebar_bleed.html").read_text()


@dataclass
class _FakeLLM:
    """Mirror the helper in ``test_recall_verify.py``.

    Either queues canned replies in ``responses`` or returns ``default`` on
    every call. Empty queue with no default raises so a test fails loudly
    rather than silently passing on a missing fixture. ``call_blocker()``
    builds an instance that explodes the moment L5 is reached.
    """

    responses: list[str | BaseException] = field(default_factory=list)
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
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, BaseException):
                raise item
            return CompletionResult(text=item, stop_reason="stop")
        if self.default is not None:
            return CompletionResult(text=self.default, stop_reason="stop")
        msg = "L5 LLM was reached but L3 should have rejected first"
        raise AssertionError(msg)


def _call_blocker() -> _FakeLLM:
    return _FakeLLM()


class _FakeWayback:
    """Pass-through enricher: returns the seed entry unchanged."""

    def __init__(self) -> None:
        self.calls: list[RawEntry] = []

    async def enrich(self, entry: RawEntry) -> RawEntry:
        self.calls.append(entry)
        return entry


class _FakeResp:
    def __init__(self, *, status: int = 200, text: str = "") -> None:
        self.status_code = status
        self.text = text


def _patch_http(
    monkeypatch: pytest.MonkeyPatch,
    *,
    head_responses: dict[str, _FakeResp],
    get_responses: dict[str, _FakeResp],
) -> None:
    async def fake_head(url: str, **_kw: object) -> _FakeResp:
        if url not in head_responses:
            msg = f"unexpected HEAD: {url}"
            raise AssertionError(msg)
        return head_responses[url]

    async def fake_get(url: str, **_kw: object) -> _FakeResp:
        if url not in get_responses:
            msg = f"unexpected GET: {url}"
            raise AssertionError(msg)
        return get_responses[url]

    monkeypatch.setattr("slopmortem.stages.recall_verify.safe_head", fake_head)
    monkeypatch.setattr("slopmortem.stages.recall_verify.safe_get", fake_get)


def _suggestion(name: str = "Acme") -> RecallSuggestion:
    slug = name.lower().replace(" ", "-")
    return RecallSuggestion(
        name=name,
        category="security",
        status="dead",
        homepage_url=f"https://{slug}.test/",
        failure_year=2023,
        one_liner=f"{name} shut down.",
    )


def _discovered(name: str = "Acme") -> str:
    """Stand-in for the L0 Tavily-discovered article URL — keyed off the name."""
    slug = name.lower().replace(" ", "-")
    return f"https://news.example/{slug}"


async def test_l3_drops_when_body_under_500_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paywall stub: extracted body is empty, L3 must reject before L5."""
    sug = _suggestion()
    discovered = _discovered(sug.name)
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=PAYWALL_HTML)},
    )
    out = await verify_suggestion(
        sug,
        discovered_urls=[discovered],
        wayback=_FakeWayback(),
        llm=_call_blocker(),
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is None


async def test_l3_rejects_when_name_only_in_sidebar(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trafilatura strips the ``<aside>`` so ``Acme`` no longer appears in body."""
    sug = _suggestion()
    discovered = _discovered(sug.name)
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=SIDEBAR_HTML)},
    )
    out = await verify_suggestion(
        sug,
        discovered_urls=[discovered],
        wayback=_FakeWayback(),
        llm=_call_blocker(),
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is None


async def test_l3_does_not_match_substring_inside_word(monkeypatch: pytest.MonkeyPatch) -> None:
    """``enclosed``/``disclosed`` contain ``closed`` as substring but aren't death keywords."""
    sug = _suggestion()
    discovered = _discovered(sug.name)
    lead = (
        "<html><body><main><p>Acme's quarterly disclosed financials enclosed in this "
        "release show steady growth. The team enclosed a multi-year roadmap and disclosed "
        "no material liabilities. "
    )
    body = lead + ("Filler content. " * 60) + "</p></main></body></html>"
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=body)},
    )
    out = await verify_suggestion(
        sug,
        discovered_urls=[discovered],
        wayback=_FakeWayback(),
        llm=_call_blocker(),
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is None


async def test_l3_admits_shuttered_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expanded vocabulary: ``shuttered`` and ``wind down`` admit through L3."""
    sug = _suggestion()
    discovered = _discovered(sug.name)
    lead = (
        "<html><body><main><p>Acme Security shuttered last month after failing to "
        "raise a Series B. The thirty-person company had been struggling for two "
        "quarters before the board voted to wind down operations. "
    )
    body = lead + ("More detail. " * 80) + "</p></main></body></html>"
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=body)},
    )
    llm = _FakeLLM(default=_DEATHNESS_PASS)
    out = await verify_suggestion(
        sug,
        discovered_urls=[discovered],
        wayback=_FakeWayback(),
        llm=llm,
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is not None
    assert len(llm.calls) == 1


async def test_l3_admits_chapter_eleven(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multi-word ``Chapter 11`` matches across the whitespace gap."""
    sug = _suggestion()
    discovered = _discovered(sug.name)
    lead = (
        "<html><body><main><p>Acme Health filed for Chapter 11 protection in March "
        "after eighteen months of negative cash flow. The 200-person staff received "
        "WARN-act notices the same day. "
    )
    body = lead + ("Background detail. " * 80) + "</p></main></body></html>"
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=body)},
    )
    llm = _FakeLLM(default=_DEATHNESS_PASS)
    out = await verify_suggestion(
        sug,
        discovered_urls=[discovered],
        wayback=_FakeWayback(),
        llm=llm,
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is not None


async def test_l3_anchors_on_crypto_native_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body using crypto-native death vocabulary (no traditional keywords) clears L3.

    Regression for the live-trace finding that Nomad Bridge's Medium obituary
    used "hacked"/"exploited"/"drained" instead of "shutdown"/"bankrupt", so
    the L3 anchor check needs to recognize those.
    """
    sug = _suggestion("Nomad Bridge")
    discovered = _discovered("Nomad Bridge")
    lead = (
        "<html><body><main><p>Nomad Bridge was a cross-chain communication protocol "
        "that got hacked in August 2022, drained for nearly $190M. The team "
        "eventually disbanded and the protocol is now dead in the water. "
    )
    body = lead + ("Background detail. " * 80) + "</p></main></body></html>"
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=body)},
    )
    out = await verify_suggestion(
        sug,
        discovered_urls=[discovered],
        wayback=_FakeWayback(),
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is not None
