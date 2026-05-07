from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from slopmortem.budget import Budget, BudgetExceededError
from slopmortem.llm import OpenRouterClient
from slopmortem.models import ToolSpec


def _stub_usage(prompt_cached: int = 0, prompt_cache_write: int = 0, cost: float = 0.001):
    return SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=10,
        total_tokens=110,
        prompt_tokens_details=SimpleNamespace(
            cached_tokens=prompt_cached,
            cache_write_tokens=prompt_cache_write,
        ),
        cost=cost,
    )


def _stub_response(*, finish_reason, content="", tool_calls=None, usage=None, error=None):
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls or []
    choice = MagicMock()
    choice.finish_reason = finish_reason
    choice.message = msg
    if error is not None:
        choice.error = error
    else:
        # Avoid MagicMock auto-attribute returning a truthy object.
        choice.error = None
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    return resp


@pytest.fixture
def fake_sdk():
    sdk = MagicMock()
    sdk.chat = MagicMock()
    sdk.chat.completions = MagicMock()
    sdk.chat.completions.create = AsyncMock()
    return sdk


async def test_finish_reason_stop_returns_text(fake_sdk):
    fake_sdk.chat.completions.create.return_value = _stub_response(
        finish_reason="stop",
        content='{"x":1}',
        usage=_stub_usage(),
    )
    c = OpenRouterClient(sdk=fake_sdk, budget=Budget(2.0))
    r = await c.complete("hi")
    assert r.text == '{"x":1}'
    assert r.stop_reason == "stop"


async def test_finish_reason_tool_calls_invokes_tool_then_continues(fake_sdk):
    class Args(BaseModel):
        x: int

    async def fn(x: int) -> str:
        return f"got {x}"

    tool = ToolSpec(name="t", description="", args_model=Args, fn=fn)
    fake_sdk.chat.completions.create.side_effect = [
        _stub_response(
            finish_reason="tool_calls",
            tool_calls=[{"id": "t1", "function": {"name": "t", "arguments": '{"x":1}'}}],
            usage=_stub_usage(),
        ),
        _stub_response(finish_reason="stop", content="done", usage=_stub_usage()),
    ]
    c = OpenRouterClient(sdk=fake_sdk, budget=Budget(2.0))
    r = await c.complete("hi", tools=[tool])
    assert r.text == "done"


async def test_finish_reason_length_raises(fake_sdk):
    fake_sdk.chat.completions.create.return_value = _stub_response(
        finish_reason="length",
        content="",
        usage=_stub_usage(),
    )
    c = OpenRouterClient(sdk=fake_sdk, budget=Budget(2.0))
    with pytest.raises(RuntimeError, match="length"):
        await c.complete("hi")


async def test_finish_reason_content_filter_raises(fake_sdk):
    fake_sdk.chat.completions.create.return_value = _stub_response(
        finish_reason="content_filter",
        content="",
        usage=_stub_usage(),
    )
    c = OpenRouterClient(sdk=fake_sdk, budget=Budget(2.0))
    with pytest.raises(RuntimeError, match="content_filter"):
        await c.complete("hi")


async def test_mid_stream_error_finish_reason_retries(fake_sdk):
    fake_sdk.chat.completions.create.side_effect = [
        _stub_response(
            finish_reason="error",
            content="",
            usage=_stub_usage(),
            error={"code": "overloaded_error"},
        ),
        _stub_response(finish_reason="stop", content="recovered", usage=_stub_usage()),
    ]
    c = OpenRouterClient(sdk=fake_sdk, budget=Budget(2.0))
    r = await c.complete("hi")
    assert r.text == "recovered"


async def test_cache_tokens_extracted(fake_sdk):
    usage = _stub_usage(prompt_cached=80, prompt_cache_write=20, cost=0.01)
    fake_sdk.chat.completions.create.return_value = _stub_response(
        finish_reason="stop",
        content="ok",
        usage=usage,
    )
    c = OpenRouterClient(sdk=fake_sdk, budget=Budget(2.0))
    r = await c.complete("hi")
    assert r.cache_read_tokens == 80
    assert r.cache_creation_tokens == 20
    assert r.cost_usd == 0.01


async def test_pre_call_gate_raises_when_budget_exhausted(fake_sdk):
    # cap=0 leaves no remaining budget; the gate must fire before any SDK call.
    c = OpenRouterClient(sdk=fake_sdk, budget=Budget(cap_usd=0.0))
    with pytest.raises(BudgetExceededError, match="budget exhausted"):
        await c.complete("hi")
    assert fake_sdk.chat.completions.create.call_count == 0


async def test_post_settle_raise_stops_subsequent_calls(fake_sdk):
    # First call settles a cost that pushes spent over cap; settle raises out of
    # complete(). The second call's pre-call gate then refuses to issue at all.
    fake_sdk.chat.completions.create.return_value = _stub_response(
        finish_reason="stop",
        content="ok",
        usage=_stub_usage(cost=2.0),
    )
    c = OpenRouterClient(sdk=fake_sdk, budget=Budget(cap_usd=1.0))
    with pytest.raises(BudgetExceededError, match="spent"):
        await c.complete("hi")
    assert fake_sdk.chat.completions.create.call_count == 1
    with pytest.raises(BudgetExceededError, match="exhausted"):
        await c.complete("hi again")
    assert fake_sdk.chat.completions.create.call_count == 1


async def test_default_call_does_not_send_tool_choice(fake_sdk):
    """Synthesis path: no single_tool_call → no tool_choice kwarg ever sent."""
    fake_sdk.chat.completions.create.return_value = _stub_response(
        finish_reason="stop", content="ok", usage=_stub_usage()
    )
    c = OpenRouterClient(sdk=fake_sdk, budget=Budget(2.0))
    await c.complete("hi")
    kwargs = fake_sdk.chat.completions.create.call_args.kwargs
    assert "tool_choice" not in kwargs


async def test_single_tool_call_pins_one_tool_invocation(fake_sdk):
    """single_tool_call=True forces tool_choice='required' on turn 0, 'none' on turn 1."""

    class Args(BaseModel):
        x: int

    async def fn(x: int) -> str:
        return f"got {x}"

    tool = ToolSpec(name="t", description="", args_model=Args, fn=fn)
    fake_sdk.chat.completions.create.side_effect = [
        _stub_response(
            finish_reason="tool_calls",
            tool_calls=[{"id": "t1", "function": {"name": "t", "arguments": '{"x":1}'}}],
            usage=_stub_usage(),
        ),
        _stub_response(finish_reason="stop", content="done", usage=_stub_usage()),
    ]
    c = OpenRouterClient(sdk=fake_sdk, budget=Budget(2.0))
    r = await c.complete("hi", tools=[tool], single_tool_call=True)
    assert r.text == "done"
    calls = fake_sdk.chat.completions.create.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["tool_choice"] == "required"
    assert calls[1].kwargs["tool_choice"] == "none"
