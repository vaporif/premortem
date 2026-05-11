"""Tests for the structured Tavily search + extract surfaces.

Mirrors the patching shape of ``tests/test_tavily_tools.py`` (mock at the
``safe_post`` boundary) — the project's documented exception to the
"fakes over mocks" rule for low-level HTTP transport.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from slopmortem.corpus.tavily import (
    TavilyHit,
    tavily_extract_structured,
    tavily_search_structured,
)


def _resp(status: int, body: dict[str, Any]) -> httpx.Response:
    request = httpx.Request("POST", "https://api.tavily.com/search")
    return httpx.Response(status, json=body, request=request)


def _extract_resp(status: int, body: dict[str, Any]) -> httpx.Response:
    request = httpx.Request("POST", "https://api.tavily.com/extract")
    return httpx.Response(status, json=body, request=request)


async def test_returns_typed_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_resp = _resp(
        200,
        {
            "results": [
                {
                    "title": "Acme shut down",
                    "url": "https://example.com/a",
                    "content": "first snippet body",
                    "published_date": "2024-11-18T11:05:19Z",
                },
                {
                    "title": "Post-mortem",
                    "url": "https://example.com/b",
                    "content": "second snippet body",
                    "published_date": "2025-01-02",
                },
            ]
        },
    )
    monkeypatch.setattr(
        "slopmortem.corpus.tavily.safe_post",
        AsyncMock(return_value=fake_resp),
    )
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-key")

    hits = await tavily_search_structured("acme failure", limit=5)

    assert hits == [
        TavilyHit(
            title="Acme shut down",
            url="https://example.com/a",
            snippet="first snippet body",
            published_date="2024-11-18T11:05:19Z",
        ),
        TavilyHit(
            title="Post-mortem",
            url="https://example.com/b",
            snippet="second snippet body",
            published_date="2025-01-02",
        ),
    ]


async def test_returns_empty_list_when_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_resp = _resp(200, {"results": []})
    monkeypatch.setattr(
        "slopmortem.corpus.tavily.safe_post",
        AsyncMock(return_value=fake_resp),
    )
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-key")

    assert await tavily_search_structured("nothing matches", limit=5) == []


async def test_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="TAVILY_API_KEY"):
        await tavily_search_structured("x", limit=1)


async def test_snippet_truncated_to_500_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    long_content = "x" * 1000
    fake_resp = _resp(
        200,
        {
            "results": [
                {
                    "title": "long",
                    "url": "https://example.com/long",
                    "content": long_content,
                }
            ]
        },
    )
    monkeypatch.setattr(
        "slopmortem.corpus.tavily.safe_post",
        AsyncMock(return_value=fake_resp),
    )
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-key")

    hits = await tavily_search_structured("long", limit=1)

    assert len(hits) == 1
    assert len(hits[0].snippet) == 500


async def test_published_date_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tavily may omit ``published_date``; the hit should carry ``None``."""
    fake_resp = _resp(
        200,
        {
            "results": [
                {
                    "title": "No date",
                    "url": "https://example.com/no-date",
                    "content": "snippet",
                }
            ]
        },
    )
    monkeypatch.setattr(
        "slopmortem.corpus.tavily.safe_post",
        AsyncMock(return_value=fake_resp),
    )
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-key")

    hits = await tavily_search_structured("x", limit=1)
    assert len(hits) == 1
    assert hits[0].published_date is None


async def test_propagates_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-2xx status surfaces as ``HTTPStatusError`` — same contract as the formatted surface."""
    fake_resp = _resp(429, {"detail": "rate limited"})
    monkeypatch.setattr(
        "slopmortem.corpus.tavily.safe_post",
        AsyncMock(return_value=fake_resp),
    )
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-key")
    with pytest.raises(httpx.HTTPStatusError):
        await tavily_search_structured("x", limit=1)


async def test_posts_documented_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """The POST body uses Tavily's documented field names."""
    fake_resp = _resp(200, {"results": []})
    mock_post = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr("slopmortem.corpus.tavily.safe_post", mock_post)
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-key")

    await tavily_search_structured("acme failure", limit=3)

    body = mock_post.call_args.kwargs["json"]
    assert body["query"] == "acme failure"
    assert body["max_results"] == 3
    assert body["api_key"] == "tv-test-key"


# ---------------------------------------------------------------------------
# tavily_extract_structured
# ---------------------------------------------------------------------------


async def test_extract_returns_raw_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tavily ``/extract`` returns ``raw_content`` on the first result."""
    fake_resp = _extract_resp(
        200,
        {
            "results": [
                {"url": "https://example.com/a", "raw_content": "full article body text"},
            ],
            "failed_results": [],
        },
    )
    monkeypatch.setattr(
        "slopmortem.corpus.tavily.safe_post",
        AsyncMock(return_value=fake_resp),
    )
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-key")

    body = await tavily_extract_structured("https://example.com/a")
    assert body == "full article body text"


async def test_extract_returns_empty_when_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``results`` entries → empty string (Tavily can't render the URL)."""
    fake_resp = _extract_resp(200, {"results": [], "failed_results": []})
    monkeypatch.setattr(
        "slopmortem.corpus.tavily.safe_post",
        AsyncMock(return_value=fake_resp),
    )
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-key")

    assert await tavily_extract_structured("https://example.com/dead") == ""


async def test_extract_propagates_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 from Tavily surfaces as ``HTTPStatusError`` so the verifier can drop cleanly."""
    fake_resp = _extract_resp(429, {"detail": "rate limited"})
    monkeypatch.setattr(
        "slopmortem.corpus.tavily.safe_post",
        AsyncMock(return_value=fake_resp),
    )
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-key")
    with pytest.raises(httpx.HTTPStatusError):
        await tavily_extract_structured("https://example.com/a")


async def test_extract_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same key-missing contract as the search surface."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="TAVILY_API_KEY"):
        await tavily_extract_structured("https://example.com/a")


async def test_extract_posts_documented_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """The POST body uses Tavily's documented ``urls`` field shape."""
    fake_resp = _extract_resp(200, {"results": []})
    mock_post = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr("slopmortem.corpus.tavily.safe_post", mock_post)
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-key")

    await tavily_extract_structured("https://example.com/a")

    body = mock_post.call_args.kwargs["json"]
    assert body["urls"] == ["https://example.com/a"]
    assert body["api_key"] == "tv-test-key"
