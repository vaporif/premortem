"""Wayback enricher: recovers content for entries whose live URL is dead.

Hits the availability API; on a snapshot, fetches it into ``raw_html`` +
``markdown_text``. No-op when ``raw_html`` is already populated.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote_plus

import anyio
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

# Wayback regularly tarpits clients (slow connects, RSTs, 429/503 bursts) when
# under load. Drop-on-error here means real dead-startup URLs disappear from
# the recall set on a transient signal — bounded retry recovers them. Terminal
# errors (SSRF block, 404, other 4xx/5xx) still drop on the first attempt.
_TRANSIENT_HTTPX_EXC: tuple[type[Exception], ...] = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)
_TRANSIENT_STATUSES: frozenset[int] = frozenset({429, 503})
# Three attempts total (initial + two retries). Backoff schedule applies to
# the wait *before* each retry; total worst-case wait per URL is 2.0s.
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (0.5, 1.5)


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    raw = cast("str | None", resp.headers.get("retry-after"))
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        # HTTP-date format also valid per RFC 7231 but we don't see it from
        # IA in practice; fall back to default backoff.
        return None


async def _safe_get_with_retry(url: str) -> httpx.Response | None:  # noqa: PLR0911 - each return is a distinct exit (terminal exc, exhausted retry, terminal status, success); flattening obscures the rate-limit logic.
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            resp = await safe_get(url)
        except SSRFBlockedError as exc:
            logger.warning("wayback: ssrf-blocked %s: %r", url, exc)
            return None
        except _TRANSIENT_HTTPX_EXC as exc:
            if attempt + 1 >= _RETRY_ATTEMPTS:
                logger.warning(
                    "wayback: transient error after %d attempts for %s: %r",
                    _RETRY_ATTEMPTS,
                    url,
                    exc,
                )
                return None
            await anyio.sleep(_RETRY_BACKOFF_SECONDS[attempt])
            continue
        except httpx.HTTPError as exc:
            logger.warning("wayback: fetch failed for %s: %r", url, exc)
            return None
        if resp.status_code in _TRANSIENT_STATUSES:
            if attempt + 1 >= _RETRY_ATTEMPTS:
                logger.warning(
                    "wayback: HTTP %s after %d attempts for %s",
                    resp.status_code,
                    _RETRY_ATTEMPTS,
                    url,
                )
                return None
            wait = _retry_after_seconds(resp) or _RETRY_BACKOFF_SECONDS[attempt]
            await anyio.sleep(wait)
            continue
        if resp.status_code >= HTTP_BAD_REQUEST:
            logger.warning("wayback: HTTP %s for %s", resp.status_code, url)
            return None
        return resp
    return None


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
        resp = await _safe_get_with_retry(url)
        return resp.text if resp is not None else None

    async def _fetch_json(self, url: str) -> dict[str, Any] | None:  # pyright: ignore[reportExplicitAny]
        if not await respect_robots(url, user_agent=self.user_agent):
            return None
        await throttle_for(url, rps=self.rps)
        resp = await _safe_get_with_retry(url)
        if resp is None:
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
