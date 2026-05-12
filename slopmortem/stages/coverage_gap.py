"""Coverage-gap predicate: decides whether the recall branch should fire.

Pipeline reads ``compute_coverage_gap`` to gate the recall call. The
``RECALL_GAP_SCORE`` and ``RECALL_GAP_SCORE_AFTER`` events that ride
on its output stay in ``pipeline.py``; this module is pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slopmortem.models import Candidate, ScoredCandidate


@dataclass(frozen=True)
class CoverageGapResult:
    """Coverage-gap counts plus the fire/don't-fire decision.

    ``qualifying``/``required`` are emitted on every query so eval can sweep
    predicate thresholds against historical traces.
    """

    qualifying: int
    required: int

    @property
    def gap(self) -> bool:
        return self.qualifying < self.required


def compute_coverage_gap(
    *,
    retrieved: list[Candidate],
    ranked: list[ScoredCandidate],
    pitch_sector: str,
    min_similarity_score: float,
    n_synthesize: int,
) -> CoverageGapResult:
    """Score the retrieve+rerank result against the LLM-recall predicate.

    Counts candidates that are both high-quality (mean perspective ≥
    ``min_similarity_score``) and in-sector (own sector or ``"other"``).
    Fewer than ``n_synthesize`` qualifying → gap.

    ``pitch_sector == "other"`` short-circuits the in-sector check: sector
    is uninformative there, so quality alone gates the count.
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
            continue
        if cand.payload.facets.sector in (pitch_sector, "other"):
            qualifying += 1
    return CoverageGapResult(qualifying=qualifying, required=n_synthesize)
