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
    # GAP_SCORE fires every query so eval can sweep predicate thresholds
    # against historical traces. Attributes: qualifying, required, pitch_sector.
    RECALL_GAP_SCORE = "recall.gap_score"
    # Second pass: same shape as GAP_SCORE but fires after the recall branch
    # re-retrieves + re-reranks. The extra ``gap_closed`` attribute lets a
    # join-on-trace query answer "did recall close the gap" without subtracting
    # before/after qualifying counts.
    RECALL_GAP_SCORE_AFTER = "recall.gap_score_after"
    RECALL_SUGGESTIONS_RECEIVED = "recall.suggestions_received"
    # L0 search head drop. Different from L2/L3 (URL fetched, body parsed but
    # failed anchor check) — this fires before any HTTP hits the citation host.
    # Carries ``reason`` attribute: "no_hits", "no_name_match", or
    # "transport_error".
    RECALL_REJECTED_NO_EVIDENCE = "recall.rejected_no_evidence"
    # L0 status-shaped query returned no name match; the status-blind retry
    # (`"<name>" <category> <year>`) found a name-matching hit. Recorded so
    # the trace dashboard can answer "how often is Opus's status guess wrong?"
    # — the count is a direct lower bound on L0 false-precision.
    RECALL_L0_NAME_ONLY_FALLBACK_RECOVERED = "recall.l0_name_only_fallback_recovered"
    # Opus pre-discovered the citation URL during its own tavily_search loop;
    # the verifier's L0 was skipped. L2-L5 still validated. Useful to measure
    # how often the recall-LLM-side search saves a verifier-side search.
    RECALL_L0_PROVIDED_BY_RECALL_LLM = "recall.l0_provided_by_recall_llm"
    # Carries ``stage`` attribute: "head" (HEAD probe) or "get" (GET body fetch).
    RECALL_REJECTED_L2 = "recall.rejected_l2"
    RECALL_REJECTED_L3_NAME_MISSING = "recall.rejected_l3_name"
    RECALL_REJECTED_L3_KEYWORD_MISSING = "recall.rejected_l3_kw"
    RECALL_REJECTED_L3_BODY_TOO_SHORT = "recall.rejected_l3_body_too_short"
    # L3 extract fallback fired and a Tavily ``/extract`` call recovered a body
    # that direct fetch couldn't get (Medium 403, decrypt.co SPA shell, etc.).
    # Attributes: ``reason`` ("l2_get_4xx" or "l3_body_too_short").
    RECALL_L3_EXTRACT_FALLBACK_RECOVERED = "recall.l3_extract_fallback_recovered"
    RECALL_VERIFIED_WAYBACK_ANCHORED = "recall.verified_wayback"
    RECALL_VERIFIED_EVIDENCE_ONLY = "recall.verified_evidence"
    RECALL_PERSISTED = "recall.persisted"
    # L5 deathness gate (Haiku judges whether the verified body establishes
    # death, distress, or neither): ALIVE on verdict=alive, LOW_CONFIDENCE
    # on dead/struggling below the matching threshold OR on transport/parse
    # failure.
    RECALL_REJECTED_L5_ALIVE = "recall.rejected_l5_alive"
    RECALL_REJECTED_L5_LOW_CONFIDENCE = "recall.rejected_l5_low_confidence"
    # Carries ``slop_score`` and ``effective_threshold`` attributes. Fires only
    # for llm_recall entries scoring within 0.4 ≤ score < threshold so future
    # tuning has the data to retune ``recall_slop_threshold`` without
    # re-classifying the corpus.
    RECALL_SLOP_BORDERLINE = "recall.slop_borderline"
    # Fires when the three-tier resolver merges an ``llm_recall`` row into an
    # existing canonical via ``alias_blocked`` — pure observability so the
    # audit dashboard can count how often recall surfaces something the
    # corpus already has. ``resolver_flipped`` is intentionally not covered
    # here; ``RESOLVER_FLIP_DETECTED`` already fires on that path.
    RECALL_DEDUPED_EXISTING = "recall.deduped_existing"
