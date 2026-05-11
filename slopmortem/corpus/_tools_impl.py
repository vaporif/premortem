"""Tool implementations exposed to the LLM via OpenRouter function-calling.

Corpus tools delegate to a module-level ``Corpus`` bound via ``set_query_corpus``
so the functions stay plain ``async def`` and match the ``ToolSpec``
signature contract (no closures, no bound methods).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from slopmortem.corpus.tavily import (
    TAVILY_SEARCH_URL,
    parse_tavily_response,
    tavily_api_key,
)
from slopmortem.http import safe_post
from slopmortem.models import (
    BusinessModelLit,
    CustomerTypeLit,
    GeographyLit,
    MonetizationLit,
    SectorLit,
    ToolSpec,
)

if TYPE_CHECKING:
    from slopmortem.corpus._store import Corpus

TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"

__all__ = [
    "GetPostMortemArgs",
    "SearchCorpusArgs",
    "SearchFacets",
    "SearchHit",
    "TavilyExtractArgs",
    "TavilySearchArgs",
    "get_post_mortem",
    "search_corpus",
    "set_query_corpus",
    "tavily_extract",
    "tavily_search",
]


class GetPostMortemArgs(BaseModel):
    canonical_id: str
    max_chars: int = 8000


class SearchFacets(BaseModel):
    """Closed-enum filters for ``search_corpus``; all optional.

    Values come from ``taxonomy.yml`` via ``*Lit``, so the JSON schema carries
    a per-field ``enum`` and grammar-constrained sampling enforces validity
    at decode time.
    """

    sector: SectorLit | None = None
    business_model: BusinessModelLit | None = None
    customer_type: CustomerTypeLit | None = None
    geography: GeographyLit | None = None
    monetization: MonetizationLit | None = None


class SearchCorpusArgs(BaseModel):
    q: str
    facets: SearchFacets | None = None
    limit: int = 5


class SearchHit(BaseModel):
    canonical_id: str
    name: str
    snippet: str
    score: float


class TavilySearchArgs(BaseModel):
    q: str
    limit: int = 5


class TavilyExtractArgs(BaseModel):
    url: str


_corpus: Corpus | None = None


def set_query_corpus(c: Corpus) -> None:
    """Wire the module-level corpus reference that the LLM tool callables read."""
    global _corpus  # noqa: PLW0603 — the module-level binding is the public init surface
    _corpus = c


async def _get_post_mortem(canonical_id: str, max_chars: int = 8000) -> str:
    if _corpus is None:
        msg = "corpus not initialized"
        raise RuntimeError(msg)
    body = await _corpus.get_post_mortem(canonical_id)
    if max_chars > 0 and len(body) > max_chars:
        return body[:max_chars] + f"\n\n[...truncated; full body is {len(body)} chars...]"
    return body


async def _search_corpus(
    q: str, facets: dict[str, str] | None = None, limit: int = 5
) -> list[SearchHit]:
    if _corpus is None:
        msg = "corpus not initialized"
        raise RuntimeError(msg)
    raw = await _corpus.search_corpus(q, facets=facets)
    hits: list[SearchHit] = []
    for row in raw[:limit]:
        # Corpus impls vary (Qdrant payloads, fakes, future stores), so coerce
        # each field to its expected scalar type at this boundary.
        summary = row.get("summary") or row.get("body") or ""
        snippet = str(summary)[:500]
        hits.append(
            SearchHit(
                canonical_id=str(row.get("canonical_id", "")),  # pyright: ignore[reportAny]
                name=str(row.get("name", "")),  # pyright: ignore[reportAny]
                snippet=snippet,
                score=float(row.get("score", 0.0)),  # pyright: ignore[reportAny]
            )
        )
    return hits


async def tavily_search_async(q: str, limit: int = 5) -> str:
    r"""Search Tavily; return ``- title — url\n  snippet`` lines or ``"(no results)"``.

    Shares ``parse_tavily_response`` with ``tavily_search_structured`` so the
    snippet truncation stays in lockstep across both surfaces. The line shape
    is load-bearing for cassette matching on the synthesis tool-call loop.
    """
    resp = await safe_post(
        TAVILY_SEARCH_URL,
        json={"api_key": tavily_api_key(), "query": q, "max_results": limit},
    )
    resp.raise_for_status()
    payload: object = resp.json()  # pyright: ignore[reportAny]  # httpx Response.json() is Any by design
    hits = parse_tavily_response(payload, limit)
    if not hits:
        return "(no results)"
    return "\n".join(f"- {h.title} — {h.url}\n  {h.snippet}" for h in hits)


async def tavily_extract_async(url: str) -> str:
    """Return ``""`` when no results."""
    resp = await safe_post(
        TAVILY_EXTRACT_URL,
        json={"api_key": tavily_api_key(), "urls": [url]},
    )
    resp.raise_for_status()
    payload = resp.json()  # pyright: ignore[reportAny]  # httpx Response.json() is Any by design
    results: list[dict[str, object]] = (
        payload.get("results", []) if payload else []  # pyright: ignore[reportAny]
    )
    if not results:
        return ""
    return str(results[0].get("raw_content", ""))


get_post_mortem = ToolSpec(
    name="get_post_mortem",
    description=(
        "Fetch the canonical post-mortem text for a candidate, truncated to "
        "max_chars (default 8000). If truncated, the response ends with a "
        "marker indicating the full length; raise max_chars to read more."
    ),
    args_model=GetPostMortemArgs,
    fn=_get_post_mortem,
)

search_corpus = ToolSpec(
    name="search_corpus",
    description=(
        "Search the corpus for additional dead startups matching a query and "
        "optional taxonomy facets (sector, business_model, customer_type, "
        "geography, monetization). Facet values are closed enums — see the "
        "schema for allowed values."
    ),
    args_model=SearchCorpusArgs,
    fn=_search_corpus,
)

tavily_search = ToolSpec(
    name="tavily_search",
    description="Search the live web via Tavily for evidence to support synthesis.",
    args_model=TavilySearchArgs,
    fn=tavily_search_async,
)

tavily_extract = ToolSpec(
    name="tavily_extract",
    description="Fetch and extract the readable content of a single URL via Tavily.",
    args_model=TavilyExtractArgs,
    fn=tavily_extract_async,
)
