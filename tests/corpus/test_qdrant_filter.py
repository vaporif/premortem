"""Truth-table tests for ``_build_sector_filter`` and ``_and_filters``.

The helpers are pure and table-driven; these cover every row of the spec
(see ``docs/specs/`` and the strict-sector-filter plan).
"""

from __future__ import annotations

from typing import cast

from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from slopmortem.corpus._qdrant_store import _and_filters, _build_sector_filter


def test_build_sector_filter_disabled_returns_none() -> None:
    assert _build_sector_filter(sector="crypto_web3", strict=False, exclude_other=False) is None
    assert _build_sector_filter(sector="crypto_web3", strict=False, exclude_other=True) is None


def test_build_sector_filter_pitch_other_returns_none() -> None:
    # Pitch sector "other" is uninformative — filter must not narrow.
    assert _build_sector_filter(sector="other", strict=True, exclude_other=False) is None
    assert _build_sector_filter(sector="other", strict=True, exclude_other=True) is None


def test_build_sector_filter_strict_keeps_other() -> None:
    f = _build_sector_filter(sector="crypto_web3", strict=True, exclude_other=False)
    assert f is not None
    assert f.must is not None
    # ``Filter.must`` is a union over many condition shapes; the helper only
    # produces ``FieldCondition`` + ``MatchAny``, so cast at the boundary.
    [raw] = f.must
    cond = cast("FieldCondition", raw)
    match = cast("MatchAny", cond.match)
    assert cond.key == "facets.sector"
    assert sorted(match.any) == ["crypto_web3", "other"]


def test_build_sector_filter_strict_excludes_other() -> None:
    f = _build_sector_filter(sector="crypto_web3", strict=True, exclude_other=True)
    assert f is not None
    assert f.must is not None
    [raw] = f.must
    cond = cast("FieldCondition", raw)
    match = cast("MatchValue", cond.match)
    assert cond.key == "facets.sector"
    assert match.value == "crypto_web3"


def _sector_eq(sector: str) -> Filter:
    return Filter(must=[FieldCondition(key="facets.sector", match=MatchValue(value=sector))])


def test_and_filters_all_none() -> None:
    assert _and_filters(None, None) is None


def test_and_filters_only_recency() -> None:
    recency = _sector_eq("crypto_web3")  # stand-in; shape is what matters
    assert _and_filters(recency, None) is recency


def test_and_filters_only_sector() -> None:
    sector = _sector_eq("ai_ml")
    assert _and_filters(None, sector) is sector


def test_and_filters_both_present_nests() -> None:
    a = _sector_eq("crypto_web3")
    b = _sector_eq("ai_ml")
    out = _and_filters(a, b)
    # Outer wrapper AND-combines via ``must=[a, b]`` — it must not flatten or
    # merge clauses, since one input may carry only ``should=[…]``.
    assert out is not None
    assert out.must is not None
    assert list(out.must) == [a, b]
