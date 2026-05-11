"""Tests for ``stages.recall_verify._search_for_evidence`` (L0 Tavily head)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from slopmortem.corpus.tavily import TavilyHit
from slopmortem.models import RecallSuggestion
from slopmortem.stages import recall_verify as _rv
from slopmortem.stages.recall_verify import _search_for_evidence
from slopmortem.tracing import SpanEvent

if TYPE_CHECKING:
    import pytest


class FakeTavilySearch:
    """Fake for ``TavilySearchFn`` — returns canned hits per query.

    ``response_map`` keys on the exact query string so a test can assert
    the query template branched correctly on ``status``. Tests that don't
    care about the query string set ``default`` instead and ignore
    ``calls``.
    """

    def __init__(
        self,
        response_map: dict[str, list[TavilyHit]] | None = None,
        default: list[TavilyHit] | None = None,
    ) -> None:
        self.response_map = response_map or {}
        self.default = default or []
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, q: str, limit: int) -> list[TavilyHit]:
        self.calls.append((q, limit))
        return self.response_map.get(q, self.default)[:limit]


class FakeTavilyExtract:
    """Fake for ``ExtractFn`` — returns a canned body (or raises) per URL.

    ``response_map`` keys on the exact URL the verifier hands to extract;
    ``default`` covers URLs the test doesn't care to enumerate. A queued
    ``BaseException`` is raised instead of returned, mirroring the live
    extract surface's contract (``httpx.HTTPError`` / ``SSRFBlockedError`` /
    ``RuntimeError`` on missing key).
    """

    def __init__(
        self,
        response_map: dict[str, str | BaseException] | None = None,
        default: str | BaseException = "",
    ) -> None:
        self.response_map = response_map or {}
        self.default = default
        self.calls: list[str] = []

    async def __call__(self, url: str) -> str:
        self.calls.append(url)
        result = self.response_map.get(url, self.default)
        if isinstance(result, BaseException):
            raise result
        return result


def _suggestion(name: str = "Hexagate") -> RecallSuggestion:
    return RecallSuggestion(
        name=name,
        category="Web3 security",
        status="dead",
        homepage_url=f"https://{name.lower()}.example.com/",
        failure_year=2024,
        one_liner=f"{name} shut down in 2024.",
    )


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[SpanEvent, dict[str, str]]]:
    """Replace the no-op ``_emit_event`` with a list-appending stub for one test.

    Mirrors the pattern in ``test_synthesize_injection_defense.py`` /
    ``test_consolidate_risks.py``. Attributes default to ``{}`` so equality
    assertions don't have to distinguish None from empty.
    """
    events: list[tuple[SpanEvent, dict[str, str]]] = []

    def stub(event: SpanEvent, attributes: dict[str, str] | None = None) -> None:
        events.append((event, dict(attributes) if attributes else {}))

    monkeypatch.setattr(_rv, "_emit_event", stub)
    return events


async def test_drops_on_zero_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Search returned no results — emit ``reason=no_hits`` and drop."""
    events = _capture_events(monkeypatch)
    sug = _suggestion()
    fake = FakeTavilySearch(default=[])
    out = await _search_for_evidence(sug, tavily_search=fake, limit=5)
    assert out is None
    assert events == [(SpanEvent.RECALL_REJECTED_NO_EVIDENCE, {"reason": "no_hits"})]
    assert len(fake.calls) == 1


async def test_drops_when_no_hit_mentions_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three hits, none containing the company name in title or snippet."""
    events = _capture_events(monkeypatch)
    sug = _suggestion()
    hits = [
        TavilyHit(
            title="Unrelated startup folds",
            url="https://news.example.com/a",
            snippet="An unrelated startup announced its shutdown today.",
        ),
        TavilyHit(
            title="Sector update",
            url="https://news.example.com/b",
            snippet="The Web3 security space saw layoffs across several firms.",
        ),
        TavilyHit(
            title="Market roundup",
            url="https://news.example.com/c",
            snippet="A broad piece on bankruptcies across crypto in 2024.",
        ),
    ]
    fake = FakeTavilySearch(default=hits)
    out = await _search_for_evidence(sug, tavily_search=fake, limit=5)
    assert out is None
    assert events == [(SpanEvent.RECALL_REJECTED_NO_EVIDENCE, {"reason": "no_name_match"})]


async def test_returns_primary_hit_with_name_and_death_keyword(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second hit has both the name and a death keyword — returned as primary."""
    events = _capture_events(monkeypatch)
    sug = _suggestion()
    hits = [
        TavilyHit(
            title="Crypto roundup",
            url="https://news.example.com/a",
            snippet="A mention of unrelated funding news.",
        ),
        TavilyHit(
            title="Hexagate shuts down operations",
            url="https://news.example.com/b",
            snippet="Hexagate announced its shutdown in late 2024 after losing clients.",
        ),
        TavilyHit(
            title="Hexagate retrospective",
            url="https://news.example.com/c",
            snippet="A look back at Hexagate's product roadmap from earlier years.",
        ),
    ]
    fake = FakeTavilySearch(default=hits)
    out = await _search_for_evidence(sug, tavily_search=fake, limit=5)
    assert out == "https://news.example.com/b"
    assert events == []


class _RaisingTavilySearch:
    """Fake whose ``__call__`` raises a transport error to exercise the L0 except branch."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def __call__(self, q: str, limit: int) -> list[TavilyHit]:
        raise self._exc


async def test_drops_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tavily raises ``httpx.ConnectError`` — emit ``reason=transport_error`` and drop."""
    events = _capture_events(monkeypatch)
    sug = _suggestion()
    fake = _RaisingTavilySearch(httpx.ConnectError("boom"))
    out = await _search_for_evidence(sug, tavily_search=fake, limit=5)
    assert out is None
    assert events == [(SpanEvent.RECALL_REJECTED_NO_EVIDENCE, {"reason": "transport_error"})]


async def test_returns_fallback_hit_with_name_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """No hit has a death keyword; first name-matching hit wins as fallback."""
    events = _capture_events(monkeypatch)
    sug = _suggestion()
    hits = [
        TavilyHit(
            title="Hexagate launches new audit tier",
            url="https://news.example.com/first",
            snippet="Hexagate, a Web3 security firm, expanded its enterprise offering.",
        ),
        TavilyHit(
            title="Crypto sector roundup",
            url="https://news.example.com/second",
            snippet="A broad overview of recent fundraising rounds in security.",
        ),
        TavilyHit(
            title="Unrelated industry report",
            url="https://news.example.com/third",
            snippet="DeFi metrics for the quarter, including TVL and active addresses.",
        ),
    ]
    fake = FakeTavilySearch(default=hits)
    out = await _search_for_evidence(sug, tavily_search=fake, limit=5)
    assert out == "https://news.example.com/first"
    assert events == []
