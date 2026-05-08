"""LLM-recall fallback: coverage-gap predicate, plus the recall stage call."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slopmortem.models import Candidate, ScoredCandidate


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
