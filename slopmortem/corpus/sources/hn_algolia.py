"""HN Algolia phrase-driven discovery source.

YAML-driven, multi-phrase, year-window-sliced, chronologically-paginated.
For each phrase, the source iterates one (epoch_start, epoch_end) window
per calendar year between ``date_from`` and ``date_to``, and within each
window paginates ``/search_by_date`` (NOT ``/search`` — relevance ranking
buries the long tail) up to ``pages_per_window``. Pagination terminates
on an empty page within a window. Dedup is by HN ``objectID`` across all
phrases and windows.

Year-window slicing is what makes the long tail reachable: a flat
chronological pagination from "most recent" only covers ~6 weeks per page,
so 20 pages = ~Nov 2023 onward — never reaching 2017's Mattermark
obituary. Per-year windows give every year its own bounded budget.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast
from urllib.parse import quote_plus

import yaml

from slopmortem.corpus.sources._names import SOURCE_HN_ALGOLIA
from slopmortem.corpus.sources._throttle import (
    HTTP_BAD_REQUEST,
    USER_AGENT,
    respect_robots,
    throttle_for,
)
from slopmortem.http import safe_get
from slopmortem.models import RawEntry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

logger = logging.getLogger(__name__)

ENDPOINT: Final = "https://hn.algolia.com/api/v1/search_by_date"
DEFAULT_PAGES_PER_WINDOW: Final = 3
DEFAULT_HITS_PER_PAGE: Final = 30
DEFAULT_LOOKBACK_YEARS: Final = 11  # fallback lookback when date_from is unset


def _epoch(date_str: str) -> int | None:
    """Parse YYYY-MM-DD to UTC epoch seconds; return None for empty string."""
    if not date_str:
        return None
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


def _coerce_int(name: str, raw: object, default: int) -> int:
    # ``bool`` is a subclass of ``int``, so check it first — otherwise
    # ``pages_per_window: true`` silently becomes ``1``.
    if isinstance(raw, bool):
        msg = f"hn_queries.yaml: 'defaults.{name}' must be an integer, got bool"
        raise TypeError(msg)
    if isinstance(raw, int):
        return raw
    if raw is None:
        return default
    msg = f"hn_queries.yaml: 'defaults.{name}' must be an integer, got {type(raw).__name__}"
    raise TypeError(msg)


def _year_windows(
    date_from_epoch: int | None,
    date_to_epoch: int | None,
) -> list[tuple[int, int]]:
    """Yield (epoch_start, epoch_end) per calendar year between bounds (inclusive).

    - If ``date_to_epoch`` is unset, defaults to "now".
    - If ``date_from_epoch`` is unset, defaults to ``DEFAULT_LOOKBACK_YEARS``
      before ``date_to``.
    - Each window is clamped to its calendar year boundary, but the first
      and last windows are clamped to the actual ``date_from``/``date_to``.
    """
    end_dt = (
        datetime.fromtimestamp(date_to_epoch, tz=UTC)
        if date_to_epoch is not None
        else datetime.now(UTC)
    )
    start_dt = (
        datetime.fromtimestamp(date_from_epoch, tz=UTC)
        if date_from_epoch is not None
        else datetime(end_dt.year - DEFAULT_LOOKBACK_YEARS, 1, 1, tzinfo=UTC)
    )

    windows: list[tuple[int, int]] = []
    year = start_dt.year
    while year <= end_dt.year:
        year_start = datetime(year, 1, 1, tzinfo=UTC)
        year_end = datetime(year, 12, 31, 23, 59, 59, tzinfo=UTC)
        win_start = max(start_dt, year_start)
        win_end = min(end_dt, year_end)
        if win_start <= win_end:
            windows.append((int(win_start.timestamp()), int(win_end.timestamp())))
        year += 1
    return windows


class HNAlgoliaSource:
    """[Source] Phrase-driven HN obituary discovery via /search_by_date.

    Sliced into one query per calendar year per phrase.
    """

    def __init__(
        self,
        *,
        queries_yaml_path: Path,
        user_agent: str = USER_AGENT,
        rps: float = 5.0,
    ) -> None:
        cfg_obj = cast(
            "object",
            yaml.safe_load(queries_yaml_path.read_text(encoding="utf-8")),
        )
        if not isinstance(cfg_obj, dict):
            msg = f"hn_queries.yaml: expected mapping, got {type(cfg_obj).__name__}"
            raise TypeError(msg)
        cfg = cast("dict[str, object]", cfg_obj)
        defaults_obj: object = cfg.get("defaults") or {}
        if not isinstance(defaults_obj, dict):
            msg = "hn_queries.yaml: 'defaults' must be a mapping"
            raise TypeError(msg)
        defaults = cast("dict[str, object]", defaults_obj)
        phrases_obj: object = cfg.get("phrases") or []
        if not isinstance(phrases_obj, list):
            msg = "hn_queries.yaml: 'phrases' must be a list"
            raise TypeError(msg)
        phrases_list = cast("list[object]", phrases_obj)
        self.phrases: list[str] = [p for p in phrases_list if isinstance(p, str) and p.strip()]
        if not self.phrases:
            msg = "hn_queries.yaml: 'phrases' must contain at least one non-empty entry"
            raise ValueError(msg)

        self.date_from_epoch: int | None = _epoch(str(defaults.get("date_from") or ""))
        self.date_to_epoch: int | None = _epoch(str(defaults.get("date_to") or ""))
        self.pages_per_window: int = _coerce_int(
            "pages_per_window",
            defaults.get("pages_per_window"),
            DEFAULT_PAGES_PER_WINDOW,
        )
        self.hits_per_page: int = _coerce_int(
            "hits_per_page",
            defaults.get("hits_per_page"),
            DEFAULT_HITS_PER_PAGE,
        )
        self.user_agent = user_agent
        self.rps = rps

        self._windows: list[tuple[int, int]] = _year_windows(
            self.date_from_epoch, self.date_to_epoch
        )

    def _build_url(self, phrase: str, page: int, win_start: int, win_end: int) -> str:
        # Literal double-quotes turn this into a phrase match. Bare tokens
        # AND-search across title/comments/body and explode recall.
        quoted_phrase = f'"{phrase}"'
        # ``>=`` keeps stories posted exactly at the year boundary.
        numeric = f"created_at_i>={win_start},created_at_i<{win_end}"
        return (
            f"{ENDPOINT}?query={quote_plus(quoted_phrase)}&tags=story"
            f"&page={page}&hitsPerPage={self.hits_per_page}"
            f"&numericFilters={quote_plus(numeric)}"
        )

    @staticmethod
    def _hit_to_entry(hit: dict[str, object]) -> RawEntry | None:
        object_id = hit.get("objectID")
        url = hit.get("url")
        title = hit.get("title") or ""
        created_at = hit.get("created_at") or ""
        points = hit.get("points")
        num_comments = hit.get("num_comments")
        if not isinstance(object_id, str) or not object_id:
            return None
        if not isinstance(url, str) or not url:
            # Ask-HN / Show-HN self-posts have no external URL; the Tavily
            # enricher would have nothing to fetch.
            return None
        title_str = title if isinstance(title, str) else str(title)
        markdown = (
            f"# {title_str}\n\n"
            f"hn_object_id: {object_id}\n"
            f"created_at: {created_at}\n"
            f"points: {points}\n"
            f"num_comments: {num_comments}\n"
        ).strip()
        return RawEntry(
            source=SOURCE_HN_ALGOLIA,
            source_id=object_id,
            url=url,
            raw_html=None,
            markdown_text=markdown,
            fetched_at=datetime.now(UTC),
        )

    async def _fetch_page(
        self, phrase: str, page: int, win_start: int, win_end: int
    ) -> list[dict[str, object]] | None:
        url = self._build_url(phrase, page, win_start, win_end)
        # ``fetch`` already cleared robots once per host; skip the recheck.
        await throttle_for(url, rps=self.rps)
        resp = await safe_get(url)
        if resp.status_code >= HTTP_BAD_REQUEST:
            logger.warning(
                "hn_algolia: HTTP %s for phrase=%r window=(%d,%d) page=%d",
                resp.status_code,
                phrase,
                win_start,
                win_end,
                page,
            )
            return None
        payload = cast("object", resp.json())
        if not isinstance(payload, dict):
            return None
        payload_dict = cast("dict[str, object]", payload)
        hits_obj: object = payload_dict.get("hits") or []
        if not isinstance(hits_obj, list):
            return None
        hits_list = cast("list[object]", hits_obj)
        return [cast("dict[str, object]", h) for h in hits_list if isinstance(h, dict)]

    async def fetch(self) -> AsyncIterator[RawEntry]:
        # Robots is checked per-host. One check up front skips ~288 redundant
        # rechecks across every (phrase, year, page) and makes a blocked
        # endpoint short-circuit the whole run.
        if not await respect_robots(ENDPOINT, user_agent=self.user_agent):
            logger.info("hn_algolia: robots blocked %s; skipping source", ENDPOINT)
            return
        seen: set[str] = set()
        for phrase in self.phrases:
            for win_start, win_end in self._windows:
                for page in range(self.pages_per_window):
                    hits = await self._fetch_page(phrase, page, win_start, win_end)
                    if hits is None or not hits:
                        # None: HTTP error or bad shape (logged upstream).
                        # Empty: window exhausted. Either way, next window.
                        break
                    for hit in hits:
                        entry = self._hit_to_entry(hit)
                        if entry is None or entry.source_id in seen:
                            continue
                        seen.add(entry.source_id)
                        yield entry
