"""Pipeline stages: facet_extract, retrieve, llm_rerank, synthesize, consolidate_risks."""

from __future__ import annotations

from slopmortem.stages.consolidate_risks import consolidate_risks as consolidate_risks
from slopmortem.stages.facet_extract import extract_facets as extract_facets
from slopmortem.stages.llm_recall import detect_coverage_gap as detect_coverage_gap
from slopmortem.stages.llm_rerank import (
    llm_rerank as llm_rerank,
)
from slopmortem.stages.llm_rerank import (
    select_top_n_by_similarity as select_top_n_by_similarity,
)
from slopmortem.stages.retrieve import (
    SparseEncoder as SparseEncoder,
)
from slopmortem.stages.retrieve import (
    retrieve as retrieve,
)
from slopmortem.stages.synthesize import (
    drop_below_min_similarity as drop_below_min_similarity,
)
from slopmortem.stages.synthesize import (
    synthesize as synthesize,
)
from slopmortem.stages.synthesize import (
    synthesize_all as synthesize_all,
)
from slopmortem.stages.synthesize import (
    synthesize_prompt_kwargs as synthesize_prompt_kwargs,
)

__all__ = [
    "SparseEncoder",
    "consolidate_risks",
    "detect_coverage_gap",
    "drop_below_min_similarity",
    "extract_facets",
    "llm_rerank",
    "retrieve",
    "select_top_n_by_similarity",
    "synthesize",
    "synthesize_all",
    "synthesize_prompt_kwargs",
]
