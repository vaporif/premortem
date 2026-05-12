"""Tests for Task 5 recall-flow gap closures.

Two behaviors:

1. ``persist_recall_entry`` skips the slop classifier — L5 is the stricter
   gate operating on the death citation, and the slop classifier was tuned
   on a different body shape (crawler output, no combined Wayback + news).
   Running it on the combined body risked false-quarantining L5-verified
   rows; this test pins the skip wired through ``skip_slop=True``.
2. The pipeline emits ``RECALL_GAP_SCORE_AFTER`` post-rerank inside
   ``_run_recall_branch`` so prod telemetry can answer "did recall close
   the gap" per query without re-running eval.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import pytest

from conftest import llm_canned_key
from slopmortem.budget import Budget
from slopmortem.config import Config
from slopmortem.corpus import MergeJournal, extract_clean
from slopmortem.corpus.sources._names import SOURCE_LLM_RECALL
from slopmortem.corpus.tavily import TavilyHit
from slopmortem.ingest import FakeSlopClassifier, IngestResult, InMemoryCorpus, NullProgress
from slopmortem.llm import FakeEmbeddingClient, FakeLLMClient, FakeResponse, render_prompt
from slopmortem.llm.client import CompletionResult
from slopmortem.models import Candidate, CandidatePayload, Facets, InputContext, RawEntry
from slopmortem.pipeline import PersistDeps, run_query
from slopmortem.recall import RecallDeps
from slopmortem.recall._verify import _recall_source_id
from slopmortem.stages import synthesize_prompt_kwargs
from slopmortem.stages.recall_persist import persist_recall_entry
from tests.recall.test_search_head import FakeTavilyExtract, FakeTavilySearch

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from slopmortem.llm import LLMClient
    from slopmortem.models import RecallSuggestion


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_tldextract_cache(  # pyright: ignore[reportUnusedFunction]
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swap ``tldextract.extract`` for a cache-isolated instance.

    Default ``~/.cache/python-tldextract`` is sandbox-blocked on macOS; the
    persist tail's entity resolver touches tldextract via
    ``slopmortem.corpus._entity_resolution``. ``TLDEXTRACT_CACHE`` env doesn't
    help because tldextract caches its module-level extractor at first call.
    """
    import tldextract  # noqa: PLC0415

    cache = tmp_path_factory.mktemp("tldextract")
    extractor = tldextract.TLDExtract(cache_dir=str(cache))
    monkeypatch.setattr("slopmortem.corpus._entity_resolution.tldextract.extract", extractor)


# ---------------------------------------------------------------------------
# Test 1: persist_recall_entry bypasses the slop classifier
# ---------------------------------------------------------------------------


_HAIKU = "anthropic/claude-haiku-4.5"
_BODY = "Hexagate was a Web3 security startup that wound down in 2024. " * 30


def _stub_sparse(_text: str) -> dict[int, float]:
    return {0: 1.0}


def _facets_json() -> str:
    return json.dumps(
        {
            "sector": "fintech",
            "business_model": "b2b_saas",
            "customer_type": "smb",
            "geography": "us",
            "monetization": "subscription_recurring",
            "founding_year": 2021,
            "failure_year": 2024,
        }
    )


def _canned_persist() -> dict[tuple[str, str, str], FakeResponse]:
    facet_prompt = render_prompt("facet_extract", description=_BODY)
    summarize_prompt = render_prompt("summarize", body=_BODY, source_id="")
    return {
        llm_canned_key("facet_extract", model=_HAIKU, prompt=facet_prompt): FakeResponse(
            text=_facets_json()
        ),
        llm_canned_key("summarize", model=_HAIKU, prompt=summarize_prompt): FakeResponse(
            text="Hexagate summary."
        ),
    }


def _suggestion() -> RecallSuggestion:
    from slopmortem.models import RecallSuggestion  # noqa: PLC0415

    return RecallSuggestion(
        name="Hexagate",
        category="Web3 security",
        status="dead",
        homepage_url="https://hexagate.example/",
        failure_year=2024,
        one_liner="Hexagate shut down.",
    )


def _entry_for(suggestion: RecallSuggestion) -> RawEntry:
    return RawEntry(
        source=SOURCE_LLM_RECALL,
        source_id=_recall_source_id(suggestion),
        url=str(suggestion.homepage_url),
        markdown_text=_BODY,
        raw_html=None,
        fetched_at=datetime(2026, 5, 10, tzinfo=UTC),
    )


@dataclass
class _CountingSlopClassifier:
    """SlopClassifier that records every call and would otherwise quarantine."""

    calls: list[str] = field(default_factory=list)
    score_value: float = 0.99  # well above default slop_threshold=0.7

    async def score(self, text: str) -> float:
        self.calls.append(text)
        return self.score_value


async def test_recall_entry_bypasses_slop_classifier(tmp_path: Path) -> None:
    """``persist_recall_entry`` never calls the slop classifier.

    Without the bypass, ``score=0.99`` would trip ``slop_threshold=0.7`` and
    quarantine the recall entry — no qdrant point, no journal row. With the
    bypass, the classifier records zero calls and the entry lands in the
    corpus through the normal write path.
    """
    cfg = Config(max_cost_usd_per_ingest=100.0, ingest_concurrency=1)
    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()
    corpus = InMemoryCorpus()
    classifier = _CountingSlopClassifier()
    entry = _entry_for(_suggestion())

    await persist_recall_entry(
        entry,
        "evidence_only",
        journal=journal,
        corpus=corpus,
        embed_client=FakeEmbeddingClient(model=cfg.embed_model_id),
        llm=FakeLLMClient(canned=_canned_persist(), default_model=_HAIKU),
        slop_classifier=classifier,
        sparse_encoder=_stub_sparse,
        config=cfg,
        post_mortems_root=tmp_path,
        progress=NullProgress(),
        result=IngestResult(),
    )

    assert classifier.calls == [], "slop classifier must not be called for recall entries"
    assert len(corpus.points) >= 1, "recall entry should land in the corpus despite slop=0.99"


# ---------------------------------------------------------------------------
# Test 2: RECALL_GAP_SCORE_AFTER emits after the recall branch re-reranks
# ---------------------------------------------------------------------------


_FACET_MODEL = "test-facet"
_RERANK_MODEL = "test-rerank"
_SYNTH_MODEL = "test-synth"
_CONSOLIDATE_MODEL = "test-consolidate"
_RECALL_MODEL = "test-recall"
_RECALL_DEATHNESS_MODEL = "test-recall-deathness"
_SUMMARIZE_MODEL = "test-summarize"
_EMBED_MODEL = "text-embedding-3-small"

_ALPHA_HOMEPAGE = "https://alpha.example.com/"
_ALPHA_EVIDENCE = "https://news.example.com/alpha-shutdown"

# L3's 500-char floor is enforced by ``extract_clean``; wrap the lead sentence
# in ``<main><article>`` HTML padded with filler so the extracted body clears
# the floor. Mirrors ``tests/stages/test_recall_verify.py::_article_html``.
_FILLER_SENTENCE = (
    "The board cited prolonged headwinds, falling renewal rates, and a stalled "
    "fundraising process as the proximate causes. Customers were notified by "
    "email and given ninety days to migrate. Vendors and contractors were "
    "instructed to file claims through the trustee. "
)


def _article_html(lead: str) -> str:
    return (
        "<html><body><main><article><p>"
        + lead
        + " "
        + (_FILLER_SENTENCE * 5)
        + "</p></article></main></body></html>"
    )


_ALPHA_EVIDENCE_BODY = _article_html(
    "Alpha shutdown its operations in 2024 after losing key clients."
)
# What the persist tail's ``_classify_phase`` runs facet+summarize over is the
# combined body the verifier produces (``# Failure citation\nSource:...\n\n<news_body>``
# with no Wayback section here because ``_NoOpWayback`` returns the seed
# unchanged → ``wayback_anchored=False``). Match the verifier's shape exactly.
_NEWS_BODY = extract_clean(_ALPHA_EVIDENCE_BODY)
_PERSISTED_BODY = (
    "# Failure citation\n\n"
    f"Source: {_ALPHA_EVIDENCE}\n"
    "Status (LLM-suggested): dead (2024)\n\n"
    f"{_NEWS_BODY}"
)
_RECALL_DEATHNESS_PASS = '{"verdict": "dead", "confidence": 0.95, "evidence_quote": "shutdown"}'  # noqa: S105 - JSON literal


def _facets_payload() -> str:
    return json.dumps(
        {
            "sector": "fintech",
            "business_model": "b2b_saas",
            "customer_type": "smb",
            "geography": "us",
            "monetization": "subscription_recurring",
            "sub_sector": "smb invoicing",
            "product_type": "saas",
            "price_point": "tiered",
            "founding_year": 2024,
            "failure_year": None,
        }
    )


def _persisted_facets_payload() -> str:
    return json.dumps(
        {
            "sector": "fintech",
            "business_model": "b2b_saas",
            "customer_type": "smb",
            "geography": "us",
            "monetization": "subscription_recurring",
            "sub_sector": "web3 security",
            "product_type": "saas",
            "price_point": "tiered",
            "founding_year": 2021,
            "failure_year": 2024,
        }
    )


def _rerank_payload(canonical_ids: list[str], *, score: float = 7.0) -> str:
    ranked = [
        {
            "candidate_id": cid,
            "perspective_scores": {
                "business_model": {"score": score, "rationale": "match"},
                "market": {"score": score, "rationale": "match"},
                "gtm": {"score": score, "rationale": "match"},
                "stage_scale": {"score": score, "rationale": "match"},
            },
            "rationale": "ranked",
        }
        for cid in canonical_ids
    ]
    return json.dumps({"ranked": ranked})


def _synthesis_payload(canonical_id: str, *, score: float = 7.0) -> str:
    return json.dumps(
        {
            "candidate_id": canonical_id,
            "name": canonical_id,
            "one_liner": "B2B fintech for SMB invoicing.",
            "failure_date": "2023-01-01",
            "lifespan_months": 60,
            "similarity": {
                "business_model": {"score": score, "rationale": "match"},
                "market": {"score": score, "rationale": "match"},
                "gtm": {"score": score, "rationale": "match"},
                "stage_scale": {"score": score, "rationale": "match"},
            },
            "why_similar": "Both target SMB invoicing.",
            "where_diverged": "Pitch is web-first; analogue was mobile-only.",
            "failure_causes": ["CAC > LTV"],
            "lessons_for_input": ["target larger ACVs"],
        }
    )


def _consolidate_payload() -> str:
    return json.dumps(
        {
            "top_risks": [
                {
                    "summary": "target larger ACVs",
                    "applies_because": "pitch matches comparable shape.",
                    "raised_by": ["acme"],
                    "severity": "medium",
                }
            ],
            "injection_detected": False,
        }
    )


def _recall_payload_single() -> str:
    return json.dumps(
        {
            "suggestions": [
                {
                    "name": "Alpha",
                    "category": "Web3 security audits",
                    "status": "dead",
                    "homepage_url": _ALPHA_HOMEPAGE,
                    "failure_year": 2024,
                    "one_liner": "Alpha did Web3 audits and shut down in 2024.",
                },
            ]
        }
    )


@dataclass
class _RecallRoutingLLM:
    """Route recall-model calls to a queue; defer everything else to ``inner``."""

    inner: FakeLLMClient
    recall_responses: list[FakeResponse] = field(default_factory=list)
    recall_calls: list[dict[str, object]] = field(default_factory=list)

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
        if model == _RECALL_MODEL:
            self.recall_calls.append({"prompt": prompt, "max_tokens": max_tokens})
            if not self.recall_responses:
                msg = "no recall responses queued"
                raise AssertionError(msg)
            item = self.recall_responses.pop(0)
            return item.to_completion()
        if model == _RECALL_DEATHNESS_MODEL:
            return CompletionResult(text=_RECALL_DEATHNESS_PASS, stop_reason="stop")
        return await self.inner.complete(
            prompt,
            system=system,
            tools=tools,
            model=model,
            cache=cache,
            response_format=response_format,
            extra_body=extra_body,
            max_tokens=max_tokens,
            single_tool_call=single_tool_call,
        )


def _payload(*, name: str, canonical_id: str) -> CandidatePayload:
    return CandidatePayload(
        name=name,
        summary=f"{name} was a B2B fintech.",
        body=f"{name} was a B2B fintech that ran out of runway.",
        facets=Facets(
            sector="fintech",
            business_model="b2b_saas",
            customer_type="smb",
            geography="us",
            monetization="subscription_recurring",
        ),
        founding_date=date(2018, 1, 1),
        failure_date=date(2023, 1, 1),
        founding_date_unknown=False,
        failure_date_unknown=False,
        provenance="curated_real",
        slop_score=0.0,
        sources=["https://news.ycombinator.com/item?id=" + canonical_id],
        text_id=canonical_id.replace("-", "") + "0123456789",
    )


def _candidate(canonical_id: str, *, score: float = 0.9) -> Candidate:
    return Candidate(
        canonical_id=canonical_id,
        score=score,
        payload=_payload(name=canonical_id, canonical_id=canonical_id),
    )


@dataclass
class _HybridCorpus:
    """Read- and write-side fake.

    ``base_candidates`` is the pre-recall pool; ``augment_with`` is appended
    after at least one ``upsert_chunk`` lands (the recall entry being
    persisted), so the second retrieve sees a wider pool.
    """

    base_candidates: list[Candidate]
    augment_with: Candidate | None = None
    points: list[object] = field(default_factory=list)

    @property
    def has_recall(self) -> bool:
        return bool(self.points) and self.augment_with is not None

    async def query(  # noqa: PLR0913 - Protocol contract dictates the signature
        self,
        *,
        dense: list[float],
        sparse: dict[int, float],
        facets: Facets,
        cutoff_iso: str | None,
        strict_deaths: bool,
        k_retrieve: int,
        strict_sector_filter: bool = False,
        strict_sector_filter_excludes_other: bool = False,
    ) -> list[Candidate]:
        del dense, sparse, facets, cutoff_iso, strict_deaths
        del strict_sector_filter, strict_sector_filter_excludes_other
        pool = list(self.base_candidates)
        if self.has_recall and self.augment_with is not None:
            pool.append(self.augment_with)
        return pool[:k_retrieve]

    async def get_post_mortem(self, canonical_id: str) -> str:
        pool = [*self.base_candidates]
        if self.augment_with is not None:
            pool.append(self.augment_with)
        for c in pool:
            if c.canonical_id == canonical_id:
                return c.payload.body
        msg = f"unknown canonical_id {canonical_id!r}"
        raise KeyError(msg)

    async def search_corpus(
        self, q: str, facets: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        del q, facets
        return []

    async def upsert_chunk(self, point: object) -> None:
        self.points.append(point)

    async def has_chunks(self, canonical_id: str) -> bool:
        del canonical_id
        return False

    async def delete_chunks_for_canonical(self, canonical_id: str) -> None:
        del canonical_id


def _build_config() -> Config:
    cfg = Config()
    return cfg.model_copy(
        update={
            "K_retrieve": 6,
            "N_synthesize": 3,
            "model_facet": _FACET_MODEL,
            "model_summarize": _SUMMARIZE_MODEL,
            "model_rerank": _RERANK_MODEL,
            "model_synthesize": _SYNTH_MODEL,
            "model_consolidate": _CONSOLIDATE_MODEL,
            "model_recall": _RECALL_MODEL,
            "model_recall_deathness": _RECALL_DEATHNESS_MODEL,
            "enable_tracing": False,
        }
    )


class _FakeResp:
    def __init__(self, *, status: int = 200, text: str = "") -> None:
        self.status_code = status
        self.text = text


def _patch_recall_http(monkeypatch: pytest.MonkeyPatch) -> None:
    head_map: dict[str, _FakeResp] = {
        _ALPHA_HOMEPAGE: _FakeResp(status=200),
        _ALPHA_EVIDENCE: _FakeResp(status=200),
    }
    get_map: dict[str, _FakeResp] = {
        _ALPHA_EVIDENCE: _FakeResp(status=200, text=_ALPHA_EVIDENCE_BODY),
    }

    async def fake_head(url: str, **_kw: object) -> _FakeResp:
        if url not in head_map:
            msg = f"unexpected HEAD: {url}"
            raise AssertionError(msg)
        return head_map[url]

    async def fake_get(url: str, **_kw: object) -> _FakeResp:
        if url not in get_map:
            msg = f"unexpected GET: {url}"
            raise AssertionError(msg)
        return get_map[url]

    monkeypatch.setattr("slopmortem.recall._verify.safe_head", fake_head)
    monkeypatch.setattr("slopmortem.recall._verify.safe_get", fake_get)


class _NoOpWayback:
    async def enrich(self, entry: RawEntry) -> RawEntry:
        return entry


def _alpha_tavily_hits() -> list[TavilyHit]:
    return [
        TavilyHit(
            title="Alpha shuts down operations",
            url=_ALPHA_EVIDENCE,
            snippet="Alpha announced its shutdown in 2024 after losing key clients.",
        )
    ]


def _build_canned(  # noqa: PLR0913 - many parameters shape distinct canned-response keys
    *,
    retrieved: list[Candidate],
    top_n: list[Candidate],
    ctx: InputContext,
    cfg: Config,
    second_pass: list[Candidate],
    second_pass_top_n: list[Candidate],
) -> Mapping[tuple[str, str, str], FakeResponse | CompletionResult]:
    parsed_facets = Facets.model_validate_json(_facets_payload())
    facet_prompt = render_prompt("facet_extract", description=ctx.description)
    canned: dict[tuple[str, str, str], FakeResponse | CompletionResult] = {
        llm_canned_key("facet_extract", model=_FACET_MODEL, prompt=facet_prompt): FakeResponse(
            text=_facets_payload(), cost_usd=0.001
        ),
    }
    rerank_prompt = render_prompt(
        "llm_rerank",
        pitch=ctx.description,
        facets=parsed_facets.model_dump(),
        top_n=cfg.N_synthesize,
        candidates=[
            {
                "candidate_id": c.canonical_id,
                "name": c.payload.name,
                "summary": c.payload.summary,
            }
            for c in retrieved
        ],
    )
    expected_first_pass = min(cfg.N_synthesize, len(retrieved))
    canned[llm_canned_key("llm_rerank", model=_RERANK_MODEL, prompt=rerank_prompt)] = FakeResponse(
        text=_rerank_payload([c.canonical_id for c in retrieved[:expected_first_pass]]),
        cost_usd=0.005,
    )

    synth_resp = FakeResponse(text=_synthesis_payload("acme"), cost_usd=0.01)
    seen: set[tuple[str, str, str]] = set()
    for cand in top_n:
        synth_prompt = render_prompt(
            "synthesize", **synthesize_prompt_kwargs(cand, pitch=ctx.description)
        )
        key = llm_canned_key("synthesize", model=_SYNTH_MODEL, prompt=synth_prompt)
        canned[key] = synth_resp
        seen.add(key)

    second_rerank_prompt = render_prompt(
        "llm_rerank",
        pitch=ctx.description,
        facets=parsed_facets.model_dump(),
        top_n=cfg.N_synthesize,
        candidates=[
            {
                "candidate_id": c.canonical_id,
                "name": c.payload.name,
                "summary": c.payload.summary,
            }
            for c in second_pass
        ],
    )
    expected_second_pass = min(cfg.N_synthesize, len(second_pass))
    canned[llm_canned_key("llm_rerank", model=_RERANK_MODEL, prompt=second_rerank_prompt)] = (
        FakeResponse(
            text=_rerank_payload([c.canonical_id for c in second_pass[:expected_second_pass]]),
            cost_usd=0.005,
        )
    )
    persist_facet_prompt = render_prompt("facet_extract", description=_PERSISTED_BODY)
    persist_summarize_prompt = render_prompt("summarize", body=_PERSISTED_BODY, source_id="")
    canned[llm_canned_key("facet_extract", model=_FACET_MODEL, prompt=persist_facet_prompt)] = (
        FakeResponse(text=_persisted_facets_payload(), cost_usd=0.001)
    )
    canned[llm_canned_key("summarize", model=_SUMMARIZE_MODEL, prompt=persist_summarize_prompt)] = (
        FakeResponse(text="Alpha was a Web3 security firm that shut down.", cost_usd=0.001)
    )

    for cand in second_pass_top_n:
        synth_prompt = render_prompt(
            "synthesize", **synthesize_prompt_kwargs(cand, pitch=ctx.description)
        )
        key = llm_canned_key("synthesize", model=_SYNTH_MODEL, prompt=synth_prompt)
        if key not in seen:
            canned[key] = synth_resp

    surviving_synths = len(second_pass_top_n)
    raised_by = ["acme"] * max(surviving_synths, 1)
    for ids in {tuple(raised_by), ("acme",)}:
        consolidate_prompt = render_prompt(
            "consolidate_risks",
            pitch=ctx.description,
            lessons=[
                {
                    "candidate_id": "acme",
                    "candidate_name": "acme",
                    "lesson": "target larger ACVs",
                }
            ],
            candidate_ids=list(ids),
        )
        canned[
            llm_canned_key("consolidate_risks", model=_CONSOLIDATE_MODEL, prompt=consolidate_prompt)
        ] = FakeResponse(text=_consolidate_payload(), cost_usd=0.005)
    return canned


def _make_recall_deps(llm: LLMClient) -> RecallDeps:
    return RecallDeps(
        llm=llm,
        tavily_search=FakeTavilySearch(default=_alpha_tavily_hits()),
        extract=FakeTavilyExtract(),
        wayback=_NoOpWayback(),
    )


async def _make_persist_deps(tmp_path: Path) -> PersistDeps:
    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()
    return PersistDeps(
        journal=journal,
        slop_classifier=FakeSlopClassifier(default_score=0.0),
        post_mortems_root=tmp_path / "post_mortems",
    )


def _capture_laminar_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Pin ``Laminar.is_initialized`` true; capture every emitted event."""
    events: list[dict[str, Any]] = []

    class _StubLaminar:
        @staticmethod
        def is_initialized() -> bool:
            return True

        @staticmethod
        def event(name: str, attributes: dict[str, str] | None = None) -> None:
            events.append({"name": name, "attributes": attributes or {}})

        @staticmethod
        def set_span_attributes(_attrs: dict[str, Any]) -> None:
            return

        @staticmethod
        def get_trace_id() -> str | None:
            return None

    monkeypatch.setattr("slopmortem.pipeline.Laminar", _StubLaminar)
    return events


async def test_post_recall_gap_score_emits_after_rerank(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pipeline emits ``RECALL_GAP_SCORE_AFTER`` after the recall branch re-reranks.

    First retrieve returns 0 candidates → coverage_gap fires; recall persists
    the alpha suggestion; second retrieve returns the persisted entry. The
    AFTER event carries the post-rerank qualifying count and the required
    threshold; ``gap_closed`` is ``str(not gap)`` so a join-on-trace query
    needs no subtraction.
    """
    events = _capture_laminar_events(monkeypatch)
    cfg = _build_config()
    ctx = InputContext(name="newco", description="A B2B fintech for SMB invoicing")
    persisted = _candidate("alpha-stub")
    corpus = _HybridCorpus(base_candidates=[], augment_with=persisted)
    canned = _build_canned(
        retrieved=[],
        top_n=[],
        ctx=ctx,
        cfg=cfg,
        second_pass=[persisted],
        second_pass_top_n=[persisted],
    )
    inner = FakeLLMClient(canned=canned, default_model=_SYNTH_MODEL)
    llm = _RecallRoutingLLM(
        inner=inner,
        recall_responses=[FakeResponse(text=_recall_payload_single(), cost_usd=0.05)],
    )
    embed = FakeEmbeddingClient(model=_EMBED_MODEL)
    budget = Budget(cap_usd=2.0)
    monkeypatch.setattr("slopmortem.corpus._embed_sparse.encode", _stub_sparse)
    _patch_recall_http(monkeypatch)
    deps = _make_recall_deps(llm)
    persist_deps = await _make_persist_deps(tmp_path)

    await run_query(
        ctx,
        llm=llm,
        embedding_client=embed,
        corpus=corpus,
        config=cfg,
        budget=budget,
        sparse_encoder=_stub_sparse,
        recall_deps=deps,
        persist_deps=persist_deps,
    )

    after_events = [e for e in events if e["name"] == "recall.gap_score_after"]
    assert len(after_events) == 1, "RECALL_GAP_SCORE_AFTER should fire exactly once per recall run"
    attrs = after_events[0]["attributes"]
    # Canned second-pass rerank returns one candidate (the persisted recall row).
    assert attrs["qualifying"] == "1"
    assert attrs["required"] == str(cfg.N_synthesize)
    # 1 qualifying < 3 required → gap not closed.
    assert attrs["gap_closed"] == "False"
    assert attrs["pitch_sector"] == "fintech"
