"""Tests for ``FakeRecaller`` — the test-time stand-in for ``recall()``."""

from __future__ import annotations

from datetime import UTC, datetime

from slopmortem.models import Facets, RawEntry
from slopmortem.recall import (
    DeathnessConfig,
    FakeRecaller,
    PriorCandidateHint,
    RecallConfig,
    RecallDeps,
    VerifiedEntry,
)


def _facets() -> Facets:
    return Facets(
        sector="crypto_web3",
        business_model="services_consulting",
        customer_type="enterprise",
        geography="global",
        monetization="services_layer",
    )


def _stub_verified_entry(name: str = "stub") -> VerifiedEntry:
    entry = RawEntry(
        source="llm_recall",
        source_id=f"recall::{name}",
        url=f"https://{name}.example.com",
        title=name.title(),
        raw_html=None,
        markdown_text=f"{name} ceased operations.",
        fetched_at=datetime.now(UTC),
    )
    return VerifiedEntry(entry=entry, tier="evidence_only", verdict="dead")


def _config() -> RecallConfig:
    return RecallConfig(
        model_facet="facet",
        max_tokens_facet=128,
        model_recall="recall",
        max_tokens_recall=256,
        suggestion_cap=8,
        tools=[],
        max_tavily_calls=2,
        tavily_max_results=5,
        deathness=DeathnessConfig(
            model="haiku",
            max_tokens=128,
            min_confidence=0.7,
            struggling_min_confidence=0.85,
        ),
    )


# RecallDeps requires concrete callables for tavily_search/extract/llm; the
# FakeRecaller never invokes them, but constructing the deps proves the
# signature parity claim in fake.py.
class _UnusedLLM:
    async def complete(self, *_args: object, **_kwargs: object) -> object:
        msg = "FakeRecaller should never call LLM"
        raise AssertionError(msg)


async def _unused_tavily(_query: str, _limit: int) -> list[object]:
    msg = "FakeRecaller should never call tavily_search"
    raise AssertionError(msg)


async def _unused_extract(_url: str) -> str:
    msg = "FakeRecaller should never call extract"
    raise AssertionError(msg)


def _deps() -> RecallDeps:
    return RecallDeps(
        llm=_UnusedLLM(),  # pyright: ignore[reportArgumentType] - the fake never uses it
        tavily_search=_unused_tavily,  # pyright: ignore[reportArgumentType] - same
        extract=_unused_extract,
    )


async def test_fake_recaller_returns_seeded_verified_list() -> None:
    seed = [_stub_verified_entry("alpha")]
    fake = FakeRecaller(verified=seed)
    out = await fake("pitch", deps=_deps(), config=_config())
    assert out == seed


async def test_fake_recaller_records_invocations() -> None:
    fake = FakeRecaller(verified=[])
    hint = PriorCandidateHint(name="known", rationale="seen before")
    await fake("pitch-1", deps=_deps(), config=_config())
    await fake(
        "pitch-2",
        facets=_facets(),
        prior_hints=[hint],
        deps=_deps(),
        config=_config(),
    )
    assert [c.pitch for c in fake.calls] == ["pitch-1", "pitch-2"]
    assert fake.calls[0].prior_hints is None
    assert fake.calls[0].facets is None
    assert fake.calls[1].prior_hints == [hint]
    assert fake.calls[1].facets == _facets()


async def test_fake_recaller_returned_list_is_a_copy() -> None:
    """Mutating the returned list must not mutate ``fake.verified``."""
    seed = [_stub_verified_entry("alpha")]
    fake = FakeRecaller(verified=seed)
    out = await fake("pitch", deps=_deps(), config=_config())
    out.append(_stub_verified_entry("beta"))
    assert len(fake.verified) == 1
