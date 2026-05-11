"""Structured Tavily ``/search`` surface.

Sibling to the formatted-string ``tavily_search_async`` in ``_tools_impl.py``:
that one feeds the synthesis LLM tool loop and must keep its line-rendering
shape (cassette matching), so it stays on the leaf-private module. Callers
that want typed hits — e.g. the recall verifier's search head — import
``tavily_search_structured`` from here without crossing the corpus leaf
boundary.

Both surfaces share ``parse_tavily_response`` so the snippet truncation
and field coercion stay identical.
"""

from __future__ import annotations

import os
from typing import Final, cast

from pydantic import BaseModel

from slopmortem.http import safe_post

TAVILY_SEARCH_URL: Final[str] = "https://api.tavily.com/search"
TAVILY_SNIPPET_CHARS: Final[int] = 500

__all__ = [
    "TAVILY_SEARCH_URL",
    "TAVILY_SNIPPET_CHARS",
    "TavilyHit",
    "parse_tavily_response",
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


def _tavily_api_key() -> str:
    """Read at call time so the helper stays usable from contexts without ``Config``."""
    key = os.environ.get("TAVILY_API_KEY", "")
    if not key:
        msg = "TAVILY_API_KEY not set; Tavily search is unavailable"
        raise RuntimeError(msg)
    return key


def parse_tavily_response(payload: object, limit: int) -> list[TavilyHit]:
    """Coerce a Tavily ``/search`` JSON body into typed hits.

    The payload comes from ``httpx.Response.json()`` (typed ``Any``) so each
    field is coerced at this boundary. Snippet is truncated to
    ``TAVILY_SNIPPET_CHARS`` to match the formatted-string surface.

    Public (no leading underscore) only because ``_tools_impl`` shares it
    to keep both Tavily surfaces in lockstep — external callers should use
    ``tavily_search_structured`` instead.
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
        json={"api_key": _tavily_api_key(), "query": q, "max_results": limit},
    )
    resp.raise_for_status()
    payload: object = resp.json()  # pyright: ignore[reportAny]  # httpx Response.json() is Any by design
    return parse_tavily_response(payload, limit)
