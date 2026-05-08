"""Tests for the ingest orchestrator: summary wiring, slop, throttle, fan-out, cache."""

from __future__ import annotations

import asyncio
import importlib
import json
import typing
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from conftest import llm_canned_key
from slopmortem.budget import Budget
from slopmortem.config import Config
from slopmortem.corpus import MergeJournal
from slopmortem.ingest import (
    FakeSlopClassifier,
    InMemoryCorpus,
    ingest,
)
from slopmortem.llm import FakeEmbeddingClient, FakeLLMClient, FakeResponse, render_prompt
from slopmortem.models import RawEntry
from slopmortem.tracing import SpanEvent

# Façade re-exports shadow submodules: monkeypatching `chunk_markdown` requires
# the fan-out submodule (where `_embed_and_upsert` calls it), not the
# `slopmortem.ingest` package facade or the orchestrator.
ingest_module = importlib.import_module("slopmortem.ingest._fan_out")

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from slopmortem.corpus import Chunk

_HAIKU = "anthropic/claude-haiku-4.5"
_ENTRY_BODY = "Acme was a startup that sold widgets and ran out of money in 2021. " * 30


def _stub_sparse(_text: str) -> dict[int, float]:
    return {0: 1.0}


def _fake_facets_json() -> str:
    return json.dumps(
        {
            "sector": "retail_ecommerce",
            "business_model": "b2b_marketplace",
            "customer_type": "smb",
            "geography": "us",
            "monetization": "transaction_fee",
            "founding_year": 2018,
            "failure_year": 2021,
        }
    )


def _summary_text() -> str:
    return (
        "Acme was a B2B marketplace for industrial scrap metal. It sold to mid-market "
        "manufacturers in the US, raised a small seed in 2018, and shut down in 2021 "
        "after running out of cash."
    )


def _canned_for_run(  # noqa: PLR0913 - every knob is a meaningful test variable
    *,
    summary_text: str | None = None,
    facets_json: str | None = None,
    cache_creation_warm: int = 1234,
    cache_read_fanout: int = 5000,
    cache_creation_fanout: int = 0,
    body: str = _ENTRY_BODY,
) -> dict[tuple[str, str, str], FakeResponse]:
    """Build canned entries for ingest of any number of entries that share ``body``.

    All ``_entry(...)`` instances in this module share the same default
    markdown_text, so the rendered prompts collapse to one (template, model,
    prompt_hash) per stage regardless of fan-out width: one cache-warm
    summarize prompt, one fan-out summarize prompt, one facet_extract
    prompt. Multiple calls to the same key all return the same response.
    """
    facets_text = facets_json or _fake_facets_json()
    sum_text = summary_text or _summary_text()
    facet_resp = FakeResponse(
        text=facets_text,
        cache_read_tokens=cache_read_fanout,
        cache_creation_tokens=cache_creation_warm,
    )
    summarize_resp = FakeResponse(
        text=sum_text,
        cache_read_tokens=cache_read_fanout,
        cache_creation_tokens=cache_creation_fanout,
    )
    facet_prompt = render_prompt("facet_extract", description=body)
    summarize_warm_prompt = render_prompt("summarize", body=body[:1000], source_id="warm")
    summarize_fanout_prompt = render_prompt("summarize", body=body, source_id="")
    return {
        llm_canned_key("facet_extract", model=_HAIKU, prompt=facet_prompt): facet_resp,
        llm_canned_key("summarize", model=_HAIKU, prompt=summarize_warm_prompt): summarize_resp,
        llm_canned_key("summarize", model=_HAIKU, prompt=summarize_fanout_prompt): summarize_resp,
    }


@dataclass
class _ListSource:
    """In-memory list of pre-built RawEntry; bypasses HTTP."""

    entries: list[RawEntry]
    raise_on_index: int | None = None
    raised_exc: Exception | None = None

    async def fetch(self) -> AsyncIterator[RawEntry]:
        for i, e in enumerate(self.entries):
            if self.raise_on_index is not None and i == self.raise_on_index:
                exc = self.raised_exc or RuntimeError("fake source error")
                raise exc
            yield e


def _entry(
    *,
    source: str = "curated",
    source_id: str = "1",
    url: str | None = "https://acme.com",
) -> RawEntry:
    return RawEntry(
        source=source,
        source_id=source_id,
        url=url,
        raw_html=None,
        markdown_text="Acme was a startup that sold widgets and ran out of money in 2021. " * 30,
        fetched_at=datetime(2026, 4, 28, tzinfo=UTC),
    )


@pytest.fixture
def cfg() -> Config:
    return Config(
        max_cost_usd_per_ingest=100.0,
        ingest_concurrency=20,
    )


async def test_ingest_wires_summary_into_payload(tmp_path, cfg):
    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()
    corpus = InMemoryCorpus()
    llm = FakeLLMClient(canned=_canned_for_run(), default_model=_HAIKU)
    embed = FakeEmbeddingClient(model=cfg.embed_model_id)
    budget = Budget(cap_usd=cfg.max_cost_usd_per_ingest)
    classifier = FakeSlopClassifier(default_score=0.0)
    sources = [_ListSource(entries=[_entry()])]

    result = await ingest(
        sources=sources,
        enrichers=[],
        journal=journal,
        corpus=corpus,
        llm=llm,
        embed_client=embed,
        budget=budget,
        slop_classifier=classifier,
        config=cfg,
        post_mortems_root=tmp_path / "post_mortems",
        sparse_encoder=_stub_sparse,
    )

    assert result.processed == 1
    assert len(corpus.points) >= 1
    payload = corpus.points[0].payload
    assert payload["summary"]
    assert payload["summary"] == _summary_text()


async def test_ingest_per_source_failure_does_not_abort_run(tmp_path, cfg):
    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()
    corpus = InMemoryCorpus()
    llm = FakeLLMClient(canned=_canned_for_run(), default_model=_HAIKU)
    embed = FakeEmbeddingClient(model=cfg.embed_model_id)
    budget = Budget(cap_usd=cfg.max_cost_usd_per_ingest)
    classifier = FakeSlopClassifier(default_score=0.0)

    bad_source = _ListSource(
        entries=[_entry(source="bad", source_id="1", url="https://bad.com")],
        raise_on_index=0,
        raised_exc=RuntimeError("HTTP 429"),
    )
    good_source = _ListSource(
        entries=[_entry(source="curated", source_id="2", url="https://good.com")],
    )

    result = await ingest(
        sources=[bad_source, good_source],
        enrichers=[],
        journal=journal,
        corpus=corpus,
        llm=llm,
        embed_client=embed,
        budget=budget,
        slop_classifier=classifier,
        config=cfg,
        post_mortems_root=tmp_path / "post_mortems",
        sparse_encoder=_stub_sparse,
    )
    assert result.processed == 1
    assert result.source_failures == 1


async def test_ingest_bounded_fan_out_concurrency(tmp_path):
    cfg = Config(max_cost_usd_per_ingest=1000.0, ingest_concurrency=5)
    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()
    corpus = InMemoryCorpus()
    embed = FakeEmbeddingClient(model=cfg.embed_model_id)
    budget = Budget(cap_usd=cfg.max_cost_usd_per_ingest)
    classifier = FakeSlopClassifier(default_score=0.0)

    in_flight = 0
    peak = 0
    lock = asyncio.Lock()
    canned = _canned_for_run()

    class _CountingLLM(FakeLLMClient):
        @typing.override
        async def complete(self, prompt, **kw):
            nonlocal in_flight, peak
            async with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            try:
                # Add latency so calls overlap.
                await asyncio.sleep(0.02)
                return await super().complete(prompt, **kw)
            finally:
                async with lock:
                    in_flight -= 1

    llm = _CountingLLM(canned=canned, default_model=_HAIKU)

    n_entries = 30
    entries = [_entry(source_id=str(i), url=f"https://e{i}.com") for i in range(n_entries)]
    sources = [_ListSource(entries=entries)]

    result = await ingest(
        sources=sources,
        enrichers=[],
        journal=journal,
        corpus=corpus,
        llm=llm,
        embed_client=embed,
        budget=budget,
        slop_classifier=classifier,
        config=cfg,
        post_mortems_root=tmp_path / "post_mortems",
        sparse_encoder=_stub_sparse,
    )
    assert result.processed == n_entries
    # Cache-warm runs serially before the fan-out, so peak measures fan-out only.
    assert peak <= cfg.ingest_concurrency, f"peak={peak} exceeds limit={cfg.ingest_concurrency}"


async def test_ingest_cache_warm_records_creation_tokens(tmp_path, cfg):
    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()
    corpus = InMemoryCorpus()
    embed = FakeEmbeddingClient(model=cfg.embed_model_id)
    budget = Budget(cap_usd=cfg.max_cost_usd_per_ingest)
    classifier = FakeSlopClassifier(default_score=0.0)

    # Cache-warm renders the summarize template, so the summarize canned entry
    # needs cache_creation_tokens > 0 to satisfy the warm check. The same
    # canned entry gets reused for fan-out; this test only asserts warm
    # bookkeeping, so reuse is fine.
    facet_resp = FakeResponse(
        text=_fake_facets_json(),
        cache_creation_tokens=0,
        cache_read_tokens=8000,
    )
    summarize_resp = FakeResponse(
        text=_summary_text(),
        cache_creation_tokens=2048,
        cache_read_tokens=8000,
    )
    facet_prompt = render_prompt("facet_extract", description=_ENTRY_BODY)
    summarize_warm_prompt = render_prompt("summarize", body=_ENTRY_BODY[:1000], source_id="warm")
    summarize_fanout_prompt = render_prompt("summarize", body=_ENTRY_BODY, source_id="")
    canned: dict[tuple[str, str, str], FakeResponse] = {
        llm_canned_key("facet_extract", model=_HAIKU, prompt=facet_prompt): facet_resp,
        llm_canned_key("summarize", model=_HAIKU, prompt=summarize_warm_prompt): summarize_resp,
        llm_canned_key("summarize", model=_HAIKU, prompt=summarize_fanout_prompt): summarize_resp,
    }
    llm = FakeLLMClient(canned=canned, default_model=_HAIKU)

    sources = [_ListSource(entries=[_entry()])]
    result = await ingest(
        sources=sources,
        enrichers=[],
        journal=journal,
        corpus=corpus,
        llm=llm,
        embed_client=embed,
        budget=budget,
        slop_classifier=classifier,
        config=cfg,
        post_mortems_root=tmp_path / "post_mortems",
        sparse_encoder=_stub_sparse,
    )
    assert result.cache_warmed is True
    assert result.cache_creation_tokens_warm > 0


async def test_ingest_cache_read_ratio_warning(tmp_path, cfg):
    """Read-ratio < 0.80 across the first 5 fan-out responses emits a warning span event."""
    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()
    corpus = InMemoryCorpus()
    embed = FakeEmbeddingClient(model=cfg.embed_model_id)
    budget = Budget(cap_usd=cfg.max_cost_usd_per_ingest)
    classifier = FakeSlopClassifier(default_score=0.0)

    # Low cache-read ratio: read=10, creation=100 means 10/(10+100) ~ 0.09, well under 0.80.
    canned = _canned_for_run(
        cache_creation_warm=2048,
        cache_read_fanout=10,
        cache_creation_fanout=100,
    )
    llm = FakeLLMClient(canned=canned, default_model=_HAIKU)

    n = 6
    entries = [_entry(source_id=str(i), url=f"https://e{i}.com") for i in range(n)]
    sources = [_ListSource(entries=entries)]

    result = await ingest(
        sources=sources,
        enrichers=[],
        journal=journal,
        corpus=corpus,
        llm=llm,
        embed_client=embed,
        budget=budget,
        slop_classifier=classifier,
        config=cfg,
        post_mortems_root=tmp_path / "post_mortems",
        sparse_encoder=_stub_sparse,
    )
    assert SpanEvent.CACHE_READ_RATIO_LOW.value in result.span_events


async def test_ingest_quarantines_slop(tmp_path, cfg):
    """slop_score > slop_threshold yields a quarantine row and no qdrant point."""
    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()
    corpus = InMemoryCorpus()
    embed = FakeEmbeddingClient(model=cfg.embed_model_id)
    budget = Budget(cap_usd=cfg.max_cost_usd_per_ingest)
    classifier = FakeSlopClassifier(default_score=0.95)
    llm = FakeLLMClient(canned=_canned_for_run(), default_model=_HAIKU)

    sources = [_ListSource(entries=[_entry(source="hn")])]
    result = await ingest(
        sources=sources,
        enrichers=[],
        journal=journal,
        corpus=corpus,
        llm=llm,
        embed_client=embed,
        budget=budget,
        slop_classifier=classifier,
        config=cfg,
        post_mortems_root=tmp_path / "post_mortems",
        sparse_encoder=_stub_sparse,
    )
    assert result.processed == 0
    assert result.quarantined == 1
    assert len(corpus.points) == 0
    quarantined_rows = await journal.fetch_quarantined()
    assert len(quarantined_rows) == 1
    # Quarantine markdown is written under post_mortems/quarantine/.
    quarantine_dir = tmp_path / "post_mortems" / "quarantine"
    assert quarantine_dir.exists()
    assert any(p.suffix == ".md" for p in quarantine_dir.iterdir())


async def test_ingest_payload_sources_url_only_when_url_present(tmp_path, cfg):
    """Payload sources holds the URL; provenance_id holds <source>:<source_id>."""
    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()
    corpus = InMemoryCorpus()
    llm = FakeLLMClient(canned=_canned_for_run(), default_model=_HAIKU)
    embed = FakeEmbeddingClient(model=cfg.embed_model_id)
    budget = Budget(cap_usd=cfg.max_cost_usd_per_ingest)
    classifier = FakeSlopClassifier(default_score=0.0)
    sources = [
        _ListSource(
            entries=[
                _entry(
                    source="curated",
                    source_id="Celsius Network",
                    url="https://example.com/celsius",
                )
            ]
        )
    ]

    result = await ingest(
        sources=sources,
        enrichers=[],
        journal=journal,
        corpus=corpus,
        llm=llm,
        embed_client=embed,
        budget=budget,
        slop_classifier=classifier,
        config=cfg,
        post_mortems_root=tmp_path / "post_mortems",
        sparse_encoder=_stub_sparse,
    )

    assert result.processed == 1
    assert len(corpus.points) >= 1
    payload = corpus.points[0].payload
    assert payload["sources"] == ["https://example.com/celsius"]
    assert payload["provenance_id"] == "curated:Celsius Network"


async def test_ingest_payload_sources_empty_when_url_missing(tmp_path, cfg):
    """When RawEntry.url is None, sources is empty and provenance_id still carries the synth id."""
    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()
    corpus = InMemoryCorpus()
    llm = FakeLLMClient(canned=_canned_for_run(), default_model=_HAIKU)
    embed = FakeEmbeddingClient(model=cfg.embed_model_id)
    budget = Budget(cap_usd=cfg.max_cost_usd_per_ingest)
    classifier = FakeSlopClassifier(default_score=0.0)
    sources = [
        _ListSource(
            entries=[
                _entry(
                    source="curated",
                    source_id="Celsius Network",
                    url=None,
                )
            ]
        )
    ]

    result = await ingest(
        sources=sources,
        enrichers=[],
        journal=journal,
        corpus=corpus,
        llm=llm,
        embed_client=embed,
        budget=budget,
        slop_classifier=classifier,
        config=cfg,
        post_mortems_root=tmp_path / "post_mortems",
        sparse_encoder=_stub_sparse,
    )

    assert result.processed == 1
    assert len(corpus.points) >= 1
    payload = corpus.points[0].payload
    assert payload["sources"] == []
    assert payload["provenance_id"] == "curated:Celsius Network"


async def test_ingest_zero_chunks_skips_mark_complete(tmp_path, cfg, monkeypatch):
    # Zero chunks must not produce a "complete" journal row: that combination
    # is silent corpus drift.
    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()
    corpus = InMemoryCorpus()
    llm = FakeLLMClient(canned=_canned_for_run(), default_model=_HAIKU)
    embed = FakeEmbeddingClient(model=cfg.embed_model_id)
    budget = Budget(cap_usd=cfg.max_cost_usd_per_ingest)
    classifier = FakeSlopClassifier(default_score=0.0)
    sources = [_ListSource(entries=[_entry()])]

    # Stub the chunker to return [] without touching the tokenizer; we want
    # the production guard to fire, not a real edge case.
    def _no_chunks(_text: str, *, parent_canonical_id: str) -> list[Chunk]:
        del parent_canonical_id
        return []

    monkeypatch.setattr(ingest_module, "chunk_markdown", _no_chunks)

    result = await ingest(
        sources=sources,
        enrichers=[],
        journal=journal,
        corpus=corpus,
        llm=llm,
        embed_client=embed,
        budget=budget,
        slop_classifier=classifier,
        config=cfg,
        post_mortems_root=tmp_path / "post_mortems",
        sparse_encoder=_stub_sparse,
    )

    assert result.processed == 0
    assert result.skipped_empty == 1
    assert corpus.points == []
    rows = await journal.fetch_all()
    assert rows, "expected a pending journal row to exist"
    assert all(r["merge_state"] != "complete" for r in rows)


class _FailingDeleteCorpus(InMemoryCorpus):
    """Re-merge variant whose ``delete_chunks_for_canonical`` always raises.

    Other corpus methods stay inherited so the first ingest pass (which never
    deletes) succeeds normally and seeds the journal for the second pass.
    """

    delete_calls: int = 0

    @typing.override
    async def delete_chunks_for_canonical(self, canonical_id: str) -> None:
        type(self).delete_calls += 1
        msg = f"simulated qdrant transport error deleting {canonical_id}"
        raise RuntimeError(msg)


async def test_delete_failure_aborts_entry_marked_failed(tmp_path, cfg):
    # Pass 1 seeds a complete row; pass 2 forces re-processing and the failing
    # delete_chunks aborts the entry. Expected: result.failed counts it, no
    # upsert lands, and the pass-1 row is untouched.
    entry = _entry()
    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()

    # Pass 1: normal ingest, populates the journal as ``complete``.
    await ingest(
        sources=[_ListSource(entries=[entry])],
        enrichers=[],
        journal=journal,
        corpus=InMemoryCorpus(),
        llm=FakeLLMClient(canned=_canned_for_run(), default_model=_HAIKU),
        embed_client=FakeEmbeddingClient(model=cfg.embed_model_id),
        budget=Budget(cap_usd=cfg.max_cost_usd_per_ingest),
        slop_classifier=FakeSlopClassifier(default_score=0.0),
        config=cfg,
        post_mortems_root=tmp_path / "post_mortems",
        sparse_encoder=_stub_sparse,
    )
    seeded = await journal.fetch_all()
    assert seeded, "pass 1 should have seeded the journal"
    canonical_id = seeded[0]["canonical_id"]

    # Pass 2: re-process into the failing corpus. There are no real orphans
    # to inspect (the corpus starts empty), so the test asserts on the abort
    # itself: corpus.points stays empty and result.failed == 1.
    failing = _FailingDeleteCorpus()
    _FailingDeleteCorpus.delete_calls = 0
    result = await ingest(
        sources=[_ListSource(entries=[entry])],
        enrichers=[],
        journal=journal,
        corpus=failing,
        llm=FakeLLMClient(canned=_canned_for_run(), default_model=_HAIKU),
        embed_client=FakeEmbeddingClient(model=cfg.embed_model_id),
        budget=Budget(cap_usd=cfg.max_cost_usd_per_ingest),
        slop_classifier=FakeSlopClassifier(default_score=0.0),
        config=cfg,
        post_mortems_root=tmp_path / "post_mortems",
        sparse_encoder=_stub_sparse,
        force=True,
    )

    assert _FailingDeleteCorpus.delete_calls == 1
    assert result.failed == 1
    assert result.processed == 0
    assert failing.points == []

    # upsert_pending demoted the row before the delete; the failing delete
    # aborts before mark_complete, so the row stays non-complete and reconcile
    # picks it up on the next pass.
    rows = await journal.fetch_by_key(canonical_id, entry.source, entry.source_id)
    assert rows, "the journal row must still exist"
    assert all(r["merge_state"] != "complete" for r in rows)


async def test_ingest_dry_run_does_no_journal_or_corpus_writes(tmp_path, cfg):
    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()
    corpus = InMemoryCorpus()
    llm = FakeLLMClient(canned={}, default_model=_HAIKU)
    embed = FakeEmbeddingClient(model=cfg.embed_model_id)
    budget = Budget(cap_usd=cfg.max_cost_usd_per_ingest)
    classifier = FakeSlopClassifier(default_score=0.0)
    sources = [_ListSource(entries=[_entry(), _entry(source_id="2", url="https://e2.com")])]

    result = await ingest(
        sources=sources,
        enrichers=[],
        journal=journal,
        corpus=corpus,
        llm=llm,
        embed_client=embed,
        budget=budget,
        slop_classifier=classifier,
        config=cfg,
        post_mortems_root=tmp_path / "post_mortems",
        sparse_encoder=_stub_sparse,
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.would_process == 2
    assert result.processed == 0
    assert corpus.points == []
    assert await journal.fetch_pending() == []


async def test_ingest_skips_entry_with_empty_body(tmp_path, cfg):
    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()
    corpus = InMemoryCorpus()
    llm = FakeLLMClient(canned={}, default_model=_HAIKU)
    embed = FakeEmbeddingClient(model=cfg.embed_model_id)
    budget = Budget(cap_usd=cfg.max_cost_usd_per_ingest)
    classifier = FakeSlopClassifier(default_score=0.0)

    empty_entry = RawEntry(
        source="curated",
        source_id="empty-1",
        url="https://example.com/empty",
        raw_html=None,
        markdown_text=None,
        fetched_at=datetime(2026, 4, 28, tzinfo=UTC),
    )
    sources = [_ListSource(entries=[empty_entry])]

    result = await ingest(
        sources=sources,
        enrichers=[],
        journal=journal,
        corpus=corpus,
        llm=llm,
        embed_client=embed,
        budget=budget,
        slop_classifier=classifier,
        config=cfg,
        post_mortems_root=tmp_path / "post_mortems",
        sparse_encoder=_stub_sparse,
    )
    assert result.seen == 1
    assert result.skipped == 1
    assert result.processed == 0
    assert corpus.points == []


async def test_ingest_all_quarantined_short_circuits_before_fanout(tmp_path, cfg):
    """If every entry quarantines, the fan-out / write phases never run — no token spend."""
    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()
    corpus = InMemoryCorpus()
    llm = FakeLLMClient(canned={}, default_model=_HAIKU)
    embed = FakeEmbeddingClient(model=cfg.embed_model_id)
    budget = Budget(cap_usd=cfg.max_cost_usd_per_ingest)
    classifier = FakeSlopClassifier(default_score=0.95)
    sources = [
        _ListSource(
            entries=[
                _entry(source="hn", source_id="1", url="https://hn.com/1"),
                _entry(source="hn", source_id="2", url="https://hn.com/2"),
            ]
        )
    ]

    result = await ingest(
        sources=sources,
        enrichers=[],
        journal=journal,
        corpus=corpus,
        llm=llm,
        embed_client=embed,
        budget=budget,
        slop_classifier=classifier,
        config=cfg,
        post_mortems_root=tmp_path / "post_mortems",
        sparse_encoder=_stub_sparse,
    )
    assert result.seen == 2
    assert result.quarantined == 2
    assert result.processed == 0
    assert corpus.points == []
    # Skipped, not failed: warming was never attempted.
    assert SpanEvent.CACHE_WARM_FAILED.value not in result.span_events


async def test_ingest_records_at_most_max_recorded_errors_attributes(tmp_path, cfg):
    """The IngestResult counter must stay accurate past the Laminar attribute cap of 50."""
    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()
    corpus = InMemoryCorpus()
    llm = FakeLLMClient(canned={}, default_model=_HAIKU)
    embed = FakeEmbeddingClient(model=cfg.embed_model_id)
    budget = Budget(cap_usd=cfg.max_cost_usd_per_ingest)
    classifier = FakeSlopClassifier(default_score=0.0)

    n = 52
    entries = [_entry(source_id=str(i), url=f"https://e{i}.com") for i in range(n)]

    class _AlwaysFailEnricher:
        async def enrich(self, entry: RawEntry) -> RawEntry:
            del entry
            msg = "enricher always raises"
            raise RuntimeError(msg)

    sources = [_ListSource(entries=entries)]
    result = await ingest(
        sources=sources,
        enrichers=[_AlwaysFailEnricher()],
        journal=journal,
        corpus=corpus,
        llm=llm,
        embed_client=embed,
        budget=budget,
        slop_classifier=classifier,
        config=cfg,
        post_mortems_root=tmp_path / "post_mortems",
        sparse_encoder=_stub_sparse,
    )
    assert result.seen == n
    assert result.errors == n
    assert result.processed == 0
    failed_events = [e for e in result.span_events if e == SpanEvent.INGEST_ENTRY_FAILED.value]
    assert len(failed_events) == n


async def test_ingest_write_phase_reraises_cancelled_error(tmp_path, cfg, monkeypatch):
    """The per-entry isolator catches ``Exception`` but lets ``BaseException`` bubble out."""
    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()
    corpus = InMemoryCorpus()
    llm = FakeLLMClient(canned=_canned_for_run(), default_model=_HAIKU)
    embed = FakeEmbeddingClient(model=cfg.embed_model_id)
    budget = Budget(cap_usd=cfg.max_cost_usd_per_ingest)
    classifier = FakeSlopClassifier(default_score=0.0)

    sources = [_ListSource(entries=[_entry()])]

    # Patch the binding in _ingest, not _journal_writes — façade re-export shadow.
    ingest_inner = importlib.import_module("slopmortem.ingest._ingest")

    async def _cancel_in_process(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(ingest_inner, "_process_entry", _cancel_in_process)

    with pytest.raises(asyncio.CancelledError):
        await ingest(
            sources=sources,
            enrichers=[],
            journal=journal,
            corpus=corpus,
            llm=llm,
            embed_client=embed,
            budget=budget,
            slop_classifier=classifier,
            config=cfg,
            post_mortems_root=tmp_path / "post_mortems",
            sparse_encoder=_stub_sparse,
        )


async def test_ingest_skips_title_pre_filter_rejected_before_slop_classify(tmp_path, cfg):
    """Rejected entries short-circuit before the slop classifier and never reach the corpus."""
    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()
    corpus = InMemoryCorpus()
    llm = FakeLLMClient(canned={}, default_model=_HAIKU)
    embed = FakeEmbeddingClient(model=cfg.embed_model_id)
    budget = Budget(cap_usd=cfg.max_cost_usd_per_ingest)

    @dataclass
    class _RejectAllEnricher:
        async def enrich(self, entry: RawEntry) -> RawEntry:
            return entry.model_copy(update={"title_pre_filter_rejected": True})

    @dataclass
    class _NeverCalledClassifier:
        async def score(self, text: str) -> float:
            del text
            msg = "slop classifier should not be called for title-pre-filter-rejected entries"
            raise AssertionError(msg)

    n = 3
    entries = [_entry(source_id=str(i), url=f"https://e{i}.com") for i in range(n)]
    sources = [_ListSource(entries=entries)]

    result = await ingest(
        sources=sources,
        enrichers=[_RejectAllEnricher()],
        journal=journal,
        corpus=corpus,
        llm=llm,
        embed_client=embed,
        budget=budget,
        slop_classifier=_NeverCalledClassifier(),
        config=cfg,
        post_mortems_root=tmp_path / "post_mortems",
        sparse_encoder=_stub_sparse,
    )
    assert result.seen == n
    assert result.skipped == n
    assert result.processed == 0
    assert result.errors == 0
    assert corpus.points == []
    assert SpanEvent.SLOP_QUARANTINED.value not in result.span_events
