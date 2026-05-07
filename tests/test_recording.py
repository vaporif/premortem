"""Tests for RecordingLLMClient, RecordingEmbeddingClient, RecordingSparseEncoder."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from slopmortem.evals.cassettes import (
    RecordingBudgetExceededError,
    load_embedding_cassettes,
)
from slopmortem.evals.recording import (
    RecordingEmbeddingClient,
    RecordingLLMClient,
    RecordingSparseEncoder,
)
from slopmortem.llm import CompletionResult, EmbeddingResult

if TYPE_CHECKING:
    from pathlib import Path


class _FakeInnerLLM:
    def __init__(self, *, text: str = "ok", cost_usd: float = 0.10) -> None:
        self.text = text
        self.cost_usd = cost_usd
        self.calls: int = 0
        self.raise_on_call: int | None = None

    async def complete(  # noqa: PLR0913 - mirrors LLMClient.complete signature
        self,
        prompt,
        *,
        system=None,
        tools=None,
        model=None,
        cache=False,
        response_format=None,
        extra_body=None,
        max_tokens=None,
        single_tool_call=False,
    ):
        self.calls += 1
        if self.raise_on_call is not None and self.calls == self.raise_on_call:
            msg = "simulated inner failure"
            raise RuntimeError(msg)
        return CompletionResult(
            text=self.text,
            stop_reason="stop",
            cost_usd=self.cost_usd,
            cache_read_tokens=0,
            cache_creation_tokens=0,
        )


async def test_recording_llm_writes_cassette_on_success(tmp_path: Path) -> None:
    inner = _FakeInnerLLM(text="hello")
    rec = RecordingLLMClient(
        inner=inner,
        out_dir=tmp_path,
        stage="facet_extract",
        model="anthropic/claude-haiku-4.5",
    )
    extra = {"prompt_template_sha": "tsha-abc"}
    result = await rec.complete("the prompt", model="anthropic/claude-haiku-4.5", extra_body=extra)
    assert result.text == "hello"
    files = sorted(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert files[0].name.startswith("facet_extract__anthropic_claude-haiku-4.5__")


async def test_recording_llm_skips_cassette_on_inner_error(tmp_path: Path) -> None:
    inner = _FakeInnerLLM()
    inner.raise_on_call = 1
    rec = RecordingLLMClient(
        inner=inner,
        out_dir=tmp_path,
        stage="facet_extract",
        model="m",
    )
    with pytest.raises(RuntimeError):
        _ = await rec.complete("the prompt", model="m", extra_body={"prompt_template_sha": "t"})
    assert list(tmp_path.glob("*.json")) == []


async def test_recording_llm_cost_ceiling_aborts_before_inner(tmp_path: Path) -> None:
    # The plan-spec'd guard (`spent >= max_cost_usd`) fires when accumulated
    # spend has reached the cap; with cost-per-call=0.40 the third call
    # enters with spent=0.80, so the cap must be 0.80 to abort before inner.
    inner = _FakeInnerLLM(cost_usd=0.40)
    rec = RecordingLLMClient(
        inner=inner,
        out_dir=tmp_path,
        stage="facet_extract",
        model="m",
        max_cost_usd=0.80,
    )
    extra = {"prompt_template_sha": "t"}
    _ = await rec.complete("a", model="m", extra_body={**extra, "prompt_hash": "0" * 16})
    _ = await rec.complete("b", model="m", extra_body={**extra, "prompt_hash": "1" * 16})
    with pytest.raises(RecordingBudgetExceededError) as exc_info:
        _ = await rec.complete("c", model="m", extra_body={**extra, "prompt_hash": "2" * 16})
    assert exc_info.value.spent == pytest.approx(0.80)
    assert exc_info.value.limit == pytest.approx(0.80)
    # Inner only called twice; the third aborts pre-call, so cassettes exist for a and b only.
    assert inner.calls == 2
    assert len(list(tmp_path.glob("*.json"))) == 2


class _FakeInnerEmbed:
    model = "text-embedding-3-small"
    dim = 1536

    async def embed(self, texts, *, model=None):
        # Return a vector distinct per input.
        return EmbeddingResult(
            vectors=[[float(i)] * 1536 for i, _ in enumerate(texts)],
            n_tokens=len(texts),
            cost_usd=0.0,
        )


async def test_recording_embed_splits_batch_into_per_text_cassettes(tmp_path: Path) -> None:
    inner = _FakeInnerEmbed()
    rec = RecordingEmbeddingClient(inner=inner, out_dir=tmp_path)
    result = await rec.embed(["hello", "world", "hello"])  # repeated text reuses cassette
    assert len(result.vectors) == 3
    files = sorted(tmp_path.glob("embed__*.json"))
    # Two unique texts give two cassettes; "hello" overwrites itself idempotently.
    assert len(files) == 2


class _FakeInnerSparse:
    @staticmethod
    def encode(text: str) -> dict[int, float]:
        return {1: 0.5, 2: 0.25}


async def test_recording_sparse_writes_qdrant_bm25_cassette(tmp_path: Path) -> None:
    rec = RecordingSparseEncoder(inner=_FakeInnerSparse.encode, out_dir=tmp_path)
    out = rec("hello")
    assert out == {1: 0.5, 2: 0.25}
    dense, sparse = load_embedding_cassettes(tmp_path)
    assert dense == {}
    [(k, (idx, vals))] = list(sparse.items())
    assert k[0] == "Qdrant/bm25"
    assert sorted(zip(idx, vals, strict=True)) == [(1, 0.5), (2, 0.25)]
