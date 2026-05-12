"""LLM brainstorm half of the recall subsystem: asks Opus for comparable failures."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx
from lmnr import observe
from pydantic import BaseModel, ValidationError

from slopmortem.llm import OpenRouterCompletionError, render_blocks, to_strict_response_schema
from slopmortem.models import RecallSuggestion, RecallSuggestionList

if TYPE_CHECKING:
    from slopmortem.llm import LLMClient
    from slopmortem.models import Facets, ToolSpec


class PriorCandidateHint(BaseModel):
    """Human-readable hint for the recall prompt's "already covered" block.

    Carries the company name (not the slug id) plus the reranker's rationale
    so Opus's dedup judgment has something to read.
    """

    name: str
    rationale: str


logger = logging.getLogger(__name__)


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
    error; the recall branch is best-effort. The cap is applied here so the
    pipeline never slices a returned list itself.

    ``tools`` is the list Opus may call mid-reasoning (today:
    ``tavily_search``). Pass ``[]`` for training-data-only recall.
    ``recall_max_tavily_calls`` surfaces the per-recall budget in the prompt.
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
    except (httpx.HTTPError, OpenRouterCompletionError) as exc:
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
