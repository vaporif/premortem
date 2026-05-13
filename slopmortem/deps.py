"""Production dependency builder for the query/replay pipeline.

Lives at the package root (not under ``cli/``) so ``slopmortem.evals.runner``
can consume it for ``--live`` mode without reaching into CLI internals.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from openai import AsyncOpenAI

from slopmortem.budget import Budget
from slopmortem.corpus import QdrantCorpus
from slopmortem.corpus.tavily import (
    tavily_extract_structured,
    tavily_search_structured,
)
from slopmortem.llm import OpenRouterClient, make_embedder

if TYPE_CHECKING:
    from slopmortem.config import Config
    from slopmortem.corpus import Corpus
    from slopmortem.corpus.tavily import TavilyHit
    from slopmortem.llm import EmbeddingClient, LLMClient
    from slopmortem.recall import ExtractFn, TavilySearchFn
    from slopmortem.stages import SparseEncoder


def build_deps(
    config: Config,
) -> tuple[LLMClient, EmbeddingClient, Corpus, Budget, SparseEncoder]:
    """Build production deps for the query pipeline.

    The sparse encoder is the callable both ``retrieve`` and
    ``recall_persist`` need; a single owner here keeps the recall branch
    from firing without one wired up. The fastembed model itself loads
    lazily on first call, so this stays cheap at startup.
    """
    from qdrant_client import AsyncQdrantClient  # noqa: PLC0415 - heavy dep, lazy import

    from slopmortem.corpus._embed_sparse import encode as sparse_encoder  # noqa: PLC0415

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

    return llm, embedder, corpus, budget, sparse_encoder


def build_tavily_recall_search(config: Config) -> TavilySearchFn | None:
    """Return a ``TavilySearchFn`` bound to ``config.tavily_api_key``, or ``None``.

    Closure captures the key so the verifier doesn't have to thread
    ``Config`` through. Set ``enable_tavily_recall_search=False`` to disable
    recall entirely — the L0 search head is mandatory.
    """
    if not config.enable_tavily_recall_search:
        return None
    api_key = config.tavily_api_key.get_secret_value()

    async def search(q: str, limit: int) -> list[TavilyHit]:
        return await tavily_search_structured(q, limit, api_key=api_key)

    return search


def build_tavily_recall_extract(config: Config) -> ExtractFn | None:
    """Return an ``ExtractFn`` bound to ``config.tavily_api_key``, or ``None``.

    Same flag as the search head: L3 extract rides whenever Tavily is wired
    in. The verifier drops gracefully on empty/raised extract, and Tavily
    quota is shared across both surfaces.
    """
    if not config.enable_tavily_recall_search:
        return None
    api_key = config.tavily_api_key.get_secret_value()

    async def extract(url: str) -> str:
        return await tavily_extract_structured(url, api_key=api_key)

    return extract
