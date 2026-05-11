"""Tavily search tool for the LLM-driven pitch filler.

Separate from the synthesis-time ``tavily_search`` in
``corpus/_tools_impl.py`` because the filler needs truncated ``raw_content``;
synthesis only needs snippets.
"""

from __future__ import annotations

import json
import logging
import os
from typing import cast

import httpx
from pydantic import BaseModel

from slopmortem.http import safe_post
from slopmortem.models import ToolSpec

__all__ = ["PitchFillerSearchArgs", "build_pitch_filler_tavily_tool"]

logger = logging.getLogger(__name__)

_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_DEFAULT_LIMIT = 5


class PitchFillerSearchArgs(BaseModel):
    q: str
    limit: int = _DEFAULT_LIMIT


def build_pitch_filler_tavily_tool(*, max_chars_per_result: int = 2500) -> ToolSpec:
    """Tavily search tool wired with raw_content truncation tuned for the filler.

    `include_raw_content=True` so the model gets enough body to synthesize
    a faithful pitch from one search call. Per-result truncation keeps the
    prompt bounded across 5 hits.
    """

    async def _search(q: str, limit: int = _DEFAULT_LIMIT) -> str:
        api_key = os.environ.get("TAVILY_API_KEY", "")
        if not api_key:
            return json.dumps({"error": "TAVILY_API_KEY not set", "results": []})
        try:
            resp = await safe_post(
                _TAVILY_SEARCH_URL,
                json={
                    "api_key": api_key,
                    "query": q,
                    "max_results": limit,
                    "include_raw_content": True,
                    "search_depth": "basic",
                },
            )
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("pitch filler tavily_search: fetch failed for q=%r: %r", q, exc)
            return json.dumps({"error": str(exc), "results": []})

        if resp.status_code >= httpx.codes.BAD_REQUEST:
            logger.warning("pitch filler tavily_search: HTTP %s for q=%r", resp.status_code, q)
            return json.dumps({"error": f"HTTP {resp.status_code}", "results": []})

        try:
            payload: object = resp.json()  # pyright: ignore[reportAny]
        except ValueError:
            return json.dumps({"error": "non-JSON response", "results": []})

        return json.dumps({"results": _shape_results(payload, max_chars_per_result)})

    return ToolSpec(
        name="tavily_search",
        description=(
            "Search the live web via Tavily for primary coverage of a dead startup. "
            "Returns up to `limit` results with truncated raw_content so a single "
            "search call surfaces enough body to synthesize a pitch."
        ),
        args_model=PitchFillerSearchArgs,
        fn=_search,
    )


def _shape_results(payload: object, max_chars: int) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    payload_dict = cast("dict[str, object]", payload)
    raw_results: object = payload_dict.get("results")
    if not isinstance(raw_results, list):
        return []
    results_list = cast("list[object]", raw_results)
    out: list[dict[str, object]] = []
    for item in results_list:
        if not isinstance(item, dict):
            continue
        item_dict = cast("dict[str, object]", item)
        raw_content = item_dict.get("raw_content")
        truncated_raw = (
            str(raw_content)[:max_chars] if isinstance(raw_content, str) and raw_content else ""
        )
        out.append(
            {
                "url": str(item_dict.get("url", "")),
                "title": str(item_dict.get("title", "")),
                "raw_content": truncated_raw,
                "score": item_dict.get("score"),
            }
        )
    return out
