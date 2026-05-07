"""HNAlgoliaSource: YAML-driven phrase discovery, pagination, dedup."""

from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent
from typing import Any
from unittest.mock import AsyncMock

import pytest

from slopmortem.corpus.sources import HNAlgoliaSource

CASSETTE_FILE = (
    Path(__file__).parent / "cassettes" / "test_hn_algolia_yaml" / "test_round_trip.yaml"
)


def _yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "hn_queries.yaml"
    p.write_text(dedent(body).strip() + "\n")
    return p


class _FakeResp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._payload


def _hits_payload(hits: list[dict[str, Any]]) -> dict[str, Any]:
    return {"hits": hits, "nbHits": len(hits), "page": 0, "nbPages": 1}


def _setup_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "slopmortem.corpus.sources.hn_algolia.respect_robots",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "slopmortem.corpus.sources.hn_algolia.throttle_for",
        AsyncMock(return_value=None),
    )


def test_build_url_uses_search_by_date_endpoint(tmp_path: Path) -> None:
    """Catches accidental swap to the relevance-ranked /search endpoint —
    ported from the deleted test_hn_algolia.py guard."""
    yaml_path = _yaml(
        tmp_path,
        """
        defaults:
          date_from: "2017-01-01"
          date_to: "2017-12-31"
          pages_per_window: 1
          hits_per_page: 30
        phrases:
          - "shut down"
        """,
    )
    src = HNAlgoliaSource(queries_yaml_path=yaml_path)
    win_start, win_end = src._windows[0]
    url = src._build_url("shut down", page=0, win_start=win_start, win_end=win_end)
    assert url.startswith("https://hn.algolia.com/api/v1/search_by_date?"), url
    assert not url.startswith("https://hn.algolia.com/api/v1/search?"), url


def test_build_url_quotes_phrase_for_phrase_match(tmp_path: Path) -> None:
    """Multi-word phrases must be wrapped in literal double-quotes so HN
    Algolia phrase-matches them; bare tokens AND-search and explode recall."""
    yaml_path = _yaml(
        tmp_path,
        """
        defaults:
          date_from: "2017-01-01"
          date_to: "2017-12-31"
          pages_per_window: 1
          hits_per_page: 30
        phrases:
          - "shutting down"
        """,
    )
    src = HNAlgoliaSource(queries_yaml_path=yaml_path)
    win_start, win_end = src._windows[0]
    url = src._build_url("shutting down", page=0, win_start=win_start, win_end=win_end)
    # The quoted phrase URL-encodes to %22shutting+down%22.
    assert "query=%22shutting+down%22" in url, url


async def test_emits_one_entry_per_phrase_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _yaml(
        tmp_path,
        """
        defaults:
          date_from: "2016-01-01"
          date_to: "2016-12-31"
          pages_per_window: 1
          hits_per_page: 30
        phrases:
          - "shutting down"
        """,
    )

    payload = _hits_payload(
        [
            {
                "objectID": "1",
                "title": "RethinkDB is shutting down",
                "url": "https://rethinkdb.com/blog/sunset",
                "created_at": "2016-10-06T00:00:00Z",
                "created_at_i": 1475712000,
                "points": 1674,
                "num_comments": 800,
            }
        ]
    )
    monkeypatch.setattr(
        "slopmortem.corpus.sources.hn_algolia.safe_get",
        AsyncMock(return_value=_FakeResp(payload)),
    )
    _setup_throttle(monkeypatch)

    src = HNAlgoliaSource(queries_yaml_path=yaml_path)
    entries = [e async for e in src.fetch()]
    assert len(entries) == 1
    e = entries[0]
    assert e.source == "hn_algolia"
    assert e.source_id == "1"
    assert e.url == "https://rethinkdb.com/blog/sunset"
    assert e.markdown_text is not None
    assert "RethinkDB" in e.markdown_text


async def test_dedups_objectid_across_phrases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same story matches two phrases — emit it once."""
    yaml_path = _yaml(
        tmp_path,
        """
        defaults:
          date_from: "2017-01-01"
          date_to: "2017-12-31"
          pages_per_window: 1
          hits_per_page: 30
        phrases:
          - "shut down"
          - "shutting down"
        """,
    )

    duplicated = _hits_payload(
        [
            {
                "objectID": "15984772",
                "title": "Mattermark (YC S12) to shut down after selling to FullContact",
                "url": "https://techcrunch.com/2017/12/21/mattermark-to-shut-down/",
                "created_at": "2017-12-22T00:00:00Z",
                "created_at_i": 1513900800,
                "points": 120,
                "num_comments": 58,
            }
        ]
    )
    monkeypatch.setattr(
        "slopmortem.corpus.sources.hn_algolia.safe_get",
        AsyncMock(return_value=_FakeResp(duplicated)),
    )
    _setup_throttle(monkeypatch)

    src = HNAlgoliaSource(queries_yaml_path=yaml_path)
    entries = [e async for e in src.fetch()]
    assert len(entries) == 1
    assert entries[0].source_id == "15984772"


async def test_paginates_until_empty_within_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pagination terminates on an empty page within a single window."""
    yaml_path = _yaml(
        tmp_path,
        """
        defaults:
          date_from: "2018-01-01"
          date_to: "2018-12-31"
          pages_per_window: 5
          hits_per_page: 30
        phrases:
          - "winding down"
        """,
    )

    pages = [
        _hits_payload(
            [
                {
                    "objectID": str(i),
                    "title": f"x{i}",
                    "url": f"https://x/{i}",
                    "created_at": "2018-06-01T00:00:00Z",
                    "created_at_i": 1527811200,
                    "points": 1,
                    "num_comments": 0,
                }
            ]
        )
        for i in range(3)
    ] + [_hits_payload([])]  # empty page terminates within-window pagination

    fake = AsyncMock(side_effect=[_FakeResp(p) for p in pages])
    monkeypatch.setattr("slopmortem.corpus.sources.hn_algolia.safe_get", fake)
    _setup_throttle(monkeypatch)

    src = HNAlgoliaSource(queries_yaml_path=yaml_path)
    entries = [e async for e in src.fetch()]
    assert len(entries) == 3
    # Should have hit 4 pages (3 with hits + 1 empty terminator), not all 5.
    assert fake.call_count == 4


async def test_iterates_one_window_per_year(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """date_from=2015-01-01, date_to=2017-12-31 should yield 3 windows x 1 page = 3 calls."""
    yaml_path = _yaml(
        tmp_path,
        """
        defaults:
          date_from: "2015-01-01"
          date_to: "2017-12-31"
          pages_per_window: 1
          hits_per_page: 30
        phrases:
          - "sunsetting"
        """,
    )

    call_log: list[str] = []

    async def fake_get(url: str, **_: object) -> _FakeResp:
        call_log.append(url)
        return _FakeResp(_hits_payload([]))  # empty — we only care about call count

    monkeypatch.setattr(
        "slopmortem.corpus.sources.hn_algolia.safe_get",
        AsyncMock(side_effect=fake_get),
    )
    _setup_throttle(monkeypatch)

    src = HNAlgoliaSource(queries_yaml_path=yaml_path)
    entries = [e async for e in src.fetch()]
    assert entries == []
    # 3 years x 1 phrase x 1 page = 3 calls. Each URL must contain a different
    # numericFilters range bracketing one calendar year.
    assert len(call_log) == 3
    # Spot-check: the three URLs reference distinct year-start epochs
    # (2015-01-01 = 1420070400, 2016-01-01 = 1451606400, 2017-01-01 = 1483228800).
    epochs = {1420070400, 1451606400, 1483228800}
    for epoch in epochs:
        # Source uses ``>=`` (encoded ``%3E%3D``); accept the ``>``-only form
        # too in case the source ever switches to a strict lower bound.
        assert any(
            f"created_at_i%3E%3D{epoch}" in u
            or f"created_at_i%3E{epoch}" in u
            or f"created_at_i>={epoch}" in u
            or f"created_at_i>{epoch}" in u
            for u in call_log
        ), f"expected one URL bracketing year starting at epoch {epoch}; got {call_log}"


async def test_respects_pages_per_window_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _yaml(
        tmp_path,
        """
        defaults:
          date_from: "2020-01-01"
          date_to: "2020-12-31"
          pages_per_window: 2
          hits_per_page: 30
        phrases:
          - "sunsetting"
        """,
    )

    # Always returns one hit — would paginate forever without the cap.
    def make_page(call_idx: int) -> _FakeResp:
        return _FakeResp(
            _hits_payload(
                [
                    {
                        "objectID": f"obj-{call_idx}",
                        "title": f"x{call_idx}",
                        "url": f"https://x/{call_idx}",
                        "created_at": "2020-06-01T00:00:00Z",
                        "created_at_i": 1590969600,
                        "points": 1,
                        "num_comments": 0,
                    }
                ]
            )
        )

    fake = AsyncMock(side_effect=[make_page(i) for i in range(10)])
    monkeypatch.setattr("slopmortem.corpus.sources.hn_algolia.safe_get", fake)
    _setup_throttle(monkeypatch)

    src = HNAlgoliaSource(queries_yaml_path=yaml_path)
    entries = [e async for e in src.fetch()]
    # 1 window x 2-page cap x 1 hit per page = 2 entries; 2 API calls.
    assert len(entries) == 2
    assert fake.call_count == 2


async def test_skips_hits_missing_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    yaml_path = _yaml(
        tmp_path,
        """
        defaults:
          date_from: "2020-01-01"
          date_to: "2020-12-31"
          pages_per_window: 1
          hits_per_page: 30
        phrases:
          - "wound down"
        """,
    )

    payload = _hits_payload(
        [
            {
                "objectID": "no-url",
                "title": "Ask HN: how",
                "created_at_i": 1577836800,
                "points": 1,
                "num_comments": 0,
            },
            {
                "objectID": "ok",
                "title": "X",
                "url": "https://x",
                "created_at_i": 1577836800,
                "points": 1,
                "num_comments": 0,
            },
        ]
    )
    monkeypatch.setattr(
        "slopmortem.corpus.sources.hn_algolia.safe_get",
        AsyncMock(return_value=_FakeResp(payload)),
    )
    _setup_throttle(monkeypatch)

    src = HNAlgoliaSource(queries_yaml_path=yaml_path)
    entries = [e async for e in src.fetch()]
    assert len(entries) == 1
    assert entries[0].source_id == "ok"


def test_rejects_non_int_pages_per_window(tmp_path: Path) -> None:
    yaml_path = _yaml(
        tmp_path,
        """
        defaults:
          pages_per_window: "three"
        phrases:
          - "shutting down"
        """,
    )
    with pytest.raises(TypeError, match="pages_per_window"):
        HNAlgoliaSource(queries_yaml_path=yaml_path)


def test_rejects_bool_hits_per_page(tmp_path: Path) -> None:
    yaml_path = _yaml(
        tmp_path,
        """
        defaults:
          hits_per_page: true
        phrases:
          - "shutting down"
        """,
    )
    with pytest.raises(TypeError, match="hits_per_page"):
        HNAlgoliaSource(queries_yaml_path=yaml_path)


@pytest.mark.vcr
async def test_round_trip(tmp_path: Path) -> None:
    """Live cassette: single year-window covering 2017 so the Mattermark
    obituary surfaces and the cassette stays small."""
    if not CASSETTE_FILE.exists() and not os.environ.get("RECORD"):
        pytest.skip(f"no cassette at {CASSETTE_FILE}; rerun with RECORD=1 to record")
    yaml_path = _yaml(
        tmp_path,
        """
        defaults:
          date_from: "2017-01-01"
          date_to: "2017-12-31"
          pages_per_window: 3
          hits_per_page: 30
        phrases:
          - "shut down"
        """,
    )
    src = HNAlgoliaSource(queries_yaml_path=yaml_path)
    entries = [e async for e in src.fetch()]
    # Mattermark obituary (Dec 22 2017) lives in this year-window.
    assert any("Mattermark" in (e.markdown_text or "") for e in entries)
