"""LLM-recall fallback: coverage-gap predicate, plus the recall stage call."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx
from pydantic import ValidationError

from slopmortem.llm import render_blocks, to_strict_response_schema
from slopmortem.models import RecallSuggestion, RecallSuggestionList

if TYPE_CHECKING:
    from slopmortem.llm import LLMClient
    from slopmortem.models import Candidate, Facets, ScoredCandidate


logger = logging.getLogger(__name__)


def detect_coverage_gap(
    *,
    retrieved: list[Candidate],
    ranked: list[ScoredCandidate],
    pitch_sector: str,
    min_similarity_score: float,
    n_synthesize: int,
) -> bool:
    """Decide whether to fire the LLM-recall fallback.

    Fires when fewer than ``n_synthesize`` candidates are both high-quality
    (mean perspective >= ``min_similarity_score``) and in-sector (own sector
    or the catch-all ``"other"``).

    A ``pitch_sector`` of ``"other"`` short-circuits the in-sector check —
    sector is uninformative there, so quality alone gates the count.
    """
    by_id: dict[str, Candidate] = {c.canonical_id: c for c in retrieved}
    pitch_sector_unknown = pitch_sector == "other"
    qualifying = 0
    for sc in ranked:
        if sc.perspective_scores.mean() < min_similarity_score:
            continue
        if pitch_sector_unknown:
            qualifying += 1
            continue
        cand = by_id.get(sc.candidate_id)
        if cand is None:
            # Rerank should never emit ids absent from retrieve, but if it
            # does we treat it as a miss rather than crashing the gate.
            continue
        if cand.payload.facets.sector in (pitch_sector, "other"):
            qualifying += 1
    return qualifying < n_synthesize


async def llm_recall(  # noqa: PLR0913 - every dependency is required at the call site
    *,
    pitch: str,
    facets: Facets,
    current_top_n: list[ScoredCandidate],
    llm: LLMClient,
    model: str,
    max_tokens: int,
    cap: int,
) -> list[RecallSuggestion]:
    """Ask the recall LLM (Opus) for comparable failures the corpus missed.

    Returns ``[]`` on transport failure, hard stops, or any wrapper-validation
    error — the recall branch is best-effort. The cap is applied here so the
    pipeline never has to slice a returned list itself.
    """
    blocks = render_blocks(
        "llm_recall",
        pitch=pitch,
        facets=facets,
        current_top_n=current_top_n,
        cap=cap,
    )
    try:
        result = await llm.complete(
            blocks["user"],
            system=blocks["system"],
            model=model,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "RecallSuggestionList",
                    "schema": to_strict_response_schema(RecallSuggestionList),
                    "strict": True,
                },
            },
            max_tokens=max_tokens,
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.warning("llm_recall: call failed: %r", exc)
        return []
    try:
        wrapper = RecallSuggestionList.model_validate_json(result.text)
    except ValidationError as exc:
        logger.info("llm_recall: dropped invalid response: %r", exc)
        return []
    return wrapper.suggestions[:cap]
