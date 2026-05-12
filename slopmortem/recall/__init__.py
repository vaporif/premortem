"""LLM-recall subsystem: find similar dead startups and decide which ones are real.

Public surface: ``recall(pitch, *, facets=None, prior_hints=None, deps, config)`` —
one async function, four runtime deps, one config record. Persistence,
re-retrieval, and the coverage-gap predicate live pipeline-side; this package
is read-only with respect to the corpus.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lmnr import Laminar

from slopmortem.corpus.sources import WaybackEnricher
from slopmortem.recall._brainstorm import PriorCandidateHint as PriorCandidateHint
from slopmortem.recall._brainstorm import llm_recall as _llm_recall
from slopmortem.recall._models import RecallConfig as RecallConfig
from slopmortem.recall._models import RecallDeps as RecallDeps
from slopmortem.recall._verify import DeathnessConfig as DeathnessConfig
from slopmortem.recall._verify import VerificationTier as VerificationTier
from slopmortem.recall._verify import VerifiedEntry as VerifiedEntry
from slopmortem.recall._verify import recall_source_id as recall_source_id
from slopmortem.recall._verify import verify_all as _verify_all
from slopmortem.recall.fake import FakeRecaller as FakeRecaller
from slopmortem.tracing import SpanEvent

if TYPE_CHECKING:
    from slopmortem.models import Facets


__all__ = [
    "DeathnessConfig",
    "FakeRecaller",
    "PriorCandidateHint",
    "RecallConfig",
    "RecallDeps",
    "VerificationTier",
    "VerifiedEntry",
    "recall",
    "recall_source_id",
]


async def recall(
    pitch: str,
    *,
    facets: Facets | None = None,
    prior_hints: list[PriorCandidateHint] | None = None,
    deps: RecallDeps,
    config: RecallConfig,
) -> list[VerifiedEntry]:
    """Find similar failed startups for ``pitch`` and decide which ones are real.

    ``facets=None`` extracts facets internally via Haiku (one extra call per
    fire on that path). Pipeline callers pass the value they already
    extracted upstream and skip the extra call. ``prior_hints=None`` is
    equivalent to ``[]`` — the prompt template renders its "(none — corpus
    returned no in-vertical matches)" branch.

    Returns ``[]`` when brainstorm produced no suggestions, every L0-L5
    drop fired, or transport failures isolated all candidates. The function
    never raises for per-suggestion failures.
    """
    if facets is None:
        # Lazy: pull stages.facet_extract only when the caller hasn't supplied
        # facets. Pipeline always passes pre-extracted facets and skips this.
        from slopmortem.stages.facet_extract import extract_facets  # noqa: PLC0415

        facets = await extract_facets(
            pitch,
            deps.llm,
            model=config.model_facet,
            max_tokens=config.max_tokens_facet,
        )

    suggestions = await _llm_recall(
        pitch=pitch,
        facets=facets,
        current_top_n=prior_hints if prior_hints is not None else [],
        llm=deps.llm,
        model=config.model_recall,
        max_tokens=config.max_tokens_recall,
        cap=config.suggestion_cap,
        tools=config.tools,
        recall_max_tavily_calls=config.max_tavily_calls,
    )
    if Laminar.is_initialized():
        Laminar.event(
            name=str(SpanEvent.RECALL_SUGGESTIONS_RECEIVED),
            attributes={"count": len(suggestions)},
        )
    if not suggestions:
        return []

    wayback = deps.wayback if deps.wayback is not None else WaybackEnricher()
    return await _verify_all(
        suggestions,
        wayback=wayback,
        llm=deps.llm,
        tavily_search=deps.tavily_search,
        extract=deps.extract,
        tavily_recall_max_results=config.tavily_max_results,
        deathness=config.deathness,
    )
