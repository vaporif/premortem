"""LLMClient Protocol: chat-completion shape shared by real, fake, and cassette backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pydantic import BaseModel


@dataclass
class CompletionResult:
    text: str
    stop_reason: str
    parsed: BaseModel | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cost_usd: float = 0.0


@runtime_checkable
class LLMClient(Protocol):
    # ``Any`` is intentional: tools/response_format/extra_body are SDK passthroughs.
    async def complete(  # noqa: PLR0913 - mirrors OpenAI chat.create
        self,
        prompt: str,
        *,
        system: str | None = None,
        tools: list[Any] | None = None,  # pyright: ignore[reportExplicitAny]
        model: str | None = None,
        cache: bool = False,
        response_format: dict[str, Any] | None = None,  # pyright: ignore[reportExplicitAny]
        extra_body: dict[str, Any] | None = None,  # pyright: ignore[reportExplicitAny]
        max_tokens: int | None = None,
        single_tool_call: bool = False,
    ) -> CompletionResult: ...
