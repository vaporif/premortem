"""Tests for ``stages.recall_verify``: L1-L4 gates plus fan-out isolation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from slopmortem.http import SSRFBlockedError
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


async def test_l2_rejects_404_homepage(monkeypatch: pytest.MonkeyPatch) -> None:
    sug = _suggestion()
    _patch_http(
        monkeypatch,
        head_responses={str(sug.homepage_url): _FakeResp(status=404)},
        get_responses={},  # evidence GET should never fire
    )
    wb = _FakeWayback()
    out = await verify_suggestion(sug, wayback=wb)
    assert out is None
    assert wb.calls == []


async def test_l2_rejects_404_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    sug = _suggestion()
    _patch_http(
        monkeypatch,
        head_responses={
            str(sug.homepage_url): _FakeResp(status=200),
            str(sug.evidence_url): _FakeResp(status=404),
        },
        get_responses={},
    )
    wb = _FakeWayback()
    out = await verify_suggestion(sug, wayback=wb)
    assert out is None
    assert wb.calls == []


async def test_l2_rejects_ssrf_homepage(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSRFBlockedError on HEAD short-circuits before any GET."""
    sug = _suggestion()
    _patch_http(
        monkeypatch,
        head_responses={str(sug.homepage_url): SSRFBlockedError("nope")},
        get_responses={},
    )
    wb = _FakeWayback()
    out = await verify_suggestion(sug, wayback=wb)
    assert out is None


async def test_l2_rejects_httpx_error_homepage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transport error (DNS, connect, timeout) on HEAD drops the suggestion."""
    sug = _suggestion()
    _patch_http(
        monkeypatch,
        head_responses={str(sug.homepage_url): httpx.ConnectError("dns")},
        get_responses={},
    )
    wb = _FakeWayback()
    out = await verify_suggestion(sug, wayback=wb)
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
                text="A small startup quietly shutdown last week, no other details.",
            ),
        },
    )
    wb = _FakeWayback()
    out = await verify_suggestion(sug, wayback=wb)
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
                text="Hexagate just announced a Series B and is hiring engineers.",
            ),
        },
    )
    wb = _FakeWayback()
    out = await verify_suggestion(sug, wayback=wb)
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
    out = await verify_suggestion(sug, wayback=wb)
    assert out is None


async def test_l3_accepts_name_and_keyword_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sug = _suggestion()
    body = "HEXAGATE shutdown its operations in late 2024 after losing key clients."
    _patch_http(
        monkeypatch,
        head_responses={
            str(sug.homepage_url): _FakeResp(status=200),
            str(sug.evidence_url): _FakeResp(status=200),
        },
        get_responses={str(sug.evidence_url): _FakeResp(status=200, text=body)},
    )
    # Wayback returns nothing → tier stays evidence_only, evidence body retained.
    wb = _FakeWayback(enriched_text=None)
    out = await verify_suggestion(sug, wayback=wb)
    assert out is not None
    entry, tier = out
    assert tier == "evidence_only"
    assert entry.markdown_text == body
    assert entry.source == "llm_recall"


async def test_l4_wayback_present_with_name_sets_anchored_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sug = _suggestion()
    evidence_body = "Hexagate shut down in 2024 per court filings."
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
        get_responses={str(sug.evidence_url): _FakeResp(status=200, text=evidence_body)},
    )
    wb = _FakeWayback(enriched_text=wayback_body)
    out = await verify_suggestion(sug, wayback=wb)
    assert out is not None
    entry, tier = out
    assert tier == "wayback_anchored"
    # Wayback body wins: it carries marketing copy that vector search prefers.
    assert entry.markdown_text == wayback_body


async def test_l4_wayback_absent_keeps_evidence_only_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sug = _suggestion()
    evidence_body = "Hexagate filed for bankruptcy yesterday."
    _patch_http(
        monkeypatch,
        head_responses={
            str(sug.homepage_url): _FakeResp(status=200),
            str(sug.evidence_url): _FakeResp(status=200),
        },
        get_responses={str(sug.evidence_url): _FakeResp(status=200, text=evidence_body)},
    )
    wb = _FakeWayback(enriched_text=None)
    out = await verify_suggestion(sug, wayback=wb)
    assert out is not None
    entry, tier = out
    assert tier == "evidence_only"
    assert entry.markdown_text == evidence_body


async def test_l4_wayback_present_but_no_name_keeps_evidence_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wayback grabbed *some* page (post-acquisition squatter) but the name is gone."""
    sug = _suggestion()
    evidence_body = "Hexagate was acquired by Chainalysis in 2024."
    squatter_body = "Buy this domain! Premium .com domains for sale."
    _patch_http(
        monkeypatch,
        head_responses={
            str(sug.homepage_url): _FakeResp(status=200),
            str(sug.evidence_url): _FakeResp(status=200),
        },
        get_responses={str(sug.evidence_url): _FakeResp(status=200, text=evidence_body)},
    )
    wb = _FakeWayback(enriched_text=squatter_body)
    out = await verify_suggestion(sug, wayback=wb)
    assert out is not None
    entry, tier = out
    assert tier == "evidence_only"
    assert entry.markdown_text == evidence_body


async def test_l4_wayback_raises_does_not_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    sug = _suggestion()
    evidence_body = "Hexagate has been wound down per filings."
    _patch_http(
        monkeypatch,
        head_responses={
            str(sug.homepage_url): _FakeResp(status=200),
            str(sug.evidence_url): _FakeResp(status=200),
        },
        get_responses={str(sug.evidence_url): _FakeResp(status=200, text=evidence_body)},
    )
    wb = _FakeWayback(raises=httpx.ReadTimeout("ia is down"))
    out = await verify_suggestion(sug, wayback=wb)
    assert out is not None
    entry, tier = out
    assert tier == "evidence_only"
    assert entry.markdown_text == evidence_body


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
        status=200, text=f"{sugs[0].name} shutdown today."
    )
    # BetaCo: GET blows up with a non-HTTP error to simulate a parse-side bug
    # in a hypothetical L3 helper. ValueError isn't in the (SSRF, HTTPError)
    # except — it'll bubble out of verify_suggestion and become an Exception
    # entry in gather_resilient's results list.
    get_responses[str(sugs[1].evidence_url)] = ValueError("decoding blew up")
    get_responses[str(sugs[2].evidence_url)] = _FakeResp(
        status=200, text=f"{sugs[2].name} declared bankruptcy."
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
        head_responses={str(sug.homepage_url): _FakeResp(status=404)},
        get_responses={},
    )
    wb = _FakeWayback()
    persisted: list[RawEntry] = []

    async def persist(entry: RawEntry, _tier: VerificationTier) -> None:
        persisted.append(entry)

    out = await verify_and_persist_all(
        [sug],
        wayback=wb,
        persist=persist,
    )
    assert out == []
    assert persisted == []


async def test_seed_entry_carries_recall_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Returned ``RawEntry`` is tagged so persistence can route it correctly."""
    sug = _suggestion()
    body = "Hexagate ceased operations in 2024."
    _patch_http(
        monkeypatch,
        head_responses={
            str(sug.homepage_url): _FakeResp(status=200),
            str(sug.evidence_url): _FakeResp(status=200),
        },
        get_responses={str(sug.evidence_url): _FakeResp(status=200, text=body)},
    )
    wb = _FakeWayback()
    out = await verify_suggestion(sug, wayback=wb)
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
    body = "Hexagate ceased operations in 2024."
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
    first = await verify_suggestion(sug, wayback=wb)
    second = await verify_suggestion(same_vendor_other_citation, wayback=wb)
    third = await verify_suggestion(diff_vendor, wayback=wb)
    assert first is not None
    assert second is not None
    assert third is not None
    assert first[0].source_id == second[0].source_id
    assert first[0].source_id != third[0].source_id
