"""Record types for the recall subsystem.

``VerifiedEntry`` is re-exported from ``_verify`` so the package surface
has a single import site for records the public ``recall()`` returns.
``RecallDeps`` / ``RecallConfig`` exist here so the recall package never
imports ``slopmortem.config`` — the pipeline builds these from the global
``Config`` at the call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from slopmortem.recall._verify import (
    VerifiedEntry as VerifiedEntry,  # noqa: PLC0414 - explicit re-export so the package surface has a single import site
)

if TYPE_CHECKING:
    from slopmortem.corpus.sources import Enricher
    from slopmortem.llm import LLMClient
    from slopmortem.models import ToolSpec
    from slopmortem.recall._verify import DeathnessConfig, ExtractFn, TavilySearchFn


@dataclass(frozen=True)
class RecallDeps:
    """Runtime deps recall cannot default from ``RecallConfig``.

    ``wayback=None`` lets eval / CLI / replay callers skip the
    ``WaybackEnricher()`` construction; ``recall()`` lazy-defaults it.
    """

    llm: LLMClient
    tavily_search: TavilySearchFn
    extract: ExtractFn
    wayback: Enricher | None = None


@dataclass(frozen=True)
class RecallConfig:
    """All knobs the recall subsystem reads.

    Names are local to this record — the pipeline-side ``_recall_config_from``
    maps from the global ``Config``. Do NOT add ``suggestion_cap`` or
    ``max_tavily_calls`` to global ``Config``; they exist only here.

    ``tools=[]`` is the canonical "tools disabled" state — neither
    ``enable_tavily_recall_search`` nor ``recall_max_tavily_calls`` survives
    as a separate field on this record. Build ``tools`` via
    ``slopmortem.llm.recall_tools(config)`` at the call site.

    ``model_facet`` and ``max_tokens_facet`` are consumed **only** when
    ``recall()`` is called with ``facets=None`` (eval / CLI / replay).
    The pipeline hot path always passes pre-extracted facets, so these
    fields are dead weight on production traffic. Don't prune them — the
    standalone ``facets=None`` branch is the package's external surface.
    """

    model_facet: str
    max_tokens_facet: int
    model_recall: str
    max_tokens_recall: int
    suggestion_cap: int
    tools: list[ToolSpec]
    max_tavily_calls: int
    tavily_max_results: int
    deathness: DeathnessConfig
