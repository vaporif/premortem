"""Pure ingest helpers. May not import from sibling ingest submodules except `_ports`."""

from __future__ import annotations

import hashlib
import logging
from datetime import date
from typing import TYPE_CHECKING, Final, Literal

from slopmortem.corpus import extract_clean
from slopmortem.corpus.sources._names import (
    SOURCE_CRUNCHBASE_CSV,
    SOURCE_CURATED,
    SOURCE_HN_ALGOLIA,
    SOURCE_LLM_RECALL,
    SOURCE_TAVILY_NEWS,
)
from slopmortem.ingest._ports import IngestPhase, NullProgress
from slopmortem.models import CandidatePayload
from slopmortem.tracing import SpanEvent

if TYPE_CHECKING:
    from collections.abc import Sequence

    from slopmortem.corpus.sources import Enricher, Source
    from slopmortem.ingest._ports import IngestProgress
    from slopmortem.models import Facets, RawEntry

__all__ = [
    "_build_payload",
    "_enrich_pipeline",
    "_entry_summary_text",
    "_gather_entries",
    "_reliability_for",
    "_skip_key",
    "_text_id_for",
    "_truncate_to_tokens",
]

logger = logging.getLogger(__name__)

# merge_text orders sections by this. Curated > HN > Crunchbase > everything else.
# llm_recall sits below Tavily news because the verifier guarantees one
# corroborating citation but the body itself can be either an article or a
# Wayback snapshot — both weaker primary sources than a curated obit.
_RELIABILITY_RANK: Final[dict[str, int]] = {
    SOURCE_CURATED: 0,
    SOURCE_HN_ALGOLIA: 1,
    SOURCE_CRUNCHBASE_CSV: 2,
    SOURCE_TAVILY_NEWS: 4,
    SOURCE_LLM_RECALL: 6,
}


def _text_id_for(canonical_id: str) -> str:
    return hashlib.sha256(canonical_id.encode("utf-8")).hexdigest()[:16]


def _reliability_for(source: str) -> int:
    return _RELIABILITY_RANK.get(source, 9)


def _skip_key(  # noqa: PLR0913 - the contract tuple is wide
    *,
    content_hash: str,
    facet_sha: str,
    summarize_sha: str,
    haiku_model_id: str,
    embed_model_id: str,
    chunk_strategy: str,
    taxonomy_version: str,
    reliability_rank_version: str,
) -> str:
    raw = (
        f"{content_hash}|{facet_sha}|{summarize_sha}|"
        f"{haiku_model_id}|{embed_model_id}|{chunk_strategy}|"
        f"{taxonomy_version}|{reliability_rank_version}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate *text* to *max_tokens* via cl100k_base.

    Anthropic's tokenizer isn't published; cl100k_base agrees within ~10%
    on English prose, well inside the truncation budget's headroom.
    """
    if max_tokens <= 0:
        return text
    import tiktoken  # noqa: PLC0415 - heavy dep; lazy

    enc = tiktoken.get_encoding("cl100k_base")
    tokens = enc.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return enc.decode(tokens[:max_tokens])


def _entry_summary_text(entry: RawEntry, *, max_tokens: int) -> str:
    """Return entry body text, clipped to *max_tokens*.

    Clipped to bound LLM input cost on long-tail articles (Wikipedia entries
    can run 60KB+ after trafilatura).
    """
    if entry.markdown_text:
        return _truncate_to_tokens(entry.markdown_text, max_tokens)
    if entry.raw_html:
        return _truncate_to_tokens(extract_clean(entry.raw_html), max_tokens)
    return ""


async def _enrich_pipeline(entry: RawEntry, enrichers: Sequence[Enricher]) -> RawEntry:
    cur = entry
    for e in enrichers:
        cur = await e.enrich(cur)
    return cur


async def _gather_entries(
    sources: Sequence[Source],
    *,
    span_events: list[str],
    limit: int | None = None,
    progress: IngestProgress | None = None,
) -> tuple[list[RawEntry], int]:
    """Per-source failures are logged and counted, never abort the run.

    ``--limit`` is a real fast-path knob, not a post-gather slice: sources
    beyond the cap aren't started, and in-progress sources break out of their
    async iterator on the next yield.
    """
    out: list[RawEntry] = []
    failures = 0
    bar = progress or NullProgress()
    for src in sources:
        if limit is not None and len(out) >= limit:
            break
        try:
            iterable = src.fetch()
            async for entry in iterable:
                out.append(entry)
                bar.advance_phase(IngestPhase.GATHER)
                if limit is not None and len(out) >= limit:
                    break
        except Exception as exc:  # noqa: BLE001 - never abort the run on a per-source failure.
            logger.warning(
                "ingest: source %r failed: %r",
                type(src).__name__,
                exc,
            )
            span_events.append(SpanEvent.SOURCE_FETCH_FAILED.value)
            failures += 1
    return out, failures


def _build_payload(  # noqa: PLR0913 - payload assembly takes every store-time field
    *,
    facets: Facets,
    summary: str,
    body: str,
    slop_score: float,
    sources_seen: list[str],
    provenance_id: str,
    text_id: str,
    name: str,
    provenance: str,
    synthesized: bool = False,
    verification_tier: Literal["wayback_anchored", "evidence_only"] | None = None,
) -> CandidatePayload:
    founding_year = facets.founding_year
    failure_year = facets.failure_year
    if synthesized:
        provenance_value: Literal["curated_real", "scraped", "synthesized"] = "synthesized"
    elif provenance == SOURCE_CURATED:
        provenance_value = "curated_real"
    else:
        provenance_value = "scraped"
    return CandidatePayload(
        name=name,
        summary=summary,
        body=body,
        facets=facets,
        founding_date=None if founding_year is None else date(founding_year, 1, 1),
        failure_date=None if failure_year is None else date(failure_year, 1, 1),
        founding_date_unknown=founding_year is None,
        failure_date_unknown=failure_year is None,
        provenance=provenance_value,
        slop_score=slop_score,
        sources=sources_seen,
        provenance_id=provenance_id,
        text_id=text_id,
        verification_tier=verification_tier,
        # Forwarded raw RawEntry.source so synthesize/render can branch on
        # llm_recall without re-deriving it from provenance_id.
        source=provenance,
    )
