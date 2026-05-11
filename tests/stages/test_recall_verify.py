"""Tests for ``stages.recall_verify``: L1-L5 gates plus fan-out isolation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import httpx

from slopmortem.corpus import extract_clean
from slopmortem.corpus.tavily import TavilyHit
from slopmortem.http import SSRFBlockedError
from slopmortem.llm.client import CompletionResult
from slopmortem.models import RawEntry, RecallSuggestion
from slopmortem.stages import recall_verify as _rv
from slopmortem.stages.recall_verify import (
    _DEATH_KEYWORDS,
    DeathnessConfig,
    VerificationTier,
    _build_status_shaped_query,
    verify_and_persist_all,
    verify_suggestion,
)
from slopmortem.tracing import SpanEvent
from tests.stages.test_recall_search_head import FakeTavilyExtract, FakeTavilySearch

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import pytest


_DEATHNESS_PASS = '{"verdict": "dead", "confidence": 0.95, "evidence_quote": "shut down"}'  # noqa: S105 - JSON literal, not a credential
_DEATHNESS = DeathnessConfig(
    model="test-haiku",
    max_tokens=128,
    min_confidence=0.7,
    struggling_min_confidence=0.85,
)
# Default extract fake: returns "" so any L3 fallback call drops without
# recovering. Most tests in this file don't exercise the extract path; the
# fake exists to satisfy the required ``extract=`` kwarg.
_NEVER_EXTRACT = FakeTavilyExtract()
# Pad sentence used to push trafilatura output past the 500-char ``LENGTH_FLOOR``
# without changing the load-bearing keyword tokens each test exercises.
_FILLER_SENTENCE = (
    "The board cited prolonged headwinds, falling renewal rates, and a stalled "
    "fundraising process as the proximate causes. Customers were notified by "
    "email and given ninety days to migrate. Vendors and contractors were "
    "instructed to file claims through the trustee. "
)


def _article_html(lead: str) -> str:
    """Build a ``<main><article>`` body that trafilatura keeps as main content.

    ``lead`` is the sentence(s) carrying the test's keyword surface; the
    filler runs five times so the extracted body stays well above 500 chars.
    """
    return (
        "<html><body><main><article><p>"
        + lead
        + " "
        + (_FILLER_SENTENCE * 5)
        + "</p></article></main></body></html>"
    )


@dataclass
class _FakeLLM:
    """Minimal LLMClient stub: returns a queued reply or raises the queued exc.

    ``FakeLLMClient`` keys on ``(prompt_template_sha, model, prompt_hash)``;
    these tests want sequenced replies per call without computing hashes,
    so a local stub is simpler than threading canned-key fixtures.

    Each call pops one entry off ``responses``; an empty queue raises so a
    test can't accidentally pass on a missing fixture. Tests that only need
    one L5 call queue exactly one entry. ``_FakeLLM(default=_DEATHNESS_PASS)``
    is the L1-L4 happy-path companion so existing tests don't have to
    queue a deathness reply they don't care about.
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
        msg = "no response queued"
        raise AssertionError(msg)


def _suggestion(name: str = "Hexagate") -> RecallSuggestion:
    return RecallSuggestion(
        name=name,
        category="Web3 security",
        status="dead",
        homepage_url=f"https://{name.lower()}.example.com/",
        failure_year=2024,
        one_liner=f"{name} shut down in 2024.",
    )


def _discovered(name: str = "Hexagate") -> str:
    """Stand-in for the L0 Tavily-discovered article URL.

    The Task 3 cutover stripped ``RecallSuggestion.evidence_url``; tests
    that used to read ``sug.evidence_url`` now compute the same shape from
    the suggestion name so fixtures key off the same string the verifier
    receives via ``discovered_url=``. Hostname matches the sibling L2/L3/L5
    test files so URL fixtures read uniformly across the recall_verify
    suite.
    """
    return f"https://news.example/{name.lower()}-shutdown"


class _FakeWayback:
    """Stand-in for ``WaybackEnricher`` driven by a per-test response map.

    ``enriched_text`` is what ``enrich`` returns as ``markdown_text``; ``None``
    leaves the seed entry untouched (mirrors the real enricher's behaviour
    when no snapshot exists or extraction is empty). ``raises`` simulates a
    transient IA outage.
    """

    def __init__(
        self,
        *,
        enriched_text: str | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.enriched_text = enriched_text
        self.raises = raises
        self.calls: list[RawEntry] = []

    async def enrich(self, entry: RawEntry) -> RawEntry:
        self.calls.append(entry)
        if self.raises is not None:
            raise self.raises
        if self.enriched_text is None:
            return entry
        return entry.model_copy(update={"markdown_text": self.enriched_text})


class _FakeResp:
    def __init__(self, *, status: int = 200, text: str = "") -> None:
        self.status_code = status
        self.text = text


def _patch_http(
    monkeypatch: pytest.MonkeyPatch,
    *,
    head_responses: dict[str, _FakeResp | BaseException] | None = None,
    get_responses: dict[str, _FakeResp | BaseException] | None = None,
) -> None:
    """Replace the ``safe_head`` / ``safe_get`` symbols imported by recall_verify.

    Routes per-URL: a missing entry raises ``AssertionError`` so tests can't
    silently pass on URLs they didn't expect to be probed.
    """
    head_map = head_responses or {}
    get_map = get_responses or {}

    async def fake_head(url: str, **_kw: object) -> _FakeResp:
        if url not in head_map:
            msg = f"unexpected HEAD: {url}"
            raise AssertionError(msg)
        item = head_map[url]
        if isinstance(item, BaseException):
            raise item
        return item

    async def fake_get(url: str, **_kw: object) -> _FakeResp:
        if url not in get_map:
            msg = f"unexpected GET: {url}"
            raise AssertionError(msg)
        item = get_map[url]
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr("slopmortem.stages.recall_verify.safe_head", fake_head)
    monkeypatch.setattr("slopmortem.stages.recall_verify.safe_get", fake_get)


async def test_l2_rejects_404_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """HEAD 404 falls through to GET; GET 404 drops at the L2 GET stage."""
    sug = _suggestion()
    discovered = _discovered(sug.name)
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=404)},
        get_responses={discovered: _FakeResp(status=404)},
    )
    wb = _FakeWayback()
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is None
    assert wb.calls == []


async def test_l2_rejects_ssrf_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSRFBlockedError on the evidence URL drops at the L2 GET stage."""
    sug = _suggestion()
    discovered = _discovered(sug.name)
    _patch_http(
        monkeypatch,
        head_responses={discovered: SSRFBlockedError("nope")},
        get_responses={discovered: SSRFBlockedError("nope")},
    )
    wb = _FakeWayback()
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is None


async def test_l2_rejects_httpx_error_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transport error (DNS, connect, timeout) on the evidence URL drops at L2 GET."""
    sug = _suggestion()
    discovered = _discovered(sug.name)
    _patch_http(
        monkeypatch,
        head_responses={discovered: httpx.ConnectError("dns")},
        get_responses={discovered: httpx.ConnectError("dns")},
    )
    wb = _FakeWayback()
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is None


async def test_l3_rejects_evidence_missing_name(monkeypatch: pytest.MonkeyPatch) -> None:
    sug = _suggestion()
    discovered = _discovered(sug.name)
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={
            discovered: _FakeResp(
                status=200,
                text=_article_html("A small startup quietly shutdown last week."),
            ),
        },
    )
    wb = _FakeWayback()
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is None
    assert wb.calls == []


async def test_l3_rejects_evidence_missing_death_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    sug = _suggestion()
    discovered = _discovered(sug.name)
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={
            discovered: _FakeResp(
                status=200,
                text=_article_html("Hexagate just announced a Series B and is hiring engineers."),
            ),
        },
    )
    wb = _FakeWayback()
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is None


async def test_l3_rejects_evidence_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """HEAD OK but evidence GET returns 5xx → drop."""
    sug = _suggestion()
    discovered = _discovered(sug.name)
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=500)},
    )
    wb = _FakeWayback()
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is None


async def test_l3_accepts_name_and_keyword_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sug = _suggestion()
    discovered = _discovered(sug.name)
    html = _article_html("HEXAGATE shutdown its operations in late 2024 after losing key clients.")
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=html)},
    )
    # Wayback returns nothing → tier stays evidence_only, evidence body retained.
    wb = _FakeWayback(enriched_text=None)
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is not None
    entry, tier, verdict = out
    assert tier == "evidence_only"
    assert verdict == "dead"
    assert entry.markdown_text is not None
    # Persisted body is the combined-section format: failure citation only
    # (no Wayback section) because the snapshot didn't anchor.
    assert "# Vendor description (archived)" not in entry.markdown_text
    assert "# Failure citation" in entry.markdown_text
    assert extract_clean(html) in entry.markdown_text
    assert entry.source == "llm_recall"


async def test_l4_wayback_present_with_name_sets_anchored_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sug = _suggestion()
    discovered = _discovered(sug.name)
    evidence_html = _article_html("Hexagate shut down in 2024 per court filings.")
    wayback_body = (
        "Hexagate is a Web3 security firm offering smart-contract auditing, "
        "intrusion detection, and runtime monitoring across major chains."
    )
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=evidence_html)},
    )
    wb = _FakeWayback(enriched_text=wayback_body)
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is not None
    entry, tier, verdict = out
    assert tier == "wayback_anchored"
    assert verdict == "dead"
    # Combined body carries both the Wayback marketing copy (so vector
    # retrieval gets the value-prop) and the news article (so synthesis
    # reads the death narrative).
    assert entry.markdown_text is not None
    assert "# Vendor description (archived)" in entry.markdown_text
    assert wayback_body in entry.markdown_text
    assert "# Failure citation" in entry.markdown_text


async def test_l4_wayback_absent_keeps_evidence_only_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sug = _suggestion()
    discovered = _discovered(sug.name)
    evidence_html = _article_html("Hexagate filed for bankruptcy yesterday.")
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=evidence_html)},
    )
    wb = _FakeWayback(enriched_text=None)
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is not None
    entry, tier, verdict = out
    assert tier == "evidence_only"
    assert verdict == "dead"
    assert entry.markdown_text is not None
    assert extract_clean(evidence_html) in entry.markdown_text
    assert "# Vendor description (archived)" not in entry.markdown_text


async def test_l4_wayback_present_but_no_name_keeps_evidence_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wayback grabbed *some* page (post-acquisition squatter) but the name is gone."""
    sug = _suggestion()
    discovered = _discovered(sug.name)
    evidence_html = _article_html("Hexagate was acquired by Chainalysis in 2024.")
    squatter_body = "Buy this domain! Premium .com domains for sale."
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=evidence_html)},
    )
    wb = _FakeWayback(enriched_text=squatter_body)
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is not None
    entry, tier, verdict = out
    assert tier == "evidence_only"
    assert verdict == "dead"
    assert entry.markdown_text is not None
    assert extract_clean(evidence_html) in entry.markdown_text
    assert "# Vendor description (archived)" not in entry.markdown_text
    # Squatter copy must not leak in — Wayback didn't anchor.
    assert "Buy this domain" not in entry.markdown_text


async def test_l4_wayback_raises_does_not_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    sug = _suggestion()
    discovered = _discovered(sug.name)
    evidence_html = _article_html("Hexagate has been wound down per filings.")
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=evidence_html)},
    )
    wb = _FakeWayback(raises=httpx.ReadTimeout("ia is down"))
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is not None
    entry, tier, verdict = out
    assert tier == "evidence_only"
    assert verdict == "dead"
    assert entry.markdown_text is not None
    assert extract_clean(evidence_html) in entry.markdown_text


async def test_l4_wayback_short_circuits_when_homepage_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``homepage_url is None`` skips Wayback entirely; tier stays ``evidence_only``.

    The verified event must carry ``wayback_attempted="false"`` so trace
    consumers can distinguish skipped (no homepage) from attempted-but-empty.
    """
    events: list[tuple[SpanEvent, dict[str, str]]] = []

    def capture(event: SpanEvent, attributes: dict[str, str] | None = None) -> None:
        events.append((event, dict(attributes) if attributes else {}))

    monkeypatch.setattr(_rv, "_emit_event", capture)
    sug = _suggestion().model_copy(update={"homepage_url": None})
    discovered = _discovered(sug.name)
    evidence_html = _article_html("Hexagate ceased operations in 2024 per court filings.")
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=evidence_html)},
    )
    wb = _FakeWayback(enriched_text="should not be read")
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is not None
    entry, tier, verdict = out
    assert tier == "evidence_only"
    assert verdict == "dead"
    assert wb.calls == []
    # Seed RawEntry falls back to the discovered URL when no homepage.
    assert entry.url == discovered
    assert (SpanEvent.RECALL_VERIFIED_EVIDENCE_ONLY, {"wayback_attempted": "false"}) in events


def _hit_for(sug: RecallSuggestion) -> TavilyHit:
    """Canned Tavily hit whose URL the L2/L3 fakes are also keyed on."""
    return TavilyHit(
        title=f"{sug.name} shuts down operations",
        url=_discovered(sug.name),
        snippet=f"{sug.name} announced its shutdown in {sug.failure_year}.",
    )


def _tavily_for(sugs: list[RecallSuggestion]) -> FakeTavilySearch:
    """One canned hit per suggestion, returned regardless of query string."""
    return FakeTavilySearch(default=[_hit_for(s) for s in sugs])


async def test_verify_all_via_gather_resilient_isolates_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One verifier raising an unexpected error must not cancel the siblings."""
    sugs = [_suggestion("AlphaCo"), _suggestion("BetaCo"), _suggestion("GammaCo")]
    head_responses: dict[str, _FakeResp | BaseException] = {}
    get_responses: dict[str, _FakeResp | BaseException] = {}
    for s in sugs:
        head_responses[_discovered(s.name)] = _FakeResp(status=200)
    get_responses[_discovered(sugs[0].name)] = _FakeResp(
        status=200, text=_article_html(f"{sugs[0].name} shutdown today.")
    )
    # BetaCo: GET blows up with a non-HTTP error to simulate a parse-side bug
    # in a hypothetical L3 helper. ValueError isn't in the (SSRF, HTTPError)
    # except — it'll bubble out of verify_suggestion and become an Exception
    # entry in gather_resilient's results list.
    get_responses[_discovered(sugs[1].name)] = ValueError("decoding blew up")
    get_responses[_discovered(sugs[2].name)] = _FakeResp(
        status=200, text=_article_html(f"{sugs[2].name} declared bankruptcy.")
    )
    _patch_http(
        monkeypatch,
        head_responses=head_responses,
        get_responses=get_responses,
    )
    # FakeTavilySearch keys each suggestion to one hit whose URL matches the
    # L2/L3 fakes above, so the L0 head feeds the right URL into L2.
    tavily = FakeTavilySearch(default=[])
    tavily.response_map = {_build_status_shaped_query(s): [_hit_for(s)] for s in sugs}
    wb = _FakeWayback(enriched_text=None)
    persisted: list[tuple[RawEntry, VerificationTier, Literal["dead", "struggling"]]] = []

    async def persist(
        entry: RawEntry,
        tier: VerificationTier,
        verdict: Literal["dead", "struggling"],
    ) -> None:
        persisted.append((entry, tier, verdict))

    typed_persist: Callable[
        [RawEntry, VerificationTier, Literal["dead", "struggling"]], Awaitable[None]
    ] = persist
    out = await verify_and_persist_all(
        sugs,
        wayback=wb,
        persist=typed_persist,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        tavily_search=tavily,
        tavily_recall_max_results=5,
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert {e.source_id for e in out} == {p[0].source_id for p in persisted}
    assert len(out) == 2
    names_in_bodies = {(entry.markdown_text or "").lower() for entry, _, _ in persisted}
    assert any("alphaco" in body for body in names_in_bodies)
    assert any("gammaco" in body for body in names_in_bodies)


async def test_death_keywords_cover_terminal_and_distress() -> None:
    """Smoke check: the keyword set covers the examples the plan calls out."""
    for kw in ("shutdown", "bankrupt", "acquired", "layoffs", "struggling"):
        assert kw in _DEATH_KEYWORDS


async def test_verify_skips_persist_for_dropped_suggestions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``verify_and_persist_all`` only invokes ``persist`` on accepted entries."""
    sug = _suggestion("DroppedCo")
    discovered = _discovered(sug.name)
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=404)},
        get_responses={discovered: _FakeResp(status=404)},
    )
    wb = _FakeWayback()
    persisted: list[RawEntry] = []

    async def persist(
        entry: RawEntry,
        _tier: VerificationTier,
        _verdict: Literal["dead", "struggling"],
    ) -> None:
        persisted.append(entry)

    out = await verify_and_persist_all(
        [sug],
        wayback=wb,
        persist=persist,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        tavily_search=_tavily_for([sug]),
        tavily_recall_max_results=5,
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out == []
    assert persisted == []


async def test_seed_entry_carries_recall_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Returned ``RawEntry`` is tagged so persistence can route it correctly."""
    sug = _suggestion()
    discovered = _discovered(sug.name)
    body = _article_html("Hexagate ceased operations in 2024.")
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=body)},
    )
    wb = _FakeWayback()
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is not None
    entry, _tier, _verdict = out
    assert entry.source == "llm_recall"
    assert entry.url == sug.homepage_url
    assert isinstance(entry.fetched_at, datetime)
    assert entry.fetched_at.tzinfo == UTC


async def test_recall_source_id_collapses_on_same_homepage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same (name, homepage_url) collapses to one source_id; different homepage diverges.

    Mirrors the Task 5 contract in the plan: the recall persist path keys
    on the vendor (homepage), not the citing article, so two articles
    about the same dead vendor produce one qdrant point.
    """
    sug = _suggestion()
    first_url = _discovered(sug.name)
    other_url = "https://other-news.example/hexagate-update"
    diff_vendor_url = "https://news.example/hexagate-different-vendor"
    diff_vendor = sug.model_copy(
        update={"homepage_url": "https://hexagate.different-tld.example.org/"},
    )
    body = _article_html("Hexagate ceased operations in 2024.")
    _patch_http(
        monkeypatch,
        head_responses={
            first_url: _FakeResp(status=200),
            other_url: _FakeResp(status=200),
            diff_vendor_url: _FakeResp(status=200),
        },
        get_responses={
            first_url: _FakeResp(status=200, text=body),
            other_url: _FakeResp(status=200, text=body),
            diff_vendor_url: _FakeResp(status=200, text=body),
        },
    )
    wb = _FakeWayback()
    llm = _FakeLLM(default=_DEATHNESS_PASS)
    first = await verify_suggestion(
        sug,
        discovered_url=first_url,
        wayback=wb,
        llm=llm,
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    second = await verify_suggestion(
        sug,
        discovered_url=other_url,
        wayback=wb,
        llm=llm,
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    third = await verify_suggestion(
        diff_vendor,
        discovered_url=diff_vendor_url,
        wayback=wb,
        llm=llm,
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert first is not None
    assert second is not None
    assert third is not None
    assert first[0].source_id == second[0].source_id
    assert first[0].source_id != third[0].source_id


def _patch_l1_l4_pass(monkeypatch: pytest.MonkeyPatch, sug: RecallSuggestion, *, body: str) -> str:
    """Wire HEAD/GET so the suggestion sails through L1-L4 cleanly. Returns the discovered URL.

    ``body`` is the lead sentence(s); ``_article_html`` wraps it in a
    long-enough ``<article>`` so the L3 extract-clean floor admits.
    """
    discovered = _discovered(sug.name)
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=_article_html(body))},
    )
    return discovered


async def test_l5_drops_when_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """L5 verdict="alive" rejects the suggestion even though L1-L4 passed.

    The body would otherwise survive L3 (name + "shutdown" both present),
    but Haiku's reading of the full passage says the company is alive.
    """
    sug = _suggestion()
    body = "Hexagate had a shutdown of one product line, then raised a Series C."
    discovered = _patch_l1_l4_pass(monkeypatch, sug, body=body)
    wb = _FakeWayback()
    llm = _FakeLLM(
        responses=['{"verdict": "alive", "confidence": 0.95, "evidence_quote": "raised series C"}'],
    )
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=wb,
        llm=llm,
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is None
    assert len(llm.calls) == 1
    assert llm.calls[0]["model"] == _DEATHNESS.model
    assert llm.calls[0]["max_tokens"] == _DEATHNESS.max_tokens


async def test_l5_drops_when_low_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """``verdict=dead`` but confidence below threshold → drop (avoid noisy admits)."""
    sug = _suggestion()
    body = "Hexagate shutdown rumored, sources unconfirmed."
    discovered = _patch_l1_l4_pass(monkeypatch, sug, body=body)
    wb = _FakeWayback()
    llm = _FakeLLM(
        responses=['{"verdict": "dead", "confidence": 0.5, "evidence_quote": "rumored"}'],
    )
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=wb,
        llm=llm,
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is None


async def test_l5_passes_at_high_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """``verdict=dead`` with confidence ≥ threshold returns the entry+tier+verdict tuple."""
    sug = _suggestion()
    lead = "Hexagate shutdown its operations in 2024 after losing key clients."
    discovered = _patch_l1_l4_pass(monkeypatch, sug, body=lead)
    wb = _FakeWayback(enriched_text=None)
    llm = _FakeLLM(
        responses=[
            '{"verdict": "dead", "confidence": 0.85, "evidence_quote": "shutdown its operations"}',
        ],
    )
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=wb,
        llm=llm,
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is not None
    entry, tier, verdict = out
    assert tier == "evidence_only"
    assert verdict == "dead"
    assert entry.markdown_text is not None
    assert extract_clean(_article_html(lead)) in entry.markdown_text


async def test_l5_drops_on_parse_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM returns malformed JSON → drop conservatively (no false admits)."""
    sug = _suggestion()
    body = "Hexagate shutdown its operations in 2024."
    discovered = _patch_l1_l4_pass(monkeypatch, sug, body=body)
    wb = _FakeWayback()
    llm = _FakeLLM(responses=["this is not json"])
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=wb,
        llm=llm,
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is None


async def test_l5_drops_on_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM raises httpx.HTTPError → drop conservatively (parity with parse failure)."""
    sug = _suggestion()
    body = "Hexagate shutdown its operations in 2024."
    discovered = _patch_l1_l4_pass(monkeypatch, sug, body=body)
    wb = _FakeWayback()
    llm = _FakeLLM(responses=[httpx.ConnectError("boom")])
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=wb,
        llm=llm,
        extract=_NEVER_EXTRACT,
        deathness=_DEATHNESS,
    )
    assert out is None
