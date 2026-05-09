"""Closed enum of span event names the tracer emits for security and health monitoring."""

from __future__ import annotations

from enum import StrEnum


class SpanEvent(StrEnum):
    """Security- and health-relevant events written as Laminar span attributes."""

    PROMPT_INJECTION_ATTEMPTED = "prompt_injection_attempted"
    TOOL_ALLOWLIST_VIOLATION = "tool_allowlist_violation"
    PARENT_SUBSIDIARY_SUSPECTED = "entity.parent_subsidiary_suspected"
    CUSTOM_ALIAS_SUSPECTED = "entity.custom_alias_suspected"
    CORPUS_POISONING_WARNING = "corpus.poisoning_warning"
    CORPUS_DOC_TRUNCATED = "corpus.doc_truncated"
    BUDGET_EXCEEDED = "budget_exceeded"
    CACHE_WARM_FAILED = "cache_warm_failed"
    SSRF_BLOCKED = "ssrf_blocked"
    RESOLVER_FLIP_DETECTED = "resolver_flip_detected"
    CACHE_READ_RATIO_LOW = "cache_read_ratio_low"
    SLOP_QUARANTINED = "slop_quarantined"
    SOURCE_FETCH_FAILED = "source_fetch_failed"
    INGEST_ENTRY_FAILED = "ingest_entry_failed"
    INGEST_ENTRY_EMPTY_CHUNKS = "ingest_entry_empty_chunks"
    RECONCILE_REPAIR_APPLIED = "reconcile_repair_applied"
    # Recall fallback: trigger and verifier outcomes for cost/audit attribution.
    # GATE_FIRED is predicate-driven only (coverage_gap=True); force_llm_recall
    # runs that bypass the predicate intentionally do NOT emit it.
    RECALL_GATE_FIRED = "recall.gate_fired"
    # GAP_SCORE fires on every query for predicate calibration. Carries
    # qualifying/required counts and pitch sector as stringly-typed attributes
    # so eval can sweep thresholds without re-running queries.
    RECALL_GAP_SCORE = "recall.gap_score"
    RECALL_SUGGESTIONS_RECEIVED = "recall.suggestions_received"
    # Carries ``stage`` attribute: "head" (HEAD probe) or "get" (GET body fetch).
    RECALL_REJECTED_L2 = "recall.rejected_l2"
    RECALL_REJECTED_L3_NAME_MISSING = "recall.rejected_l3_name"
    RECALL_REJECTED_L3_KEYWORD_MISSING = "recall.rejected_l3_kw"
    RECALL_VERIFIED_WAYBACK_ANCHORED = "recall.verified_wayback"
    RECALL_VERIFIED_EVIDENCE_ONLY = "recall.verified_evidence"
    RECALL_PERSISTED = "recall.persisted"
    # L5 deathness gate (Haiku judges whether the verified body actually
    # establishes the company died): NOT_DEAD on died=false, LOW_CONFIDENCE
    # on died=true below threshold OR on transport/parse failure.
    RECALL_REJECTED_L5_NOT_DEAD = "recall.rejected_l5_not_dead"
    RECALL_REJECTED_L5_LOW_CONFIDENCE = "recall.rejected_l5_low_confidence"
