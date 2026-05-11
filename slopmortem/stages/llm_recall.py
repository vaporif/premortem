"""LLM-recall fallback: coverage-gap predicate, plus the recall stage call."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from lmnr import observe
from pydantic import BaseModel, ValidationError

from slopmortem.llm import render_blocks, to_strict_response_schema
from slopmortem.models import RecallSuggestion, RecallSuggestionList

if TYPE_CHECKING:
    from slopmortem.llm import LLMClient
    from slopmortem.models import Candidate, Facets, ScoredCandidate, ToolSpec


class PriorCandidateHint(BaseModel):
    """Human-readable hint for the recall prompt's "already covered" block.

    Carries the company name (not the slug id) plus the reranker's rationale
    so Opus's dedup judgment has something to read. The pipeline joins
    ``ScoredCandidate.candidate_id`` against retrieved payloads to build these.
    """

    name: str
    rationale: str


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CoverageGapResult:
    """Coverage-gap counts plus the fire/don't-fire decision.

    The pipeline emits ``qualifying``/``required`` on every query so eval can
    sweep predicate thresholds against historical traces.
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

    Counts candidates that are both high-quality (mean perspective >=
    ``min_similarity_score``) and in-sector (own sector or the catch-all
    ``"other"``). Fewer than ``n_synthesize`` qualifying → gap.

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
    return CoverageGapResult(qualifying=qualifying, required=n_synthesize)


# Drop user pitch, candidate payloads, and rerank rationales from span attrs:
# CLAUDE.md forbids prompt/response bodies in tracing. Candidate id/score still
# show up via the ``stage.llm_rerank`` upstream span.
@observe(name="stage.llm_recall", ignore_inputs=["pitch", "facets", "current_top_n"])
async def llm_recall(  # noqa: PLR0913 - every dependency is required at the call site
    *,
    pitch: str,
    facets: Facets,
    current_top_n: list[PriorCandidateHint],
    llm: LLMClient,
    model: str,
    max_tokens: int,
    cap: int,
    tools: list[ToolSpec],
    recall_max_tavily_calls: int = 0,
) -> list[RecallSuggestion]:
    """Ask the recall LLM (Opus) for comparable failures the corpus missed.

    Returns ``[]`` on transport failure, hard stops, or any wrapper-validation
    error — the recall branch is best-effort. The cap is applied here so the
    pipeline never has to slice a returned list itself.

    ``tools`` is the list of tool specs Opus may call mid-reasoning to discover
    candidates (today: just ``tavily_search`` built via ``recall_tools(config)``).
    Pass ``[]`` to keep recall training-data-only. ``recall_max_tavily_calls``
    is the per-recall budget surfaced to the prompt so Opus knows how many
    searches it may issue before returning a final answer.
    """
    blocks = render_blocks(
        "llm_recall",
        pitch=pitch,
        facets=facets,
        current_top_n=current_top_n,
        cap=cap,
        recall_max_tavily_calls=recall_max_tavily_calls,
    )
    try:
        result = await llm.complete(
            blocks["user"],
            system=blocks["system"],
            model=model,
            tools=tools,
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
    # RuntimeError covers OpenRouter's hard-stop / null-tool-calls / unknown
    # finish-reason failures (slopmortem/llm/openrouter.py raises plain
    # RuntimeError on those). BudgetExceededError extends Exception directly,
    # not RuntimeError, so budget exhaustion still propagates up.
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.warning("llm_recall: call failed: %r", exc)
        return []
    try:
        wrapper = RecallSuggestionList.model_validate_json(result.text)
    except ValidationError as exc:
        logger.info("llm_recall: dropped invalid response: %r", exc)
        return []
    suggestions = wrapper.suggestions[:cap]
    if logger.isEnabledFor(logging.INFO):
        logger.info(
            "llm_recall: received %d suggestion(s): %s",
            len(suggestions),
            [(s.name, s.status) for s in suggestions],
        )
    return suggestions
