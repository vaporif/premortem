"""Synthesis tool unit tests (plan §9.1).

The corpus tool functions ``_get_post_mortem`` / ``_search_corpus`` delegate
through the `slopmortem.corpus.store.Corpus` protocol. These tests
hit the delegation wiring with an in-memory fake corpus. Live Qdrant
integration shape is covered separately by
``tests/stages/test_retrieve.py`` (gated on ``requires_qdrant``).

A fake (vs. the live ``fixture_corpus`` from the retrieve tests) keeps
Task 9 unit-testable without a Qdrant service. The contract under test is
tool-function to Corpus protocol delegation, not Qdrant behavior.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from slopmortem.corpus._tools_impl import _get_post_mortem, _search_corpus, _set_corpus

if TYPE_CHECKING:
    from slopmortem.models import Candidate, Facets


class _FakeCorpus:
    """Minimal Corpus stand-in; Task 9 only reads ``get_post_mortem`` / ``search_corpus``.

    A no-op ``query`` exists to satisfy the structural `Corpus`
    protocol for ``_set_corpus``'s signature.
    """

    def __init__(
        self,
        *,
        canonical: dict[str, str] | None = None,
        hits: list[dict[str, Any]] | None = None,
    ) -> None:
        self._canonical = canonical or {}
        self._hits = hits or []
        self.last_query: tuple[str, dict[str, str] | None] | None = None

    async def query(  # noqa: PLR0913
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
        _ = (
            dense,
            sparse,
            facets,
            cutoff_iso,
            strict_deaths,
            k_retrieve,
            strict_sector_filter,
            strict_sector_filter_excludes_other,
        )
        return []

    async def get_post_mortem(self, canonical_id: str) -> str:
        return self._canonical.get(canonical_id, "")

    async def search_corpus(
        self,
        q: str,
        facets: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        self.last_query = (q, facets)
        return list(self._hits)


@pytest.fixture
def fixture_corpus():
    """In-memory fake corpus with one Acme post-mortem and one search hit."""
    corpus = _FakeCorpus(
        canonical={"acme.com": "# Acme\n\nAcme failed because of scrap-metal margins."},
        hits=[
            {
                "canonical_id": "rivals.com",
                "name": "Rivals Scrap",
                "summary": "Rivals scrap-metal marketplace pivoted away.",
                "body": "Long body text about industrial scrap aggregation.",
                "score": 0.42,
            }
        ],
    )
    _set_corpus(corpus)
    try:
        yield corpus
    finally:
        # Reset module-level binding so other tests don't see the fake.
        import slopmortem.corpus._tools_impl as ti  # noqa: PLC0415

        ti._corpus = None


async def test_get_post_mortem_reads_canonical(fixture_corpus):
    text = await _get_post_mortem("acme.com")
    assert "Acme" in text or len(text) > 0


async def test_search_corpus_returns_hits(fixture_corpus):
    hits = await _search_corpus("scrap metal", facets={"sector": "logistics_supply_chain"})
    assert len(hits) > 0


# Per-synthesis Tavily budget gate (spec line 1005).
from slopmortem.config import Config  # noqa: E402
from slopmortem.llm import recall_tools, synthesis_tools  # noqa: E402


@pytest.fixture
def tavily_key(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")


async def test_tavily_calls_under_cap_pass_through(monkeypatch, tavily_key):
    cfg = Config(enable_tavily_synthesis=True, tavily_calls_per_synthesis=2)

    calls: list[tuple[str, int]] = []

    async def fake_real(q: str, limit: int = 5) -> str:
        calls.append((q, limit))
        return f"hit:{q}"

    monkeypatch.setattr("slopmortem.corpus._tools_impl.tavily_search_async", fake_real)
    # Re-fetch tools so the patched fn is the inner of the bounded wrapper.
    tools = synthesis_tools(cfg)
    tavily = next(t for t in tools if t.name == "tavily_search")

    out1 = await tavily.fn(q="acme", limit=5)
    out2 = await tavily.fn(q="beta", limit=5)
    assert "hit:acme" in out1
    assert "hit:beta" in out2
    assert len(calls) == 2


async def test_third_tavily_call_returns_budget_message(monkeypatch, tavily_key):
    """The third call returns a budget-exceeded string, not an exception."""
    cfg = Config(enable_tavily_synthesis=True, tavily_calls_per_synthesis=2)
    real_calls: list[str] = []

    async def fake_real(q: str, limit: int = 5) -> str:
        real_calls.append(q)
        return "ok"

    monkeypatch.setattr("slopmortem.corpus._tools_impl.tavily_search_async", fake_real)
    tools = synthesis_tools(cfg)
    tavily = next(t for t in tools if t.name == "tavily_search")

    await tavily.fn(q="a", limit=5)
    await tavily.fn(q="b", limit=5)
    out3 = await tavily.fn(q="c", limit=5)
    assert "budget exceeded" in out3
    assert real_calls == ["a", "b"]  # third call did not reach the real fn


async def test_tavily_search_and_extract_share_budget(monkeypatch, tavily_key):
    """The cap covers tavily_search and tavily_extract combined, not each independently."""
    cfg = Config(enable_tavily_synthesis=True, tavily_calls_per_synthesis=2)

    async def fake_search(q: str, limit: int = 5) -> str:
        return "search-hit"

    async def fake_extract(url: str) -> str:
        return "extract-hit"

    monkeypatch.setattr("slopmortem.corpus._tools_impl.tavily_search_async", fake_search)
    monkeypatch.setattr("slopmortem.corpus._tools_impl.tavily_extract_async", fake_extract)
    tools = synthesis_tools(cfg)
    search = next(t for t in tools if t.name == "tavily_search")
    extract = next(t for t in tools if t.name == "tavily_extract")

    await search.fn(q="a", limit=1)
    await extract.fn(url="https://example.com/x")
    out3 = await search.fn(q="b", limit=1)  # third call across both tools
    assert "budget exceeded" in out3


async def test_each_synthesis_gets_a_fresh_budget(monkeypatch, tavily_key):
    cfg = Config(enable_tavily_synthesis=True, tavily_calls_per_synthesis=1)

    async def fake_search(q: str, limit: int = 5) -> str:
        return "ok"

    monkeypatch.setattr("slopmortem.corpus._tools_impl.tavily_search_async", fake_search)

    # Synthesis #1 exhausts the budget after one call.
    tools_a = synthesis_tools(cfg)
    search_a = next(t for t in tools_a if t.name == "tavily_search")
    out_a1 = await search_a.fn(q="a", limit=1)
    out_a2 = await search_a.fn(q="b", limit=1)
    assert "ok" in out_a1
    assert "budget exceeded" in out_a2

    # Synthesis #2: fresh tools, fresh budget.
    tools_b = synthesis_tools(cfg)
    search_b = next(t for t in tools_b if t.name == "tavily_search")
    out_b1 = await search_b.fn(q="x", limit=1)
    assert "ok" in out_b1


def test_tavily_disabled_means_no_tavily_tools_in_factory():
    """When enable_tavily_synthesis=False, the factory does not return Tavily tools."""
    cfg = Config(enable_tavily_synthesis=False)
    tools = synthesis_tools(cfg)
    names = {t.name for t in tools}
    assert "tavily_search" not in names
    assert "tavily_extract" not in names


# ----- recall_tools (Opus tavily_search during candidate discovery) ----------


def test_recall_tools_returns_empty_when_disabled(monkeypatch):
    # enable_tavily_recall_search=False overrides everything else — the recall
    # branch falls back to training-data-only suggestions.
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    cfg = Config(enable_tavily_recall_search=False)
    assert recall_tools(cfg) == []


def test_recall_tools_returns_empty_when_cap_is_zero(tavily_key):
    # recall_max_tavily_calls=0 short-circuits the tool list even when the
    # outer enable flag is on.
    del tavily_key
    cfg = Config(enable_tavily_recall_search=True, recall_max_tavily_calls=0)
    assert recall_tools(cfg) == []


def test_recall_tools_returns_both_search_and_extract(tavily_key):
    del tavily_key
    cfg = Config(enable_tavily_recall_search=True, recall_max_tavily_calls=5)
    tools = recall_tools(cfg)
    # Both surfaces are exposed: search for discovery, extract for high-stakes
    # picks where the snippet alone isn't enough to commit.
    assert {t.name for t in tools} == {"tavily_search", "tavily_extract"}


async def test_recall_tools_bounded_search_caps_at_budget(monkeypatch, tavily_key):
    """Sixth call returns the budget-exceeded string instead of hitting Tavily."""
    del tavily_key
    cfg = Config(enable_tavily_recall_search=True, recall_max_tavily_calls=5)
    real_calls: list[str] = []

    async def fake_real(q: str, limit: int = 5) -> str:
        del limit
        real_calls.append(q)
        return f"hit:{q}"

    monkeypatch.setattr("slopmortem.corpus._tools_impl.tavily_search_async", fake_real)
    tools = recall_tools(cfg)
    search = next(t for t in tools if t.name == "tavily_search")

    for i in range(5):
        out = await search.fn(q=f"q{i}", limit=5)
        assert "hit:" in out
    out6 = await search.fn(q="q5", limit=5)
    assert "budget exceeded" in out6
    # Real implementation was hit exactly cap times — the sixth call short-
    # circuited before reaching Tavily.
    assert real_calls == ["q0", "q1", "q2", "q3", "q4"]


async def test_recall_tools_shared_budget_caps_combined_calls(monkeypatch, tavily_key):
    """The recall cap spans tavily_search + tavily_extract — not each independently."""
    del tavily_key
    cfg = Config(enable_tavily_recall_search=True, recall_max_tavily_calls=5)
    search_calls: list[str] = []
    extract_calls: list[str] = []

    async def fake_search(q: str, limit: int = 5) -> str:
        del limit
        search_calls.append(q)
        return f"hit:{q}"

    async def fake_extract(url: str) -> str:
        extract_calls.append(url)
        return "body"

    monkeypatch.setattr("slopmortem.corpus._tools_impl.tavily_search_async", fake_search)
    monkeypatch.setattr("slopmortem.corpus._tools_impl.tavily_extract_async", fake_extract)
    tools = recall_tools(cfg)
    search = next(t for t in tools if t.name == "tavily_search")
    extract = next(t for t in tools if t.name == "tavily_extract")

    # 3 searches + 2 extracts = 5 (at the cap); the 6th call (whichever shape)
    # must refuse.
    for i in range(3):
        out = await search.fn(q=f"q{i}", limit=5)
        assert "hit:" in out
    for i in range(2):
        out = await extract.fn(url=f"https://example.com/{i}")
        assert out == "body"
    out6 = await extract.fn(url="https://example.com/over")
    assert "budget exceeded" in out6
    assert len(search_calls) == 3
    assert len(extract_calls) == 2
