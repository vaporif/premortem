"""L5 tri-state tests: dead/struggling/alive thresholds + body composition.

Task 3 replaces the binary ``died: bool`` with a ``Literal["dead",
"struggling", "alive"]`` verdict and a separate ``struggling_min_confidence``
threshold. The persisted body now combines the news article (always) and
the Wayback snapshot (when anchored) under section markers; L5 still reads
the news article only — Wayback marketing copy never says "we died".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from conftest import llm_canned_key
from slopmortem.config import Config
from slopmortem.corpus import MergeJournal
from slopmortem.corpus.sources._names import SOURCE_LLM_RECALL
from slopmortem.ingest import FakeSlopClassifier, IngestResult, InMemoryCorpus, NullProgress
from slopmortem.llm import FakeEmbeddingClient, FakeLLMClient, FakeResponse, render_prompt
from slopmortem.llm.client import CompletionResult
from slopmortem.models import RawEntry, RecallSuggestion
from slopmortem.stages.recall_persist import persist_recall_entry
from slopmortem.stages.recall_verify import _recall_source_id, verify_suggestion
from tests.stages.test_recall_search_head import FakeTavilyExtract

if TYPE_CHECKING:
    from pathlib import Path


_DEATHNESS_MODEL = "test-haiku"
_DEATHNESS_MAX_TOKENS = 128
_DEATHNESS_MIN_CONFIDENCE = 0.7
_STRUGGLING_MIN_CONFIDENCE = 0.85
# Default extract fake: returns "" so any L3 fallback call drops without
# recovering. These tests don't exercise the extract path.
_NEVER_EXTRACT = FakeTavilyExtract()

_FILLER = (
    "The board cited prolonged headwinds, falling renewal rates, and a stalled "
    "fundraising process as the proximate causes. Customers were notified by "
    "email and given ninety days to migrate. Vendors and contractors were "
    "instructed to file claims through the trustee. "
)


def _article_html(lead: str) -> str:
    """Wrap a lead sentence in ``<main><article>`` past the 500-char floor."""
    return (
        "<html><body><main><article><p>"
        + lead
        + " "
        + (_FILLER * 5)
        + "</p></article></main></body></html>"
    )


@dataclass
class _FakeLLM:
    """Captures every prompt and returns a queued reply, or ``default``.

    ``captured_user_prompts`` is the list of user-block strings the verifier
    fed into L5 — tests assert against it to prove L5 saw the news body
    and not the Wayback marketing copy.
    """

    responses: list[str | BaseException] = field(default_factory=list)
    default: str | None = None
    captured_user_prompts: list[str] = field(default_factory=list)

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
        del system, tools, model, cache, response_format, extra_body, max_tokens
        del single_tool_call
        self.captured_user_prompts.append(prompt)
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, BaseException):
                raise item
            return CompletionResult(text=item, stop_reason="stop")
        if self.default is not None:
            return CompletionResult(text=self.default, stop_reason="stop")
        msg = "no response queued"
        raise AssertionError(msg)


class _FakeWayback:
    """Wayback stub: optionally overwrites ``markdown_text`` with marketing copy."""

    def __init__(self, *, enriched_text: str | None = None) -> None:
        self.enriched_text = enriched_text

    async def enrich(self, entry: RawEntry) -> RawEntry:
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


def _patch_l1_l4_pass(monkeypatch: pytest.MonkeyPatch, sug: RecallSuggestion, *, body: str) -> str:
    """Wire HEAD/GET to admit through L1-L4 cleanly. Returns the discovered URL."""
    discovered = _discovered(sug.name)
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=_article_html(body))},
    )
    return discovered


@pytest.mark.parametrize(
    ("verdict", "confidence", "expect_admit"),
    [
        ("dead", 0.95, True),
        ("dead", 0.50, False),
        ("struggling", 0.95, True),
        ("struggling", 0.80, False),
        ("alive", 0.99, False),
    ],
)
async def test_l5_tristate_thresholds(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verdict: str,
    confidence: float,
    expect_admit: bool,
) -> None:
    """L5 admits only when verdict is dead/struggling AND confidence ≥ verdict-specific floor."""
    sug = _suggestion()
    discovered = _patch_l1_l4_pass(
        monkeypatch, sug, body="Acme shut down in 2023 after losing funding."
    )
    payload = (
        f'{{"verdict": "{verdict}", "confidence": {confidence}, "evidence_quote": "from the body"}}'
    )
    llm = _FakeLLM(responses=[payload])
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=_FakeWayback(),
        llm=llm,
        extract=_NEVER_EXTRACT,
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
        struggling_min_confidence=_STRUGGLING_MIN_CONFIDENCE,
    )
    if expect_admit:
        assert out is not None
        _, _, returned_verdict = out
        assert returned_verdict == verdict
    else:
        assert out is None


async def test_l5_news_body_and_persisted_combined_when_anchored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L5 reads the news article only; persisted body combines both sources.

    Wayback returns marketing copy ("secure your stack"); the evidence
    article carries the death citation ("shut down in 2023"). The Haiku
    prompt body must contain only the latter — Wayback is the wrong
    substrate for the deathness judgment. The persisted ``markdown_text``
    must contain BOTH under their section markers.
    """
    sug = _suggestion()
    discovered = _discovered(sug.name)
    evidence_lead = "Acme shut down in 2023 per court filings."
    wayback_marketing = (
        "Acme — secure your stack with our runtime threat detection platform "
        "trusted by 200+ enterprises across the financial services sector."
    )
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=_article_html(evidence_lead))},
    )
    llm = _FakeLLM(
        responses=['{"verdict": "dead", "confidence": 0.9, "evidence_quote": "shut down"}'],
    )
    wb = _FakeWayback(enriched_text=wayback_marketing)
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=wb,
        llm=llm,
        extract=_NEVER_EXTRACT,
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
        struggling_min_confidence=_STRUGGLING_MIN_CONFIDENCE,
    )
    assert out is not None
    entry, tier, verdict = out
    assert tier == "wayback_anchored"
    assert verdict == "dead"
    assert entry.markdown_text is not None
    assert "# Vendor description (archived)" in entry.markdown_text
    assert "secure your stack" in entry.markdown_text
    assert "# Failure citation" in entry.markdown_text
    assert "shut down in 2023" in entry.markdown_text
    # L5 saw only the news article — Wayback marketing copy must not appear
    # in the body Haiku read.
    assert len(llm.captured_user_prompts) == 1
    l5_body = llm.captured_user_prompts[0]
    assert "shut down in 2023" in l5_body
    assert "secure your stack" not in l5_body


async def test_persisted_body_omits_wayback_section_when_not_anchored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wayback didn't anchor → persisted body has only the failure citation section."""
    sug = _suggestion()
    discovered = _discovered(sug.name)
    evidence_lead = "Acme filed for bankruptcy yesterday after eighteen months of losses."
    _patch_http(
        monkeypatch,
        head_responses={discovered: _FakeResp(status=200)},
        get_responses={discovered: _FakeResp(status=200, text=_article_html(evidence_lead))},
    )
    llm = _FakeLLM(
        responses=['{"verdict": "dead", "confidence": 0.9, "evidence_quote": "bankruptcy"}'],
    )
    out = await verify_suggestion(
        sug,
        discovered_url=discovered,
        wayback=_FakeWayback(enriched_text=None),
        llm=llm,
        extract=_NEVER_EXTRACT,
        model_recall_deathness=_DEATHNESS_MODEL,
        max_tokens_recall_deathness=_DEATHNESS_MAX_TOKENS,
        min_confidence=_DEATHNESS_MIN_CONFIDENCE,
        struggling_min_confidence=_STRUGGLING_MIN_CONFIDENCE,
    )
    assert out is not None
    entry, tier, verdict = out
    assert tier == "evidence_only"
    assert verdict == "dead"
    assert entry.markdown_text is not None
    assert "# Vendor description (archived)" not in entry.markdown_text
    assert "# Failure citation" in entry.markdown_text


_HAIKU = "anthropic/claude-haiku-4.5"


def _stub_sparse(_text: str) -> dict[int, float]:
    return {0: 1.0}


async def test_struggling_verdict_lands_in_qdrant_payload(tmp_path: Path) -> None:
    """End-to-end: a struggling admit reaches ``CandidatePayload.deathness_verdict``.

    Bypasses the verifier (already exercised above) and exercises the persist
    chain directly: ``persist_recall_entry`` → ``write_phase`` →
    ``_process_entry`` → ``_build_payload`` → ``CandidatePayload``.
    """
    sug = _suggestion("StruggleCo")
    sentence = "StruggleCo cut 40% of staff and is restructuring around a smaller core product."
    body = (sentence + " ") * 30
    entry = RawEntry(
        source=SOURCE_LLM_RECALL,
        source_id=_recall_source_id(sug),
        url=str(sug.homepage_url),
        markdown_text=body,
        raw_html=None,
        fetched_at=datetime(2026, 5, 10, tzinfo=UTC),
    )

    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()
    corpus = InMemoryCorpus()
    cfg = Config(max_cost_usd_per_ingest=100.0, ingest_concurrency=1)

    facets_json = json.dumps(
        {
            "sector": "fintech",
            "business_model": "b2b_saas",
            "customer_type": "smb",
            "geography": "us",
            "monetization": "subscription_recurring",
            "founding_year": 2020,
            "failure_year": 2023,
        }
    )
    facet_prompt = render_prompt("facet_extract", description=body)
    summarize_prompt = render_prompt("summarize", body=body, source_id="")
    canned: dict[tuple[str, str, str], FakeResponse] = {
        llm_canned_key("facet_extract", model=_HAIKU, prompt=facet_prompt): FakeResponse(
            text=facets_json
        ),
        llm_canned_key("summarize", model=_HAIKU, prompt=summarize_prompt): FakeResponse(
            text="StruggleCo summary."
        ),
    }
    llm = FakeLLMClient(canned=canned, default_model=_HAIKU)
    embed = FakeEmbeddingClient(model=cfg.embed_model_id)
    classifier = FakeSlopClassifier(default_score=0.0)

    await persist_recall_entry(
        entry,
        "evidence_only",
        deathness_verdict="struggling",
        journal=journal,
        corpus=corpus,
        embed_client=embed,
        llm=llm,
        slop_classifier=classifier,
        sparse_encoder=_stub_sparse,
        config=cfg,
        post_mortems_root=tmp_path,
        progress=NullProgress(),
        result=IngestResult(),
    )

    assert corpus.points, "expected at least one qdrant point"
    for point in corpus.points:
        assert point.payload.get("deathness_verdict") == "struggling"
        assert point.payload.get("verification_tier") == "evidence_only"
