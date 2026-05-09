"""Wayback enricher: recovers content for curated rows whose live URL is dead.

* No-op when ``raw_html`` is already populated.
* When ``raw_html`` is empty, hit Wayback's availability API, fetch the snapshot
  URL it returns, and stash the result in ``raw_html`` and ``markdown_text``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import httpx

from slopmortem.corpus.sources import WaybackEnricher
from slopmortem.models import RawEntry

if TYPE_CHECKING:
    import pytest


class _FakeResp:
    def __init__(
        self,
        *,
        text: str = "",
        json_payload: dict[str, Any] | None = None,
        status: int = 200,
    ) -> None:
        self.text = text
        self.status_code = status
        self._json = json_payload or {}

    def json(self) -> dict[str, Any]:
        return self._json


async def test_wayback_noop_when_raw_html_present(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = AsyncMock()
    monkeypatch.setattr("slopmortem.corpus.sources.wayback.safe_get", fake)
    entry = RawEntry(
        source="curated",
        source_id="acme",
        url="https://acme.example/post",
        raw_html="<html><body>existing</body></html>",
        markdown_text=None,
        fetched_at=datetime.now(UTC),
    )
    enr = WaybackEnricher()
    out = await enr.enrich(entry)
    # No HTTP triggered; raw_html unchanged.
    assert fake.call_count == 0
    assert out.raw_html == entry.raw_html


def _long_body(seed: str) -> str:
    body = f"{seed} " + ("padding " * 250)
    return f"<html><body><p>{body}</p></body></html>"


async def test_wayback_fetches_snapshot_when_html_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_url = "https://web.archive.org/web/20230101000000/https://acme.example/post"
    availability_payload = {
        "archived_snapshots": {
            "closest": {
                "available": True,
                "url": snapshot_url,
                "timestamp": "20230101000000",
                "status": "200",
            }
        }
    }
    snapshot_html = _long_body("ACME ARCHIVED CONTENT")
    responses = {
        "https://archive.org/wayback/available?url=https%3A%2F%2Facme.example%2Fpost": _FakeResp(
            json_payload=availability_payload
        ),
        snapshot_url: _FakeResp(text=snapshot_html),
    }

    async def fake_get(url: str, **_kw: object) -> Any:
        if url not in responses:
            msg = f"unexpected URL: {url}"
            raise AssertionError(msg)
        return responses[url]

    monkeypatch.setattr("slopmortem.corpus.sources.wayback.safe_get", fake_get)
    monkeypatch.setattr(
        "slopmortem.corpus.sources.wayback.respect_robots",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "slopmortem.corpus.sources.wayback.throttle_for",
        AsyncMock(return_value=None),
    )

    entry = RawEntry(
        source="curated",
        source_id="acme",
        url="https://acme.example/post",
        raw_html=None,
        markdown_text=None,
        fetched_at=datetime.now(UTC),
    )
    enr = WaybackEnricher()
    out = await enr.enrich(entry)
    assert out.raw_html == snapshot_html
    assert out.markdown_text is not None
    assert "ACME ARCHIVED CONTENT" in out.markdown_text


async def test_wayback_returns_entry_unchanged_when_no_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"archived_snapshots": {}}
    monkeypatch.setattr(
        "slopmortem.corpus.sources.wayback.safe_get",
        AsyncMock(return_value=_FakeResp(json_payload=payload)),
    )
    monkeypatch.setattr(
        "slopmortem.corpus.sources.wayback.respect_robots",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "slopmortem.corpus.sources.wayback.throttle_for",
        AsyncMock(return_value=None),
    )

    entry = RawEntry(
        source="curated",
        source_id="acme",
        url="https://acme.example/post",
        raw_html=None,
        markdown_text=None,
        fetched_at=datetime.now(UTC),
    )
    enr = WaybackEnricher()
    out = await enr.enrich(entry)
    assert out.raw_html is None
    assert out.markdown_text is None


async def test_wayback_leaves_entry_unchanged_when_extraction_yields_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot found but trafilatura returns nothing — must not poison raw_html.

    Regression: an archived nav-only homepage would set raw_html=<html> and
    markdown_text=None, which short-circuited TavilyEnricher's skip-guard and
    left the entry effectively bodyless.
    """
    snapshot_url = "https://web.archive.org/web/20160614000000/http://acme.example/"
    availability_payload = {
        "archived_snapshots": {
            "closest": {
                "available": True,
                "url": snapshot_url,
                "timestamp": "20160614000000",
                "status": "200",
            }
        }
    }
    # HTML that trafilatura/readability will reduce to no extracted content.
    nav_only_html = "<html><head><title>x</title></head><body></body></html>"
    responses = {
        "https://archive.org/wayback/available?url=http%3A%2F%2Facme.example%2F": _FakeResp(
            json_payload=availability_payload
        ),
        snapshot_url: _FakeResp(text=nav_only_html),
    }

    async def fake_get(url: str, **_kw: object) -> Any:
        if url not in responses:
            msg = f"unexpected URL: {url}"
            raise AssertionError(msg)
        return responses[url]

    monkeypatch.setattr("slopmortem.corpus.sources.wayback.safe_get", fake_get)
    monkeypatch.setattr(
        "slopmortem.corpus.sources.wayback.respect_robots",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "slopmortem.corpus.sources.wayback.throttle_for",
        AsyncMock(return_value=None),
    )

    entry = RawEntry(
        source="hn_algolia",
        source_id="42",
        url="http://acme.example/",
        raw_html=None,
        markdown_text=None,
        fetched_at=datetime.now(UTC),
    )
    enr = WaybackEnricher()
    out = await enr.enrich(entry)
    # Critical: raw_html stays None so the next enricher in the chain (Tavily)
    # can attempt the live URL instead of being short-circuited.
    assert out.raw_html is None
    assert out.markdown_text is None


class _Resp:
    def __init__(self, *, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.headers: dict[str, str] = headers or {}
        self.text = ""

    def json(self) -> dict[str, Any]:
        return {}


async def test_wayback_retries_then_succeeds_on_transient_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `ReadTimeout` followed by a 503 followed by a 200 must yield the
    successful response, not drop the candidate. Confirms wait-and-retry on
    rate-limit signals (the user-memory rule)."""
    snapshot_url = "https://web.archive.org/web/20230101000000/https://acme.example/post"
    snapshot_html = _long_body("ACME ARCHIVED CONTENT")
    availability_payload = {
        "archived_snapshots": {
            "closest": {
                "available": True,
                "url": snapshot_url,
                "timestamp": "20230101000000",
                "status": "200",
            }
        }
    }
    availability_url = "https://archive.org/wayback/available?url=https%3A%2F%2Facme.example%2Fpost"

    snapshot_calls = 0

    async def fake_get(url: str, **_kw: object) -> Any:
        nonlocal snapshot_calls
        if url == availability_url:
            return _FakeResp(json_payload=availability_payload)
        if url == snapshot_url:
            snapshot_calls += 1
            if snapshot_calls == 1:
                msg = "simulated transient timeout"
                raise httpx.ReadTimeout(msg)
            if snapshot_calls == 2:
                return _Resp(status=503, headers={"retry-after": "0"})
            return _FakeResp(text=snapshot_html)
        msg = f"unexpected URL: {url}"
        raise AssertionError(msg)

    monkeypatch.setattr("slopmortem.corpus.sources.wayback.safe_get", fake_get)
    monkeypatch.setattr(
        "slopmortem.corpus.sources.wayback.respect_robots",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "slopmortem.corpus.sources.wayback.throttle_for",
        AsyncMock(return_value=None),
    )
    # Don't actually wait between retries.
    monkeypatch.setattr(
        "slopmortem.corpus.sources.wayback.anyio.sleep",
        AsyncMock(return_value=None),
    )

    entry = RawEntry(
        source="curated",
        source_id="acme",
        url="https://acme.example/post",
        raw_html=None,
        markdown_text=None,
        fetched_at=datetime.now(UTC),
    )
    enr = WaybackEnricher()
    out = await enr.enrich(entry)
    assert snapshot_calls == 3, "expected two retries before success"
    assert out.raw_html == snapshot_html
    assert out.markdown_text is not None
    assert "ACME ARCHIVED CONTENT" in out.markdown_text


async def test_wayback_drops_after_exhausting_retries_on_persistent_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistent 503 still drops the candidate after the retry budget,
    matching the documented contract that terminal failures return the entry
    unchanged. The fallback exists to recover from transients, not to loop
    forever."""
    availability_url = "https://archive.org/wayback/available?url=https%3A%2F%2Facme.example%2Fpost"
    calls = 0

    async def fake_get(url: str, **_kw: object) -> Any:
        nonlocal calls
        if url != availability_url:
            msg = f"unexpected URL: {url}"
            raise AssertionError(msg)
        calls += 1
        return _Resp(status=503)

    monkeypatch.setattr("slopmortem.corpus.sources.wayback.safe_get", fake_get)
    monkeypatch.setattr(
        "slopmortem.corpus.sources.wayback.respect_robots",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "slopmortem.corpus.sources.wayback.throttle_for",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "slopmortem.corpus.sources.wayback.anyio.sleep",
        AsyncMock(return_value=None),
    )

    entry = RawEntry(
        source="curated",
        source_id="acme",
        url="https://acme.example/post",
        raw_html=None,
        markdown_text=None,
        fetched_at=datetime.now(UTC),
    )
    enr = WaybackEnricher()
    out = await enr.enrich(entry)
    assert calls == 3, "expected exactly _RETRY_ATTEMPTS calls before drop"
    assert out.raw_html is None
    assert out.markdown_text is None


async def test_wayback_does_not_retry_terminal_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 is permanent — the caller should drop on the first attempt,
    not waste retry budget on something that won't resolve."""
    availability_url = "https://archive.org/wayback/available?url=https%3A%2F%2Facme.example%2Fpost"
    calls = 0

    async def fake_get(url: str, **_kw: object) -> Any:
        nonlocal calls
        if url != availability_url:
            msg = f"unexpected URL: {url}"
            raise AssertionError(msg)
        calls += 1
        return _Resp(status=404)

    monkeypatch.setattr("slopmortem.corpus.sources.wayback.safe_get", fake_get)
    monkeypatch.setattr(
        "slopmortem.corpus.sources.wayback.respect_robots",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "slopmortem.corpus.sources.wayback.throttle_for",
        AsyncMock(return_value=None),
    )

    entry = RawEntry(
        source="curated",
        source_id="acme",
        url="https://acme.example/post",
        raw_html=None,
        markdown_text=None,
        fetched_at=datetime.now(UTC),
    )
    enr = WaybackEnricher()
    out = await enr.enrich(entry)
    assert calls == 1, "404 must not trigger retries"
    assert out.raw_html is None
    assert out.markdown_text is None


async def test_wayback_honors_retry_after_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 with `Retry-After: 7` must drive the next backoff to ~7s rather
    than the default schedule. The schedule kicks in only when the server
    didn't tell us how long to wait."""
    availability_url = "https://archive.org/wayback/available?url=https%3A%2F%2Facme.example%2Fpost"
    calls = 0

    async def fake_get(url: str, **_kw: object) -> Any:
        nonlocal calls
        if url != availability_url:
            msg = f"unexpected URL: {url}"
            raise AssertionError(msg)
        calls += 1
        if calls == 1:
            return _Resp(status=429, headers={"retry-after": "7"})
        return _FakeResp(json_payload={"archived_snapshots": {}})

    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("slopmortem.corpus.sources.wayback.safe_get", fake_get)
    monkeypatch.setattr(
        "slopmortem.corpus.sources.wayback.respect_robots",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "slopmortem.corpus.sources.wayback.throttle_for",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "slopmortem.corpus.sources.wayback.anyio.sleep",
        record_sleep,
    )

    entry = RawEntry(
        source="curated",
        source_id="acme",
        url="https://acme.example/post",
        raw_html=None,
        markdown_text=None,
        fetched_at=datetime.now(UTC),
    )
    enr = WaybackEnricher()
    _ = await enr.enrich(entry)
    assert sleeps == [7.0], f"expected one 7s sleep from Retry-After, got {sleeps}"
