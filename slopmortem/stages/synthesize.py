"""Synthesize stage: one candidate → one ``synthesize`` call.

``synthesize_all`` uses the cache-warm pattern (first call alone, then
`gather_resilient` on the rest) so one candidate's failure can't cancel
the others. The tool-call loop, ``<untrusted_document>`` wrapping, and 5-turn
bound live in ``slopmortem/llm/openrouter.py``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from lmnr import Laminar

from slopmortem.concurrency import gather_resilient
from slopmortem.llm import (
    prompt_template_sha,
    render_prompt,
    synthesis_tools,
    to_strict_response_schema,
)
from slopmortem.models import LLMSynthesis, Synthesis
from slopmortem.tracing import SpanEvent

if TYPE_CHECKING:
    from collections.abc import Callable

    from slopmortem.config import Config
    from slopmortem.llm import LLMClient
    from slopmortem.models import Candidate, InputContext


logger = logging.getLogger(__name__)


def synthesize_prompt_kwargs(candidate: Candidate, *, pitch: str) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
    """Build the kwargs dict for the ``synthesize`` prompt template.

    Shared by the production stage and tests so both stay in lockstep when
    the template's variable list changes.
    """
    payload = candidate.payload
    facets = payload.facets
    return {
        "pitch": pitch,
        "candidate_id": candidate.canonical_id,
        "candidate_name": payload.name,
        "candidate_body": payload.body,
        # The j2 template renders ``source`` directly (no ``or "unknown"``
        # fallback), so coerce here to keep legacy qdrant rows from emitting
        # ``None`` into the prompt.
        "source": payload.source or "unknown",
        "founding_date": payload.founding_date.isoformat() if payload.founding_date else None,
        "failure_date": payload.failure_date.isoformat() if payload.failure_date else None,
        "sub_sector": facets.sub_sector,
        "customer_type": facets.customer_type,
        "geography": facets.geography,
        "monetization": facets.monetization,
        "product_type": facets.product_type,
        "price_point": facets.price_point,
    }


# Literal contract from synthesize.j2: the LLM must put this exact string in
# ``where_diverged`` when it detects an injection attempt.
_INJECTION_MARKER = "prompt_injection_attempted"


def _emit_event(event: SpanEvent) -> None:
    if Laminar.is_initialized():
        Laminar.event(name=str(event))


async def synthesize(  # noqa: PLR0913 — every dependency is required at the call site
    candidate: Candidate,
    ctx: InputContext,
    llm: LLMClient,
    config: Config,
    *,
    model: str | None = None,
    max_tokens: int | None = None,
) -> Synthesis:
    """Synthesize one candidate against *ctx*.

    ``sources`` is passed through from ``candidate.payload.sources`` rather
    than asked of the LLM — the LLM never sees provenance URLs, so asking
    produced empty or hallucinated lists.

    When ``where_diverged == "prompt_injection_attempted"`` (the
    `_INJECTION_MARKER`), fires `SpanEvent.PROMPT_INJECTION_ATTEMPTED`
    on the active Laminar span.
    """
    prompt = render_prompt(
        "synthesize",
        **synthesize_prompt_kwargs(candidate, pitch=ctx.description),
    )
    result = await llm.complete(
        prompt,
        model=model,
        cache=True,
        tools=synthesis_tools(config),
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "LLMSynthesis",
                "schema": to_strict_response_schema(LLMSynthesis),
                "strict": True,
            },
        },
        extra_body={
            "provider": {"require_parameters": True},
            "prompt_template_sha": prompt_template_sha("synthesize"),
        },
        max_tokens=max_tokens,
    )
    llm_parsed = LLMSynthesis.model_validate_json(result.text)

    injection_detected = llm_parsed.where_diverged.strip() == _INJECTION_MARKER
    if injection_detected:
        _emit_event(SpanEvent.PROMPT_INJECTION_ATTEMPTED)

    return Synthesis.from_llm(
        llm_parsed,
        founding_date=candidate.payload.founding_date,
        failure_date=candidate.payload.failure_date,
        sources=candidate.payload.sources,
        injection_detected=injection_detected,
        source=candidate.payload.source,
    )


async def synthesize_all(  # noqa: PLR0913 — mirrors ``synthesize`` for the fan-out wrapper
    candidates: list[Candidate],
    ctx: InputContext,
    llm: LLMClient,
    config: Config,
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    on_candidate_done: Callable[[BaseException | None], None] | None = None,
) -> list[Synthesis | BaseException]:
    """Fan out `synthesize` across *candidates* with cache-warm + resilient gather.

    First call runs alone so the prompt cache is populated before parallel
    calls race to write it. The rest use `gather_resilient` so a single
    failure doesn't cancel siblings — exceptions are returned in-list.
    """
    if not candidates:
        return []

    async def _run_one(candidate: Candidate) -> Synthesis | BaseException:
        try:
            result: Synthesis | BaseException = await synthesize(
                candidate, ctx, llm, config, model=model, max_tokens=max_tokens
            )
        except Exception as exc:  # noqa: BLE001 — record failure as a list entry
            result = exc
        if on_candidate_done is not None:
            on_candidate_done(result if isinstance(result, BaseException) else None)
        return result

    first = await _run_one(candidates[0])
    rest_results = await gather_resilient(*(_run_one(c) for c in candidates[1:]))
    return [first, *rest_results]


def drop_below_min_similarity(
    syntheses: list[Synthesis], *, min_similarity: float
) -> tuple[list[Synthesis], int]:
    """Drop syntheses whose own similarity mean falls below *min_similarity*.

    Synthesis sometimes re-scores a candidate lower than rerank did, so a
    row that cleared the rerank-side filter can come back below the bar.
    Returns ``(kept, dropped_count)`` so the caller can record
    ``PipelineMeta.filtered_post_synth`` without recomputing.
    """
    kept = [s for s in syntheses if s.similarity.mean() >= min_similarity]
    dropped = len(syntheses) - len(kept)
    if dropped > 0:
        logger.info(
            "min_similarity dropped %d/%d candidates post-synth (threshold=%.2f)",
            dropped,
            len(syntheses),
            min_similarity,
        )
    return kept, dropped
