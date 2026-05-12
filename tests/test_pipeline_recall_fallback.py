"""Pipeline wiring tests for the LLM-recall fallback branch.

Covers Task 6 of ``docs/plans/2026-05-08-llm-recall-fallback.md`` and the
always-on follow-up in ``docs/plans/2026-05-09-recall-always-on.md``:
predicate evaluation, the ``force_llm_recall`` bypass, the single-pass
guarantee, and ``PipelineMeta`` flag surfacing.

All upstream dependencies are faked. The verifier's HTTP probes are patched
at ``slopmortem.recall._verify.safe_head`` / ``safe_get`` — same
shape as ``tests/recall/test_verify.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

import pytest

from conftest import llm_canned_key
from slopmortem.budget import Budget
from slopmortem.config import Config
from slopmortem.corpus import MergeJournal, extract_clean
from slopmortem.corpus.tavily import TavilyHit
from slopmortem.ingest import FakeSlopClassifier
from slopmortem.llm import FakeEmbeddingClient, FakeLLMClient, FakeResponse, render_prompt
from slopmortem.llm.client import CompletionResult
from slopmortem.models import (
    Candidate,
    CandidatePayload,
    Facets,
    InputContext,
)
from slopmortem.pipeline import PersistDeps, run_query
from slopmortem.recall import FakeRecaller, RecallDeps, VerifiedEntry
from slopmortem.stages import synthesize_prompt_kwargs
from tests.recall.test_search_head import FakeTavilyExtract, FakeTavilySearch

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from slopmortem.llm import LLMClient
    from slopmortem.models import RawEntry


@pytest.fixture(autouse=True)
def _isolate_tldextract_cache(  # pyright: ignore[reportUnusedFunction]
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replace ``tldextract.extract`` with a cache-free instance.

    Default ``~/.cache/python-tldextract`` is sandbox-blocked on macOS; the
    persist tail's entity resolver touches tldextract via
    ``slopmortem.corpus._entity_resolution``. Setting ``TLDEXTRACT_CACHE``
    in env doesn't help because tldextract caches its module-level extractor
    at first call. Instead, swap in a fresh ``TLDExtract`` whose cache lives
    under ``tmp_path``.
    """
    import tldextract  # noqa: PLC0415 - lazy: keeps the module importable in envs without it

    cache = tmp_path_factory.mktemp("tldextract")
    extractor = tldextract.TLDExtract(cache_dir=str(cache))
    monkeypatch.setattr("slopmortem.corpus._entity_resolution.tldextract.extract", extractor)


_FACET_MODEL = "test-facet"
_RERANK_MODEL = "test-rerank"
_SYNTH_MODEL = "test-synth"
_CONSOLIDATE_MODEL = "test-consolidate"
_RECALL_MODEL = "test-recall"
_RECALL_DEATHNESS_MODEL = "test-recall-deathness"
_SUMMARIZE_MODEL = "test-summarize"
_EMBED_MODEL = "text-embedding-3-small"

# Default L5 reply for the routing LLM. The pipeline tests don't exercise L5
# semantics — they care that recall fires, persists, and surfaces meta flags.
# Confidence sits well above the default 0.7 threshold so verified suggestions
# pass the gate and reach the persist tail.
_RECALL_DEATHNESS_PASS = '{"verdict": "dead", "confidence": 0.95, "evidence_quote": "shutdown"}'  # noqa: S105 - JSON literal, not a credential


# ---------------------------------------------------------------------------
# Canned payloads
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Recall suggestions: alpha verifies, beta drops at L3
# ---------------------------------------------------------------------------

_ALPHA_HOMEPAGE = "https://alpha.example.com/"
_ALPHA_EVIDENCE = "https://news.example.com/alpha-shutdown"
_BETA_HOMEPAGE = "https://beta.example.com/"
_BETA_EVIDENCE = "https://news.example.com/beta-launch"
# L3 hygiene (Task 1) gates on ``extract_clean``'s 500-char floor; raw text
# under that returns ``""`` and L3 rejects. Wrap the lead sentence in
# ``<main><article>`` and pad with filler so trafilatura's main-content
# extractor keeps it well above the floor. Mirrors
# ``tests/stages/test_recall_verify.py::_article_html``.
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
_BETA_EVIDENCE_BODY = _article_html("Beta announced a Series B and is hiring engineers.")


def _recall_payload() -> str:
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
                {
                    "name": "Beta",
                    "category": "Web3 security audits",
                    "status": "struggling",
                    "homepage_url": _BETA_HOMEPAGE,
                    "failure_year": 2024,
                    "one_liner": "Beta did Web3 audits.",
                },
            ]
        }
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _RecallRoutingLLM:
    """Wrap ``FakeLLMClient`` and intercept the ``model_recall`` route.

    ``llm_recall`` doesn't supply ``extra_body['prompt_template_sha']``
    (that's a deliberate stage choice; recall isn't keyed by sha cassette).
    FakeLLMClient requires the sha, so the recall call would be a fixture
    miss. Routing on ``model`` keeps the rest of the pipeline on FakeLLMClient
    while letting recall return its canned text from a flat per-model lookup.
    """

    inner: FakeLLMClient
    recall_responses: list[FakeResponse | RuntimeError | Exception]
    recall_calls: list[dict[str, object]] = field(default_factory=list)
    deathness_calls: list[dict[str, object]] = field(default_factory=list)

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
            self.recall_calls.append({"prompt": prompt, "system": system, "max_tokens": max_tokens})
            if not self.recall_responses:
                msg = "no recall responses queued"
                raise AssertionError(msg)
            item = self.recall_responses.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item.to_completion()
        if model == _RECALL_DEATHNESS_MODEL:
            # L5 sits inside the verifier, which fires once per suggestion that
            # cleared L1-L4. These pipeline tests don't exercise L5 semantics;
            # always admit so verified suggestions reach the persist tail.
            self.deathness_calls.append(
                {"prompt": prompt, "system": system, "max_tokens": max_tokens}
            )
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
    """Combined fake satisfying both ``corpus.Corpus`` and ``ingest.Corpus``.

    Read side: ``query`` returns ``self.candidates`` plus ``self.added`` if
    a recall entry has been persisted. Write side: ``upsert_chunk`` records
    each point and appends the canonical id from the payload to ``self.added``.
    """

    base_candidates: list[Candidate]
    augment_with: Candidate | None = None
    points: list[object] = field(default_factory=list)
    queries: list[dict[str, object]] = field(default_factory=list)

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
        self.queries.append(
            {
                "dense_dim": len(dense),
                "sparse_keys": list(sparse.keys()),
                "facets": facets.model_dump(),
                "cutoff_iso": cutoff_iso,
                "strict_deaths": strict_deaths,
                "k_retrieve": k_retrieve,
                "strict_sector_filter": strict_sector_filter,
                "strict_sector_filter_excludes_other": strict_sector_filter_excludes_other,
            }
        )
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


def _stub_sparse(_text: str) -> dict[int, float]:
    return {0: 1.0}


def _build_config(
    *,
    k_retrieve: int = 6,
    n_synthesize: int = 3,
    force_llm_recall: bool = False,
) -> Config:
    cfg = Config()
    return cfg.model_copy(
        update={
            "K_retrieve": k_retrieve,
            "N_synthesize": n_synthesize,
            "model_facet": _FACET_MODEL,
            "model_summarize": _SUMMARIZE_MODEL,
            "model_rerank": _RERANK_MODEL,
            "model_synthesize": _SYNTH_MODEL,
            "model_consolidate": _CONSOLIDATE_MODEL,
            "model_recall": _RECALL_MODEL,
            "model_recall_deathness": _RECALL_DEATHNESS_MODEL,
            "force_llm_recall": force_llm_recall,
            "enable_tracing": False,
        }
    )


class _FakeResp:
    def __init__(self, *, status: int = 200, text: str = "") -> None:
        self.status_code = status
        self.text = text


def _patch_recall_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the verifier's HTTP probes to accept alpha and reject beta."""
    head_map: dict[str, _FakeResp] = {
        _ALPHA_HOMEPAGE: _FakeResp(status=200),
        _ALPHA_EVIDENCE: _FakeResp(status=200),
        _BETA_HOMEPAGE: _FakeResp(status=200),
        _BETA_EVIDENCE: _FakeResp(status=200),
    }
    get_map: dict[str, _FakeResp] = {
        _ALPHA_EVIDENCE: _FakeResp(status=200, text=_ALPHA_EVIDENCE_BODY),
        _BETA_EVIDENCE: _FakeResp(status=200, text=_BETA_EVIDENCE_BODY),
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
    """Stand-in for ``WaybackEnricher`` that never anchors (returns the seed)."""

    async def enrich(self, entry: RawEntry) -> RawEntry:
        return entry


def _tavily_hits() -> list[TavilyHit]:
    """Hits for Alpha (dead, with death keyword) and Beta (no death keyword).

    Alpha's hit clears L0 as the primary (name + death keyword in title);
    Beta's hit clears L0 as the fallback (name-only). L3 then drops Beta
    because the article body never mentions a death keyword.
    """
    return [
        TavilyHit(
            title="Alpha shutdown its operations",
            url=_ALPHA_EVIDENCE,
            snippet="Alpha announced shutdown after losing key clients in 2024.",
        ),
        TavilyHit(
            title="Beta announces Series B",
            url=_BETA_EVIDENCE,
            snippet="Beta raised a fresh round to expand its audit team.",
        ),
    ]


# Body the verifier will plant on the persisted entry. After Task 3's combine,
# the verifier wraps the cleaned news body in a ``# Failure citation`` section
# (and a ``# Vendor description (archived)`` section when Wayback anchored —
# ``_NoOpWayback`` returns the seed unchanged, so no anchor here). The combined
# body is what ``_classify_phase`` feeds through facet_extract + summarize.
_ALPHA_NEWS_BODY = extract_clean(_ALPHA_EVIDENCE_BODY)
_PERSISTED_BODY = (
    "# Failure citation\n\n"
    f"Source: {_ALPHA_EVIDENCE}\n"
    "Status (LLM-suggested): dead (2024)\n\n"
    f"{_ALPHA_NEWS_BODY}"
)


def _build_canned(  # noqa: PLR0913 - test fixture: every parameter shapes a canned response key
    *,
    retrieved: list[Candidate],
    top_n: list[Candidate],
    ctx: InputContext,
    cfg: Config,
    second_pass: list[Candidate] | None = None,
    second_pass_top_n: list[Candidate] | None = None,
    rerank_score: float = 7.0,
    synth_score: float = 7.0,
) -> Mapping[tuple[str, str, str], FakeResponse | CompletionResult]:
    """Canned-response map covering every non-recall LLM call.

    Always emits: facet_extract for the pitch, llm_rerank for the first pass,
    synthesize per top_n, consolidate_risks. When a second pass runs, also
    emits: facet_extract + summarize for ``_PERSISTED_BODY``, second-pass
    rerank, and second-pass synthesize per ``second_pass_top_n``.
    """
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
        text=_rerank_payload(
            [c.canonical_id for c in retrieved[:expected_first_pass]],
            score=rerank_score,
        ),
        cost_usd=0.005,
    )

    # Synthesize: deterministic ``candidate_id="acme"`` so consolidate sees
    # one canonical lesson set regardless of which candidates land.
    synth_resp = FakeResponse(
        text=_synthesis_payload("acme", score=synth_score),
        cost_usd=0.01,
        cache_creation_tokens=10,
    )
    seen: set[tuple[str, str, str]] = set()
    for cand in top_n:
        synth_prompt = render_prompt(
            "synthesize", **synthesize_prompt_kwargs(cand, pitch=ctx.description)
        )
        key = llm_canned_key("synthesize", model=_SYNTH_MODEL, prompt=synth_prompt)
        canned[key] = synth_resp
        seen.add(key)

    if second_pass is not None:
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
                text=_rerank_payload(
                    [c.canonical_id for c in second_pass[:expected_second_pass]],
                    score=rerank_score,
                ),
                cost_usd=0.005,
            )
        )
        # Persist tail: facet_extract + summarize on ``_PERSISTED_BODY``.
        persist_facet_prompt = render_prompt("facet_extract", description=_PERSISTED_BODY)
        persist_summarize_prompt = render_prompt("summarize", body=_PERSISTED_BODY, source_id="")
        canned[llm_canned_key("facet_extract", model=_FACET_MODEL, prompt=persist_facet_prompt)] = (
            FakeResponse(text=_persisted_facets_payload(), cost_usd=0.001)
        )
        canned[
            llm_canned_key("summarize", model=_SUMMARIZE_MODEL, prompt=persist_summarize_prompt)
        ] = FakeResponse(text="Alpha was a Web3 security firm that shut down.", cost_usd=0.001)

    if second_pass_top_n is not None:
        for cand in second_pass_top_n:
            synth_prompt = render_prompt(
                "synthesize", **synthesize_prompt_kwargs(cand, pitch=ctx.description)
            )
            key = llm_canned_key("synthesize", model=_SYNTH_MODEL, prompt=synth_prompt)
            if key not in seen:
                canned[key] = synth_resp

    # ``candidate_ids`` reflects the synthesis count *with duplicates*. Tests
    # collapse all synth replies to ``candidate_id="acme"``, so the count is
    # the number of syntheses that survive (post-recall, post-similarity-drop).
    second_top_count = len(second_pass_top_n) if second_pass_top_n is not None else 0
    surviving_synths = second_top_count if second_pass_top_n is not None else len(top_n)
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


async def _make_recall_deps(llm: LLMClient) -> RecallDeps:
    return RecallDeps(
        llm=llm,
        tavily_search=FakeTavilySearch(default=_tavily_hits()),
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_pipeline_recall_fires_on_zero_candidates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty corpus auto-fires recall (predicate trips), persists 1, surfaces flags."""
    cfg = _build_config(k_retrieve=6, n_synthesize=3)
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
        recall_responses=[FakeResponse(text=_recall_payload(), cost_usd=0.05)],
    )
    embed = FakeEmbeddingClient(model=_EMBED_MODEL)
    budget = Budget(cap_usd=2.0)
    monkeypatch.setattr("slopmortem.corpus._embed_sparse.encode", _stub_sparse)
    _patch_recall_http(monkeypatch)
    deps = await _make_recall_deps(llm)
    persist_deps = await _make_persist_deps(tmp_path)

    report = await run_query(
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

    assert report.pipeline_meta.coverage_gap is True
    assert report.pipeline_meta.recall_used is True
    assert report.pipeline_meta.recall_persisted_count == 1
    # Beta dropped at L3, alpha persisted, second-pass synthesis ran.
    assert len(report.candidates) == 1
    assert report.candidates[0].name == "acme"


async def test_pipeline_recall_quiet_when_predicate_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Healthy corpus → ``coverage_gap`` stays False → recall doesn't fire.

    With recall always-on, the only thing that suppresses the branch is the
    predicate itself: enough qualifying candidates after rerank+min_similarity.
    """
    cfg = _build_config(k_retrieve=6, n_synthesize=3)
    ctx = InputContext(name="newco", description="A B2B fintech for SMB invoicing")
    candidates = [_candidate(f"cand-{i}") for i in range(cfg.K_retrieve)]
    corpus = _HybridCorpus(base_candidates=candidates)
    canned = _build_canned(
        retrieved=candidates,
        top_n=candidates[: cfg.N_synthesize],
        ctx=ctx,
        cfg=cfg,
    )
    inner = FakeLLMClient(canned=canned, default_model=_SYNTH_MODEL)
    # No recall responses queued — nothing should call recall.
    llm = _RecallRoutingLLM(inner=inner, recall_responses=[])
    embed = FakeEmbeddingClient(model=_EMBED_MODEL)
    budget = Budget(cap_usd=2.0)
    monkeypatch.setattr("slopmortem.corpus._embed_sparse.encode", _stub_sparse)

    report = await run_query(
        ctx,
        llm=llm,
        embedding_client=embed,
        corpus=corpus,
        config=cfg,
        budget=budget,
        sparse_encoder=_stub_sparse,
    )

    assert report.pipeline_meta.coverage_gap is False
    assert report.pipeline_meta.recall_used is False
    assert report.pipeline_meta.recall_persisted_count == 0
    assert llm.recall_calls == []
    del tmp_path  # unused; pytest fixture must accept it for parity with siblings


async def test_pipeline_recall_trigger_fires_when_qualifying_low(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Predicate fires when qualifying < N_synthesize (always-on)."""
    cfg = _build_config(k_retrieve=6, n_synthesize=3)
    ctx = InputContext(name="newco", description="A B2B fintech for SMB invoicing")
    seed = [_candidate("seed-0")]
    persisted = _candidate("alpha-stub")
    corpus = _HybridCorpus(base_candidates=seed, augment_with=persisted)
    canned = _build_canned(
        retrieved=seed,
        top_n=seed,
        ctx=ctx,
        cfg=cfg,
        second_pass=[*seed, persisted],
        second_pass_top_n=[*seed, persisted],
    )
    inner = FakeLLMClient(canned=canned, default_model=_SYNTH_MODEL)
    llm = _RecallRoutingLLM(
        inner=inner,
        recall_responses=[FakeResponse(text=_recall_payload(), cost_usd=0.05)],
    )
    embed = FakeEmbeddingClient(model=_EMBED_MODEL)
    budget = Budget(cap_usd=2.0)
    monkeypatch.setattr("slopmortem.corpus._embed_sparse.encode", _stub_sparse)
    _patch_recall_http(monkeypatch)
    deps = await _make_recall_deps(llm)
    persist_deps = await _make_persist_deps(tmp_path)

    report = await run_query(
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

    assert report.pipeline_meta.coverage_gap is True
    assert report.pipeline_meta.recall_used is True
    assert report.pipeline_meta.recall_persisted_count == 1


async def test_pipeline_force_llm_recall_bypasses_quiet_predicate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``force_llm_recall=True`` fires recall even when the predicate stays quiet.

    Healthy corpus → ``coverage_gap`` stays ``False``; force flips ``recall_used``
    to ``True``. The pair lets operators distinguish the two paths in telemetry.
    """
    cfg = _build_config(k_retrieve=6, n_synthesize=3, force_llm_recall=True)
    ctx = InputContext(name="newco", description="A B2B fintech for SMB invoicing")
    base = [_candidate(f"cand-{i}") for i in range(cfg.K_retrieve)]
    persisted = _candidate("alpha-stub")
    corpus = _HybridCorpus(base_candidates=base, augment_with=persisted)
    second_pass = [*base, persisted][: cfg.K_retrieve]
    canned = _build_canned(
        retrieved=base,
        top_n=base[: cfg.N_synthesize],
        ctx=ctx,
        cfg=cfg,
        second_pass=second_pass,
        second_pass_top_n=second_pass[: cfg.N_synthesize],
    )
    inner = FakeLLMClient(canned=canned, default_model=_SYNTH_MODEL)
    llm = _RecallRoutingLLM(
        inner=inner,
        recall_responses=[FakeResponse(text=_recall_payload(), cost_usd=0.05)],
    )
    embed = FakeEmbeddingClient(model=_EMBED_MODEL)
    budget = Budget(cap_usd=2.0)
    monkeypatch.setattr("slopmortem.corpus._embed_sparse.encode", _stub_sparse)
    _patch_recall_http(monkeypatch)
    deps = await _make_recall_deps(llm)
    persist_deps = await _make_persist_deps(tmp_path)

    report = await run_query(
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

    assert report.pipeline_meta.coverage_gap is False
    assert report.pipeline_meta.recall_used is True
    assert report.pipeline_meta.recall_persisted_count == 1


async def test_pipeline_force_llm_recall_with_quiet_trigger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Force fires recall even when ``coverage_gap`` is quiet (force overrides)."""
    cfg = _build_config(k_retrieve=6, n_synthesize=3, force_llm_recall=True)
    ctx = InputContext(name="newco", description="A B2B fintech for SMB invoicing")
    # 6 strong fintech matches → 6 qualify, well above N_synthesize=3.
    base = [_candidate(f"cand-{i}") for i in range(cfg.K_retrieve)]
    persisted = _candidate("alpha-stub")
    corpus = _HybridCorpus(base_candidates=base, augment_with=persisted)
    second_pass = [*base, persisted][: cfg.K_retrieve]
    canned = _build_canned(
        retrieved=base,
        top_n=base[: cfg.N_synthesize],
        ctx=ctx,
        cfg=cfg,
        second_pass=second_pass,
        second_pass_top_n=second_pass[: cfg.N_synthesize],
    )
    inner = FakeLLMClient(canned=canned, default_model=_SYNTH_MODEL)
    llm = _RecallRoutingLLM(
        inner=inner,
        recall_responses=[FakeResponse(text=_recall_payload(), cost_usd=0.05)],
    )
    embed = FakeEmbeddingClient(model=_EMBED_MODEL)
    budget = Budget(cap_usd=2.0)
    monkeypatch.setattr("slopmortem.corpus._embed_sparse.encode", _stub_sparse)
    _patch_recall_http(monkeypatch)
    deps = await _make_recall_deps(llm)
    persist_deps = await _make_persist_deps(tmp_path)

    report = await run_query(
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

    assert report.pipeline_meta.coverage_gap is False
    assert report.pipeline_meta.recall_used is True
    assert report.pipeline_meta.recall_persisted_count == 1


async def test_pipeline_recall_max_one_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Recall runs at most once per query.

    First pass retrieves nothing; recall persists alpha; second pass returns
    one candidate (still < N_synthesize=3). Pipeline must NOT loop the recall
    branch — the LLM client records exactly one recall-route call.
    """
    cfg = _build_config(k_retrieve=6, n_synthesize=3)
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
    # Only ONE recall response queued. A second call would raise
    # "no recall responses queued" inside _RecallRoutingLLM.
    llm = _RecallRoutingLLM(
        inner=inner,
        recall_responses=[FakeResponse(text=_recall_payload(), cost_usd=0.05)],
    )
    embed = FakeEmbeddingClient(model=_EMBED_MODEL)
    budget = Budget(cap_usd=2.0)
    monkeypatch.setattr("slopmortem.corpus._embed_sparse.encode", _stub_sparse)
    _patch_recall_http(monkeypatch)
    deps = await _make_recall_deps(llm)
    persist_deps = await _make_persist_deps(tmp_path)

    report = await run_query(
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

    assert len(llm.recall_calls) == 1
    assert report.pipeline_meta.coverage_gap is True
    assert report.pipeline_meta.recall_used is True
    assert report.pipeline_meta.recall_persisted_count == 1


async def test_pipeline_recall_raises_when_recall_deps_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``force_llm_recall=True`` with no ``RecallDeps`` raises so misconfig surfaces.

    The pipeline can't silently no-op the recall path when the operator asked
    for it; surfacing ``RuntimeError`` at the call site beats crashing deeper
    in the persist tail.
    """
    cfg = _build_config(k_retrieve=6, n_synthesize=3, force_llm_recall=True)
    ctx = InputContext(name="newco", description="A B2B fintech for SMB invoicing")
    base = [_candidate(f"cand-{i}") for i in range(cfg.K_retrieve)]
    corpus = _HybridCorpus(base_candidates=base)
    canned = _build_canned(
        retrieved=base,
        top_n=base[: cfg.N_synthesize],
        ctx=ctx,
        cfg=cfg,
    )
    inner = FakeLLMClient(canned=canned, default_model=_SYNTH_MODEL)
    llm = _RecallRoutingLLM(inner=inner, recall_responses=[])
    embed = FakeEmbeddingClient(model=_EMBED_MODEL)
    budget = Budget(cap_usd=2.0)
    monkeypatch.setattr("slopmortem.corpus._embed_sparse.encode", _stub_sparse)

    with pytest.raises(RuntimeError, match="force_llm_recall=True but RecallDeps not provided"):
        await run_query(
            ctx,
            llm=llm,
            embedding_client=embed,
            corpus=corpus,
            config=cfg,
            budget=budget,
            sparse_encoder=_stub_sparse,
            recall_deps=None,
        )


async def test_pipeline_force_llm_recall_raises_when_persist_deps_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``force_llm_recall=True`` with ``persist_deps=None`` raises so misconfig surfaces.

    Mirrors the existing ``recall_deps=None`` guard. The two records split
    apart in the recall extraction; both must be present for recall to fire.
    """
    cfg = _build_config(k_retrieve=6, n_synthesize=3, force_llm_recall=True)
    ctx = InputContext(name="newco", description="A B2B fintech for SMB invoicing")
    base = [_candidate(f"cand-{i}") for i in range(cfg.K_retrieve)]
    corpus = _HybridCorpus(base_candidates=base)
    canned = _build_canned(
        retrieved=base,
        top_n=base[: cfg.N_synthesize],
        ctx=ctx,
        cfg=cfg,
    )
    inner = FakeLLMClient(canned=canned, default_model=_SYNTH_MODEL)
    llm = _RecallRoutingLLM(inner=inner, recall_responses=[])
    embed = FakeEmbeddingClient(model=_EMBED_MODEL)
    budget = Budget(cap_usd=2.0)
    monkeypatch.setattr("slopmortem.corpus._embed_sparse.encode", _stub_sparse)
    deps = await _make_recall_deps(llm)

    with pytest.raises(RuntimeError, match="force_llm_recall=True but PersistDeps not provided"):
        await run_query(
            ctx,
            llm=llm,
            embedding_client=embed,
            corpus=corpus,
            config=cfg,
            budget=budget,
            sparse_encoder=_stub_sparse,
            recall_deps=deps,
            persist_deps=None,
        )


async def test_pipeline_recall_raises_when_sparse_encoder_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Recall fires but ``sparse_encoder=None`` — raise rather than crash deeper.

    The persist tail needs the sparse encoder to upsert the new candidate;
    failing fast at the gate is clearer than a downstream ``TypeError``.
    """
    cfg = _build_config(k_retrieve=6, n_synthesize=3, force_llm_recall=True)
    ctx = InputContext(name="newco", description="A B2B fintech for SMB invoicing")
    base = [_candidate(f"cand-{i}") for i in range(cfg.K_retrieve)]
    corpus = _HybridCorpus(base_candidates=base)
    canned = _build_canned(
        retrieved=base,
        top_n=base[: cfg.N_synthesize],
        ctx=ctx,
        cfg=cfg,
    )
    inner = FakeLLMClient(canned=canned, default_model=_SYNTH_MODEL)
    llm = _RecallRoutingLLM(inner=inner, recall_responses=[])
    embed = FakeEmbeddingClient(model=_EMBED_MODEL)
    budget = Budget(cap_usd=2.0)
    monkeypatch.setattr("slopmortem.corpus._embed_sparse.encode", _stub_sparse)
    deps = await _make_recall_deps(llm)
    persist_deps = await _make_persist_deps(tmp_path)

    with pytest.raises(RuntimeError, match="recall requires sparse_encoder"):
        await run_query(
            ctx,
            llm=llm,
            embedding_client=embed,
            corpus=corpus,
            config=cfg,
            budget=budget,
            sparse_encoder=None,
            recall_deps=deps,
            persist_deps=persist_deps,
        )


async def test_pipeline_recall_lowers_synth_floor_when_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Recall persisted >=1 → pre-synth filter uses ``min_similarity_score_after_recall``.

    Spike rerank score at 3.5: between the recall floor (3.0) and the corpus-
    normal floor (4.0). Without the adaptive threshold the candidate would be
    dropped and synthesis would run on zero entries. With it, the persisted
    recall entry reaches synthesis.
    """
    cfg = _build_config(k_retrieve=6, n_synthesize=3)
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
        # Below corpus-normal (4.0) but above recall floor (3.0): exercises
        # the adaptive-threshold branch.
        rerank_score=3.5,
    )
    inner = FakeLLMClient(canned=canned, default_model=_SYNTH_MODEL)
    llm = _RecallRoutingLLM(
        inner=inner,
        recall_responses=[FakeResponse(text=_recall_payload(), cost_usd=0.05)],
    )
    embed = FakeEmbeddingClient(model=_EMBED_MODEL)
    budget = Budget(cap_usd=2.0)
    monkeypatch.setattr("slopmortem.corpus._embed_sparse.encode", _stub_sparse)
    _patch_recall_http(monkeypatch)
    deps = await _make_recall_deps(llm)
    persist_deps = await _make_persist_deps(tmp_path)

    report = await run_query(
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

    # Recall fired and persisted alpha; the 3.5 rerank score clears the
    # adaptive 3.0 floor (it would have been dropped at 4.0).
    assert report.pipeline_meta.recall_used is True
    assert report.pipeline_meta.recall_persisted_count == 1
    assert len(report.candidates) == 1
    assert report.candidates[0].name == "acme"


async def test_pipeline_recall_lowers_post_synth_floor_when_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Recall persisted >=1 → post-synth filter also uses the lowered floor.

    The synthesizer sometimes re-scores a candidate below the rerank-side
    similarity (the synthesis stage's own ``drop_below_min_similarity`` call).
    Without the after-recall threshold being honoured post-synth, the recall
    branch synthesises a candidate (paying tokens), the synthesiser scores
    3.5, then the corpus-normal 4.0 floor drops it — undoing the pre-synth
    rationale and burning the recall budget.

    Spike both rerank AND synth at 3.5: between the recall floor (3.0) and
    the corpus-normal floor (4.0). Pre-synth filter (3.0) admits it; without
    the fix post-synth (4.0) drops it. With the fix both filters apply 3.0.
    """
    cfg = _build_config(k_retrieve=6, n_synthesize=3)
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
        rerank_score=3.5,
        synth_score=3.5,
    )
    inner = FakeLLMClient(canned=canned, default_model=_SYNTH_MODEL)
    llm = _RecallRoutingLLM(
        inner=inner,
        recall_responses=[FakeResponse(text=_recall_payload(), cost_usd=0.05)],
    )
    embed = FakeEmbeddingClient(model=_EMBED_MODEL)
    budget = Budget(cap_usd=2.0)
    monkeypatch.setattr("slopmortem.corpus._embed_sparse.encode", _stub_sparse)
    _patch_recall_http(monkeypatch)
    deps = await _make_recall_deps(llm)
    persist_deps = await _make_persist_deps(tmp_path)

    report = await run_query(
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

    assert report.pipeline_meta.recall_used is True
    assert report.pipeline_meta.recall_persisted_count == 1
    # The synthesised candidate clears the lowered 3.0 post-synth bar that
    # mirrors the pre-synth choice; the corpus-normal 4.0 would have dropped it.
    assert report.pipeline_meta.filtered_post_synth == 0
    assert len(report.candidates) == 1
    assert report.candidates[0].name == "acme"


def test_min_similarity_score_after_recall_validator_at_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``min_similarity_score_after_recall=4.5`` above 4.0 → load-time ``ValidationError``."""
    from pydantic import ValidationError  # noqa: PLC0415 - local import, narrow rebind for test

    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError, match="min_similarity_score_after_recall"):
        Config(min_similarity_score=4.0, min_similarity_score_after_recall=4.5)


def _capture_laminar_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Pin ``Laminar.is_initialized`` true and capture every emitted event.

    The tracer is inert under default test fakes, so gap-score would never fire.
    Patching the import site in ``slopmortem.pipeline`` keeps the rest of the
    suite untouched.
    """
    events: list[dict[str, Any]] = []

    class _StubLaminar:
        @staticmethod
        def is_initialized() -> bool:
            return True

        @staticmethod
        def event(name: str, attributes: dict[str, str] | None = None) -> None:
            events.append({"name": name, "attributes": attributes or {}})

        # run_query also calls these on the same gate; provide no-op stubs.
        @staticmethod
        def set_span_attributes(_attrs: dict[str, Any]) -> None:
            return

        @staticmethod
        def get_trace_id() -> str | None:
            return None

    monkeypatch.setattr("slopmortem.pipeline.Laminar", _StubLaminar)
    return events


async def test_gap_score_event_emitted_on_every_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``recall.gap_score`` fires once per query even when the predicate is quiet.

    Calibration eval needs qualifying/required on every run, not just on fires.
    Healthy corpus → ``coverage_gap=False`` and no ``RECALL_GATE_FIRED``.
    """
    events = _capture_laminar_events(monkeypatch)
    cfg = _build_config(k_retrieve=6, n_synthesize=3)
    ctx = InputContext(name="newco", description="A B2B fintech for SMB invoicing")
    candidates = [_candidate(f"cand-{i}") for i in range(cfg.K_retrieve)]
    corpus = _HybridCorpus(base_candidates=candidates)
    canned = _build_canned(
        retrieved=candidates,
        top_n=candidates[: cfg.N_synthesize],
        ctx=ctx,
        cfg=cfg,
    )
    inner = FakeLLMClient(canned=canned, default_model=_SYNTH_MODEL)
    llm = _RecallRoutingLLM(inner=inner, recall_responses=[])
    embed = FakeEmbeddingClient(model=_EMBED_MODEL)
    budget = Budget(cap_usd=2.0)
    monkeypatch.setattr("slopmortem.corpus._embed_sparse.encode", _stub_sparse)

    report = await run_query(
        ctx,
        llm=llm,
        embedding_client=embed,
        corpus=corpus,
        config=cfg,
        budget=budget,
        sparse_encoder=_stub_sparse,
    )

    gap_events = [e for e in events if e["name"] == "recall.gap_score"]
    assert len(gap_events) == 1
    attrs = gap_events[0]["attributes"]
    # Canned rerank ranks at most N_synthesize candidates, so qualifying caps there.
    assert attrs["qualifying"] == str(cfg.N_synthesize)
    assert attrs["required"] == str(cfg.N_synthesize)
    assert attrs["pitch_sector"] == "fintech"
    # Quiet predicate: gate event must not fire and recall stays untouched.
    assert not any(e["name"] == "recall.gate_fired" for e in events)
    assert report.pipeline_meta.coverage_gap is False


def _stub_verified_entry_for_pipeline(name: str) -> VerifiedEntry:
    """Minimal VerifiedEntry for pipeline persist-isolation tests."""
    from datetime import UTC, datetime  # noqa: PLC0415 - test-local

    from slopmortem.models import RawEntry  # noqa: PLC0415 - test-local

    return VerifiedEntry(
        entry=RawEntry(
            source="llm_recall",
            source_id=f"recall::{name}",
            url=f"https://{name}.example.com",
            title=name.title(),
            raw_html=None,
            markdown_text=f"{name} shut down operations.",
            fetched_at=datetime.now(UTC),
        ),
        tier="evidence_only",
        verdict="dead",
    )


async def test_pipeline_persist_failure_isolates_per_suggestion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One ``persist_recall_entry`` raising must not abort sibling persists.

    Mirrors the per-suggestion isolation that ``verify_and_persist_all``
    used to provide internally. After the recall extraction, the same
    invariant is enforced by ``_persist_one`` in ``pipeline._run_recall_branch``.
    """
    cfg = _build_config(k_retrieve=6, n_synthesize=3)
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
    llm = _RecallRoutingLLM(inner=inner, recall_responses=[])
    embed = FakeEmbeddingClient(model=_EMBED_MODEL)
    budget = Budget(cap_usd=2.0)
    monkeypatch.setattr("slopmortem.corpus._embed_sparse.encode", _stub_sparse)

    call_count = {"n": 0}

    async def flaky_persist(*_args: object, **_kwargs: object) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            msg = "simulated persist failure"
            raise RuntimeError(msg)

    monkeypatch.setattr("slopmortem.pipeline.persist_recall_entry", flaky_persist)

    fake_recaller = FakeRecaller(
        verified=[
            _stub_verified_entry_for_pipeline("a"),
            _stub_verified_entry_for_pipeline("b"),
        ],
    )
    monkeypatch.setattr("slopmortem.pipeline.recall", fake_recaller)

    deps = await _make_recall_deps(llm)
    persist_deps = await _make_persist_deps(tmp_path)

    report = await run_query(
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

    assert report.pipeline_meta.recall_persisted_count == 1
    assert report.pipeline_meta.recall_used is True
    # FakeRecaller saw exactly one call with the right pitch.
    assert len(fake_recaller.calls) == 1
    assert fake_recaller.calls[0].pitch == ctx.description
    # Two persist attempts: one raised, one succeeded.
    assert call_count["n"] == 2
