"""Structured Tavily ``/search`` and ``/extract`` surfaces.

Sibling to the formatted-string ``tavily_search_async`` in ``_tools_impl.py``,
which keeps its line-rendering shape for cassette matching. Callers that want
typed hits (e.g. the recall verifier) import from here.

``tavily_extract_structured`` is the L3 fallback when direct GET 4xxs
(bot-blocked hosts like Medium) or returns a body too short to admit (SPA
shells like decrypt.co); Tavily fetches via its own IP pool and headless
browser.

Both surfaces share ``parse_tavily_response`` for snippet truncation.
"""

from __future__ import annotations

import os
from typing import Final, cast

from pydantic import BaseModel

from slopmortem.http import safe_post

TAVILY_SEARCH_URL: Final[str] = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL: Final[str] = "https://api.tavily.com/extract"
TAVILY_SNIPPET_CHARS: Final[int] = 500

__all__ = [
    "TAVILY_EXTRACT_URL",
    "TAVILY_SEARCH_URL",
    "TAVILY_SNIPPET_CHARS",
    "TavilyHit",
    "parse_tavily_response",
    "tavily_api_key",
    "tavily_extract_structured",
    "tavily_search_structured",
]


class TavilyHit(BaseModel):
    """One parsed result from Tavily ``/search``.

    ``published_date`` is passed through as Tavily emits it (ISO 8601 string,
    per their API docs) — downstream code decides whether to parse it.
    """

    title: str
    url: str
    snippet: str
    published_date: str | None = None


def tavily_api_key() -> str:
    """Return ``TAVILY_API_KEY`` from env or raise; canonical helper for both Tavily surfaces.

    Read at call time so the helper stays usable from contexts without ``Config``
    (LLM tool callables are passed bare to OpenRouter and can't carry settings).
    """
    key = os.environ.get("TAVILY_API_KEY", "")
    if not key:
        msg = "TAVILY_API_KEY not set; Tavily search unavailable"
        raise RuntimeError(msg)
    return key


def parse_tavily_response(payload: object, limit: int) -> list[TavilyHit]:
    """Parse Tavily ``/search`` JSON into typed hits.

    Snippets are capped at ``TAVILY_SNIPPET_CHARS``.
    """
    if not isinstance(payload, dict):
        return []
    raw_payload = cast("dict[str, object]", payload)
    raw_results = raw_payload.get("results", [])
    if not isinstance(raw_results, list):
        return []
    raw_list = cast("list[object]", raw_results)
    hits: list[TavilyHit] = []
    for raw in raw_list[:limit]:
        if not isinstance(raw, dict):
            continue
        raw_hit = cast("dict[str, object]", raw)
        published = raw_hit.get("published_date")
        content = raw_hit.get("content") or ""
        hits.append(
            TavilyHit(
                title=str(raw_hit.get("title", "(no title)")),
                url=str(raw_hit.get("url", "")),
                snippet=str(content)[:TAVILY_SNIPPET_CHARS],
                published_date=published if isinstance(published, str) else None,
            )
        )
    return hits


async def tavily_search_structured(q: str, limit: int) -> list[TavilyHit]:
    """Hit ``api.tavily.com/search`` and return parsed hits (empty list = no results).

    Raises:
        RuntimeError: ``TAVILY_API_KEY`` is unset.
        httpx.HTTPError: Tavily returned a non-2xx status or the request failed.
    """
    resp = await safe_post(
        TAVILY_SEARCH_URL,
        json={"api_key": tavily_api_key(), "query": q, "max_results": limit},
    )
    resp.raise_for_status()
    payload: object = resp.json()  # pyright: ignore[reportAny]  # httpx Response.json() is Any by design
    return parse_tavily_response(payload, limit)


async def tavily_extract_structured(url: str) -> str:
    """Hit ``api.tavily.com/extract`` and return the rendered article body.

    Returns ``""`` when Tavily has no usable content for the URL. The recall
    verifier's L3 fallback treats empty as a hard drop — same shape as direct
    GET returning a body below the 500-char floor.

    Raises:
        RuntimeError: ``TAVILY_API_KEY`` is unset.
        httpx.HTTPError: Tavily returned a non-2xx status or the request failed.
    """
    resp = await safe_post(
        TAVILY_EXTRACT_URL,
        json={"api_key": tavily_api_key(), "urls": [url]},
    )
    resp.raise_for_status()
    payload: object = resp.json()  # pyright: ignore[reportAny]  # httpx Response.json() is Any by design
    if not isinstance(payload, dict):
        return ""
    raw_payload = cast("dict[str, object]", payload)
    raw_results = raw_payload.get("results", [])
    if not isinstance(raw_results, list):
        return ""
    raw_list = cast("list[object]", raw_results)
    if not raw_list:
        return ""
    first = raw_list[0]
    if not isinstance(first, dict):
        return ""
    raw_first = cast("dict[str, object]", first)
    return str(raw_first.get("raw_content", ""))
