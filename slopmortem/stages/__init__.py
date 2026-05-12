"""Pipeline stages: facet_extract, retrieve, llm_rerank, synthesize, consolidate_risks."""

from __future__ import annotations

from slopmortem.stages.consolidate_risks import consolidate_risks as consolidate_risks
from slopmortem.stages.facet_extract import extract_facets as extract_facets
from slopmortem.stages.llm_recall import compute_coverage_gap as compute_coverage_gap
from slopmortem.stages.llm_recall import llm_recall as llm_recall
from slopmortem.stages.llm_rerank import (
    llm_rerank as llm_rerank,
)
from slopmortem.stages.llm_rerank import (
    select_top_n_by_similarity as select_top_n_by_similarity,
)
from slopmortem.stages.recall_persist import persist_recall_entry as persist_recall_entry
from slopmortem.stages.recall_verify import (
    VerificationTier as VerificationTier,
)
from slopmortem.stages.recall_verify import (
    verify_and_persist_all as verify_and_persist_all,
)
from slopmortem.stages.recall_verify import (
    verify_suggestion as verify_suggestion,
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
    "VerificationTier",
    "compute_coverage_gap",
    "consolidate_risks",
    "drop_below_min_similarity",
    "extract_facets",
    "llm_recall",
    "llm_rerank",
    "persist_recall_entry",
    "retrieve",
    "select_top_n_by_similarity",
    "synthesize",
    "synthesize_all",
    "synthesize_prompt_kwargs",
    "verify_and_persist_all",
    "verify_suggestion",
]
