"""Warm-cache pattern for ingest.

First entry runs alone so the prompt prefix lands in the OpenRouter cache,
the rest fan out concurrently. Preserves the CACHE_READ_RATIO_LOW invariant
(see CLAUDE.md).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from slopmortem.llm import prompt_template_sha, render_prompt
from slopmortem.tracing import SpanEvent

if TYPE_CHECKING:
    from collections.abc import Sequence

    from slopmortem.llm import LLMClient

__all__ = ["cache_read_ratio_event", "cache_warm"]

logger = logging.getLogger(__name__)


@runtime_checkable
class _CacheRatioResult(Protocol):
    cache_read: int
    cache_creation: int


async def cache_warm(
    *,
    llm: LLMClient,
    model: str | None,
    seed_text: str,
    max_tokens: int | None = None,
) -> tuple[bool, int, list[str]]:
    """Serial summarize call to warm the prompt cache.

    ``warmed`` is True iff ``cache_creation_tokens > 0`` (cache actually got
    written).
    """
    span: list[str] = []
    try:
        prompt = render_prompt("summarize", body=seed_text, source_id="warm")
        res = await llm.complete(
            prompt,
            model=model,
            cache=True,
            extra_body={"prompt_template_sha": prompt_template_sha("summarize")},
            max_tokens=max_tokens,
        )
        creation = res.cache_creation_tokens or 0
        if creation == 0:
            span.append(SpanEvent.CACHE_WARM_FAILED.value)
            return False, 0, span
    except Exception as exc:  # noqa: BLE001 - warming is best-effort
        logger.warning("ingest: cache warm failed: %r", exc)
        span.append(SpanEvent.CACHE_WARM_FAILED.value)
        return False, 0, span
    else:
        return True, creation, span


def cache_read_ratio_event(
    fanout: Sequence[_CacheRatioResult],
    *,
    threshold: float,
    probe_n: int,
) -> str | None:
    """Emit a span event if the probe's cache-read ratio falls under *threshold*.

    Probes only the first *probe_n* results; returns ``None`` when no tokens
    flowed or the probe is empty.
    """
    probe = list(fanout)[:probe_n]
    if not probe:
        return None
    total_read = sum(r.cache_read for r in probe)
    total_creation = sum(r.cache_creation for r in probe)
    denom = total_read + total_creation
    if denom <= 0:
        return None
    ratio = total_read / denom
    if ratio < threshold:
        return SpanEvent.CACHE_READ_RATIO_LOW.value
    return None
