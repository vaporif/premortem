"""LLM-driven pitch filler.

Last-resort enricher for entries whose body is still empty after the cheap
fetch chain (Tavily-extract, Wayback). Asks Haiku 4.5 to research a
URL-only stub via a single ``tavily_search`` tool call and synthesize a
faithful pitch + cause-of-death narrative; gates on confidence ``"high"``.

Per-entry isolation contract: the enricher logs and returns the entry
unchanged on every recoverable failure (HTTP, JSON parse, schema mismatch).
``BudgetExceededError`` is the one fatal class — it propagates so the
orchestrator can short-circuit the run.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ValidationError

from slopmortem.budget import BudgetExceededError
from slopmortem.llm import (
    OpenRouterCompletionError,
    build_pitch_filler_tavily_tool,
    prompt_template_sha,
    render_blocks,
    to_strict_response_schema,
)

if TYPE_CHECKING:
    from slopmortem.budget import Budget
    from slopmortem.llm import LLMClient
    from slopmortem.models import RawEntry

__all__ = ["HaikuPitchFiller"]

logger = logging.getLogger(__name__)


class _PitchFillerOutput(BaseModel):
    pitch_markdown: str
    confidence: Literal["high", "medium", "low"]
    source_urls: list[str]
    entity_match_reason: str


@dataclass
class HaikuPitchFiller:
    """[Enricher] Haiku-driven pitch synthesizer for URL-only stubs.

    Confidence gate accepts only ``"high"`` — false positives (wrong-entity
    pitches) are corpus poison; false negatives are recoverable by re-running.
    """

    llm: LLMClient
    model: str
    budget: Budget
    tavily_api_key: str
    max_tokens: int = 1500
    max_chars_per_result: int = 2500

    def _should_skip(self, entry: RawEntry) -> bool:
        """Return True when the filler must not call the LLM for this entry."""
        has_body = (entry.markdown_text is not None and entry.markdown_text.strip()) or (
            entry.raw_html is not None and entry.raw_html.strip()
        )
        # Silent skips (no log line): upstream gate already rejected, body is
        # already populated, or there's no URL to research.
        silently_skip = entry.title_pre_filter_rejected or has_body or not entry.url
        if silently_skip:
            return True
        if not entry.title:
            logger.info(
                "pitch filler: skipped %s:%s (no title — can't search)",
                entry.source,
                entry.source_id,
            )
            return True
        # Pre-call budget gate avoids spinning up tool/prompt overhead on a
        # call that would raise inside ``llm.complete`` anyway.
        if self.budget.remaining <= 0.0:
            logger.info(
                "pitch filler: skipped %s:%s (budget exhausted)",
                entry.source,
                entry.source_id,
            )
            return True
        return False

    async def enrich(self, entry: RawEntry) -> RawEntry:
        if self._should_skip(entry):
            return entry

        # ``_should_skip`` guarantees a non-empty URL at this point.
        if not entry.url:
            msg = "pitch filler reached enrich() with empty entry.url"
            raise AssertionError(msg)
        url = entry.url
        domain = urlparse(url).netloc.removeprefix("www.")
        blocks = render_blocks(
            "pitch_filler",
            title=entry.title,
            url=url,
            domain=domain,
        )
        system_block = blocks.get("system", "")
        user_block = blocks.get("user", "")
        tool = build_pitch_filler_tavily_tool(
            api_key=self.tavily_api_key,
            max_chars_per_result=self.max_chars_per_result,
        )

        try:
            result = await self.llm.complete(
                user_block,
                system=system_block,
                tools=[tool],
                model=self.model,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "PitchFillerOutput",
                        "schema": to_strict_response_schema(_PitchFillerOutput),
                        "strict": True,
                    },
                },
                extra_body={"prompt_template_sha": prompt_template_sha("pitch_filler")},
                max_tokens=self.max_tokens,
                single_tool_call=True,
            )
        except BudgetExceededError:
            # Fatal: let the orchestrator short-circuit the run.
            raise
        except (httpx.HTTPError, OpenRouterCompletionError) as exc:
            logger.warning(
                "pitch filler: LLM call failed for %s:%s: %r",
                entry.source,
                entry.source_id,
                exc,
            )
            return entry

        try:
            parsed_obj = cast("object", json.loads(result.text))
            output = _PitchFillerOutput.model_validate(parsed_obj)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning(
                "pitch filler: malformed output for %s:%s: %r",
                entry.source,
                entry.source_id,
                exc,
            )
            return entry

        if output.confidence != "high" or not output.pitch_markdown.strip():
            logger.info(
                "pitch filler: refused %s:%s (confidence=%s, pitch_len=%d) reason=%s",
                entry.source,
                entry.source_id,
                output.confidence,
                len(output.pitch_markdown),
                output.entity_match_reason,
            )
            return entry

        logger.info(
            "pitch filler: kept %s:%s body=%d chars sources=%d reason=%s",
            entry.source,
            entry.source_id,
            len(output.pitch_markdown),
            len(output.source_urls),
            output.entity_match_reason,
        )
        return entry.model_copy(
            update={"markdown_text": output.pitch_markdown, "synthesized": True},
        )
