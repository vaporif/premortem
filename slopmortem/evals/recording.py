"""Recording wrappers: forward to a real client, write a cassette on success.

Lives under ``slopmortem/evals/`` not ``slopmortem/llm/`` so prod doesn't
pull in test infra. Import direction is one-way: ``evals → llm``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from slopmortem.evals.cassettes import (
    EmbeddingCassette,
    LlmCassette,
    RecordingBudgetExceededError,
    SparseCassette,
    write_embedding_cassette,
    write_llm_cassette,
    write_sparse_cassette,
)
from slopmortem.llm import embed_cassette_key, llm_cassette_key

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from slopmortem.llm import CompletionResult, EmbeddingClient, EmbeddingResult, LLMClient


_PREVIEW_CHARS_PROMPT = 500
_PREVIEW_CHARS_TEXT = 200


def _tool_name(t: object) -> str:
    """Best-effort string name for a tool object; falls back to ``str(t)``."""
    name = getattr(t, "name", None)
    return str(name) if name is not None else str(t)  # pyright: ignore[reportAny]


class RecordingLLMClient:
    """Wrap a real ``LLMClient``; write one cassette per ``complete()`` call."""

    def __init__(  # noqa: PLR0913 — wrapper exposes every recording knob explicitly
        self,
        *,
        inner: LLMClient,
        out_dir: Path,
        stage: str,
        model: str,
        max_cost_usd: float | None = None,
        on_cost: Callable[[float], None] | None = None,
    ) -> None:
        self._inner = inner
        self._out_dir = out_dir
        self._stage = stage
        self._model = model
        self._max_cost_usd = max_cost_usd
        self._spent_usd = 0.0
        self._on_cost = on_cost

    async def complete(  # noqa: PLR0913 — mirrors LLMClient.complete public signature
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
    ) -> CompletionResult:
        if self._max_cost_usd is not None and self._spent_usd >= self._max_cost_usd:
            raise RecordingBudgetExceededError(
                spent=self._spent_usd,
                limit=self._max_cost_usd,
            )
        result = await self._inner.complete(
            prompt,
            system=system,
            tools=tools,
            model=model,
            cache=cache,
            response_format=response_format,
            extra_body=extra_body,
            max_tokens=max_tokens,
            single_tool_call=single_tool_call,
        )
        eff_model = model or self._model
        template_sha = ""
        if extra_body and "prompt_template_sha" in extra_body:
            template_sha = str(extra_body["prompt_template_sha"])  # pyright: ignore[reportAny]
        # Allow override (tests pin a known prompt_hash); otherwise compute.
        if extra_body and "prompt_hash" in extra_body:
            prompt_hash = str(extra_body["prompt_hash"])  # pyright: ignore[reportAny]
        else:
            _, _, prompt_hash = llm_cassette_key(
                prompt=prompt,
                system=system,
                template_sha=template_sha,
                model=eff_model,
            )
        tool_names: list[str] = [_tool_name(t) for t in tools or []]  # pyright: ignore[reportAny]
        cas = LlmCassette(
            template_sha=template_sha,
            model=eff_model,
            prompt_hash=prompt_hash,
            text=result.text,
            stop_reason=result.stop_reason,
            cost_usd=result.cost_usd,
            cache_read_tokens=result.cache_read_tokens,
            cache_creation_tokens=result.cache_creation_tokens,
            prompt_preview=prompt[:_PREVIEW_CHARS_PROMPT],
            system_preview=(system or "")[:_PREVIEW_CHARS_PROMPT],
            tools_present=tool_names,
            response_format_present=response_format is not None,
        )
        write_llm_cassette(cas, self._out_dir, stage=self._stage)
        self._spent_usd += result.cost_usd
        # After the counter so handlers reading back into the wrapper see a
        # consistent view.
        if self._on_cost is not None:
            self._on_cost(result.cost_usd)
        return result


class RecordingEmbeddingClient:
    """Wrap a real ``EmbeddingClient``; write one cassette per text per ``embed()``.

    ``on_cost`` is always called with ``0.0`` under the default fastembed
    provider; only the OpenAI provider moves the meter.
    """

    def __init__(
        self,
        *,
        inner: EmbeddingClient,
        out_dir: Path,
        on_cost: Callable[[float], None] | None = None,
    ) -> None:
        self._inner = inner
        self._out_dir = out_dir
        self._on_cost = on_cost

    @property
    def model(self) -> str:
        """Forward the wrapped client's ``model`` (not in the Protocol; every impl exposes it)."""
        inner_model: object = getattr(self._inner, "model", "")
        return str(inner_model) if inner_model else ""

    async def embed(self, texts: list[str], *, model: str | None = None) -> EmbeddingResult:
        result = await self._inner.embed(texts, model=model)
        eff_model = model or self.model
        for text, vector in zip(texts, result.vectors, strict=True):
            _, text_hash = embed_cassette_key(text=text, model=eff_model)
            cas = EmbeddingCassette(
                model=eff_model,
                text_hash=text_hash,
                vector=list(vector),
                text_preview=text[:_PREVIEW_CHARS_TEXT],
            )
            write_embedding_cassette(cas, self._out_dir)
        if self._on_cost is not None:
            self._on_cost(result.cost_usd)
        return result


class RecordingSparseEncoder:
    """Wrap a sparse-encoder callable; write one cassette per ``__call__``."""

    def __init__(
        self,
        *,
        inner: Callable[[str], dict[int, float]],
        out_dir: Path,
        model: str = "Qdrant/bm25",
    ) -> None:
        self._inner = inner
        self._out_dir = out_dir
        self._model = model

    def __call__(self, text: str) -> dict[int, float]:
        result = self._inner(text)
        _, text_hash = embed_cassette_key(text=text, model=self._model)
        if result:
            indices_t, values_t = zip(*sorted(result.items()), strict=True)
            indices, values = list(indices_t), list(values_t)
        else:
            indices, values = [], []
        cas = SparseCassette(
            model=self._model,
            text_hash=text_hash,
            indices=indices,
            values=values,
            text_preview=text[:_PREVIEW_CHARS_TEXT],
        )
        write_sparse_cassette(cas, self._out_dir)
        return result
