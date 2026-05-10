"""Tests for ``stages.recall_verify``: L1-L5 gates plus fan-out isolation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from slopmortem.corpus import extract_clean
from slopmortem.http import SSRFBlockedError
from slopmortem.llm.client import CompletionResult
from slopmortem.models import RawEntry, RecallSuggestion
from slopmortem.stages.recall_verify import (
    _DEATH_KEYWORDS,
    VerificationTier,
    verify_and_persist_all,
    verify_suggestion,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import pytest


_DEATHNESS_PASS = '{"died": true, "confidence": 0.95, "evidence_quote": "shut down"}'  # noqa: S105 - JSON literal, not a credential
_DEATHNESS_MODEL = "test-haiku"
_DEATHNESS_MAX_TOKENS = 128
_DEATHNESS_MIN_CONFIDENCE = 0.7
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
        evidence_url=f"https://news.example.com/{name.lower()}-shutdown",
        one_liner=f"{name} shut down in 2024.",
    )


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
    _patch_http(
        monkeypatch,
        head_responses={str(sug.evidence_url): _FakeResp(status=404)},
        get_responses={str(sug.evidence_url): _FakeResp(status=404)},
    )
    wb = _FakeWayback()
    out = await verify_suggestion(
        sug,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    assert out is None
    assert wb.calls == []


async def test_l2_rejects_ssrf_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSRFBlockedError on the evidence URL drops at the L2 GET stage."""
    sug = _suggestion()
    _patch_http(
        monkeypatch,
        head_responses={str(sug.evidence_url): SSRFBlockedError("nope")},
        get_responses={str(sug.evidence_url): SSRFBlockedError("nope")},
    )
    wb = _FakeWayback()
    out = await verify_suggestion(
        sug,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    assert out is None


async def test_l2_rejects_httpx_error_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transport error (DNS, connect, timeout) on the evidence URL drops at L2 GET."""
    sug = _suggestion()
    _patch_http(
        monkeypatch,
        head_responses={str(sug.evidence_url): httpx.ConnectError("dns")},
        get_responses={str(sug.evidence_url): httpx.ConnectError("dns")},
    )
    wb = _FakeWayback()
    out = await verify_suggestion(
        sug,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    assert out is None


async def test_l3_rejects_evidence_missing_name(monkeypatch: pytest.MonkeyPatch) -> None:
    sug = _suggestion()
    _patch_http(
        monkeypatch,
        head_responses={
            str(sug.homepage_url): _FakeResp(status=200),
            str(sug.evidence_url): _FakeResp(status=200),
        },
        get_responses={
            str(sug.evidence_url): _FakeResp(
                status=200,
                text=_article_html("A small startup quietly shutdown last week."),
            ),
        },
    )
    wb = _FakeWayback()
    out = await verify_suggestion(
        sug,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    assert out is None
    assert wb.calls == []


async def test_l3_rejects_evidence_missing_death_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    sug = _suggestion()
    _patch_http(
        monkeypatch,
        head_responses={
            str(sug.homepage_url): _FakeResp(status=200),
            str(sug.evidence_url): _FakeResp(status=200),
        },
        get_responses={
            str(sug.evidence_url): _FakeResp(
                status=200,
                text=_article_html("Hexagate just announced a Series B and is hiring engineers."),
            ),
        },
    )
    wb = _FakeWayback()
    out = await verify_suggestion(
        sug,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    assert out is None


async def test_l3_rejects_evidence_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both HEADs OK but evidence GET returns 5xx → drop."""
    sug = _suggestion()
    _patch_http(
        monkeypatch,
        head_responses={
            str(sug.homepage_url): _FakeResp(status=200),
            str(sug.evidence_url): _FakeResp(status=200),
        },
        get_responses={str(sug.evidence_url): _FakeResp(status=500)},
    )
    wb = _FakeWayback()
    out = await verify_suggestion(
        sug,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    assert out is None


async def test_l3_accepts_name_and_keyword_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sug = _suggestion()
    html = _article_html("HEXAGATE shutdown its operations in late 2024 after losing key clients.")
    _patch_http(
        monkeypatch,
        head_responses={
            str(sug.homepage_url): _FakeResp(status=200),
            str(sug.evidence_url): _FakeResp(status=200),
        },
        get_responses={str(sug.evidence_url): _FakeResp(status=200, text=html)},
    )
    # Wayback returns nothing → tier stays evidence_only, evidence body retained.
    wb = _FakeWayback(enriched_text=None)
    out = await verify_suggestion(
        sug,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    assert out is not None
    entry, tier = out
    assert tier == "evidence_only"
    assert entry.markdown_text == extract_clean(html)
    assert entry.source == "llm_recall"


async def test_l4_wayback_present_with_name_sets_anchored_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sug = _suggestion()
    evidence_html = _article_html("Hexagate shut down in 2024 per court filings.")
    wayback_body = (
        "Hexagate is a Web3 security firm offering smart-contract auditing, "
        "intrusion detection, and runtime monitoring across major chains."
    )
    _patch_http(
        monkeypatch,
        head_responses={
            str(sug.homepage_url): _FakeResp(status=200),
            str(sug.evidence_url): _FakeResp(status=200),
        },
        get_responses={str(sug.evidence_url): _FakeResp(status=200, text=evidence_html)},
    )
    wb = _FakeWayback(enriched_text=wayback_body)
    out = await verify_suggestion(
        sug,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    assert out is not None
    entry, tier = out
    assert tier == "wayback_anchored"
    # Wayback body wins: it carries marketing copy that vector search prefers.
    assert entry.markdown_text == wayback_body


async def test_l4_wayback_absent_keeps_evidence_only_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sug = _suggestion()
    evidence_html = _article_html("Hexagate filed for bankruptcy yesterday.")
    _patch_http(
        monkeypatch,
        head_responses={
            str(sug.homepage_url): _FakeResp(status=200),
            str(sug.evidence_url): _FakeResp(status=200),
        },
        get_responses={str(sug.evidence_url): _FakeResp(status=200, text=evidence_html)},
    )
    wb = _FakeWayback(enriched_text=None)
    out = await verify_suggestion(
        sug,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    assert out is not None
    entry, tier = out
    assert tier == "evidence_only"
    assert entry.markdown_text == extract_clean(evidence_html)


async def test_l4_wayback_present_but_no_name_keeps_evidence_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wayback grabbed *some* page (post-acquisition squatter) but the name is gone."""
    sug = _suggestion()
    evidence_html = _article_html("Hexagate was acquired by Chainalysis in 2024.")
    squatter_body = "Buy this domain! Premium .com domains for sale."
    _patch_http(
        monkeypatch,
        head_responses={
            str(sug.homepage_url): _FakeResp(status=200),
            str(sug.evidence_url): _FakeResp(status=200),
        },
        get_responses={str(sug.evidence_url): _FakeResp(status=200, text=evidence_html)},
    )
    wb = _FakeWayback(enriched_text=squatter_body)
    out = await verify_suggestion(
        sug,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    assert out is not None
    entry, tier = out
    assert tier == "evidence_only"
    assert entry.markdown_text == extract_clean(evidence_html)


async def test_l4_wayback_raises_does_not_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    sug = _suggestion()
    evidence_html = _article_html("Hexagate has been wound down per filings.")
    _patch_http(
        monkeypatch,
        head_responses={
            str(sug.homepage_url): _FakeResp(status=200),
            str(sug.evidence_url): _FakeResp(status=200),
        },
        get_responses={str(sug.evidence_url): _FakeResp(status=200, text=evidence_html)},
    )
    wb = _FakeWayback(raises=httpx.ReadTimeout("ia is down"))
    out = await verify_suggestion(
        sug,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    assert out is not None
    entry, tier = out
    assert tier == "evidence_only"
    assert entry.markdown_text == extract_clean(evidence_html)


async def test_verify_all_via_gather_resilient_isolates_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One verifier raising an unexpected error must not cancel the siblings."""
    sugs = [_suggestion("AlphaCo"), _suggestion("BetaCo"), _suggestion("GammaCo")]
    head_responses: dict[str, _FakeResp | BaseException] = {}
    get_responses: dict[str, _FakeResp | BaseException] = {}
    for s in sugs:
        head_responses[str(s.homepage_url)] = _FakeResp(status=200)
        head_responses[str(s.evidence_url)] = _FakeResp(status=200)
    get_responses[str(sugs[0].evidence_url)] = _FakeResp(
        status=200, text=_article_html(f"{sugs[0].name} shutdown today.")
    )
    # BetaCo: GET blows up with a non-HTTP error to simulate a parse-side bug
    # in a hypothetical L3 helper. ValueError isn't in the (SSRF, HTTPError)
    # except — it'll bubble out of verify_suggestion and become an Exception
    # entry in gather_resilient's results list.
    get_responses[str(sugs[1].evidence_url)] = ValueError("decoding blew up")
    get_responses[str(sugs[2].evidence_url)] = _FakeResp(
        status=200, text=_article_html(f"{sugs[2].name} declared bankruptcy.")
    )
    _patch_http(
        monkeypatch,
        head_responses=head_responses,
        get_responses=get_responses,
    )
    wb = _FakeWayback(enriched_text=None)
    persisted: list[tuple[RawEntry, VerificationTier]] = []

    async def persist(entry: RawEntry, tier: VerificationTier) -> None:
        persisted.append((entry, tier))

    typed_persist: Callable[[RawEntry, VerificationTier], Awaitable[None]] = persist
    out = await verify_and_persist_all(
        sugs,
        wayback=wb,
        persist=typed_persist,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    assert {e.source_id for e in out} == {e.source_id for e in [persisted[0][0], persisted[1][0]]}
    assert len(out) == 2
    names_in_bodies = {(entry.markdown_text or "").lower() for entry, _ in persisted}
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
    _patch_http(
        monkeypatch,
        head_responses={str(sug.evidence_url): _FakeResp(status=404)},
        get_responses={str(sug.evidence_url): _FakeResp(status=404)},
    )
    wb = _FakeWayback()
    persisted: list[RawEntry] = []

    async def persist(entry: RawEntry, _tier: VerificationTier) -> None:
        persisted.append(entry)

    out = await verify_and_persist_all(
        [sug],
        wayback=wb,
        persist=persist,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    assert out == []
    assert persisted == []


async def test_seed_entry_carries_recall_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Returned ``RawEntry`` is tagged so persistence can route it correctly."""
    sug = _suggestion()
    body = _article_html("Hexagate ceased operations in 2024.")
    _patch_http(
        monkeypatch,
        head_responses={
            str(sug.homepage_url): _FakeResp(status=200),
            str(sug.evidence_url): _FakeResp(status=200),
        },
        get_responses={str(sug.evidence_url): _FakeResp(status=200, text=body)},
    )
    wb = _FakeWayback()
    out = await verify_suggestion(
        sug,
        wayback=wb,
        llm=_FakeLLM(default=_DEATHNESS_PASS),
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    assert out is not None
    entry, _tier = out
    assert entry.source == "llm_recall"
    assert entry.url == str(sug.homepage_url)
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
    body = _article_html("Hexagate ceased operations in 2024.")
    # Same name + same homepage but a *different* evidence article.
    same_vendor_other_citation = sug.model_copy(
        update={"evidence_url": "https://other-news.example.com/hexagate-update"},
    )
    diff_vendor = sug.model_copy(
        update={"homepage_url": "https://hexagate.different-tld.example.org/"},
    )
    _patch_http(
        monkeypatch,
        head_responses={
            str(sug.homepage_url): _FakeResp(status=200),
            str(sug.evidence_url): _FakeResp(status=200),
            str(same_vendor_other_citation.evidence_url): _FakeResp(status=200),
            str(diff_vendor.homepage_url): _FakeResp(status=200),
            str(diff_vendor.evidence_url): _FakeResp(status=200),
        },
        get_responses={
            str(sug.evidence_url): _FakeResp(status=200, text=body),
            str(same_vendor_other_citation.evidence_url): _FakeResp(status=200, text=body),
            str(diff_vendor.evidence_url): _FakeResp(status=200, text=body),
        },
    )
    wb = _FakeWayback()
    llm = _FakeLLM(default=_DEATHNESS_PASS)
    first = await verify_suggestion(
        sug,
        wayback=wb,
        llm=llm,
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    second = await verify_suggestion(
        same_vendor_other_citation,
        wayback=wb,
        llm=llm,
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    third = await verify_suggestion(
        diff_vendor,
        wayback=wb,
        llm=llm,
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    assert first is not None
    assert second is not None
    assert third is not None
    assert first[0].source_id == second[0].source_id
    assert first[0].source_id != third[0].source_id


def _patch_l1_l4_pass(monkeypatch: pytest.MonkeyPatch, sug: RecallSuggestion, *, body: str) -> None:
    """Wire HEAD/GET so the suggestion sails through L1-L4 cleanly.

    ``body`` is the lead sentence(s); ``_article_html`` wraps it in a
    long-enough ``<article>`` so the L3 extract-clean floor admits.
    """
    _patch_http(
        monkeypatch,
        head_responses={
            str(sug.homepage_url): _FakeResp(status=200),
            str(sug.evidence_url): _FakeResp(status=200),
        },
        get_responses={str(sug.evidence_url): _FakeResp(status=200, text=_article_html(body))},
    )


async def test_l5_drops_when_not_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    """L5 deathness=false rejects the suggestion even though L1-L4 passed.

    The body would otherwise survive L3 (name + "shutdown" both present),
    but Haiku's reading of the full passage says the company is alive.
    """
    sug = _suggestion()
    body = "Hexagate had a shutdown of one product line, then raised a Series C."
    _patch_l1_l4_pass(monkeypatch, sug, body=body)
    wb = _FakeWayback()
    llm = _FakeLLM(
        responses=['{"died": false, "confidence": 0.95, "evidence_quote": "raised series C"}'],
    )
    out = await verify_suggestion(
        sug,
        wayback=wb,
        llm=llm,
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    assert out is None
    assert len(llm.calls) == 1
    assert llm.calls[0]["model"] == _DEATHNESS_MODEL
    assert llm.calls[0]["max_tokens"] == _DEATHNESS_MAX_TOKENS


async def test_l5_drops_when_low_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """``died=true`` but confidence below threshold → drop (avoid noisy admits)."""
    sug = _suggestion()
    body = "Hexagate shutdown rumored, sources unconfirmed."
    _patch_l1_l4_pass(monkeypatch, sug, body=body)
    wb = _FakeWayback()
    llm = _FakeLLM(
        responses=['{"died": true, "confidence": 0.5, "evidence_quote": "rumored"}'],
    )
    out = await verify_suggestion(
        sug,
        wayback=wb,
        llm=llm,
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    assert out is None


async def test_l5_passes_at_high_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """``died=true`` with confidence ≥ threshold returns the entry+tier tuple."""
    sug = _suggestion()
    lead = "Hexagate shutdown its operations in 2024 after losing key clients."
    _patch_l1_l4_pass(monkeypatch, sug, body=lead)
    wb = _FakeWayback(enriched_text=None)
    llm = _FakeLLM(
        responses=[
            '{"died": true, "confidence": 0.85, "evidence_quote": "shutdown its operations"}',
        ],
    )
    out = await verify_suggestion(
        sug,
        wayback=wb,
        llm=llm,
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    assert out is not None
    entry, tier = out
    assert tier == "evidence_only"
    assert entry.markdown_text == extract_clean(_article_html(lead))


async def test_l5_drops_on_parse_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM returns malformed JSON → drop conservatively (no false admits)."""
    sug = _suggestion()
    body = "Hexagate shutdown its operations in 2024."
    _patch_l1_l4_pass(monkeypatch, sug, body=body)
    wb = _FakeWayback()
    llm = _FakeLLM(responses=["this is not json"])
    out = await verify_suggestion(
        sug,
        wayback=wb,
        llm=llm,
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    assert out is None


async def test_l5_drops_on_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM raises httpx.HTTPError → drop conservatively (parity with parse failure)."""
    sug = _suggestion()
    body = "Hexagate shutdown its operations in 2024."
    _patch_l1_l4_pass(monkeypatch, sug, body=body)
    wb = _FakeWayback()
    llm = _FakeLLM(responses=[httpx.ConnectError("boom")])
    out = await verify_suggestion(
        sug,
        wayback=wb,
        llm=llm,
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
    )
    assert out is None
