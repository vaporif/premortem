"""Wayback enricher: recovers content for entries whose live URL is dead.

Hits the availability API; on a snapshot, fetches it into ``raw_html`` +
``markdown_text``. No-op when ``raw_html`` is already populated.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote_plus

import httpx

from slopmortem.corpus._extract import extract_clean
from slopmortem.corpus.sources._throttle import (
    HTTP_BAD_REQUEST,
    USER_AGENT,
    respect_robots,
    throttle_for,
)
from slopmortem.http import SSRFBlockedError, safe_get

if TYPE_CHECKING:
    from slopmortem.models import RawEntry

logger = logging.getLogger(__name__)

AVAILABILITY_ENDPOINT = "https://archive.org/wayback/available"


def _availability_url(target: str) -> str:
    return f"{AVAILABILITY_ENDPOINT}?url={quote_plus(target)}"


def _pick_snapshot_url(
    payload: dict[str, Any] | None,  # pyright: ignore[reportExplicitAny]
) -> str | None:
    if not payload:
        return None
    snapshots: object = payload.get("archived_snapshots") or {}
    if not isinstance(snapshots, dict):
        return None
    snapshots_dict = cast("dict[str, object]", snapshots)
    closest: object = snapshots_dict.get("closest")
    if not isinstance(closest, dict):
        return None
    closest_dict = cast("dict[str, object]", closest)
    if not closest_dict.get("available"):
        return None
    snapshot_url: object = closest_dict.get("url")
    if not isinstance(snapshot_url, str) or not snapshot_url:
        return None
    return snapshot_url


class WaybackEnricher:
    """[Enricher] Internet Archive client that recovers dead curated URLs."""

    def __init__(
        self,
        *,
        user_agent: str = USER_AGENT,
        rps: float = 1.0,
    ) -> None:
        self.user_agent = user_agent
        self.rps = rps

    async def _fetch(self, url: str) -> str | None:
        if not await respect_robots(url, user_agent=self.user_agent):
            logger.info("wayback: robots blocked %s", url)
            return None
        await throttle_for(url, rps=self.rps)
        try:
            resp = await safe_get(url)
        except (SSRFBlockedError, httpx.HTTPError) as exc:
            logger.warning("wayback: fetch failed for %s: %r", url, exc)
            return None
        if resp.status_code >= HTTP_BAD_REQUEST:
            logger.warning("wayback: HTTP %s for %s", resp.status_code, url)
            return None
        return resp.text

    async def _fetch_json(self, url: str) -> dict[str, Any] | None:  # pyright: ignore[reportExplicitAny]
        if not await respect_robots(url, user_agent=self.user_agent):
            return None
        await throttle_for(url, rps=self.rps)
        try:
            resp = await safe_get(url)
        except (SSRFBlockedError, httpx.HTTPError) as exc:
            logger.warning("wayback: availability fetch failed for %s: %r", url, exc)
            return None
        if resp.status_code >= HTTP_BAD_REQUEST:
            return None
        try:
            payload = cast(
                "dict[str, Any]",  # pyright: ignore[reportExplicitAny]
                resp.json(),
            )
        except (ValueError, TypeError):
            return None
        return payload

    async def enrich(self, entry: RawEntry) -> RawEntry:  # noqa: PLR0911 - guards are semantically distinct; splitting just spreads them.
        """Skip when *any* body is already present.

        The ``markdown_text`` guard matters for HN: without it, a Wayback
        recovery would overwrite HN's own title+story_text with whatever the
        linked URL's snapshot happened to be — quality regression on top of
        the latency cost (archive.org is ~5x slower for deep-linked HN URLs).
        """
        if entry.raw_html is not None and entry.raw_html.strip():
            return entry
        if entry.markdown_text is not None and entry.markdown_text.strip():
            return entry
        if not entry.url:
            return entry
        payload = await self._fetch_json(_availability_url(entry.url))
        snapshot_url = _pick_snapshot_url(payload)
        if not snapshot_url:
            logger.info("wayback: no snapshot for %s", entry.url)
            return entry
        html = await self._fetch(snapshot_url)
        if html is None:
            return entry
        markdown_text = extract_clean(html) or None
        if markdown_text is None:
            # Snapshot HTML didn't yield extractable text — typically a nav-only
            # archived homepage or a paywall stub. Don't half-fill the entry:
            # leaving raw_html set here would short-circuit the next enricher
            # (Tavily's skip-guard treats any non-empty raw_html as "done").
            logger.info(
                "wayback: snapshot for %s extracted to empty text; leaving for next enricher",
                entry.url,
            )
            return entry
        logger.info(
            "wayback: recovered %s (%d bytes html, %d chars text)",
            entry.url,
            len(html),
            len(markdown_text),
        )
        return entry.model_copy(update={"raw_html": html, "markdown_text": markdown_text})
