"""LLM and embedding clients, prompt rendering, and OpenRouter retry logic."""

from __future__ import annotations

from slopmortem.llm._pitch_filler_tools import (
    PitchFillerSearchArgs as PitchFillerSearchArgs,
)
from slopmortem.llm._pitch_filler_tools import (
    build_pitch_filler_tavily_tool as build_pitch_filler_tavily_tool,
)
from slopmortem.llm.cassettes import (
    NoCannedEmbeddingError as NoCannedEmbeddingError,
)
from slopmortem.llm.cassettes import (
    embed_cassette_key as embed_cassette_key,
)
from slopmortem.llm.cassettes import (
    llm_cassette_key as llm_cassette_key,
)
from slopmortem.llm.cassettes import (
    template_sha as template_sha,
)
from slopmortem.llm.client import (
    CompletionResult as CompletionResult,
)
from slopmortem.llm.client import (
    LLMClient as LLMClient,
)
from slopmortem.llm.embedding_client import (
    EmbeddingClient as EmbeddingClient,
)
from slopmortem.llm.embedding_client import (
    EmbeddingResult as EmbeddingResult,
)
from slopmortem.llm.embedding_factory import make_embedder as make_embedder
from slopmortem.llm.fake import (
    FakeLLMClient as FakeLLMClient,
)
from slopmortem.llm.fake import (
    FakeResponse as FakeResponse,
)
from slopmortem.llm.fake import (
    NoCannedResponseError as NoCannedResponseError,
)
from slopmortem.llm.fake_embeddings import FakeEmbeddingClient as FakeEmbeddingClient
from slopmortem.llm.fastembed_client import FastEmbedEmbeddingClient as FastEmbedEmbeddingClient
from slopmortem.llm.openai_embeddings import (
    EMBED_DIMS as EMBED_DIMS,
)
from slopmortem.llm.openai_embeddings import (
    OPENAI_EMBED_MODELS as OPENAI_EMBED_MODELS,
)
from slopmortem.llm.openai_embeddings import (
    OpenAIEmbeddingClient as OpenAIEmbeddingClient,
)
from slopmortem.llm.openrouter import (
    OpenRouterClient as OpenRouterClient,
)
from slopmortem.llm.openrouter import (
    OpenRouterCompletionError as OpenRouterCompletionError,
)
from slopmortem.llm.openrouter import (
    gather_with_limit as gather_with_limit,
)
from slopmortem.llm.openrouter import (
    is_transient_http as is_transient_http,
)
from slopmortem.llm.prompts import (
    prompt_template_sha as prompt_template_sha,
)
from slopmortem.llm.prompts import (
    render_blocks as render_blocks,
)
from slopmortem.llm.prompts import (
    render_prompt as render_prompt,
)
from slopmortem.llm.tools import (
    recall_tools as recall_tools,
)
from slopmortem.llm.tools import (
    synthesis_tools as synthesis_tools,
)
from slopmortem.llm.tools import (
    to_openai_input_schema as to_openai_input_schema,
)
from slopmortem.llm.tools import (
    to_strict_response_schema as to_strict_response_schema,
)

__all__ = [
    "EMBED_DIMS",
    "OPENAI_EMBED_MODELS",
    "CompletionResult",
    "EmbeddingClient",
    "EmbeddingResult",
    "FakeEmbeddingClient",
    "FakeLLMClient",
    "FakeResponse",
    "FastEmbedEmbeddingClient",
    "LLMClient",
    "NoCannedEmbeddingError",
    "NoCannedResponseError",
    "OpenAIEmbeddingClient",
    "OpenRouterClient",
    "OpenRouterCompletionError",
    "PitchFillerSearchArgs",
    "build_pitch_filler_tavily_tool",
    "embed_cassette_key",
    "gather_with_limit",
    "is_transient_http",
    "llm_cassette_key",
    "make_embedder",
    "prompt_template_sha",
    "recall_tools",
    "render_blocks",
    "render_prompt",
    "synthesis_tools",
    "template_sha",
    "to_openai_input_schema",
    "to_strict_response_schema",
]
