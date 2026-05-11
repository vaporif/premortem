"""L2/L4 restructure tests: homepage is provenance, Wayback is advisory.

Task 2 of the recall-verifier hardening plan drops the homepage HEAD gate
(homepage URL is provenance, not corroboration), adds a HEAD->GET fallback
on the evidence URL (paywalled/anti-bot citations 401/403/405 on HEAD but
200 on GET), and makes Wayback a pure body-enrichment step that never
drops a candidate when it fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from slopmortem.llm.client import CompletionResult
from slopmortem.models import RawEntry, RecallSuggestion
from slopmortem.stages.recall_verify import verify_suggestion

if TYPE_CHECKING:
    import pytest


_DEATHNESS_PASS = '{"verdict": "dead", "confidence": 0.95, "evidence_quote": "shut down"}'  # noqa: S105 - JSON literal, not a credential
_DEATHNESS_MODEL = "test-haiku"
_DEATHNESS_MAX_TOKENS = 128
_DEATHNESS_MIN_CONFIDENCE = 0.7
_STRUGGLING_MIN_CONFIDENCE = 0.85

_FILLER = (
    "The board cited prolonged headwinds, falling renewal rates, and a stalled "
    "fundraising process as the proximate causes. Customers were notified by "
    "email and given ninety days to migrate. Vendors and contractors were "
    "instructed to file claims through the trustee. "
)


def _article_html(lead: str) -> str:
    """Wrap a lead sentence in ``<main><article>`` and pad past the 500-char floor."""
    return (
        "<html><body><main><article><p>"
        + lead
        + " "
        + (_FILLER * 5)
        + "</p></article></main></body></html>"
    )


@dataclass
class _FakeLLM:
    """LLM stub that returns ``default`` (or raises ``AssertionError`` if hit unexpectedly)."""

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


def _admits_llm() -> _FakeLLM:
    return _FakeLLM(default=_DEATHNESS_PASS)


def _blocker_llm() -> _FakeLLM:
    return _FakeLLM()


class _PassThroughWayback:
    """Wayback stub: returns the seed entry unchanged."""

    def __init__(self) -> None:
        self.calls: list[RawEntry] = []

    async def enrich(self, entry: RawEntry) -> RawEntry:
        self.calls.append(entry)
        return entry


class _AnchoringWayback:
    """Wayback stub: overwrites ``markdown_text`` with marketing copy containing the name."""

    def __init__(self, *, text: str) -> None:
        self.text = text
        self.calls: list[RawEntry] = []

    async def enrich(self, entry: RawEntry) -> RawEntry:
        self.calls.append(entry)
        return entry.model_copy(update={"markdown_text": self.text})


class _RaisingWayback:
    """Wayback stub that raises a transient error — refactor guard for fakes."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def enrich(self, entry: RawEntry) -> RawEntry:
        del entry
        raise self.exc


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
    """Route HEAD/GET per-URL; unknown URLs raise so unexpected probes are loud."""
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


async def test_homepage_head_does_not_gate_when_wayback_anchors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Homepage HEAD 404 must NOT drop the candidate when Wayback anchors the name."""
    sug = _suggestion()
    discovered = _discovered(sug.name)
    evidence_html = _article_html("Acme shut down in 2023 per court filings.")
    wayback_body = "Acme is a security firm specializing in runtime monitoring."
    _patch_http(
        monkeypatch,
        # Note: no entry for the homepage URL — the refactored verifier must
        # not probe it. The fake will raise AssertionError if it tries.
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=evidence_html)},
    )
    wb = _AnchoringWayback(text=wayback_body)
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=wb,
        llm=_admits_llm(),
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
        struggling_min_confidence=_STRUGGLING_MIN_CONFIDENCE,
    )
    assert out is not None
    entry, tier, _verdict = out
    assert tier == "wayback_anchored"
    assert entry.markdown_text is not None
    # Combined body carries the Wayback marketing copy under its section
    # marker; the news article rides alongside under the failure citation.
    assert wayback_body in entry.markdown_text


async def test_wayback_empty_admits_at_evidence_only_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wayback returns the seed unchanged → admit at ``evidence_only`` tier (no drop)."""
    sug = _suggestion()
    discovered = _discovered(sug.name)
    evidence_html = _article_html("Acme filed for bankruptcy yesterday.")
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=evidence_html)},
    )
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=_PassThroughWayback(),
        llm=_admits_llm(),
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
        struggling_min_confidence=_STRUGGLING_MIN_CONFIDENCE,
    )
    assert out is not None
    _, tier, _verdict = out
    assert tier == "evidence_only"


async def test_wayback_transient_failure_does_not_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wayback raising ``httpx.ReadTimeout`` must not drop the candidate.

    Real ``WaybackEnricher.enrich`` swallows transient errors internally,
    but this is a refactor guard against a future Wayback change that
    surfaces exceptions to the caller.
    """
    sug = _suggestion()
    discovered = _discovered(sug.name)
    evidence_html = _article_html("Acme was wound down per filings.")
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=evidence_html)},
    )
    wb = _RaisingWayback(httpx.ReadTimeout("ia is down"))
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=wb,
        llm=_admits_llm(),
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
        struggling_min_confidence=_STRUGGLING_MIN_CONFIDENCE,
    )
    assert out is not None
    _, tier, _verdict = out
    assert tier == "evidence_only"


async def test_evidence_head_405_falls_through_to_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """News sites that 405 on HEAD but 200 on GET must NOT be dropped.

    HEAD returns 405 (the most common anti-bot/paywall HEAD failure shape);
    GET returns a real body. The verifier should admit via the GET fallback.
    """
    sug = _suggestion()
    discovered = _discovered(sug.name)
    evidence_html = _article_html("Acme ceased operations in 2023.")
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=405)},
        get_responses={discovered: _FakeResp(status=200, text=evidence_html)},
    )
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=_PassThroughWayback(),
        llm=_admits_llm(),
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
        struggling_min_confidence=_STRUGGLING_MIN_CONFIDENCE,
    )
    assert out is not None


async def test_evidence_get_404_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    """When both HEAD and GET fail, drop. L5 must not run (LLM blocker proves it)."""
    sug = _suggestion()
    discovered = _discovered(sug.name)
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=404)},
        get_responses={discovered: _FakeResp(status=404)},
    )
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=_PassThroughWayback(),
        llm=_blocker_llm(),
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
        struggling_min_confidence=_STRUGGLING_MIN_CONFIDENCE,
    )
    assert out is None
