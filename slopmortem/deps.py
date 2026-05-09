"""Production dependency builder for the query/replay pipeline.

Lives at the package root (not under ``cli/``) so `slopmortem.evals.runner`
can consume it for ``--live`` mode without reaching into CLI internals.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from openai import AsyncOpenAI

from slopmortem.budget import Budget
from slopmortem.corpus import QdrantCorpus
from slopmortem.llm import OpenRouterClient, make_embedder

if TYPE_CHECKING:
    from slopmortem.config import Config
    from slopmortem.corpus import Corpus
    from slopmortem.llm import EmbeddingClient, LLMClient


def build_deps(
    config: Config,
) -> tuple[LLMClient, EmbeddingClient, Corpus, Budget]:
    """Build production deps for the query pipeline: LLM, embedder, corpus, budget."""
    from qdrant_client import AsyncQdrantClient  # noqa: PLC0415 - heavy dep, lazy import

    budget = Budget(cap_usd=config.max_cost_usd_per_query)

    openrouter_sdk = AsyncOpenAI(
        api_key=config.openrouter_api_key.get_secret_value(),
        base_url=config.openrouter_base_url,
    )
    llm = OpenRouterClient(
        sdk=openrouter_sdk,
        budget=budget,
        model=config.model_synthesize,
    )

    embedder = make_embedder(config, budget)

    qdrant_client = AsyncQdrantClient(host=config.qdrant_host, port=config.qdrant_port)
    corpus = QdrantCorpus(
        client=qdrant_client,
        collection=config.qdrant_collection,
        post_mortems_root=Path(config.post_mortems_root),
        facet_boost=config.facet_boost,
        rrf_k=config.rrf_k,
        recall_score_factor=config.recall_score_factor,
    )

    return llm, embedder, corpus, budget
