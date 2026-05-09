"""Tests for ``stages.recall_persist``: journal+qdrant write through the ingest tail."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from conftest import llm_canned_key
from slopmortem.config import Config
from slopmortem.corpus import MergeJournal
from slopmortem.corpus.sources._names import SOURCE_LLM_RECALL
from slopmortem.ingest import FakeSlopClassifier, IngestResult, InMemoryCorpus, NullProgress
from slopmortem.ingest._helpers import _build_payload
from slopmortem.llm import FakeEmbeddingClient, FakeLLMClient, FakeResponse, render_prompt
from slopmortem.models import Facets, RawEntry, RecallSuggestion
from slopmortem.stages.recall_persist import persist_recall_entry
from slopmortem.stages.recall_verify import VerificationTier, _recall_source_id

if TYPE_CHECKING:
    from pathlib import Path


_HAIKU = "anthropic/claude-haiku-4.5"
# Long enough that ``_entry_summary_text`` doesn't truncate it down to nothing
# (the slop gate would drop an empty body). 30x is also well clear of any token
# budget the fakes might hit.
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


def _canned() -> dict[tuple[str, str, str], FakeResponse]:
    facets_resp = FakeResponse(text=_facets_json())
    summary_resp = FakeResponse(text="Hexagate summary.")
    facet_prompt = render_prompt("facet_extract", description=_BODY)
    summarize_prompt = render_prompt("summarize", body=_BODY, source_id="")
    return {
        llm_canned_key("facet_extract", model=_HAIKU, prompt=facet_prompt): facets_resp,
        llm_canned_key("summarize", model=_HAIKU, prompt=summarize_prompt): summary_resp,
    }


def _suggestion(
    name: str = "Hexagate",
    homepage: str = "https://hexagate.example/",
) -> RecallSuggestion:
    return RecallSuggestion(
        name=name,
        category="Web3 security",
        status="dead",
        homepage_url=homepage,
        failure_year=2024,
        evidence_url="https://news.example/hexagate-shutdown",
        one_liner=f"{name} shut down.",
    )


def _entry_for(suggestion: RecallSuggestion) -> RawEntry:
    """Mirror the verifier's RawEntry shape: ``source=llm_recall``, deterministic id."""
    return RawEntry(
        source=SOURCE_LLM_RECALL,
        source_id=_recall_source_id(suggestion),
        url=str(suggestion.homepage_url),
        markdown_text=_BODY,
        raw_html=None,
        fetched_at=datetime(2026, 5, 8, tzinfo=UTC),
    )


@pytest.fixture
def cfg() -> Config:
    return Config(max_cost_usd_per_ingest=100.0, ingest_concurrency=1)


@dataclass
class _Ctx:
    journal: MergeJournal
    corpus: InMemoryCorpus
    config: Config
    root: Path


async def _make_ctx(tmp_path: Path, cfg: Config) -> _Ctx:
    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()
    return _Ctx(journal=journal, corpus=InMemoryCorpus(), config=cfg, root=tmp_path)


async def _persist(ctx: _Ctx, entry: RawEntry, tier: VerificationTier) -> IngestResult:
    """Wire fakes around ``persist_recall_entry`` and return the IngestResult."""
    llm = FakeLLMClient(canned=_canned(), default_model=_HAIKU)
    embed = FakeEmbeddingClient(model=ctx.config.embed_model_id)
    classifier = FakeSlopClassifier(default_score=0.0)
    result = IngestResult()
    await persist_recall_entry(
        entry,
        tier,
        journal=ctx.journal,
        corpus=ctx.corpus,
        embed_client=embed,
        llm=llm,
        slop_classifier=classifier,
        sparse_encoder=_stub_sparse,
        config=ctx.config,
        post_mortems_root=ctx.root,
        progress=NullProgress(),
        result=result,
    )
    return result


async def test_persist_writes_to_journal_and_qdrant(tmp_path: Path, cfg: Config) -> None:
    ctx = await _make_ctx(tmp_path, cfg)
    entry = _entry_for(_suggestion())

    result = await _persist(ctx, entry, "evidence_only")

    assert result.processed == 1
    assert len(ctx.corpus.points) >= 1
    # is_terminal probes "row written under (source, source_id)";
    # mark_complete is what flips it true.
    assert await ctx.journal.is_terminal(entry.source, entry.source_id)


async def test_persist_idempotent(tmp_path: Path, cfg: Config) -> None:
    ctx = await _make_ctx(tmp_path, cfg)
    entry = _entry_for(_suggestion())

    r1 = await _persist(ctx, entry, "evidence_only")
    n_after_first = len(ctx.corpus.points)
    assert r1.processed == 1
    assert n_after_first >= 1

    # Second call: same source_id, journal short-circuits inside _classify_phase.
    r2 = await _persist(ctx, entry, "evidence_only")
    assert r2.skipped >= 1
    assert r2.processed == 0
    assert len(ctx.corpus.points) == n_after_first


def test_persist_deterministic_source_id() -> None:
    a = _suggestion(name="Hexagate", homepage="https://hexagate.example/")
    b = _suggestion(name="Hexagate", homepage="https://hexagate.example/")
    c = _suggestion(name="Hexagate", homepage="https://hexagate-different.example/")
    assert _recall_source_id(a) == _recall_source_id(b)
    assert _recall_source_id(a) != _recall_source_id(c)


@pytest.mark.parametrize("tier", ["wayback_anchored", "evidence_only"])
async def test_persist_writes_verification_tier_to_payload(
    tmp_path: Path, cfg: Config, tier: VerificationTier
) -> None:
    ctx = await _make_ctx(tmp_path, cfg)
    entry = _entry_for(_suggestion())

    await _persist(ctx, entry, tier)

    assert ctx.corpus.points, "expected at least one qdrant point"
    # CandidatePayload.model_dump emits verification_tier alongside facets/etc;
    # guard against a future refactor that adds `exclude=` in _embed_and_upsert
    # and silently drops the field.
    for point in ctx.corpus.points:
        assert point.payload.get("verification_tier") == tier


def test_build_payload_default_verification_tier_is_none() -> None:
    """Sanity: omitting the tier kwarg (crawler path) keeps ``verification_tier=None``.

    Catches a regression where the default flips or the kwarg becomes required.
    """
    facets = Facets(
        sector="fintech",
        business_model="b2b_saas",
        customer_type="smb",
        geography="us",
        monetization="subscription_recurring",
    )
    payload = _build_payload(
        facets=facets,
        summary="s",
        body="b",
        slop_score=0.0,
        sources_seen=[],
        provenance_id="curated:example",
        text_id="abc123",
        name="Example",
        entry_source="curated",
    )
    assert payload.verification_tier is None
    # model_dump still emits the field as None — crawler-path qdrant payloads
    # carry the same keys as recall-path ones for schema parity.
    dumped = payload.model_dump(mode="json")
    assert dumped["verification_tier"] is None
