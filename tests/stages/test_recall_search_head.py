"""Tests for ``stages.recall_verify._search_for_evidence`` (L0 Tavily head)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from slopmortem.corpus.tavily import TavilyHit
from slopmortem.models import RecallSuggestion
from slopmortem.stages import recall_verify as _rv
from slopmortem.stages.recall_verify import (
    _build_status_shaped_query as _status_shaped_query,
)
from slopmortem.stages.recall_verify import (
    _search_for_evidence,
)
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


async def test_drops_on_zero_hits_both_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both status-shaped and status-blind queries return 0 hits — drop with no_name_match.

    Pass-1 ``no_hits`` collapses into the consolidated ``no_name_match`` event
    so the dashboard sees one final reason per dropped suggestion.
    """
    events = _capture_events(monkeypatch)
    sug = _suggestion()
    fake = FakeTavilySearch(default=[])
    out = await _search_for_evidence(sug, tavily_search=fake, limit=5)
    assert out == []
    assert events == [(SpanEvent.RECALL_REJECTED_NO_EVIDENCE, {"reason": "no_name_match"})]
    assert len(fake.calls) == 2


async def test_drops_when_no_hit_mentions_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hits exist but none contain the company name — both passes run, then drop."""
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
    assert out == []
    assert events == [(SpanEvent.RECALL_REJECTED_NO_EVIDENCE, {"reason": "no_name_match"})]
    assert len(fake.calls) == 2


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
    assert out[0] == "https://news.example.com/b"
    assert events == []
    # Pass 1 found a primary hit — pass 2 must not run.
    assert len(fake.calls) == 1


class _RaisingTavilySearch:
    """Fake whose ``__call__`` raises a transport error to exercise the L0 except branch."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, q: str, limit: int) -> list[TavilyHit]:
        self.calls.append((q, limit))
        raise self._exc


async def test_drops_on_both_passes_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tavily raises on both passes — emit ONE ``reason=transport_error`` event."""
    events = _capture_events(monkeypatch)
    sug = _suggestion()
    fake = _RaisingTavilySearch(httpx.ConnectError("boom"))
    out = await _search_for_evidence(sug, tavily_search=fake, limit=5)
    assert out == []
    assert events == [(SpanEvent.RECALL_REJECTED_NO_EVIDENCE, {"reason": "transport_error"})]
    assert len(fake.calls) == 2


async def test_returns_fallback_hit_with_name_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """No hit has a death keyword; fallback name-only hit lands AFTER pass 2 also fires.

    Short-circuit triggers on a primary (death-keyword) hit. Pass 1 here returns
    name-matching hits without a death keyword, so pass 2 runs to look for a
    better candidate. With nothing better in pass 2, the original first hit wins.
    """
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
    ]
    fake = FakeTavilySearch(default=hits)
    out = await _search_for_evidence(sug, tavily_search=fake, limit=5)
    assert out[0] == "https://news.example.com/first"
    assert (SpanEvent.RECALL_L0_NAME_ONLY_FALLBACK_RECOVERED, {}) in events
    assert len(fake.calls) == 2


async def test_fallback_prefers_third_party_over_self_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """External hits beat project-controlled pages in the fallback rank.

    Neither hit carries a death keyword, so the primary path doesn't fire. The
    external-host hit ranks ahead of the github.com/<name> hit.
    """
    _ = _capture_events(monkeypatch)
    sug = _suggestion()
    hits = [
        TavilyHit(
            title="Hexagate codebase",
            url="https://github.com/hexagate",
            snippet="The Hexagate organization on GitHub.",
        ),
        TavilyHit(
            title="Hexagate covered in industry recap",
            url="https://news.example.com/recap",
            snippet="Hexagate, a Web3 security firm, made the year-end roundup.",
        ),
    ]
    fake = FakeTavilySearch(default=hits)
    out = await _search_for_evidence(sug, tavily_search=fake, limit=5)
    assert out[0] == "https://news.example.com/recap"


async def test_fallback_uses_self_published_when_no_third_party_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If every name-matching hit is project-controlled, the self-published fallback still lands."""
    _ = _capture_events(monkeypatch)
    sug = _suggestion()
    hits = [
        TavilyHit(
            title="Hexagate codebase",
            url="https://github.com/hexagate",
            snippet="The Hexagate organization on GitHub.",
        ),
        TavilyHit(
            title="Hexagate docs",
            url="https://docs.hexagate.io/",
            snippet="Hexagate developer documentation.",
        ),
    ]
    fake = FakeTavilySearch(default=hits)
    out = await _search_for_evidence(sug, tavily_search=fake, limit=5)
    # First-encountered self-published hit wins when no external one exists.
    assert out[0] == "https://github.com/hexagate"


# ---------------------------------------------------------------------------
# Two-pass L0 shape (Fix 2): pass-1 status-shaped → pass-2 status-blind
# ---------------------------------------------------------------------------


def _status_blind_query(sug: RecallSuggestion) -> str:
    return f'"{sug.name}" {sug.category} {sug.failure_year}'


async def test_l0_falls_back_to_status_blind_query_when_status_shaped_returns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass 1 returns 0 hits; pass 2 (status-blind) finds a name match."""
    events = _capture_events(monkeypatch)
    sug = _suggestion()
    fallback_hit = TavilyHit(
        title="Hexagate raises Series B",
        url="https://news.example.com/series-b",
        snippet="Hexagate, a Web3 security firm, raised a Series B this quarter.",
    )
    fake = FakeTavilySearch(
        response_map={
            _status_shaped_query(sug): [],
            _status_blind_query(sug): [fallback_hit],
        }
    )
    out = await _search_for_evidence(sug, tavily_search=fake, limit=5)
    assert out[0] == fallback_hit.url
    assert events == [(SpanEvent.RECALL_L0_NAME_ONLY_FALLBACK_RECOVERED, {})]
    assert len(fake.calls) == 2
    assert fake.calls[0][0] == _status_shaped_query(sug)
    assert fake.calls[1][0] == _status_blind_query(sug)


async def test_l0_returns_pass1_result_when_status_shaped_finds_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass 1 finds a primary hit; pass 2 must not fire."""
    events = _capture_events(monkeypatch)
    sug = _suggestion()
    primary_hit = TavilyHit(
        title="Hexagate shuts down operations",
        url="https://news.example.com/primary",
        snippet="Hexagate announced its shutdown in late 2024 after losing clients.",
    )
    fake = FakeTavilySearch(
        response_map={
            _status_shaped_query(sug): [primary_hit],
            # If pass 2 fires by accident, the URL will differ and the assert below catches it.
            _status_blind_query(sug): [
                TavilyHit(
                    title="Hexagate launches new tier",
                    url="https://news.example.com/should-not-be-used",
                    snippet="Hexagate, a Web3 security firm, expanded its enterprise offering.",
                )
            ],
        }
    )
    out = await _search_for_evidence(sug, tavily_search=fake, limit=5)
    assert out[0] == primary_hit.url
    assert events == []
    assert len(fake.calls) == 1


async def test_l0_drops_when_both_passes_return_no_name_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both queries return hits, none mention the name — drop with no_name_match."""
    events = _capture_events(monkeypatch)
    sug = _suggestion()
    unrelated = [
        TavilyHit(
            title="Some other startup folds",
            url="https://news.example.com/a",
            snippet="A startup unrelated to the subject went under this week.",
        ),
    ]
    fake = FakeTavilySearch(
        response_map={
            _status_shaped_query(sug): unrelated,
            _status_blind_query(sug): unrelated,
        }
    )
    out = await _search_for_evidence(sug, tavily_search=fake, limit=5)
    assert out == []
    assert events == [(SpanEvent.RECALL_REJECTED_NO_EVIDENCE, {"reason": "no_name_match"})]
    assert len(fake.calls) == 2


async def test_l0_fallback_on_pass1_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass 1 raises a transport error; pass 2 recovers — no rejection event fires."""
    events = _capture_events(monkeypatch)
    sug = _suggestion()
    fallback_hit = TavilyHit(
        title="Hexagate raises Series B",
        url="https://news.example.com/recovered",
        snippet="Hexagate, a Web3 security firm, raised a Series B this quarter.",
    )
    pass1_query = _status_shaped_query(sug)
    pass2_query = _status_blind_query(sug)

    calls: list[str] = []

    async def fake_search(q: str, _limit: int) -> list[TavilyHit]:
        calls.append(q)
        if q == pass1_query:
            err = httpx.ConnectError("boom")
            raise err
        if q == pass2_query:
            return [fallback_hit]
        msg = f"unexpected query: {q!r}"
        raise AssertionError(msg)

    out = await _search_for_evidence(sug, tavily_search=fake_search, limit=5)
    assert out[0] == fallback_hit.url
    assert events == [(SpanEvent.RECALL_L0_NAME_ONLY_FALLBACK_RECOVERED, {})]
    assert calls == [pass1_query, pass2_query]


async def test_l0_drops_when_pass2_transport_error_after_pass1_no_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass 1 returns 0 hits, pass 2 raises — emit transport_error (signals Tavily down)."""
    events = _capture_events(monkeypatch)
    sug = _suggestion()
    pass1_query = _status_shaped_query(sug)
    pass2_query = _status_blind_query(sug)

    async def fake_search(q: str, _limit: int) -> list[TavilyHit]:
        if q == pass1_query:
            return []
        if q == pass2_query:
            err = httpx.ConnectError("tavily down")
            raise err
        msg = f"unexpected query: {q!r}"
        raise AssertionError(msg)

    out = await _search_for_evidence(sug, tavily_search=fake_search, limit=5)
    assert out == []
    assert events == [(SpanEvent.RECALL_REJECTED_NO_EVIDENCE, {"reason": "transport_error"})]


async def test_l0_places_evidence_url_first_then_gathers_backups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Opus supplies ``evidence_url``, it ranks first; Tavily still runs for backups.

    L2/L3 walking is the resilience layer — if Opus's pick fails (anti-bot host,
    deleted page), the verifier needs alternatives. Tavily's backups land after
    the LLM-provided URL in the candidate list.
    """
    events = _capture_events(monkeypatch)
    pre_discovered = "https://news.example.com/hexagate-shutdown"
    sug = RecallSuggestion(
        name="Hexagate",
        category="Web3 security",
        status="dead",
        homepage_url="https://hexagate.example.com/",
        evidence_url=pre_discovered,
        failure_year=2024,
        one_liner="Hexagate shut down in 2024.",
    )
    backup_hit = TavilyHit(
        title="Hexagate shuts down operations",
        url="https://news.example.com/hexagate-retro",
        snippet="Hexagate, a Web3 security firm, announced its shutdown last week.",
    )
    fake = FakeTavilySearch(default=[backup_hit])
    out = await _search_for_evidence(sug, tavily_search=fake, limit=5)
    assert out[0] == pre_discovered
    assert backup_hit.url in out
    # Tavily ran pass 1; pass 2 short-circuited because pass 1 had a primary (death-keyword) hit.
    assert len(fake.calls) == 1
    # The LLM-provided event still fires.
    assert (SpanEvent.RECALL_L0_PROVIDED_BY_RECALL_LLM, {}) in events
