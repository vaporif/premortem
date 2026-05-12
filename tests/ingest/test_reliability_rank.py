"""Regression: the reliability rank table must key on the actual emitted source strings."""

from __future__ import annotations

import pytest

from slopmortem.corpus.sources._names import (
    SOURCE_CRUNCHBASE_CSV,
    SOURCE_CURATED,
    SOURCE_HN_ALGOLIA,
    SOURCE_LLM_RECALL,
    SOURCE_TAVILY_NEWS,
)
from slopmortem.ingest._helpers import _reliability_for


@pytest.mark.parametrize(
    ("source", "expected_rank"),
    [
        (SOURCE_CURATED, 0),
        (SOURCE_HN_ALGOLIA, 1),
        (SOURCE_CRUNCHBASE_CSV, 2),
        (SOURCE_TAVILY_NEWS, 4),
        (SOURCE_LLM_RECALL, 6),
    ],
)
def test_known_sources_have_explicit_rank(source: str, expected_rank: int) -> None:
    assert _reliability_for(source) == expected_rank


def test_unknown_source_lands_at_dead_letter_rank() -> None:
    assert _reliability_for("definitely-not-a-source") == 9
