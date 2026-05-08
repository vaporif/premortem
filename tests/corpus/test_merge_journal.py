from __future__ import annotations

import asyncio

import pytest_asyncio

from slopmortem.corpus import MergeJournal
from slopmortem.models import AliasEdge


@pytest_asyncio.fixture
async def journal(tmp_path):
    j = MergeJournal(tmp_path / "j.sqlite")
    await j.init()
    return j


async def test_pending_then_complete(journal):
    await journal.upsert_pending(canonical_id="a.com", source="hn", source_id="1")
    pending = await journal.fetch_pending()
    assert len(pending) == 1
    await journal.mark_complete(
        canonical_id="a.com",
        source="hn",
        source_id="1",
        skip_key="abc",
        merged_at="2026-04-28T00:00:00Z",
    )
    rows = await journal.fetch_pending()
    assert rows == []


async def test_concurrent_writes_dont_block_loop(journal):
    await asyncio.gather(
        *[
            journal.upsert_pending(canonical_id=f"x{i}.com", source="hn", source_id=str(i))
            for i in range(50)
        ]
    )
    pending = await journal.fetch_pending()
    assert len(pending) == 50


async def test_reverse_index_detects_resolver_flip(journal):
    await journal.upsert_pending(canonical_id="acme.com", source="hn", source_id="1")
    await journal.mark_complete(
        canonical_id="acme.com",
        source="hn",
        source_id="1",
        skip_key="k1",
        merged_at="2026-04-28T00:00:00Z",
    )
    prior = await journal.lookup_canonical_for_source("hn", "1")
    assert prior == "acme.com"


async def test_resolver_flipped_terminal_state(journal):
    await journal.upsert_pending(canonical_id="acme.com", source="hn", source_id="1")
    await journal.mark_complete(
        canonical_id="acme.com",
        source="hn",
        source_id="1",
        skip_key="k1",
        merged_at="2026-04-28T00:00:00Z",
    )
    # Resolver now flips the same (hn, 1) to acme-ai.com.
    await journal.upsert_resolver_flipped(canonical_id="acme-ai.com", source="hn", source_id="1")
    rows = await journal.fetch_by_key("acme-ai.com", "hn", "1")
    assert len(rows) == 1
    assert rows[0]["merge_state"] == "resolver_flipped"


async def test_upsert_alias_blocked_atomic(journal):
    edge = AliasEdge(
        canonical_id="acme-ai.com",
        alias_kind="rebranded_to",
        target_canonical_id="acme.com",
        evidence_source_id="hn:42",
        confidence=0.92,
    )
    await journal.upsert_alias_blocked(
        canonical_id="acme-ai.com",
        source="hn",
        source_id="42",
        alias_edge=edge,
    )
    rows = await journal.fetch_by_key("acme-ai.com", "hn", "42")
    assert len(rows) == 1
    assert rows[0]["merge_state"] == "alias_blocked"
    edges = await journal.fetch_aliases("acme-ai.com")
    assert len(edges) == 1
    assert edges[0].target_canonical_id == "acme.com"
    assert edges[0].alias_kind == "rebranded_to"


async def test_pending_idempotent_upsert(journal):
    await journal.upsert_pending(canonical_id="a.com", source="hn", source_id="1")
    await journal.upsert_pending(canonical_id="a.com", source="hn", source_id="1")
    rows = await journal.fetch_pending()
    assert len(rows) == 1


async def test_is_terminal_returns_false_when_unseen(journal):
    assert await journal.is_terminal("hn", "999") is False


async def test_is_terminal_true_for_complete_row(journal):
    await journal.upsert_pending(canonical_id="acme.com", source="hn", source_id="1")
    # pending alone must NOT count as terminal — a crashed prior run should re-run.
    assert await journal.is_terminal("hn", "1") is False
    await journal.mark_complete(
        canonical_id="acme.com",
        source="hn",
        source_id="1",
        skip_key="k1",
        merged_at="2026-05-08T00:00:00Z",
    )
    assert await journal.is_terminal("hn", "1") is True


async def test_is_terminal_true_for_quarantined_row(journal):
    await journal.write_quarantine(
        content_sha256="0" * 64,
        source="hn",
        source_id="2",
        reason="slop",
        slop_score=0.92,
    )
    assert await journal.is_terminal("hn", "2") is True


async def test_is_terminal_false_for_alias_blocked(journal):
    # alias_blocked / resolver_flipped are mid-flight entity-resolution decisions,
    # not terminal — re-run is expected when the alias graph is edited.
    edge = AliasEdge(
        canonical_id="a.com",
        alias_kind="rebranded_to",
        target_canonical_id="b.com",
        evidence_source_id="hn:3",
        confidence=0.9,
    )
    await journal.upsert_alias_blocked(
        canonical_id="a.com", source="hn", source_id="3", alias_edge=edge
    )
    assert await journal.is_terminal("hn", "3") is False
