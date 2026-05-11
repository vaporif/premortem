"""Verifier for LLM-recalled suggestions: gates L0-L5 before persistence.

The recall stage (``stages.llm_recall``) returns ``RecallSuggestion`` rows
the LLM thinks failed. None of them have been verified, and a fraction will
be hallucinated. This module gates each one through:

L0 — Search head. One Tavily search per suggestion, shaped by ``status`` and
     ``failure_year``. Drops the suggestion when no hit's title or snippet
     mentions the company name — equivalent to "the article URL would have
     404'd anyway, save the L2 round-trip". The selected URL replaces what
     used to be ``suggestion.evidence_url`` and threads through L2-L5 as
     ``discovered_url``.
L1 — Pydantic schema (already enforced at ``RecallSuggestion`` parse time;
     no work here).
L2 — Liveness on the discovered URL only. HEAD->GET fallthrough: HEAD is the
     cheap first probe, but paywalled/anti-bot citations routinely 401/403/405
     on HEAD while serving GET. The GET status code is the authoritative gate.
     The homepage URL is provenance, not corroboration — its liveness isn't
     load-bearing here. On L2 4xx OR L3 body-too-short, the Tavily ``/extract``
     fallback fires once: its own IP pool/headless browser unblocks bot-blocked
     hosts (Medium) and SPA shells (decrypt.co Next.js). Anchor-missing
     rejections don't retry — extract won't change which words are in the body.
L3 — Body extraction on the GET response. Drops if the body doesn't contain
     both the company name AND a death/distress keyword (case-insensitive).
L4 — Wayback enrichment, advisory only. Short-circuits when
     ``suggestion.homepage_url is None`` — no Wayback round-trip happens and
     the tier stays ``evidence_only`` with ``wayback_attempted="false"`` on
     the verified event. When a homepage is present, a snapshot whose body
     still mentions the name promotes the suggestion to ``wayback_anchored``
     and appends the snapshot text (richer marketing copy wins for vector
     retrieval). Failure or empty result never drops the suggestion.
L5 — Deathness judgment. L1-L4 only prove a URL exists, serves content,
     and mentions the name + a death-ish word. They don't tell apart "real
     dead company" from "real live company that had layoffs once". Haiku
     reads the news article body (always — Wayback marketing copy never
     says "we died") and returns a tri-state ``verdict`` plus
     ``confidence``. We drop on verdict="alive", on confidence below the
     verdict-specific threshold, and on any LLM transport/parse failure
     (conservative — false admits are worse than false drops in the recall
     fallback). The admitted verdict ("dead" or "struggling") rides through
     the persistence chain to ``CandidatePayload.deathness_verdict`` so
     synthesis can weight terminal vs distress citations differently.

The verified ``RawEntry`` rides a ``VerificationTier`` sibling argument to
the persistence helper. ``RawEntry`` itself is unchanged across non-recall
sources.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, cast

import anyio
import httpx
import yaml
from lmnr import Laminar, observe
from pydantic import BaseModel, ValidationError, model_validator

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
    from slopmortem.corpus.tavily import TavilyHit
    from slopmortem.llm import LLMClient


# Callable shape of ``tavily_search_structured``: ``(query, limit) -> hits``.
# Defined as a top-level ``type`` alias so the pipeline can plumb whatever
# concrete (live Tavily, eval-mode fake, recording wrapper) it builds
# without recall_verify importing the corpus-leaf implementation.
type TavilySearchFn = Callable[[str, int], Awaitable[list[TavilyHit]]]


# Callable shape of ``tavily_extract_structured``: ``(url) -> raw_content``.
# Returns ``""`` when Tavily has no usable content. Used as L3 fallback when
# direct GET is bot-blocked (Medium 403) or returns a SPA shell (decrypt.co).
type ExtractFn = Callable[[str], Awaitable[str]]


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

# Word-boundary regex builds from the keyword list at module-load. Source
# of truth is ``death_keywords.yml`` (sibling file) — edit there to add or
# remove signals. Tuple, not frozenset: the regex builder needs a stable
# longest-first sort so "Chapter 11" wins over a stray "Chapter" prefix
# and "shut down" wins over "shut".
_DEATH_KEYWORDS_PATH: Final = Path(__file__).parent / "death_keywords.yml"


@cache
def _load_death_keywords() -> tuple[str, ...]:
    """Load death/distress keywords from the YAML sibling, flatten across groups."""
    raw = cast("dict[str, list[str]]", yaml.safe_load(_DEATH_KEYWORDS_PATH.read_text()))
    return tuple(kw for group in raw.values() for kw in group)


_DEATH_KEYWORDS: Final[tuple[str, ...]] = _load_death_keywords()


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
    """Stable id keyed on (name, homepage_url) when present, (name,) otherwise.

    Two suggestions for the same vendor (same homepage) collapse to one
    source_id. When the recall LLM didn't supply a homepage, fall back to
    name-only — across runs the same homepage-less vendor surfaces via
    different citation hosts, and a domain-keyed fallback would create
    duplicate Qdrant points. Name-only fallback risks merging two distinct
    startups with the same name, but that's vanishingly rare per pitch and
    alias_graph collapses obvious name collisions at persist time.
    """
    homepage = suggestion.homepage_url
    fingerprint = f"{suggestion.name}|{homepage}" if homepage is not None else suggestion.name
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


class _DeathnessJudgment(BaseModel):
    """L5 verdict: did the evidence body establish death, distress, or neither.

    ``evidence_quote`` rides along for audit trails — it's not consumed by
    the gate, just preserved for diagnostics if a downstream operator wants
    to inspect why a suggestion was admitted or rejected.

    ``confidence`` carries no field-level ``ge``/``le``: strict
    ``response_format`` mode (both OpenAI and Anthropic) rejects
    ``minimum``/``maximum`` on numeric schemas. The [0.0, 1.0] bound is
    enforced post-parse in ``_validate_confidence`` below.
    """

    verdict: Literal["dead", "struggling", "alive"]
    confidence: float
    evidence_quote: str

    @model_validator(mode="after")
    def _validate_confidence(self) -> _DeathnessJudgment:
        if not 0.0 <= self.confidence <= 1.0:
            msg = f"confidence {self.confidence} outside [0.0, 1.0]"
            raise ValueError(msg)
        return self


# Module-private alias: the bare ``Literal`` is what threads through the
# persist chain (CandidatePayload, _build_payload, _process_entry, _write_phase,
# persist_recall_entry, the persist callback) so the leaf alias doesn't leak
# across import boundaries.
type _AdmitVerdict = Literal["dead", "struggling"]


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


async def _l5_decide(  # noqa: PLR0913 - mirrors the deathness knob set passed through from config
    *,
    suggestion: RecallSuggestion,
    body: str,
    llm: LLMClient,
    model: str,
    max_tokens: int,
    min_confidence: float,
    struggling_min_confidence: float,
) -> _AdmitVerdict | None:
    """L5 verdict: returns the admitted verdict, or ``None`` to drop.

    ``"alive"`` and any below-threshold or transport/parse failure all map
    to ``None``. Logging + span events live here so ``verify_suggestion``
    keeps a single L5 branch and stays under the cyclomatic-complexity cap.
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
        return None
    if judgment.verdict == "alive":
        logger.info(
            "recall_verify: L5 ruled %r alive (confidence=%.2f)",
            suggestion.name,
            judgment.confidence,
        )
        _emit_event(SpanEvent.RECALL_REJECTED_L5_ALIVE)
        return None
    threshold = min_confidence if judgment.verdict == "dead" else struggling_min_confidence
    if judgment.confidence < threshold:
        logger.info(
            "recall_verify: L5 %s confidence %.2f below threshold %.2f for %r",
            judgment.verdict,
            judgment.confidence,
            threshold,
            suggestion.name,
        )
        _emit_event(SpanEvent.RECALL_REJECTED_L5_LOW_CONFIDENCE)
        return None
    logger.info(
        "recall_verify: L5 %s confidence %.2f admitted %r (quote=%r)",
        judgment.verdict,
        judgment.confidence,
        suggestion.name,
        judgment.evidence_quote[:120],
    )
    return judgment.verdict


def _combine_recall_body(
    *,
    wayback_body: str | None,
    evidence_url: str,
    news_body: str,
    suggestion: RecallSuggestion,
) -> str:
    """Compose the persisted body from the news article and the Wayback snapshot.

    The news article is the citation L5 verifies against and is therefore
    always present; Wayback contributes a "Vendor description (archived)"
    section only when it anchored. Section markers preserve semantic
    boundaries for the chunker so news content and snapshot content don't
    blur into a single chunk.

    The status/year line is labeled "LLM-suggested" because it comes from
    the Sonnet recall payload, not the news body — downstream synthesis
    should treat it as a hint, not a fact.
    """
    parts: list[str] = []
    if wayback_body:
        parts.append(f"# Vendor description (archived)\n\n{wayback_body}")
    status_line = f"Status (LLM-suggested): {suggestion.status} ({suggestion.failure_year})"
    citation = f"# Failure citation\n\nSource: {evidence_url}\n{status_line}\n\n{news_body}"
    parts.append(citation)
    return "\n\n---\n\n".join(parts)


# Outcome discriminator for ``_l3_classify``. ``body_too_short`` is the
# recoverable case (Tavily extract may render the article fully); the two
# anchor failures are not — extract returns the same words.
type _L3Outcome = Literal["ok", "body_too_short", "name_missing", "keyword_missing"]


def _l3_classify(*, name: str, body_text: str) -> tuple[_L3Outcome, str]:
    """Extract clean text and classify against the 500-char floor + anchor checks.

    Returns ``(outcome, cleaned_body)``. ``cleaned_body`` is the trafilatura
    output (possibly empty on ``body_too_short``). The caller decides whether
    to retry via Tavily extract — only ``body_too_short`` is recoverable.

    ``extract_clean`` strips HTML, runs trafilatura/readability, and returns
    ``""`` below its 500-char floor. Treating empty as a hard reject means
    paywalls, JS shells, and any page where main-article extraction failed
    fall through to the extract fallback rather than fabricating a body from
    raw HTML.
    """
    cleaned = extract_clean(body_text)
    if len(cleaned) < _L3_MIN_BODY_CHARS:
        return "body_too_short", cleaned
    anchor = _body_anchors_name_and_death(name, cleaned)
    if anchor == "name_missing":
        return "name_missing", cleaned
    if anchor == "keyword_missing":
        return "keyword_missing", cleaned
    return "ok", cleaned


def _emit_l3_rejection(outcome: _L3Outcome, *, name: str, evidence: str, body_len: int) -> None:
    """Emit the right L3 rejection event for a non-``ok`` outcome."""
    if outcome == "body_too_short":
        logger.info("recall_verify: L3 body too short (%d chars) for %s", body_len, evidence)
        _emit_event(SpanEvent.RECALL_REJECTED_L3_BODY_TOO_SHORT)
        return
    if outcome == "name_missing":
        logger.info("recall_verify: L3 body lacks name anchor for %r at %s", name, evidence)
        _emit_event(SpanEvent.RECALL_REJECTED_L3_NAME_MISSING)
        return
    if outcome == "keyword_missing":
        logger.info("recall_verify: L3 body lacks death keyword for %r at %s", name, evidence)
        _emit_event(SpanEvent.RECALL_REJECTED_L3_KEYWORD_MISSING)


async def _try_extract_fallback(
    *,
    name: str,
    evidence: str,
    extract: ExtractFn,
    fallback_reason: Literal["l2_get_4xx", "l3_body_too_short"],
) -> str | None:
    """Run the Tavily ``/extract`` fallback; return the cleaned body on success.

    Returns ``None`` when extract raised, returned no content, or returned a
    body that still fails ``_l3_classify``. Only emits the recovery event on
    full success. Anchor-missing outcomes from the fallback body don't retry
    again — extract won't change which words are in the article.
    """
    try:
        raw = await extract(evidence)
    except (httpx.HTTPError, SSRFBlockedError, RuntimeError) as exc:
        logger.info(
            "recall_verify: L3 extract fallback transport failure for %s: %r", evidence, exc
        )
        return None
    if not raw:
        return None
    outcome, cleaned = _l3_classify(name=name, body_text=raw)
    if outcome != "ok":
        # Don't re-emit a fresh L3 rejection — the caller already emitted the
        # original drop event. The fallback simply failed to recover.
        return None
    _emit_event(
        SpanEvent.RECALL_L3_EXTRACT_FALLBACK_RECOVERED,
        attributes={"reason": fallback_reason},
    )
    return cleaned


def _hit_mentions(hit: TavilyHit, needle: str) -> bool:
    """Case-insensitive substring check across title and snippet.

    Title-or-snippet (not snippet alone): Tavily snippets cap at ~150-200
    chars and routinely drop the company name even when the article body
    is on-topic. The title carries the editorial framing and almost always
    names the subject.
    """
    needle_lower = needle.lower()
    return needle_lower in hit.title.lower() or needle_lower in hit.snippet.lower()


def _has_death_keyword(text: str) -> bool:
    """Reuse the L3 death-keyword regex on title+snippet to rank primary vs fallback."""
    return _DEATH_REGEX.search(text) is not None


async def _search_for_evidence(
    suggestion: RecallSuggestion,
    *,
    tavily_search: TavilySearchFn,
    limit: int,
) -> str | None:
    """L0: ask Tavily for a citation URL. Return None to drop.

    Status-shaped query. Prose syntax (the Task 0 winner). Selection:
    primary = first hit whose title-or-snippet contains the name AND a
    death keyword from ``_DEATH_KEYWORDS``; fallback = first hit whose
    title-or-snippet contains the name. ``None`` means the article URL
    would have 404'd anyway, save the L2 round-trip.
    """
    match suggestion.status:
        case "dead" | "absorbed":
            q = (
                f'"{suggestion.name}" shutdown or closed or bankrupt or "Chapter 11" '
                f"{suggestion.failure_year}"
            )
        case "struggling" | "bruised":
            q = (
                f'"{suggestion.name}" layoffs or restructuring or struggling '
                f"{suggestion.failure_year}"
            )
    try:
        hits = await tavily_search(q, limit)
    except (httpx.HTTPError, SSRFBlockedError) as exc:
        logger.info("recall_verify: L0 Tavily transport failure for %r: %r", suggestion.name, exc)
        _emit_event(SpanEvent.RECALL_REJECTED_NO_EVIDENCE, attributes={"reason": "transport_error"})
        return None
    if not hits:
        logger.info("recall_verify: L0 dropped %r — no_hits", suggestion.name)
        _emit_event(SpanEvent.RECALL_REJECTED_NO_EVIDENCE, attributes={"reason": "no_hits"})
        return None
    primary: TavilyHit | None = None
    fallback: TavilyHit | None = None
    for hit in hits:
        if not _hit_mentions(hit, suggestion.name):
            continue
        if fallback is None:
            fallback = hit
        if _has_death_keyword(f"{hit.title} {hit.snippet}"):
            primary = hit
            break
    chosen = primary or fallback
    if chosen is None:
        logger.info("recall_verify: L0 dropped %r — no_name_match", suggestion.name)
        _emit_event(SpanEvent.RECALL_REJECTED_NO_EVIDENCE, attributes={"reason": "no_name_match"})
        return None
    return chosen.url


async def _run_l4_wayback(
    *,
    suggestion: RecallSuggestion,
    seed: RawEntry,
    wayback: Enricher,
) -> tuple[VerificationTier, str | None]:
    """L4: enrich via Wayback when a homepage is present. Emit the verified event.

    Returns ``(tier, wayback_body)``: the body is non-None only when the
    snapshot anchored on the company name. Skipped entirely when
    ``suggestion.homepage_url is None`` — the news URL would archive an
    unrelated article, so we record ``wayback_attempted="false"`` and move
    on.
    """
    if suggestion.homepage_url is None:
        _emit_event(
            SpanEvent.RECALL_VERIFIED_EVIDENCE_ONLY,
            attributes={"wayback_attempted": "false"},
        )
        return "evidence_only", None
    try:
        enriched = await wayback.enrich(seed)
    except (httpx.HTTPError, RuntimeError) as exc:
        # Real WaybackEnricher catches these internally. Logged so a future
        # Wayback change that starts surfacing exceptions doesn't go unnoticed.
        logger.info("recall_verify: wayback raised (unexpected — enricher should swallow): %r", exc)
        enriched = seed
    snapshot_body = enriched.markdown_text
    if snapshot_body and suggestion.name.lower() in snapshot_body.lower():
        _emit_event(SpanEvent.RECALL_VERIFIED_WAYBACK_ANCHORED)
        return "wayback_anchored", snapshot_body
    _emit_event(
        SpanEvent.RECALL_VERIFIED_EVIDENCE_ONLY,
        attributes={"wayback_attempted": "true"},
    )
    return "evidence_only", None


async def _l2_head_failed(evidence: str) -> bool:
    """Probe ``evidence`` via HEAD; return True if HEAD failed or returned 4xx/5xx.

    HEAD is the cheap first probe but routinely 401/403/405's on paywalled or
    anti-bot citation hosts even when GET works — so we never drop on HEAD,
    we only record whether it succeeded so the eventual drop event carries
    the right attribute.
    """
    try:
        head_resp = await safe_head(evidence, timeout=_FETCH_TIMEOUT_S)
    except (SSRFBlockedError, httpx.HTTPError) as exc:
        logger.info(
            "recall_verify: L2 HEAD failed for %s, falling through to GET: %r", evidence, exc
        )
        return True
    if head_resp.status_code >= _HTTP_BAD_REQUEST:
        logger.info(
            "recall_verify: L2 HEAD %s for %s, falling through to GET",
            head_resp.status_code,
            evidence,
        )
        return True
    return False


async def _l2_get_body(evidence: str) -> str | None:
    """GET ``evidence`` and return the body text. ``None`` if GET 4xx'd or raised.

    ``safe_get`` does NOT consult robots.txt (unlike Wayback's ``_fetch``).
    The evidence URL is a third-party citation, so vendor robots policy doesn't apply.
    """
    try:
        resp = await safe_get(evidence, timeout=_FETCH_TIMEOUT_S)
    except (SSRFBlockedError, httpx.HTTPError) as exc:
        logger.info("recall_verify: L3 GET failed for %s: %r", evidence, exc)
        return None
    if resp.status_code >= _HTTP_BAD_REQUEST:
        logger.info("recall_verify: L3 GET %s for %s", resp.status_code, evidence)
        return None
    return resp.text


async def _l2_l3_fetch_body(
    *,
    name: str,
    evidence: str,
    extract: ExtractFn,
) -> str | None:
    """Run the L2 HEAD->GET ladder + L3 classify; return the cleaned body or ``None``.

    On L2 GET 4xx/transport failure, fall back to Tavily ``/extract`` once.
    On L3 ``body_too_short``, fall back to Tavily ``/extract`` once. Anchor
    failures don't retry — extract returns the same words.
    """
    head_failed = await _l2_head_failed(evidence)
    body_text = await _l2_get_body(evidence)
    if body_text is None:
        # L2 4xx / transport failure: try the extract fallback before dropping.
        recovered = await _try_extract_fallback(
            name=name, evidence=evidence, extract=extract, fallback_reason="l2_get_4xx"
        )
        if recovered is not None:
            return recovered
        _emit_event(
            SpanEvent.RECALL_REJECTED_L2,
            attributes={"stage": "get", "head_failed": str(head_failed)},
        )
        return None
    outcome, cleaned = _l3_classify(name=name, body_text=body_text)
    if outcome == "ok":
        return cleaned
    # Emit the original rejection event first so traces always carry the
    # primary drop reason even when the fallback also fails.
    _emit_l3_rejection(outcome, name=name, evidence=evidence, body_len=len(cleaned))
    if outcome != "body_too_short":
        # name_missing / keyword_missing: extract returns the same words. No retry.
        return None
    return await _try_extract_fallback(
        name=name, evidence=evidence, extract=extract, fallback_reason="l3_body_too_short"
    )


async def verify_suggestion(  # noqa: PLR0913 - L5 needs LLM + four knobs from config
    suggestion: RecallSuggestion,
    *,
    discovered_url: str,
    wayback: Enricher,
    llm: LLMClient,
    extract: ExtractFn,
    model_recall_deathness: str,
    max_tokens_recall_deathness: int,
    min_confidence: float,
    struggling_min_confidence: float,
) -> tuple[RawEntry, VerificationTier, _AdmitVerdict] | None:
    """Run L1-L5 against one suggestion. Returns ``None`` if any gate drops.

    ``discovered_url`` is the L0 Tavily output — the article URL that the
    L2/L3 ladder probes. ``extract`` is the Tavily ``/extract`` fallback fired
    once when direct GET 4xx's or the body is too short to admit. ``RawEntry.url``
    falls back to the discovered URL when ``suggestion.homepage_url`` is None so
    the persisted point always has provenance.
    """
    homepage = suggestion.homepage_url
    evidence = discovered_url
    seed_url = homepage if homepage is not None else discovered_url
    evidence_body = await _l2_l3_fetch_body(
        name=suggestion.name, evidence=evidence, extract=extract
    )
    if evidence_body is None:
        return None
    # markdown_text=None AND raw_html=None matter — WaybackEnricher.enrich
    # short-circuits if either body is already populated, so the seed has to
    # arrive empty for L4 to do its enrichment pass.
    seed = RawEntry(
        source=SOURCE_LLM_RECALL,
        source_id=_recall_source_id(suggestion),
        url=seed_url,
        markdown_text=None,
        raw_html=None,
        fetched_at=datetime.now(UTC),
    )
    tier, wayback_body = await _run_l4_wayback(suggestion=suggestion, seed=seed, wayback=wayback)
    # L5 reads the news article only — Wayback marketing copy never says
    # "we died", so it's the wrong substrate for the deathness judgment.
    # Conservative on failure: false admits cost more than drops.
    verdict = await _l5_decide(
        suggestion=suggestion,
        body=evidence_body,
        llm=llm,
        model=model_recall_deathness,
        max_tokens=max_tokens_recall_deathness,
        min_confidence=min_confidence,
        struggling_min_confidence=struggling_min_confidence,
    )
    if verdict is None:
        return None
    logger.info(
        "recall_verify: admitted %r tier=%s verdict=%s evidence=%s",
        suggestion.name,
        tier,
        verdict,
        evidence,
    )
    # Persisted body: news article (always) + Wayback snapshot (when anchored).
    # Synthesizer reads both the value-prop and the death narrative from the
    # same Qdrant entry; verifier and synthesizer agree on which document
    # represents this vendor.
    combined = _combine_recall_body(
        wayback_body=wayback_body,
        evidence_url=evidence,
        news_body=evidence_body,
        suggestion=suggestion,
    )
    final = seed.model_copy(update={"markdown_text": combined})
    return final, tier, verdict


# Decorator lives at the fan-out level so the trace gets one
# ``stage.recall_verify`` parent with N child spans, not N siblings with no
# parent. Suggestions, persist, wayback, and llm handles never go to span
# attrs: CLAUDE.md forbids prompt/response bodies in tracing, and the
# evidence body can be sizeable.
@observe(
    name="stage.recall_verify",
    ignore_inputs=["suggestions", "persist", "wayback", "llm", "tavily_search", "extract"],
    ignore_output=True,
)
async def verify_and_persist_all(  # noqa: PLR0913 - leaf helper; deathness knobs flow through from pipeline
    suggestions: list[RecallSuggestion],
    *,
    wayback: Enricher,
    persist: Callable[[RawEntry, VerificationTier, Literal["dead", "struggling"]], Awaitable[None]],
    llm: LLMClient,
    tavily_search: TavilySearchFn,
    extract: ExtractFn,
    tavily_recall_max_results: int,
    model_recall_deathness: str,
    max_tokens_recall_deathness: int,
    min_confidence: float,
    struggling_min_confidence: float,
    concurrency: int = _DEFAULT_CONCURRENCY,
) -> list[RawEntry]:
    """Verify each suggestion under a capacity limiter; persist accepted entries.

    ``gather_resilient`` keeps a sibling failure from cancelling the rest —
    a transient outage on one citation host shouldn't poison the whole batch.
    Returned list contains only the entries that passed L0-L5 AND persisted
    cleanly; per-suggestion exceptions land in the logs instead.
    """
    limiter = anyio.CapacityLimiter(concurrency)

    async def _one(s: RecallSuggestion) -> RawEntry | None:
        async with limiter:
            discovered = await _search_for_evidence(
                s, tavily_search=tavily_search, limit=tavily_recall_max_results
            )
            if discovered is None:
                return None
            verified = await verify_suggestion(
                s,
                discovered_url=discovered,
                wayback=wayback,
                llm=llm,
                extract=extract,
                model_recall_deathness=model_recall_deathness,
                max_tokens_recall_deathness=max_tokens_recall_deathness,
                min_confidence=min_confidence,
                struggling_min_confidence=struggling_min_confidence,
            )
        if verified is None:
            return None
        entry, tier, verdict = verified
        await persist(entry, tier, verdict)
        return entry

    results = await gather_resilient(*(_one(s) for s in suggestions))
    return [r for r in results if isinstance(r, RawEntry)]
