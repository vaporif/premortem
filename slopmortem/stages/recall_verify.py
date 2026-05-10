"""Verifier for LLM-recalled suggestions: gates L1-L5 before persistence.

The recall stage (``stages.llm_recall``) returns ``RecallSuggestion`` rows
the LLM thinks failed. None of them have been verified, and a fraction will
be hallucinated. This module gates each one through:

L1 — Pydantic schema (already enforced at ``RecallSuggestion`` parse time;
     no work here).
L2 — Liveness HEAD on both URLs. Drops suggestions whose homepage or
     citation host is dead, returns 4xx/5xx, or trips the SSRF guard.
L3 — Body GET on the evidence URL. Drops if the body doesn't contain both
     the company name AND a death/distress keyword (case-insensitive).
L4 — Wayback corroboration. Best-effort: a snapshot whose body still
     mentions the name promotes the suggestion to ``wayback_anchored``
     and replaces the body with the snapshot text (richer marketing copy
     wins for vector retrieval). Failure here never drops the suggestion.
L5 — Deathness judgment. L1-L4 only prove a URL exists, serves content,
     and mentions the name + a death-ish word. They don't tell apart "real
     dead company" from "real live company that had layoffs once". Haiku
     reads the verified body and answers ``died`` + ``confidence``. We
     drop on died=false, on confidence below threshold, and on any LLM
     transport/parse failure (conservative — false admits are worse than
     false drops in the recall fallback).

The verified ``RawEntry`` rides a ``VerificationTier`` sibling argument to
the persistence helper (Task 5). ``RawEntry`` itself is unchanged across
non-recall sources.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

import anyio
import httpx
from lmnr import Laminar, observe
from pydantic import BaseModel, Field, ValidationError

from slopmortem.concurrency import gather_resilient
from slopmortem.corpus import extract_clean
from slopmortem.corpus.sources._names import SOURCE_LLM_RECALL
from slopmortem.http import SSRFBlockedError, safe_get, safe_head
from slopmortem.llm import prompt_template_sha, render_blocks, to_strict_response_schema
from slopmortem.models import RawEntry, RecallSuggestion
from slopmortem.tracing import SpanEvent

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from slopmortem.corpus.sources import Enricher
    from slopmortem.llm import LLMClient


logger = logging.getLogger(__name__)


# 4xx/5xx threshold. Matches ``corpus.sources._throttle.HTTP_BAD_REQUEST`` but
# duplicated here to keep ``stages`` off the leaf-private throttle module
# (the import-linter contract forbids it).
_HTTP_BAD_REQUEST: Final = 400

# 40s budget per fetch. Citation hosts (court filings, archived blogs) are
# routinely slow on cold caches; HEAD and GET share the same budget so a
# host that's slow on HEAD would have timed out on the GET anyway.
_FETCH_TIMEOUT_S: Final = 40.0

# Default fan-out concurrency. archive.org rate-limits aggressively at >1 rps
# per host, and citation hosts are heterogeneous; 3 in flight stays under any
# single host's limit while keeping the wall clock reasonable.
_DEFAULT_CONCURRENCY: Final = 3

# Tuple, not frozenset: the order is irrelevant for membership but the regex
# build needs a stable longest-first sort so "Chapter 11" wins over a stray
# "Chapter" prefix and "shut down" wins over "shut". `acquired`/`acquisition`
# stay in — the false-positive cost is one Haiku call at L5; the false-negative
# would silently drop real fire-sale exits.
_DEATH_KEYWORDS: Final[tuple[str, ...]] = (
    # Terminal: the company is gone.
    "shutdown",
    "shut down",
    "shuttered",
    "closed",
    "ceased",
    "defunct",
    "dissolved",
    "bankrupt",
    "bankruptcy",
    "Chapter 11",
    "Chapter 7",
    "liquidation",
    "wound down",
    "wind-down",
    "wind down",
    "going out of business",
    "out of business",
    "obituary",
    "delisted",
    "cease operations",
    "acquired",
    "acquisition",
    # Distress: still operating but visibly hurting.
    "layoffs",
    "layoff",
    "restructuring",
    "struggling",
    "missed payroll",
    "downsizing",
    "troubled",
)


def _build_death_regex() -> re.Pattern[str]:
    r"""Compile one word-boundary regex covering every keyword.

    Longest-first sort avoids prefix shadowing (``shut`` swallowing
    ``shut down``). Spaces in multi-word entries become ``\s+`` so HTML
    whitespace runs and newlines still match.
    """
    parts = sorted(_DEATH_KEYWORDS, key=len, reverse=True)
    escaped = [re.escape(p).replace(r"\ ", r"\s+") for p in parts]
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.IGNORECASE)


_DEATH_REGEX: Final = _build_death_regex()

# Mirrors ``corpus._extract.LENGTH_FLOOR``: ``extract_clean`` already returns
# ``""`` below 500 chars, so the verifier rejecting at the same floor means
# we only admit bodies that produced a real article extraction.
_L3_MIN_BODY_CHARS: Final = 500


type VerificationTier = Literal["wayback_anchored", "evidence_only"]


def _recall_source_id(suggestion: RecallSuggestion) -> str:
    """Stable id keyed on (name, homepage_url) per the plan.

    Two suggestions for the same vendor (same homepage) collapse to one
    ``source_id`` regardless of which article cited them; a different
    homepage diverges. 16 hex chars is enough for the recall stage's
    per-pitch cap (~8 suggestions).
    """
    fingerprint = f"{suggestion.name}|{suggestion.homepage_url}"
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]


def _emit_event(event: SpanEvent, attributes: Mapping[str, str] | None = None) -> None:
    if Laminar.is_initialized():
        # Laminar's signature is invariant ``dict[str, AttributeValue]``; we
        # accept a covariant ``Mapping`` from callers and rebuild a dict here.
        Laminar.event(name=str(event), attributes=dict(attributes) if attributes else None)


# Tri-state so callers can tell name-missing from keyword-missing without
# re-scanning the body. ``"ok"`` means both anchors present.
type _AnchorResult = Literal["ok", "name_missing", "keyword_missing"]


def _body_anchors_name_and_death(name: str, body: str) -> _AnchorResult:
    if name.lower() not in body.lower():
        return "name_missing"
    if not _DEATH_REGEX.search(body):
        return "keyword_missing"
    return "ok"


def _log_and_emit_l3_rejection(
    anchor: _AnchorResult,
    *,
    name: str,
    evidence: str,
) -> None:
    """Log + emit the right L3 rejection event for the failing anchor."""
    if anchor == "name_missing":
        logger.info("recall_verify: L3 body lacks name anchor for %r at %s", name, evidence)
        _emit_event(SpanEvent.RECALL_REJECTED_L3_NAME_MISSING)
        return
    if anchor == "keyword_missing":
        logger.info("recall_verify: L3 body lacks death keyword for %r at %s", name, evidence)
        _emit_event(SpanEvent.RECALL_REJECTED_L3_KEYWORD_MISSING)


class _DeathnessJudgment(BaseModel):
    """L5 verdict: did the verified evidence body actually establish death.

    ``evidence_quote`` rides along for audit trails — it's not consumed by
    the gate, just preserved for diagnostics if a downstream operator wants
    to inspect why a suggestion was admitted or rejected.
    """

    died: bool
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_quote: str


# Mirrors ``stages._classify_phase``'s 8000-char cap on the slop-classifier
# prompt: Haiku doesn't need the whole article to call death-or-not, and
# capping keeps the L5 spend predictable per suggestion.
_L5_BODY_CHAR_BUDGET: Final = 8000


async def _l5_deathness_judgment(
    *,
    suggestion: RecallSuggestion,
    body: str,
    llm: LLMClient,
    model: str,
    max_tokens: int,
) -> _DeathnessJudgment | None:
    """Ask Haiku whether the body proves the company died.

    Returns ``None`` on transport or parse failure; ``verify_suggestion``
    treats that as a drop. False admits are worse than false drops here:
    a hallucinated suggestion that slips L1-L4 still has to defeat L5.
    """
    blocks = render_blocks(
        "recall_deathness",
        # ``company_name`` not ``name`` — ``render_blocks(name, ...)`` would collide.
        company_name=suggestion.name,
        status=suggestion.status,
        failure_year=suggestion.failure_year,
        body=body[:_L5_BODY_CHAR_BUDGET],
    )
    try:
        result = await llm.complete(
            blocks["user"],
            system=blocks["system"],
            model=model,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "DeathnessJudgment",
                    "schema": to_strict_response_schema(_DeathnessJudgment),
                    "strict": True,
                },
            },
            extra_body={"prompt_template_sha": prompt_template_sha("recall_deathness")},
            max_tokens=max_tokens,
        )
    # RuntimeError covers OpenRouter's hard-stop / null-tool-calls / unknown
    # finish-reason failures. BudgetExceededError extends Exception directly
    # so budget exhaustion still propagates up.
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.info("recall_verify: L5 LLM call failed: %r", exc)
        return None
    try:
        return _DeathnessJudgment.model_validate_json(result.text)
    except ValidationError as exc:
        logger.info("recall_verify: L5 invalid response: %r", exc)
        return None


async def _l5_admits(  # noqa: PLR0913 - mirrors the deathness knob set passed through from config
    *,
    suggestion: RecallSuggestion,
    body: str,
    llm: LLMClient,
    model: str,
    max_tokens: int,
    min_confidence: float,
) -> bool:
    """L5 verdict: ``True`` admits the suggestion, ``False`` drops it.

    Logging + span events live here so ``verify_suggestion`` keeps a single
    L5 branch and stays under the cyclomatic-complexity cap.
    """
    judgment = await _l5_deathness_judgment(
        suggestion=suggestion,
        body=body,
        llm=llm,
        model=model,
        max_tokens=max_tokens,
    )
    if judgment is None:
        _emit_event(SpanEvent.RECALL_REJECTED_L5_LOW_CONFIDENCE)
        return False
    if not judgment.died:
        logger.info(
            "recall_verify: L5 ruled %r not dead (confidence=%.2f)",
            suggestion.name,
            judgment.confidence,
        )
        _emit_event(SpanEvent.RECALL_REJECTED_L5_NOT_DEAD)
        return False
    if judgment.confidence < min_confidence:
        logger.info(
            "recall_verify: L5 confidence %.2f below threshold %.2f for %r",
            judgment.confidence,
            min_confidence,
            suggestion.name,
        )
        _emit_event(SpanEvent.RECALL_REJECTED_L5_LOW_CONFIDENCE)
        return False
    return True


async def _fetch_l3_body(*, name: str, evidence: str) -> str | None:
    """GET the evidence URL and return an extracted body, or ``None`` to drop.

    Folds the three L3 failure modes (transport, HTTP status, body too short
    or anchor missing) into one helper. Each rejection path emits its own
    span event so the audit dashboard keeps the granularity. Pulled out of
    ``verify_suggestion`` to stay under the cyclomatic-complexity cap.
    """
    # ``safe_get`` does NOT consult robots.txt (unlike Wayback's ``_fetch``).
    # Recall surfaces vendors whose own sites are robots-blocked or vanished;
    # the evidence URL is a third-party citation, so vendor robots policy
    # doesn't apply.
    try:
        evidence_resp = await safe_get(evidence, timeout=_FETCH_TIMEOUT_S)
    except (SSRFBlockedError, httpx.HTTPError) as exc:
        logger.info("recall_verify: L3 GET failed for %s: %r", evidence, exc)
        # GET-level failure on the evidence URL is functionally an L2 outcome:
        # the URL didn't deliver a body. Fold into RECALL_REJECTED_L2 with
        # ``stage="get"`` so the audit dashboard can still split HEAD-probe
        # failures from GET-body transport failures.
        _emit_event(SpanEvent.RECALL_REJECTED_L2, attributes={"stage": "get"})
        return None
    if evidence_resp.status_code >= _HTTP_BAD_REQUEST:
        logger.info("recall_verify: L3 GET %s for %s", evidence_resp.status_code, evidence)
        _emit_event(SpanEvent.RECALL_REJECTED_L2, attributes={"stage": "get"})
        return None
    # ``extract_clean`` strips HTML, runs trafilatura/readability, and returns
    # ``""`` below its 500-char floor. Treating empty as a hard reject means
    # paywalls, JS shells, and any page where main-article extraction failed
    # get dropped — no fallback to raw HTML, which would reintroduce the
    # sidebar-bleed and nav-link false positives the strip prevents.
    evidence_body = extract_clean(evidence_resp.text)
    if len(evidence_body) < _L3_MIN_BODY_CHARS:
        logger.info(
            "recall_verify: L3 body too short (%d chars) for %s", len(evidence_body), evidence
        )
        _emit_event(SpanEvent.RECALL_REJECTED_L3_BODY_TOO_SHORT)
        return None
    anchor = _body_anchors_name_and_death(name, evidence_body)
    if anchor != "ok":
        _log_and_emit_l3_rejection(anchor, name=name, evidence=evidence)
        return None
    return evidence_body


async def verify_suggestion(  # noqa: PLR0913 - L5 needs LLM + three knobs from config
    suggestion: RecallSuggestion,
    *,
    wayback: Enricher,
    llm: LLMClient,
    model_recall_deathness: str,
    max_tokens_recall_deathness: int,
    min_confidence: float,
) -> tuple[RawEntry, VerificationTier] | None:
    """Run L1-L5 against one suggestion. Returns ``None`` if any gate drops."""
    homepage = suggestion.homepage_url
    evidence = suggestion.evidence_url
    # L2: HEAD both URLs. Each emission carries ``stage="head"`` so the audit
    # dashboard can split HEAD-probe rejections from GET-stage transport
    # failures (which use ``stage="get"`` below).
    for url in (homepage, evidence):
        try:
            head_resp = await safe_head(url, timeout=_FETCH_TIMEOUT_S)
        except (SSRFBlockedError, httpx.HTTPError) as exc:
            logger.info("recall_verify: L2 HEAD failed for %s: %r", url, exc)
            _emit_event(SpanEvent.RECALL_REJECTED_L2, attributes={"stage": "head"})
            return None
        if head_resp.status_code >= _HTTP_BAD_REQUEST:
            logger.info("recall_verify: L2 HEAD %s for %s", head_resp.status_code, url)
            _emit_event(SpanEvent.RECALL_REJECTED_L2, attributes={"stage": "head"})
            return None
    evidence_body = await _fetch_l3_body(name=suggestion.name, evidence=evidence)
    if evidence_body is None:
        return None
    # L4: optional Wayback corroboration.
    tier: VerificationTier = "evidence_only"
    body = evidence_body
    # markdown_text=None AND raw_html=None matter — WaybackEnricher.enrich
    # short-circuits if either body is already populated.
    seed = RawEntry(
        source=SOURCE_LLM_RECALL,
        source_id=_recall_source_id(suggestion),
        url=homepage,
        markdown_text=None,
        raw_html=None,
        fetched_at=datetime.now(UTC),
    )
    try:
        enriched = await wayback.enrich(seed)
    except Exception as exc:  # noqa: BLE001 - L3 already passed; L4 is best-effort.
        logger.info("recall_verify: wayback corroboration failed: %r", exc)
        enriched = seed
    if enriched.markdown_text and suggestion.name.lower() in enriched.markdown_text.lower():
        tier = "wayback_anchored"
        # Wayback marketing copy beats the article body for vector search.
        body = enriched.markdown_text
        _emit_event(SpanEvent.RECALL_VERIFIED_WAYBACK_ANCHORED)
    else:
        _emit_event(SpanEvent.RECALL_VERIFIED_EVIDENCE_ONLY)
    # L5: does the body actually prove the company died? Run on the same
    # body L4 picked (Wayback marketing copy if anchored, else the evidence
    # article). Conservative on failure — false admits cost more than drops.
    if not await _l5_admits(
        suggestion=suggestion,
        body=body,
        llm=llm,
        model=model_recall_deathness,
        max_tokens=max_tokens_recall_deathness,
        min_confidence=min_confidence,
    ):
        return None
    # Body lands on markdown_text. _entry_summary_text already prefers
    # markdown_text over raw_html; leaving raw_html=None avoids a second
    # extract pass downstream.
    final = seed.model_copy(update={"markdown_text": body})
    return final, tier


# Decorator lives at the fan-out level so the trace gets one
# ``stage.recall_verify`` parent with N child spans, not N siblings with no
# parent. Suggestions, persist, wayback, and llm handles never go to span
# attrs: CLAUDE.md forbids prompt/response bodies in tracing, and the
# evidence body can be sizeable.
@observe(
    name="stage.recall_verify",
    ignore_inputs=["suggestions", "persist", "wayback", "llm"],
    ignore_output=True,
)
async def verify_and_persist_all(  # noqa: PLR0913 - leaf helper; deathness knobs flow through from pipeline
    suggestions: list[RecallSuggestion],
    *,
    wayback: Enricher,
    persist: Callable[[RawEntry, VerificationTier], Awaitable[None]],
    llm: LLMClient,
    model_recall_deathness: str,
    max_tokens_recall_deathness: int,
    min_confidence: float,
    concurrency: int = _DEFAULT_CONCURRENCY,
) -> list[RawEntry]:
    """Verify each suggestion under a capacity limiter; persist accepted entries.

    ``gather_resilient`` keeps a sibling failure from cancelling the rest —
    a transient outage on one citation host shouldn't poison the whole batch.
    Returned list contains only the entries that passed L1-L5 AND persisted
    cleanly; per-suggestion exceptions land in the logs instead.
    """
    limiter = anyio.CapacityLimiter(concurrency)

    async def _one(s: RecallSuggestion) -> RawEntry | None:
        async with limiter:
            verified = await verify_suggestion(
                s,
                wayback=wayback,
                llm=llm,
                model_recall_deathness=model_recall_deathness,
                max_tokens_recall_deathness=max_tokens_recall_deathness,
                min_confidence=min_confidence,
            )
        if verified is None:
            return None
        entry, tier = verified
        await persist(entry, tier)
        return entry

    results = await gather_resilient(*(_one(s) for s in suggestions))
    return [r for r in results if isinstance(r, RawEntry)]
