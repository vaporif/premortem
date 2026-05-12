"""Edge-case branches for ingest port stand-ins, helpers, and classifier impls."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from conftest import llm_canned_key
from slopmortem.ingest import (
    FakeSlopClassifier,
    HaikuSlopClassifier,
    IngestPhase,
    InMemoryCorpus,
)
from slopmortem.ingest._helpers import (
    _entry_name,
    _entry_summary_text,
    _gather_entries,
    _truncate_to_tokens,
)
from slopmortem.ingest._ports import NullProgress
from slopmortem.llm import FakeLLMClient, FakeResponse, render_prompt
from slopmortem.models import RawEntry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_HAIKU = "anthropic/claude-haiku-4.5"


def _entry(
    *,
    source: str = "curated",
    source_id: str = "1",
    markdown_text: str | None = "body",
    raw_html: str | None = None,
) -> RawEntry:
    return RawEntry(
        source=source,
        source_id=source_id,
        url="https://example.com",
        raw_html=raw_html,
        markdown_text=markdown_text,
        fetched_at=datetime(2026, 4, 28, tzinfo=UTC),
    )


def test_truncate_to_tokens_zero_max_returns_input_unchanged():
    text = "any text whatsoever, doesn't matter how long " * 100
    assert _truncate_to_tokens(text, 0) == text


def test_truncate_to_tokens_under_limit_returns_input_unchanged():
    text = "short"
    assert _truncate_to_tokens(text, 1000) == text


def test_truncate_to_tokens_over_limit_clips_to_token_budget():
    text = "the quick brown fox jumps over the lazy dog. " * 200
    out = _truncate_to_tokens(text, 10)
    assert out != text
    assert len(out) < len(text)


def test_entry_summary_text_returns_markdown_when_present():
    e = _entry(markdown_text="hello body")
    assert _entry_summary_text(e, max_tokens=100) == "hello body"


def test_entry_summary_text_falls_back_to_html_extract():
    body_paragraph = (
        "<p>Acme Corporation was a B2B marketplace for industrial widgets. "
        "It served mid-market manufacturers across the United States and "
        "raised a small seed round in 2018, before shutting down in 2021.</p>"
    )
    html = "<html><body>" + (body_paragraph * 5) + "</body></html>"
    e = _entry(markdown_text=None, raw_html=html)
    out = _entry_summary_text(e, max_tokens=400)
    assert "Acme Corporation" in out
    assert "<p>" not in out


def test_entry_summary_text_returns_empty_when_no_content():
    e = _entry(markdown_text=None, raw_html=None)
    assert _entry_summary_text(e, max_tokens=100) == ""


def test_entry_name_prefers_title_when_set():
    e = RawEntry(
        source="hn_algolia",
        source_id="21055034",
        url="https://example.com",
        title="Kin (by Kik Interactive) shuts down",
        markdown_text="# Some article heading\n\nbody",
        fetched_at=datetime(2026, 4, 28, tzinfo=UTC),
    )
    assert _entry_name(e) == "Kin (by Kik Interactive) shuts down"


def test_entry_name_falls_back_to_first_heading():
    e = RawEntry(
        source="tavily_news",
        source_id="https://example.com/post",
        url="https://example.com/post",
        title=None,
        markdown_text="Some preamble\n\n# Acme Corp shuts down\n\nDetails follow.",
        fetched_at=datetime(2026, 4, 28, tzinfo=UTC),
    )
    assert _entry_name(e) == "Acme Corp shuts down"


def test_entry_name_matches_h2_when_no_h1():
    e = RawEntry(
        source="tavily_news",
        source_id="https://example.com/post",
        url="https://example.com/post",
        title=None,
        markdown_text="preamble\n## Foo Co goes bankrupt\nrest",
        fetched_at=datetime(2026, 4, 28, tzinfo=UTC),
    )
    assert _entry_name(e) == "Foo Co goes bankrupt"


def test_entry_name_warns_and_falls_back_to_source_id(caplog):
    e = RawEntry(
        source="hn_algolia",
        source_id="21055034",
        url="https://example.com",
        title=None,
        markdown_text="plain body with no markdown headings at all",
        fetched_at=datetime(2026, 4, 28, tzinfo=UTC),
    )
    with caplog.at_level("WARNING", logger="slopmortem.ingest._helpers"):
        assert _entry_name(e) == "21055034"
    assert any("no name extractable" in r.message for r in caplog.records)


def test_entry_name_treats_whitespace_only_title_as_missing():
    e = RawEntry(
        source="hn_algolia",
        source_id="21055034",
        url="https://example.com",
        title="   ",
        markdown_text=None,
        fetched_at=datetime(2026, 4, 28, tzinfo=UTC),
    )
    assert _entry_name(e) == "21055034"


def test_entry_name_strips_title_whitespace():
    e = RawEntry(
        source="hn_algolia",
        source_id="21055034",
        url="https://example.com",
        title="  Kin (by Kik Interactive)\t\n",
        markdown_text=None,
        fetched_at=datetime(2026, 4, 28, tzinfo=UTC),
    )
    assert _entry_name(e) == "Kin (by Kik Interactive)"


async def test_inmemory_corpus_rejects_non_point_payload():
    corpus = InMemoryCorpus()
    with pytest.raises(TypeError, match="expects _Point"):
        await corpus.upsert_chunk({"id": "x", "vector": {}, "payload": {}})


async def test_fake_slop_classifier_matches_substring_not_only_prefix():
    classifier = FakeSlopClassifier(default_score=0.0, scores={"slop-marker": 0.95})
    assert await classifier.score("preamble slop-marker tail") == 0.95
    assert await classifier.score("clean text") == 0.0


def _haiku_canned(text: str, body_response: str) -> dict[tuple[str, str, str], FakeResponse]:
    prompt = render_prompt("slop_judge", text=text)
    key = llm_canned_key("slop_judge", model=_HAIKU, prompt=prompt)
    return {key: FakeResponse(text=body_response)}


def _haiku_classifier(text: str, body_response: str) -> HaikuSlopClassifier:
    canned = _haiku_canned(text, body_response)
    return HaikuSlopClassifier(llm=FakeLLMClient(canned=canned, default_model=_HAIKU), model=_HAIKU)


async def test_haiku_slop_classifier_returns_zero_when_is_dead_true():
    text = "Acme shut down in 2021 after running out of money."
    classifier = _haiku_classifier(text, json.dumps({"is_dead_company": True}))
    assert await classifier.score(text) == 0.0


async def test_haiku_slop_classifier_returns_one_when_is_dead_false():
    text = "Acme is hiring senior engineers in San Francisco."
    classifier = _haiku_classifier(text, json.dumps({"is_dead_company": False}))
    assert await classifier.score(text) == 1.0


async def test_haiku_slop_classifier_returns_zero_on_invalid_json():
    """Conservative on parse failure: keep the entry rather than silently drop."""
    text = "Acme is a startup."
    classifier = _haiku_classifier(text, "this is not json {")
    assert await classifier.score(text) == 0.0


async def test_haiku_slop_classifier_returns_one_on_non_dict_json():
    text = "Acme."
    classifier = _haiku_classifier(text, json.dumps(["unexpected", "shape"]))
    assert await classifier.score(text) == 1.0


async def test_haiku_slop_classifier_returns_one_when_field_missing():
    text = "Acme."
    classifier = _haiku_classifier(text, json.dumps({"some_other_field": True}))
    assert await classifier.score(text) == 1.0


@dataclass
class _ListSrc:
    entries: list[RawEntry]

    async def fetch(self) -> AsyncIterator[RawEntry]:
        for e in self.entries:
            yield e


async def test_gather_entries_limit_breaks_inside_a_source():
    entries = [_entry(source_id=str(i)) for i in range(5)]
    out, failures = await _gather_entries(
        [_ListSrc(entries=entries)], span_events=[], limit=2, progress=NullProgress()
    )
    assert [e.source_id for e in out] == ["0", "1"]
    assert failures == 0


async def test_gather_entries_limit_skips_subsequent_sources():
    """The cap firing inside source A must short-circuit before source B is touched."""
    started: list[str] = []

    @dataclass
    class _RecordingSrc:
        name: str
        entries: list[RawEntry]

        async def fetch(self) -> AsyncIterator[RawEntry]:
            started.append(self.name)
            for e in self.entries:
                yield e

    src_a = _RecordingSrc("A", entries=[_entry(source_id="a1"), _entry(source_id="a2")])
    src_b = _RecordingSrc("B", entries=[_entry(source_id="b1")])
    out, failures = await _gather_entries(
        [src_a, src_b], span_events=[], limit=2, progress=NullProgress()
    )
    assert len(out) == 2
    assert started == ["A"]
    assert failures == 0


def test_null_progress_methods_are_pure_noops():
    p = NullProgress()
    p.start_phase(IngestPhase.GATHER, total=None)
    p.advance_phase(IngestPhase.GATHER, n=1)
    p.end_phase(IngestPhase.GATHER)
    p.log("anything")
    p.error(IngestPhase.GATHER, "anything")


def test_haiku_slop_classifier_default_char_limit_pins_six_thousand():
    """Tightening this caused false-negative quarantines historically."""
    classifier = HaikuSlopClassifier(
        llm=FakeLLMClient(canned={}, default_model=_HAIKU), model=_HAIKU
    )
    assert classifier.char_limit == 6000
