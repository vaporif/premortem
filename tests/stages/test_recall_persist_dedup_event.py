"""Tests for the ``RECALL_DEDUPED_EXISTING`` span event wiring.

The new event fires in ``_journal_writes._process_entry`` when the resolver
returns ``alias_blocked`` for an ``llm_recall`` row — pure telemetry so the
audit dashboard can count how often LLM recall surfaces something the
corpus already has. ``resolver_flipped`` deliberately stays uncovered:
``RESOLVER_FLIP_DETECTED`` already fires on that path via
``res.span_events``, so emitting here too would double-count.

Both tests stub ``resolve_entity`` so they exercise the wiring in isolation
from the three-tier resolver internals.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from lmnr import Laminar

from slopmortem.config import Config
from slopmortem.corpus import MergeJournal, ResolveResult
from slopmortem.corpus.sources._names import SOURCE_CRUNCHBASE_CSV, SOURCE_LLM_RECALL
from slopmortem.ingest import InMemoryCorpus
from slopmortem.ingest._fan_out import _FanoutResult
from slopmortem.ingest._journal_writes import ProcessOutcome, _process_entry
from slopmortem.llm import FakeEmbeddingClient, FakeLLMClient
from slopmortem.models import Facets, RawEntry
from slopmortem.tracing import SpanEvent

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


_CANONICAL = "acme.com"
_HAIKU = "anthropic/claude-haiku-4.5"
_BODY = "Acme was a fintech startup that shut down in 2024. " * 30


def _stub_sparse(_text: str) -> dict[int, float]:
    return {0: 1.0}


def _facets() -> Facets:
    return Facets(
        sector="fintech",
        business_model="b2b_saas",
        customer_type="smb",
        geography="us",
        monetization="subscription_recurring",
        founding_year=2021,
        failure_year=2024,
    )


def _entry(source: str, source_id: str = "acme-recall") -> RawEntry:
    return RawEntry(
        source=source,
        source_id=source_id,
        url="https://acme.com/post",
        markdown_text=_BODY,
        raw_html=None,
        fetched_at=datetime(2026, 5, 8, tzinfo=UTC),
    )


async def _make_journal(tmp_path: Path) -> MergeJournal:
    j = MergeJournal(tmp_path / "j.sqlite")
    await j.init()
    return j


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    captured: list[str] = []

    def _fake_event(
        *,
        name: str,
        attributes: dict[str, object] | None = None,
    ) -> None:
        captured.append(name)

    # Production guards every Laminar.event() behind ``is_initialized()``; the
    # tracer is inert under default test fakes, so pin it true to exercise the
    # emit wiring.
    monkeypatch.setattr(Laminar, "is_initialized", staticmethod(lambda: True))
    monkeypatch.setattr(Laminar, "event", _fake_event)
    return captured


def _stub_resolver(
    monkeypatch: pytest.MonkeyPatch,
    action: Literal["alias_blocked", "resolver_flipped"],
) -> None:
    """Force ``resolve_entity`` to return *action*; the wiring is what we test."""
    span_events: list[str] = []
    if action == "resolver_flipped":
        # Mirror the resolver's real behavior — RESOLVER_FLIP_DETECTED rides
        # back via span_events. The wiring must NOT add a second emission.
        span_events.append(SpanEvent.RESOLVER_FLIP_DETECTED.value)

    async def _fake_resolve(*_args: object, **_kwargs: object) -> ResolveResult:
        return ResolveResult(
            canonical_id=_CANONICAL,
            action=action,
            span_events=span_events,
        )

    monkeypatch.setattr("slopmortem.ingest._journal_writes.resolve_entity", _fake_resolve)


async def test_alias_blocked_recall_emits_recall_deduped_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _capture_events(monkeypatch)
    _stub_resolver(monkeypatch, "alias_blocked")

    journal = await _make_journal(tmp_path)
    corpus = InMemoryCorpus()
    config = Config(max_cost_usd_per_ingest=100.0, ingest_concurrency=1)
    span_events: list[str] = []
    entry = _entry(SOURCE_LLM_RECALL)
    fan = _FanoutResult(facets=_facets(), summary="acme summary.", cache_read=0, cache_creation=0)

    outcome = await _process_entry(
        entry,
        body=_BODY,
        fan=fan,
        journal=journal,
        corpus=corpus,
        embed_client=FakeEmbeddingClient(model=config.embed_model_id),
        llm=FakeLLMClient(canned={}, default_model=_HAIKU),
        config=config,
        post_mortems_root=tmp_path,
        slop_score=0.0,
        force=False,
        span_events=span_events,
        sparse_encoder=_stub_sparse,
    )

    assert outcome is ProcessOutcome.SKIPPED
    assert SpanEvent.RECALL_DEDUPED_EXISTING.value in captured


async def test_alias_blocked_non_recall_does_not_emit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crunchbase rows hitting ``alias_blocked`` are not the recall-dedup signal."""
    captured = _capture_events(monkeypatch)
    _stub_resolver(monkeypatch, "alias_blocked")

    journal = await _make_journal(tmp_path)
    corpus = InMemoryCorpus()
    config = Config(max_cost_usd_per_ingest=100.0, ingest_concurrency=1)
    entry = _entry(SOURCE_CRUNCHBASE_CSV, source_id="acme-cb")
    fan = _FanoutResult(facets=_facets(), summary="acme summary.", cache_read=0, cache_creation=0)

    await _process_entry(
        entry,
        body=_BODY,
        fan=fan,
        journal=journal,
        corpus=corpus,
        embed_client=FakeEmbeddingClient(model=config.embed_model_id),
        llm=FakeLLMClient(canned={}, default_model=_HAIKU),
        config=config,
        post_mortems_root=tmp_path,
        slop_score=0.0,
        force=False,
        span_events=[],
        sparse_encoder=_stub_sparse,
    )

    assert SpanEvent.RECALL_DEDUPED_EXISTING.value not in captured


async def test_resolver_flipped_recall_does_not_emit_recall_deduped_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``RESOLVER_FLIP_DETECTED`` already covers the flip path; this event must not also fire."""
    captured = _capture_events(monkeypatch)
    _stub_resolver(monkeypatch, "resolver_flipped")

    journal = await _make_journal(tmp_path)
    corpus = InMemoryCorpus()
    config = Config(max_cost_usd_per_ingest=100.0, ingest_concurrency=1)
    span_events: list[str] = []
    entry = _entry(SOURCE_LLM_RECALL)
    fan = _FanoutResult(facets=_facets(), summary="acme summary.", cache_read=0, cache_creation=0)

    outcome = await _process_entry(
        entry,
        body=_BODY,
        fan=fan,
        journal=journal,
        corpus=corpus,
        embed_client=FakeEmbeddingClient(model=config.embed_model_id),
        llm=FakeLLMClient(canned={}, default_model=_HAIKU),
        config=config,
        post_mortems_root=tmp_path,
        slop_score=0.0,
        force=False,
        span_events=span_events,
        sparse_encoder=_stub_sparse,
    )

    assert outcome is ProcessOutcome.SKIPPED
    assert SpanEvent.RECALL_DEDUPED_EXISTING.value not in captured
    # Sanity: the existing flip telemetry rides through span_events, not Laminar.event.
    assert SpanEvent.RESOLVER_FLIP_DETECTED.value in span_events
