"""Slop-gate routing.

Entries scoring above ``config.slop_threshold`` route to `_quarantine`
and get no Qdrant point and no merge-journal row. ``--reclassify`` is the only
path back.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from typing import TYPE_CHECKING, Final

from anyio import to_thread

from slopmortem.corpus import safe_path
from slopmortem.corpus.sources._names import (
    SOURCE_CRUNCHBASE_CSV,
    SOURCE_CURATED,
    SOURCE_LLM_RECALL,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from slopmortem.config import Config
    from slopmortem.corpus import MergeJournal
    from slopmortem.ingest._ports import SlopClassifier
    from slopmortem.models import RawEntry

__all__ = [
    "_PRE_VETTED_SOURCES",
    "_effective_slop_threshold",
    "_quarantine",
    "classify_one",
]

logger = logging.getLogger(__name__)

# Pre-filtered to "confirmed dead company" upstream: curated YAML is human-
# reviewed, crunchbase_csv is filtered to status=closed. Running the LLM on
# these wastes spend and misclassifies (Wayback'd Crunchbase homepages are
# pre-death marketing copy, not death narratives).
_PRE_VETTED_SOURCES: Final[frozenset[str]] = frozenset({SOURCE_CURATED, SOURCE_CRUNCHBASE_CSV})


def _content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _effective_slop_threshold(entry: RawEntry, config: Config) -> float:
    """Per-source threshold override for llm_recall; global default otherwise."""
    if entry.source == SOURCE_LLM_RECALL and config.recall_slop_threshold is not None:
        return config.recall_slop_threshold
    return config.slop_threshold


async def classify_one(
    *,
    entry: RawEntry,
    body: str,
    slop_classifier: SlopClassifier,
    pre_vetted_sources: frozenset[str] = _PRE_VETTED_SOURCES,
    on_error: Callable[[Exception], None],
) -> float:
    """Score *body* with the slop classifier.

    Returns 0.0 for pre-vetted sources or on classifier failure (failures are
    reported via *on_error* and never abort the run).
    """
    if entry.source in pre_vetted_sources:
        return 0.0
    try:
        return await slop_classifier.score(body)
    except Exception as exc:  # noqa: BLE001 - defensive: never abort on classifier failure.
        logger.warning("ingest: slop classifier failed: %r", exc)
        on_error(exc)
        return 0.0


async def _quarantine(
    *,
    journal: MergeJournal,
    entry: RawEntry,
    body: str,
    slop_score: float,
    post_mortems_root: Path,
) -> None:
    sha = _content_sha256(body)
    path = safe_path(post_mortems_root, kind="quarantine", content_sha256=sha)

    def _write_sync() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f"{path.suffix}.{secrets.token_hex(8)}.tmp")
        try:
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(path)
        finally:
            if tmp.exists():
                tmp.unlink()

    await to_thread.run_sync(_write_sync)
    await journal.write_quarantine(
        content_sha256=sha,
        source=entry.source,
        source_id=entry.source_id,
        reason="slop_classifier",
        slop_score=slop_score,
    )
