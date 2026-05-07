# pyright: reportAny=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""Async OpenRouter / OpenAI-compatible chat client; retries, tool-call loop, cache control.

Vendor SDK is duck-typed at the boundary; reportAny / reportUnknown* are
silenced file-wide, reportExplicitAny stays per-site.
"""

from __future__ import annotations

import json
import random
from typing import TYPE_CHECKING, Any

import anyio

from slopmortem.budget import BudgetExceededError
from slopmortem.concurrency import gather_resilient
from slopmortem.llm.client import CompletionResult
from slopmortem.tracing import SpanEvent

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Iterable

    from slopmortem.budget import Budget
    from slopmortem.models import ToolSpec

# HTTP status code constants used by the transient-vs-fatal classifier.
_HTTP_SERVER_ERROR_MIN = 500
_HTTP_SERVER_ERROR_MAX = 600
_HTTP_TOO_MANY_REQUESTS = 429


class MidStreamError(Exception):
    """SSE chunk arrived at HTTP 200 with finish_reason='error'.

    Carries the raw error payload so the retry layer can decide whether
    ``error.code`` is transient (e.g. ``overloaded_error``) or fatal.
    """

    def __init__(self, error: object) -> None:
        super().__init__(str(error))
        self.error = error or {}

    @property
    def code(self) -> str:
        if isinstance(self.error, dict):
            return str(self.error.get("code", ""))
        return str(getattr(self.error, "code", ""))


_TRANSIENT_MIDSTREAM_CODES = frozenset({"overloaded_error"})


async def gather_with_limit[T](
    coros: Iterable[Coroutine[Any, Any, T]],  # pyright: ignore[reportExplicitAny]
    limit: int,
) -> list[T | Exception]:
    """Run *coros* concurrently with at most *limit* in flight.

    Wraps ``gather_resilient`` behind a ``CapacityLimiter`` so callers can cap
    parallel OpenRouter calls at ``config.ingest_concurrency``.
    """
    limiter = anyio.CapacityLimiter(limit)

    async def _run(coro: Coroutine[Any, Any, T]) -> T:  # pyright: ignore[reportExplicitAny]
        async with limiter:
            return await coro

    return await gather_resilient(*(_run(c) for c in coros))


class OpenRouterClient:
    """OpenAI-compatible SDK wrapper for OpenRouter; runs the retry and tool-call loops."""

    def __init__(  # noqa: PLR0913 - knobs are public API; users construct this directly.
        self,
        *,
        sdk: object,
        budget: Budget,
        model: str | None = None,
        max_retries: int = 3,
        max_tool_turns: int = 5,
        initial_backoff: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._sdk = sdk
        self._budget = budget
        self._default_model = model
        self._max_retries = max_retries
        self._max_tool_turns = max_tool_turns
        self._initial_backoff = initial_backoff
        self._sleep: Callable[[float], Awaitable[None]] = sleep or anyio.sleep

    async def complete(  # noqa: C901, PLR0913 - mirrors OpenAI chat.create kwargs.
        self,
        prompt: str,
        *,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
        cache: bool = False,
        response_format: dict[str, Any] | None = None,  # pyright: ignore[reportExplicitAny]
        extra_body: dict[str, Any] | None = None,  # pyright: ignore[reportExplicitAny]
        max_tokens: int | None = None,
        single_tool_call: bool = False,
    ) -> CompletionResult:
        """Run a chat completion, including the tool-call loop and transient-error retries."""
        # ``single_tool_call`` is wired in Task 2; accepted here only so the
        # Protocol signature is stable across this commit.
        del single_tool_call
        # Pre-call gate: a runaway loop stops issuing calls once the budget is
        # exhausted. Concurrent fan-out can still tail-overshoot by up to
        # N_synthesize x per-call cost.
        if self._budget.remaining <= 0.0:
            msg = f"budget exhausted: remaining {self._budget.remaining:.4f}"
            raise BudgetExceededError(msg)
        messages = self._build_messages(system, prompt, cache=cache)
        tools_payload = self._build_tools(tools)
        registered = {t.name: t for t in (tools or [])}
        cache_read = 0
        cache_write = 0
        cost = 0.0

        # Only include max_tokens when set so unset callers keep the SDK's
        # "no cap" behavior — sending max_tokens=None upstream is rejected.
        base_kw: dict[str, Any] = {  # pyright: ignore[reportExplicitAny]
            "model": model or self._default_model,
            "response_format": response_format,
            "extra_body": extra_body,
        }
        if max_tokens is not None:
            base_kw["max_tokens"] = max_tokens

        try:
            for _turn in range(self._max_tool_turns):
                resp = await self._call_with_retry(
                    messages=messages,
                    tools=tools_payload,
                    **base_kw,
                )
                usage = resp.usage
                if usage is not None:
                    ptd = getattr(usage, "prompt_tokens_details", None)
                    cache_read += getattr(ptd, "cached_tokens", 0) or 0 if ptd else 0
                    cache_write += getattr(ptd, "cache_write_tokens", 0) or 0 if ptd else 0
                    cost += getattr(usage, "cost", 0.0) or 0.0
                choice = resp.choices[0]
                fr = choice.finish_reason

                if fr == "stop":
                    if cache and cache_write == 0:
                        self._emit(SpanEvent.CACHE_WARM_FAILED)
                    return CompletionResult(
                        text=choice.message.content or "",
                        stop_reason="stop",
                        cache_read_tokens=cache_read,
                        cache_creation_tokens=cache_write,
                        cost_usd=cost,
                    )

                if fr == "tool_calls":
                    self._assert_tool_allowlist(choice.message.tool_calls, registered)
                    messages.append(_assistant_with_tools(choice.message))
                    for tc in choice.message.tool_calls:
                        name = _tc_name(tc)
                        args_raw = _tc_arguments(tc)
                        args = json.loads(args_raw)
                        spec = registered[name]
                        spec.args_model.model_validate(args)
                        result = await spec.fn(**args)
                        wrapped = (
                            f'<untrusted_document source="{name}">\n{result}\n</untrusted_document>'
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": _tc_id(tc),
                                "content": wrapped,
                            }
                        )
                    continue

                if fr in ("length", "content_filter"):
                    msg = f"hard stop: {fr}"
                    raise RuntimeError(msg)

                if fr == "error":
                    # _call_with_retry should have consumed the stream and raised
                    # MidStreamError before this branch runs. Defensive only.
                    raise MidStreamError(getattr(choice, "error", {"code": "unknown"}))

            msg = "tool-loop bound exceeded"
            raise RuntimeError(msg)
        finally:
            if cost > 0.0:
                # True cost lands on response.usage.cost — settle without a
                # prior reserve. settle() raises if spent crosses the cap; the
                # pre-call gate above stops the next call.
                await self._budget.settle("openrouter:complete", cost)

    @staticmethod
    def _is_transient(exc: BaseException) -> bool:
        if isinstance(exc, MidStreamError):
            return exc.code in _TRANSIENT_MIDSTREAM_CODES
        return is_transient_http(exc)

    async def _call_with_retry(self, **kw: Any) -> Any:  # pyright: ignore[reportExplicitAny]
        """Call SDK with retry/backoff on transient errors.

        Mid-stream ``error.code='overloaded_error'`` is transient. Auth
        (401/403), 402 (no credits), 503 (no provider), and non-overloaded
        mid-stream errors are fatal — re-raised immediately.
        """
        sdk: Any = self._sdk  # pyright: ignore[reportExplicitAny]
        for attempt in range(self._max_retries + 1):
            try:
                resp = await sdk.chat.completions.create(**kw)
                # Even non-streaming responses surface the final SSE chunk's
                # mid-stream error here: choices[0].finish_reason=='error'
                # with the upstream payload on choices[0].error.
                if resp.choices and resp.choices[0].finish_reason == "error":
                    err = getattr(resp.choices[0], "error", None) or {"code": "unknown"}
                    raise MidStreamError(err)  # noqa: TRY301 - caught locally to drive retry loop
            except Exception as exc:
                if not self._is_transient(exc) or attempt >= self._max_retries:
                    raise
                await self._backoff(attempt)
                continue
            else:
                return resp
        msg = "retry loop exited without resolution"  # pragma: no cover - unreachable
        raise RuntimeError(msg)

    async def _backoff(self, attempt: int) -> None:
        delay = self._initial_backoff * (2**attempt)
        delay += random.uniform(0, delay * 0.25)  # noqa: S311 - non-cryptographic jitter
        await self._sleep(delay)

    def _build_messages(
        self, system: str | None, prompt: str, *, cache: bool
    ) -> list[dict[str, Any]]:  # pyright: ignore[reportExplicitAny]
        msgs: list[dict[str, Any]] = []  # pyright: ignore[reportExplicitAny]
        if system:
            sys_block: dict[str, Any] = {"type": "text", "text": system}  # pyright: ignore[reportExplicitAny]
            if cache:
                sys_block["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
            msgs.append({"role": "system", "content": [sys_block]})
        user_block: dict[str, Any] = {"type": "text", "text": prompt}  # pyright: ignore[reportExplicitAny]
        if cache:
            user_block["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        msgs.append({"role": "user", "content": [user_block]})
        return msgs

    def _build_tools(self, tools: list[ToolSpec] | None) -> list[dict[str, Any]] | None:  # pyright: ignore[reportExplicitAny]
        if not tools:
            return None
        from slopmortem.llm.tools import (  # noqa: PLC0415 - break import cycle
            to_openai_input_schema,
        )

        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": to_openai_input_schema(t.args_model),
                },
            }
            for t in tools
        ]

    def _assert_tool_allowlist(
        self,
        tcs: Iterable[Any],  # pyright: ignore[reportExplicitAny]
        registered: dict[str, ToolSpec],
    ) -> None:
        for tc in tcs:
            name = _tc_name(tc)
            if name not in registered:
                msg = f"{SpanEvent.TOOL_ALLOWLIST_VIOLATION.value}: {name}"
                raise RuntimeError(msg)

    def _emit(self, _event: SpanEvent) -> None:
        # No-op hook so tests can patch it to observe emissions. Active emit
        # path lives in stages.synthesize._emit_event.
        return


def _tc_name(tc: object) -> str:
    if isinstance(tc, dict):
        return str(tc["function"]["name"])
    return str(tc.function.name)  # pyright: ignore[reportAttributeAccessIssue]


def _tc_arguments(tc: object) -> str:
    if isinstance(tc, dict):
        return str(tc["function"]["arguments"])
    return str(tc.function.arguments)  # pyright: ignore[reportAttributeAccessIssue]


def _tc_id(tc: object) -> str:
    if isinstance(tc, dict):
        return str(tc["id"])
    return str(tc.id)  # pyright: ignore[reportAttributeAccessIssue]


def _assistant_with_tools(message: object) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
    """Render the assistant turn that requested tool calls (replayed with the tool response)."""
    tcs = [
        {
            "id": _tc_id(tc),
            "type": "function",
            "function": {
                "name": _tc_name(tc),
                "arguments": _tc_arguments(tc),
            },
        }
        for tc in getattr(message, "tool_calls", []) or []
    ]
    return {
        "role": "assistant",
        "content": getattr(message, "content", None) or "",
        "tool_calls": tcs,
    }


def is_transient_http(exc: BaseException) -> bool:
    """Classify openai SDK exceptions as transient or fatal.

    Duck-typed; no SDK internals imported.
    """
    name = type(exc).__name__
    if name in ("APIConnectionError", "APITimeoutError", "RateLimitError"):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in (401, 402, 403, 503):
            return False
        if _HTTP_SERVER_ERROR_MIN <= status < _HTTP_SERVER_ERROR_MAX:
            return True
        if status == _HTTP_TOO_MANY_REQUESTS:
            return True
    return False
