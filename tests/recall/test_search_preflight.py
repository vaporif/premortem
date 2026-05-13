"""Task 0 pre-flight gate: walk the recorded Tavily cassette and check hit rates.

The cassette at ``tests/fixtures/cassettes/recall/tavily_search_preflight.yaml`` was
captured by ``scripts/record_tavily_preflight.py`` — one live Tavily call per
``(variant, company)`` for the three query syntaxes (pipe-OR, comma-OR, prose) and
five known-dead Web3 startups. This test is read-only over that artifact: it
recomputes per-variant hit rates and asserts the **plan's chosen variant (prose)**
clears 4/5. Other variants' rates are logged but not asserted on — the gate only
cares that the variant the plan picked still works well enough to unblock Task 1.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import pytest
import yaml

if TYPE_CHECKING:
    from collections.abc import Mapping

type Variant = Literal["pipe", "comma", "prose"]
_VARIANTS: tuple[Variant, ...] = ("pipe", "comma", "prose")

# The plan picked prose as the safe baseline (see Open Questions §1: Tavily is a
# natural-language engine; pipe-OR is not a documented operator). Hard-coded on
# purpose: if a re-record drops prose below the gate, this test SHOULD fail loudly
# so someone revisits the choice. Computing the winner dynamically would silently
# swap the gate target and hide that regression.
_PLAN_CHOSEN_VARIANT: Variant = "prose"
_MIN_HITS_REQUIRED = 4
_EXPECTED_COMPANY_COUNT = 5

_CASSETTE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "cassettes"
    / "recall"
    / "tavily_search_preflight.yaml"
)


def _is_usable_hit(name: str, hit: Mapping[str, str | None]) -> bool:
    needle = name.casefold()
    title = (hit.get("title") or "").casefold()
    content = (hit.get("content") or "").casefold()
    return needle in title or needle in content


def _hit_counts_by_variant(
    queries: list[Mapping[str, object]],
) -> dict[Variant, tuple[int, int]]:
    counts: dict[Variant, list[bool]] = {v: [] for v in _VARIANTS}
    for entry in queries:
        variant_raw = entry["variant"]
        variant = _coerce_variant(variant_raw)
        if variant is None:
            continue
        name = entry["company"]
        if not isinstance(name, str):
            continue
        results = entry["results"]
        if not isinstance(results, list):
            continue
        # The cassette is our own recorded artifact; every list element is a hit dict.
        typed_results = cast("list[Mapping[str, str | None]]", results)
        counts[variant].append(any(_is_usable_hit(name, r) for r in typed_results))
    return {v: (sum(hits), len(hits)) for v, hits in counts.items()}


def _coerce_variant(value: object) -> Variant | None:
    return value if value in _VARIANTS else None


@pytest.fixture
def cassette_queries() -> list[Mapping[str, object]]:
    raw = yaml.safe_load(_CASSETTE_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"unexpected cassette root shape: {type(raw).__name__}"
        raise TypeError(msg)
    queries = raw.get("queries")
    if not isinstance(queries, list):
        msg = f"cassette 'queries' is not a list: {type(queries).__name__}"
        raise TypeError(msg)
    return cast("list[Mapping[str, object]]", queries)


def test_cassette_covers_all_variants_and_companies(
    cassette_queries: list[Mapping[str, object]],
) -> None:
    counts = _hit_counts_by_variant(cassette_queries)
    for variant in _VARIANTS:
        _, total = counts[variant]
        assert total == _EXPECTED_COMPANY_COUNT, (
            f"variant {variant} covers {total}/{_EXPECTED_COMPANY_COUNT} companies"
        )


def test_plan_chosen_variant_clears_gate(
    cassette_queries: list[Mapping[str, object]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    counts = _hit_counts_by_variant(cassette_queries)

    with caplog.at_level(logging.INFO, logger="tavily_preflight"):
        logger = logging.getLogger("tavily_preflight")
        for variant in _VARIANTS:
            hits, total = counts[variant]
            logger.info("variant=%s hit_rate=%d/%d", variant, hits, total)

    chosen_hits, chosen_total = counts[_PLAN_CHOSEN_VARIANT]
    assert chosen_total == _EXPECTED_COMPANY_COUNT
    assert chosen_hits >= _MIN_HITS_REQUIRED, (
        f"plan's chosen variant {_PLAN_CHOSEN_VARIANT!r} only got "
        f"{chosen_hits}/{chosen_total} usable hits; "
        f"gate requires >= {_MIN_HITS_REQUIRED}/{_EXPECTED_COMPANY_COUNT}"
    )
