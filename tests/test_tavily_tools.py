"""Tests for the Tavily synthesis tools (``tavily_search`` / ``tavily_extract``).

The tools wrap Tavily's POST-only ``/search`` and ``/extract`` endpoints
behind ``safe_post``. They read ``TAVILY_API_KEY`` from the environment at
call time (not from `Config`), because the tool callables are
passed bare to OpenRouter's function-calling surface and the existing
``set_query_corpus`` indirection would not extend cleanly to a second
binding.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from slopmortem.corpus._tools_impl import tavily_extract_async, tavily_search_async


def _resp(status: int, body: dict[str, Any]) -> httpx.Response:
    """Build an ``httpx.Response`` with a request attached so ``raise_for_status`` works."""
    request = httpx.Request("POST", "https://api.tavily.com/")
    return httpx.Response(status, json=body, request=request)


async def test_tavily_search_calls_api_and_formats_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tavily_search`` posts the documented body and renders a text summary."""
    fake_resp = _resp(
        200,
        {
            "results": [
                {
                    "title": "Co. shut down",
                    "url": "https://example.com/a",
                    "content": "first snippet",
                },
                {
                    "title": "Post-mortem",
                    "url": "https://example.com/b",
                    "content": "second snippet",
                },
            ]
        },
    )
    mock_post = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr("slopmortem.corpus._tools_impl.safe_post", mock_post)
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-key")

    out = await tavily_search_async("acme failure", limit=2)

    # Result is a string the LLM can read: contains URLs and titles, no raw HTML.
    assert "example.com/a" in out
    assert "example.com/b" in out
    assert "Co. shut down" in out
    # Body uses Tavily's documented JSON shape.
    body = mock_post.call_args.kwargs["json"]
    assert body["query"] == "acme failure"
    assert body["max_results"] == 2
    assert body["api_key"] == "tv-test-key"


async def test_tavily_search_raises_on_missing_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing TAVILY_API_KEY surfaces a clear RuntimeError."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="TAVILY_API_KEY"):
        await tavily_search_async("x", limit=1)


async def test_tavily_search_returns_marker_when_no_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty results list yields a recognizable placeholder, not an empty string."""
    fake_resp = _resp(200, {"results": []})
    monkeypatch.setattr(
        "slopmortem.corpus._tools_impl.safe_post",
        AsyncMock(return_value=fake_resp),
    )
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-key")

    out = await tavily_search_async("nothing matches", limit=5)
    assert "no results" in out.lower()


async def test_tavily_extract_calls_api_and_returns_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tavily_extract`` posts the URL and returns the ``raw_content`` body."""
    fake_resp = _resp(
        200,
        {"results": [{"url": "https://example.com/x", "raw_content": "extracted body"}]},
    )
    mock_post = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr("slopmortem.corpus._tools_impl.safe_post", mock_post)
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-key")

    out = await tavily_extract_async("https://example.com/x")
    assert "extracted body" in out
    body = mock_post.call_args.kwargs["json"]
    assert body["urls"] == ["https://example.com/x"]
    assert body["api_key"] == "tv-test-key"


async def test_tavily_extract_propagates_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-2xx response surfaces as ``HTTPStatusError`` for the caller to handle."""
    fake_resp = _resp(429, {"detail": "rate limited"})
    monkeypatch.setattr(
        "slopmortem.corpus._tools_impl.safe_post", AsyncMock(return_value=fake_resp)
    )
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-key")
    with pytest.raises(httpx.HTTPStatusError):
        await tavily_extract_async("https://example.com/x")


async def test_tavily_extract_returns_empty_on_no_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty Tavily ``results`` list yields the empty string."""
    fake_resp = _resp(200, {"results": []})
    monkeypatch.setattr(
        "slopmortem.corpus._tools_impl.safe_post", AsyncMock(return_value=fake_resp)
    )
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-key")
    assert await tavily_extract_async("https://example.com/x") == ""
