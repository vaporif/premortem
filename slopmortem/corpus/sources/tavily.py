"""TavilyEnricher: Tavily /extract recovery for entries with empty raw_html."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, cast

import httpx

from slopmortem.corpus._extract import extract_clean
from slopmortem.corpus._tools_impl import TAVILY_EXTRACT_URL
from slopmortem.http import safe_post

if TYPE_CHECKING:
    from slopmortem.models import RawEntry

logger = logging.getLogger(__name__)


def _pick_raw_content(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    payload_dict = cast("dict[str, object]", payload)
    results: object = payload_dict.get("results")
    if not isinstance(results, list) or not results:
        return None
    results_list = cast("list[object]", results)
    first: object = results_list[0]
    if not isinstance(first, dict):
        return None
    first_dict = cast("dict[str, object]", first)
    raw_content: object = first_dict.get("raw_content")
    if not isinstance(raw_content, str) or not raw_content:
        return None
    return raw_content


class TavilyEnricher:
    """[Enricher] Tavily /extract client that recovers article bodies on empty entries."""

    async def enrich(self, entry: RawEntry) -> RawEntry:
        """Best-effort. Returns *entry* unchanged on failure or when the body is already there."""
        if entry.raw_html is not None and entry.raw_html.strip():
            return entry
        if entry.markdown_text is not None and entry.markdown_text.strip():
            return entry
        if not entry.url:
            return entry
        api_key = os.environ.get("TAVILY_API_KEY", "")
        if not api_key:
            logger.warning("tavily enricher: TAVILY_API_KEY not set; skipping")
            return entry

        raw_content = await self._fetch_raw_content(entry.url, api_key)
        if not raw_content:
            logger.info(
                "tavily enricher: empty/no extract for %s:%s url=%s",
                entry.source,
                entry.source_id,
                entry.url,
            )
            return entry

        markdown_text = extract_clean(raw_content) or None
        logger.info(
            "tavily enricher: filled %s:%s body=%d chars (raw=%d) url=%s",
            entry.source,
            entry.source_id,
            len(markdown_text or ""),
            len(raw_content),
            entry.url,
        )
        return entry.model_copy(update={"raw_html": raw_content, "markdown_text": markdown_text})

    async def _fetch_raw_content(self, url: str, api_key: str) -> str | None:
        try:
            resp = await safe_post(
                TAVILY_EXTRACT_URL,
                json={"api_key": api_key, "urls": [url]},
            )
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("tavily enricher: fetch failed for %s: %r", url, exc)
            return None

        if resp.status_code >= httpx.codes.BAD_REQUEST:
            logger.warning("tavily enricher: HTTP %s for %s", resp.status_code, url)
            return None

        try:
            payload: object = resp.json()  # pyright: ignore[reportAny]
        except ValueError:
            return None

        return _pick_raw_content(payload)
