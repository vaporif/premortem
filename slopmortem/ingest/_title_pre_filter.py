"""Cheap title-only Haiku gate that runs before the pitch filler.

Most HN-Algolia hits the phrase search returns are not actually startup
death narratives — denials, listicles, generic essays, Show HN posts.
Asking Haiku a yes/no question on the title alone (no body, no Tavily)
cuts pitch-filler invocations and Tavily credit burn substantially.
Rejected entries get ``title_pre_filter_rejected=True``; downstream
enrichers and the ingest classify loop short-circuit on the flag.

Per-entry isolation contract: log and return the entry unchanged on every
recoverable failure (HTTP, JSON parse, schema mismatch).
``BudgetExceededError`` is the one fatal class — it propagates so the
orchestrator can short-circuit the run.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

import httpx
from pydantic import BaseModel, ValidationError

from slopmortem.budget import BudgetExceededError
from slopmortem.llm import (
    prompt_template_sha,
    render_blocks,
    to_strict_response_schema,
)

if TYPE_CHECKING:
    from slopmortem.budget import Budget
    from slopmortem.llm import LLMClient
    from slopmortem.models import RawEntry

__all__ = ["HaikuTitlePreFilter"]

logger = logging.getLogger(__name__)


class _TitlePreFilterOutput(BaseModel):
    decision: Literal["yes", "no"]


@dataclass
class HaikuTitlePreFilter:
    """[Enricher] Title-only Haiku gate that flips a skip flag for non-death titles."""

    llm: LLMClient
    model: str
    budget: Budget
    max_tokens: int = 16

    def _should_skip(self, entry: RawEntry) -> bool:
        if entry.markdown_text is not None and entry.markdown_text.strip():
            return True
        if entry.raw_html is not None and entry.raw_html.strip():
            return True
        if not entry.title:
            return True
        # Pre-call budget gate avoids spinning up prompt overhead on a call
        # that would raise inside ``llm.complete`` anyway.
        if self.budget.remaining <= 0.0:
            logger.info(
                "title pre-filter: skipped %s:%s (budget exhausted)",
                entry.source,
                entry.source_id,
            )
            return True
        return False

    async def enrich(self, entry: RawEntry) -> RawEntry:
        if self._should_skip(entry):
            return entry

        blocks = render_blocks("title_pre_filter", title=entry.title)
        system_block = blocks.get("system", "")
        user_block = blocks.get("user", "")

        try:
            result = await self.llm.complete(
                user_block,
                system=system_block,
                model=self.model,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "TitlePreFilterOutput",
                        "schema": to_strict_response_schema(_TitlePreFilterOutput),
                        "strict": True,
                    },
                },
                extra_body={"prompt_template_sha": prompt_template_sha("title_pre_filter")},
                max_tokens=self.max_tokens,
            )
        except BudgetExceededError:
            # Fatal: let the orchestrator short-circuit the run.
            raise
        except (httpx.HTTPError, RuntimeError) as exc:
            logger.warning(
                "title pre-filter: LLM call failed for %s:%s: %r",
                entry.source,
                entry.source_id,
                exc,
            )
            return entry

        try:
            parsed_obj = cast("object", json.loads(result.text))
            output = _TitlePreFilterOutput.model_validate(parsed_obj)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning(
                "title pre-filter: malformed output for %s:%s: %r",
                entry.source,
                entry.source_id,
                exc,
            )
            return entry

        if output.decision == "no":
            logger.info(
                "title pre-filter: rejected %s:%s title=%r",
                entry.source,
                entry.source_id,
                entry.title,
            )
            return entry.model_copy(update={"title_pre_filter_rejected": True})
        return entry
