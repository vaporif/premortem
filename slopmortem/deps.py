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
from slopmortem.corpus.tavily import tavily_search_structured
from slopmortem.llm import OpenRouterClient, make_embedder

if TYPE_CHECKING:
    from slopmortem.config import Config
    from slopmortem.corpus import Corpus
    from slopmortem.llm import EmbeddingClient, LLMClient
    from slopmortem.stages import SparseEncoder
    from slopmortem.stages.recall_verify import TavilySearchFn


def build_deps(
    config: Config,
) -> tuple[LLMClient, EmbeddingClient, Corpus, Budget, SparseEncoder]:
    """Build production deps for the query pipeline: LLM, embedder, corpus, budget, sparse encoder.

    The sparse encoder is the same callable both ``retrieve`` and
    ``recall_persist`` need; threading it through ``build_deps`` keeps a
    single owner so the recall branch can't fire without one wired up.
    The fastembed model itself loads lazily on first call (see
    ``slopmortem.corpus._embed_sparse``), so this stays cheap at startup.
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
    """Return ``tavily_search_structured`` when recall search is enabled, else ``None``.

    ``tavily_search_structured`` reads ``TAVILY_API_KEY`` at call time, so the
    builder doesn't need to capture credentials. To disable recall entirely,
    set ``enable_tavily_recall_search=False`` — the L0 search head is
    mandatory under the new contract, so no Tavily means no recall.
    """
    if not config.enable_tavily_recall_search:
        return None
    return tavily_search_structured
