"""Structured Tavily ``/search`` and ``/extract`` surfaces.

Sibling to the formatted-string ``tavily_search_async`` in ``_tools_impl.py``,
which keeps its line-rendering shape for cassette matching. Callers that want
typed hits (e.g. the recall verifier) import from here.

``tavily_extract_structured`` is the L3 fallback when a direct GET 4xxs
(bot-blocked hosts like Medium) or returns a body too short to admit (SPA
shells like decrypt.co).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final, cast

import anyio
from pydantic import BaseModel

from slopmortem.http import safe_post

if TYPE_CHECKING:
    import httpx

TAVILY_SEARCH_URL: Final[str] = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL: Final[str] = "https://api.tavily.com/extract"
TAVILY_SNIPPET_CHARS: Final[int] = 500

# Tavily applies per-second rate limits and signals them with 432 ("limit
# reached"); 429 and 5xx surface the same shape. ``recall_verify`` fans out
# 3 suggestions x 2 search passes concurrently, which trips the cap. Without
# retry, a single 432 permanently drops a candidate the verifier would
# otherwise admit. Exponential backoff (1s, 2s, 4s) absorbs the burst
# without serializing the whole stage.
_RETRY_STATUSES: Final[frozenset[int]] = frozenset({429, 432, 503, 504})
_MAX_RETRIES: Final[int] = 3
_BASE_BACKOFF_S: Final[float] = 1.0

logger = logging.getLogger(__name__)

__all__ = [
    "TAVILY_EXTRACT_URL",
    "TAVILY_SEARCH_URL",
    "TAVILY_SNIPPET_CHARS",
    "TavilyHit",
    "parse_tavily_response",
    "tavily_extract_structured",
    "tavily_search_structured",
]


async def _post_retrying(url: str, *, json: dict[str, object]) -> httpx.Response:
    """POST with retry-with-backoff on Tavily's transient failure codes."""
    delay = _BASE_BACKOFF_S
    for attempt in range(_MAX_RETRIES):
        resp = await safe_post(url, json=json)
        if resp.status_code not in _RETRY_STATUSES:
            return resp
        logger.info(
            "tavily: %d on %s, retrying in %.1fs (attempt %d/%d)",
            resp.status_code,
            url,
            delay,
            attempt + 1,
            _MAX_RETRIES + 1,
        )
        await anyio.sleep(delay)
        delay *= 2
    # Final attempt; if still transient, the caller's ``raise_for_status``
    # surfaces the error and the verifier drops the candidate as before.
    return await safe_post(url, json=json)


class TavilyHit(BaseModel):
    """One parsed result from Tavily ``/search``.

    ``published_date`` passes through as Tavily emits it (ISO 8601 string per
    their API docs); downstream code decides whether to parse it.
    """

    title: str
    url: str
    snippet: str
    published_date: str | None = None


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


async def tavily_search_structured(q: str, limit: int, *, api_key: str) -> list[TavilyHit]:
    """Hit ``api.tavily.com/search`` and return parsed hits (empty list = no results).

    Raises:
        httpx.HTTPError: Tavily returned a non-2xx status or the request failed.
    """
    resp = await _post_retrying(
        TAVILY_SEARCH_URL,
        json={"api_key": api_key, "query": q, "max_results": limit},
    )
    resp.raise_for_status()
    payload: object = resp.json()  # pyright: ignore[reportAny]  # httpx Response.json() is Any by design
    return parse_tavily_response(payload, limit)


async def tavily_extract_structured(url: str, *, api_key: str) -> str:
    """Hit ``api.tavily.com/extract`` and return the rendered article body.

    Returns ``""`` when Tavily has no usable content. The recall verifier's L3
    fallback treats empty as a hard drop (same shape as a direct GET body
    below the 500-char floor).

    Raises:
        httpx.HTTPError: Tavily returned a non-2xx status or the request failed.
    """
    resp = await _post_retrying(
        TAVILY_EXTRACT_URL,
        json={"api_key": api_key, "urls": [url]},
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
