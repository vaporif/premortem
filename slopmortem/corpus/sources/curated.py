"""Curated source: YAML of hand-vetted post-mortem URLs → ``RawEntry``.

Curated entries are user-vouched, so no host blocklist applies — the
resolver's tier-2 demotion handles multi-tenant hosts.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import httpx
import yaml

from slopmortem.corpus._extract import extract_clean
from slopmortem.corpus.sources._names import SOURCE_CURATED
from slopmortem.corpus.sources._throttle import (
    HTTP_BAD_REQUEST,
    USER_AGENT,
    respect_robots,
    throttle_for,
)
from slopmortem.http import SSRFBlockedError, safe_get
from slopmortem.models import RawEntry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

logger = logging.getLogger(__name__)


class CuratedSource:
    """[Source] YAML loader that fetches curated post-mortem URLs."""

    def __init__(
        self,
        yaml_path: Path,
        *,
        user_agent: str = USER_AGENT,
        rps: float = 1.0,
    ) -> None:
        self.yaml_path = yaml_path
        self.user_agent = user_agent
        self.rps = rps

    def _load_rows(self) -> list[dict[str, object]]:
        with self.yaml_path.open("r", encoding="utf-8") as fh:
            data: object = yaml.safe_load(fh) or []
        if not isinstance(data, list):
            msg = f"curated YAML at {self.yaml_path} must be a list"
            raise TypeError(msg)
        rows = cast("list[object]", data)
        return [r for r in rows if isinstance(r, dict)]

    async def fetch(self) -> AsyncIterator[RawEntry]:
        rows = self._load_rows()
        for row in rows:
            url = row.get("url")
            if not isinstance(url, str) or not url:
                logger.debug("curated: skipping row without url: %r", row)
                continue
            if not await respect_robots(url, user_agent=self.user_agent):
                logger.info("curated: skipping robots-disallowed url %s", url)
                continue
            await throttle_for(url, rps=self.rps)
            try:
                resp = await safe_get(url)
            except (SSRFBlockedError, httpx.HTTPError) as exc:
                logger.warning("curated: fetch failed for %s: %r", url, exc)
                continue
            if resp.status_code >= HTTP_BAD_REQUEST:
                logger.warning("curated: HTTP %s for %s", resp.status_code, url)
                continue
            text = extract_clean(resp.text)
            if not text:
                logger.info("curated: extracted text below length floor for %s", url)
                continue
            logger.info(
                "curated: ok %s (%d bytes html, %d chars text)", url, len(resp.text), len(text)
            )
            startup_name = row.get("startup_name")
            source_id = str(startup_name) if isinstance(startup_name, str) and startup_name else url
            yield RawEntry(
                source=SOURCE_CURATED,
                source_id=source_id,
                url=url,
                raw_html=resp.text,
                markdown_text=text,
                fetched_at=datetime.now(UTC),
            )
