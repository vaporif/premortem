"""End-to-end name extraction through ``_process_entry``.

The unit tests for ``_entry_name`` live in ``test_ingest_internals.py``; these
tests pin the behaviour at the ``_process_entry`` boundary so a regression in
the resolver tier-2 keying gets caught even if the helper is renamed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest_asyncio

from slopmortem.config import Config
from slopmortem.corpus import MergeJournal
from slopmortem.corpus.sources._names import SOURCE_HN_ALGOLIA, SOURCE_LLM_RECALL
from slopmortem.ingest import InMemoryCorpus
from slopmortem.ingest._fan_out import _FanoutResult
from slopmortem.ingest._journal_writes import _process_entry
from slopmortem.llm import FakeEmbeddingClient, FakeLLMClient
from slopmortem.models import Facets, RawEntry

if TYPE_CHECKING:
    from pathlib import Path


def _stub_sparse(_text: str) -> dict[int, float]:
    return {0: 1.0}


def _facets() -> Facets:
    return Facets(
        sector="crypto_web3",
        business_model="b2b_marketplace",
        customer_type="smb",
        geography="us",
        monetization="transaction_fee",
        founding_year=2018,
        failure_year=2021,
    )


def _fan() -> _FanoutResult:
    return _FanoutResult(facets=_facets(), summary="s", cache_read=0, cache_creation=0)


def _cfg() -> Config:
    return Config(max_cost_usd_per_ingest=100.0, ingest_concurrency=5)


@pytest_asyncio.fixture
async def journal(tmp_path: Path) -> MergeJournal:
    j = MergeJournal(tmp_path / "journal.sqlite")
    await j.init()
    return j


async def _run(
    entry: RawEntry,
    *,
    journal: MergeJournal,
    corpus: InMemoryCorpus,
    tmp_path: Path,
) -> str:
    config = _cfg()
    body = entry.markdown_text or ""
    outcome = await _process_entry(
        entry,
        body=body,
        fan=_fan(),
        journal=journal,
        corpus=corpus,
        embed_client=FakeEmbeddingClient(model=config.embed_model_id),
        llm=FakeLLMClient(canned={}, default_model=config.model_facet),
        config=config,
        post_mortems_root=tmp_path / "post_mortems",
        slop_score=0.0,
        force=False,
        span_events=[],
        sparse_encoder=_stub_sparse,
    )
    assert outcome.value == "processed"
    # The canonical id keyed under (source, source_id) is the resolver's output.
    rows = await journal.fetch_by_key("ignored", entry.source, entry.source_id)
    # fetch_by_key takes canonical_id+source+source_id; fall back to fetch_all
    # filtered by (source, source_id) since we don't know the canonical yet.
    if not rows:
        all_rows = await journal.fetch_all()
        rows = [
            r for r in all_rows if r["source"] == entry.source and r["source_id"] == entry.source_id
        ]
    assert rows
    return str(rows[0]["canonical_id"])


async def test_hn_entries_with_same_title_collide_at_tier2(
    tmp_path: Path, journal: MergeJournal
) -> None:
    """Two HN articles about the same company share a canonical id."""
    body = "Kin was a chat-app cryptocurrency by Kik Interactive. " * 20
    # HN article URLs point at the linked target; common platform hosts
    # (medium.com, substack.com, ...) hit the tier-1 demotion list so tier-2
    # name keying is what decides the merge.
    e1 = RawEntry(
        source=SOURCE_HN_ALGOLIA,
        source_id="21055034",
        url="https://medium.com/@alice/kin-shuts-down-21055034",
        title="Kin (by Kik Interactive)",
        markdown_text=body,
        fetched_at=datetime(2026, 4, 28, tzinfo=UTC),
    )
    e2 = RawEntry(
        source=SOURCE_HN_ALGOLIA,
        source_id="21120792",
        url="https://medium.com/@bob/kin-postmortem-21120792",
        title="Kin (by Kik Interactive)",
        markdown_text=body,
        fetched_at=datetime(2026, 4, 28, tzinfo=UTC),
    )
    corpus = InMemoryCorpus()
    canonical_1 = await _run(e1, journal=journal, corpus=corpus, tmp_path=tmp_path)
    canonical_2 = await _run(e2, journal=journal, corpus=corpus, tmp_path=tmp_path)
    assert canonical_1 == canonical_2
    # Sanity: the canonical is tier-2 keyed on the title, not on a source_id.
    assert "kin (by kik interactive)" in canonical_1
    assert "21055034" not in canonical_1
    assert "21120792" not in canonical_1


async def test_titleless_hn_entries_keep_source_id_fallback(
    tmp_path: Path, journal: MergeJournal
) -> None:
    """No title and no markdown heading → resolver still gets *some* key.

    Two such entries stay distinct because tier-2 falls back to ``source_id``;
    this matches today's behaviour for legacy ingests and is the warned path.
    """
    body = "plain prose body without any markdown headings at all. " * 20
    e1 = RawEntry(
        source=SOURCE_HN_ALGOLIA,
        source_id="33333333",
        url="https://medium.com/@x/post-33333333",
        title=None,
        markdown_text=body,
        fetched_at=datetime(2026, 4, 28, tzinfo=UTC),
    )
    e2 = RawEntry(
        source=SOURCE_HN_ALGOLIA,
        source_id="44444444",
        url="https://medium.com/@y/post-44444444",
        title=None,
        markdown_text=body,
        fetched_at=datetime(2026, 4, 28, tzinfo=UTC),
    )
    corpus = InMemoryCorpus()
    c1 = await _run(e1, journal=journal, corpus=corpus, tmp_path=tmp_path)
    c2 = await _run(e2, journal=journal, corpus=corpus, tmp_path=tmp_path)
    assert c1 != c2


async def test_recall_seed_title_drives_canonical_id_when_homepage_missing(
    tmp_path: Path, journal: MergeJournal
) -> None:
    """A recall-style RawEntry (title=suggestion.name) keys tier-2 on the name.

    The recall persist path threads ``RecallSuggestion.name`` into
    ``RawEntry.title``. When ``url`` is set to a platform / news host (no
    registrable domain match for the *vendor*), tier-2 takes over and the
    canonical id reflects the suggestion's name, not the opaque source_id
    hash.
    """
    body = "Ribbon Finance was a structured-products protocol on Ethereum. " * 20
    entry = RawEntry(
        source=SOURCE_LLM_RECALL,
        source_id="5b2af439550af405",
        # A platform host triggers tier-1 demotion to tier-2; the name (from
        # ``title``) becomes the key. Mirrors the real recall path when the
        # citing article is on a content-platform host like medium.
        url="https://medium.com/@author/ribbon-finance-shut-down",
        title="Ribbon Finance",
        markdown_text=body,
        fetched_at=datetime(2026, 4, 28, tzinfo=UTC),
    )
    corpus = InMemoryCorpus()
    canonical = await _run(entry, journal=journal, corpus=corpus, tmp_path=tmp_path)
    assert "5b2af439550af405" not in canonical
    assert "ribbon" in canonical.lower()


async def test_recall_url_none_tier2s_on_name_not_citation_host(
    tmp_path: Path, journal: MergeJournal
) -> None:
    """``url=None`` (no homepage) forces tier-2 keying on suggestion.name.

    Mirrors the real recall path after the citation-host fix: when the
    verifier has no vendor homepage, it leaves ``RawEntry.url=None`` rather
    than falling back to the evidence URL. The resolver's tier-1 check sees
    ``not domain`` and routes to tier-2 keyed on the title — preventing
    two recall suggestions cited via the same news outlet from collapsing
    onto one canonical id.
    """
    body = "Ribbon Finance was a structured-products protocol on Ethereum. " * 20
    entry = RawEntry(
        source=SOURCE_LLM_RECALL,
        source_id="5b2af439550af405",
        url=None,
        title="Ribbon Finance",
        markdown_text=body,
        fetched_at=datetime(2026, 4, 28, tzinfo=UTC),
    )
    corpus = InMemoryCorpus()
    canonical = await _run(entry, journal=journal, corpus=corpus, tmp_path=tmp_path)
    assert "5b2af439550af405" not in canonical
    assert "ribbon" in canonical.lower()


async def test_two_recall_entries_with_no_homepage_dont_collide_on_citation_host(
    tmp_path: Path, journal: MergeJournal
) -> None:
    """The corruption case the fix prevents.

    Two recall suggestions about different companies, both with no
    ``homepage_url``, would historically collide onto the citation host's
    registrable domain (e.g. both ``canonical_id=news.example.com``) and
    their bodies would merge across unrelated companies. With ``url=None``
    each gets its own tier-2 canonical keyed on its name.
    """
    body_a = "Acme Co was a security IP vendor that wound down in 2023. " * 20
    body_b = "Widget Inc was a hardware accelerator startup that ceased ops in 2024. " * 20
    entry_a = RawEntry(
        source=SOURCE_LLM_RECALL,
        source_id="aaaaaaaaaaaaaaaa",
        url=None,
        title="Acme Co",
        markdown_text=body_a,
        fetched_at=datetime(2026, 4, 28, tzinfo=UTC),
    )
    entry_b = RawEntry(
        source=SOURCE_LLM_RECALL,
        source_id="bbbbbbbbbbbbbbbb",
        url=None,
        title="Widget Inc",
        markdown_text=body_b,
        fetched_at=datetime(2026, 4, 28, tzinfo=UTC),
    )
    corpus = InMemoryCorpus()
    canon_a = await _run(entry_a, journal=journal, corpus=corpus, tmp_path=tmp_path)
    canon_b = await _run(entry_b, journal=journal, corpus=corpus, tmp_path=tmp_path)
    assert canon_a != canon_b
    assert "acme" in canon_a.lower()
    assert "widget" in canon_b.lower()
