"""Verifier for LLM-recalled suggestions: gates L0-L5 before persistence.

``recall._brainstorm`` returns ``RecallSuggestion`` rows the LLM thinks failed.
None are verified yet, and a fraction will be hallucinated. This module gates
each through:

L0 — Search head. Up to two Tavily searches per suggestion (status-shaped
     pass 1; status-blind pass 2 only when pass 1 found no death-keyword hit)
     and ranks the pooled hits into up to ``_MAX_CANDIDATE_URLS`` candidates.
     Drops when nothing mentions the company name. The ranked list becomes
     ``discovered_urls`` and threads through L2-L5.
L1 — Pydantic schema, enforced at ``RecallSuggestion`` parse time. No-op here.
L2 — Liveness on each candidate URL until one passes. HEAD→GET fallthrough:
     HEAD is cheap, but paywalled/anti-bot hosts routinely 401/403/405 on HEAD
     while serving GET. The GET status is authoritative. The homepage URL is
     provenance, not corroboration. On L2 4xx OR L3 body-too-short, the Tavily
     ``/extract`` fallback fires once per URL. Anchor-missing rejections don't
     retry: extract won't change which words are in the body.
L3 — Body extraction on the GET response. Drops if the body lacks both the
     company name AND a death/distress keyword (case-insensitive).
L4 — Wayback enrichment, advisory only. Skipped when ``homepage_url`` is
     None; the tier stays ``evidence_only`` with ``wayback_attempted="false"``.
     A snapshot whose body mentions the name promotes the tier to
     ``wayback_anchored`` and appends the snapshot text. Failure or empty
     result never drops the suggestion.
L5 — Deathness judgment. L1-L4 only prove a URL exists, serves content, and
     mentions the name plus a death-ish word; they don't separate "dead
     company" from "live company that had layoffs once". Haiku reads the
     news body (Wayback marketing copy never says "we died") and returns a
     tri-state ``verdict`` plus ``confidence``. Drop on verdict="alive",
     below-threshold confidence, or any LLM transport/parse failure
     (conservative: false admits cost more than false drops here). The
     admitted verdict rides through to ``CandidatePayload.deathness_verdict``
     so synthesis weights terminal vs distress citations differently.

The verified ``RawEntry`` carries a ``VerificationTier`` sibling argument to
the persistence helper. ``RawEntry`` itself is unchanged across non-recall
sources.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, cast
from urllib.parse import urlparse

import anyio
import httpx
import yaml
from lmnr import Laminar, observe
from pydantic import BaseModel, ValidationError, model_validator

from slopmortem.concurrency import gather_resilient
from slopmortem.corpus import extract_clean
from slopmortem.corpus.sources import SOURCE_LLM_RECALL
from slopmortem.http import SSRFBlockedError, safe_get, safe_head
from slopmortem.llm import (
    OpenRouterCompletionError,
    prompt_template_sha,
    render_blocks,
    to_strict_response_schema,
)
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
def _load_death_groups() -> dict[str, dict[str, object]]:
    """Parse ``death_keywords.yml`` into the ``{group: {applies_to, words}}`` shape."""
    return cast("dict[str, dict[str, object]]", yaml.safe_load(_DEATH_KEYWORDS_PATH.read_text()))


@cache
def _load_death_keywords() -> tuple[str, ...]:
    """Flat list of every keyword across every group — backs the L3 anchor regex."""
    out: list[str] = []
    for group in _load_death_groups().values():
        words = group.get("words")
        if not isinstance(words, list):
            continue
        out.extend(str(w) for w in cast("list[object]", words))
    return tuple(out)


@cache
def _l0_query_terms(status: str) -> tuple[str, ...]:
    """Subset of keywords pulled into the L0 Tavily query for ``status``.

    Reads ``applies_to`` on each YAML group: a group contributes its
    ``words`` when the suggestion's status is named there. Single source of
    truth shared with ``_DEATH_KEYWORDS`` — edit the YAML, both surfaces
    pick it up.
    """
    out: list[str] = []
    for group in _load_death_groups().values():
        applies_to = group.get("applies_to")
        words = group.get("words")
        if not isinstance(applies_to, list) or not isinstance(words, list):
            continue
        if status in cast("list[object]", applies_to):
            out.extend(str(w) for w in cast("list[object]", words))
    return tuple(out)


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


@dataclass(frozen=True, slots=True)
class DeathnessConfig:
    """L5 deathness gate knobs. Populated from ``Config`` at the pipeline; tests build it inline."""

    model: str
    max_tokens: int
    min_confidence: float
    struggling_min_confidence: float


def recall_source_id(suggestion: RecallSuggestion) -> str:
    """Stable id keyed on (name, homepage_url) when present, (name,) otherwise.

    Same vendor → same source_id. Without a homepage, falling back to
    citation domain would create duplicate Qdrant points (same vendor, different
    citation hosts across runs). Name-only collisions across distinct startups
    are rare per pitch and ``alias_graph`` collapses obvious cases at persist.
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

    ``evidence_quote`` is audit-only; the gate doesn't read it. ``confidence``
    has no field-level ``ge``/``le`` because strict ``response_format`` (OpenAI
    + Anthropic) rejects ``minimum``/``maximum`` on numeric schemas;
    ``_validate_confidence`` enforces [0.0, 1.0] post-parse.
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

    Returns ``None`` on transport or parse failure; the caller treats that
    as a drop. False admits cost more than false drops here.
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
    except (httpx.HTTPError, OpenRouterCompletionError) as exc:
        logger.info("recall_verify: L5 LLM call failed: %r", exc)
        return None
    try:
        return _DeathnessJudgment.model_validate_json(result.text)
    except ValidationError as exc:
        logger.info("recall_verify: L5 invalid response: %r", exc)
        return None


async def _l5_decide(
    *,
    suggestion: RecallSuggestion,
    body: str,
    llm: LLMClient,
    deathness: DeathnessConfig,
) -> _AdmitVerdict | None:
    """L5 verdict: returns the admitted verdict, or ``None`` to drop.

    ``"alive"``, below-threshold confidence, and transport/parse failure all
    map to ``None``. Logging and span events live here so the caller keeps a
    single L5 branch.
    """
    judgment = await _l5_deathness_judgment(
        suggestion=suggestion,
        body=body,
        llm=llm,
        model=deathness.model,
        max_tokens=deathness.max_tokens,
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
    threshold = (
        deathness.min_confidence
        if judgment.verdict == "dead"
        else deathness.struggling_min_confidence
    )
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

    News is always present (it's the L5 substrate); Wayback contributes a
    "Vendor description (archived)" section only when anchored. Section
    markers keep news and snapshot from blurring into one chunk.

    The status/year line is tagged "LLM-suggested" because it comes from
    the Sonnet recall payload, not the news body — synthesis treats it as a
    hint, not a fact.
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

    Returns ``(outcome, cleaned_body)``. Only ``body_too_short`` is
    recoverable via the Tavily extract fallback. ``extract_clean`` returns
    ``""`` below its 500-char floor, so paywalls and JS shells route to
    extract instead of synthesizing a body from raw HTML.
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

    Returns ``None`` when extract raised, returned no content, or still fails
    ``_l3_classify``. Anchor-missing outcomes from the fallback body don't
    retry: extract returns the same words.
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

    Snippets cap at ~150-200 chars and often drop the company name even on
    on-topic articles; the title nearly always names the subject.
    """
    needle_lower = needle.lower()
    return needle_lower in hit.title.lower() or needle_lower in hit.snippet.lower()


def _has_death_keyword(text: str) -> bool:
    """Reuse the L3 death-keyword regex on title+snippet to rank primary vs fallback."""
    return _DEATH_REGEX.search(text) is not None


# Length threshold below which a leading name token is too generic to use as
# a self-published-host discriminator. ``Opyn`` (4) qualifies; ``The`` (3),
# ``DAO`` (3), ``AI`` (2) do not. Empirically picked to keep the false-match
# rate near zero across the project-name shapes Opus emits.
_MIN_DISTINCTIVE_TOKEN_LEN: Final = 4


def _is_self_published(url: str, name: str) -> bool:
    """True when ``url`` is a page the project itself controls.

    Heuristic: the leading distinctive token of ``name`` appears in the URL
    host or path. Catches ``github.com/<name>``, ``learn.<name>.*``,
    ``docs.<name>.*``, ``<name>.com``, dApp-directory listings like
    ``alchemy.com/dapps/<name>``. ``_rank_candidate_urls`` uses this to demote
    project-controlled pages in the fallback rank without rejecting them: a
    primary hit (death keyword in title) still wins regardless.

    Returns ``False`` for leading tokens shorter than
    ``_MIN_DISTINCTIVE_TOKEN_LEN`` so generic prefixes don't trip the check.
    """
    tokens = name.split()
    if not tokens:
        return False
    leading = tokens[0].lower()
    if len(leading) < _MIN_DISTINCTIVE_TOKEN_LEN:
        return False
    parsed = urlparse(url)
    haystack = f"{(parsed.hostname or '').lower()}/{parsed.path.lower()}"
    return leading in haystack


def _quote_for_search(term: str) -> str:
    """Wrap multi-word terms in quotes so Tavily treats them as phrases."""
    return f'"{term}"' if " " in term else term


# Tavily's /search rejects queries over 400 characters with HTTP 400. The
# OR-clause must fit alongside ``"name"`` + parens + spaces + year. Reserve
# a small buffer so a longer suggestion name doesn't tip us over.
_TAVILY_QUERY_MAX_CHARS: Final = 400
_TAVILY_QUERY_HEADROOM: Final = 20


def _build_status_shaped_query(suggestion: RecallSuggestion) -> str:
    """Pass-1 query: biases Tavily toward articles whose surface text screams death.

    Vocabulary is read from ``death_keywords.yml`` (groups whose ``applies_to``
    contains the suggestion's status). Single source of truth with the L3
    anchor regex — adding a term in the YAML lifts it into the L0 search
    automatically. Terms are taken in YAML order until Tavily's 400-char
    query budget runs out; order high-signal vocabulary first in the YAML.

    Precise but misses live companies that Opus over-labeled as
    ``bruised``/``struggling``; pass 2 picks those up.
    """
    name_quoted = f'"{suggestion.name}"'
    year = str(suggestion.failure_year)
    # name_quoted + " (" + clause + ") " + year ; the parens/spaces total 4 chars.
    fixed_overhead = len(name_quoted) + len(year) + 4
    budget = _TAVILY_QUERY_MAX_CHARS - _TAVILY_QUERY_HEADROOM - fixed_overhead

    chosen: list[str] = []
    used = 0
    for raw in _l0_query_terms(suggestion.status):
        candidate = _quote_for_search(raw)
        added = len(candidate) + (4 if chosen else 0)  # " OR " separator
        if used + added > budget:
            continue
        chosen.append(candidate)
        used += added

    or_clause = " OR ".join(chosen)
    return f"{name_quoted} ({or_clause}) {year}"


# Cap on the number of distinct URLs the L0 head returns per suggestion.
# Verifier walks the list in priority order; each URL costs an L2 HEAD+GET
# and possibly a Tavily /extract call, so unbounded growth would bloat the
# per-suggestion budget. 5 covers the common case (1-2 obituary candidates
# plus self-published fallback) without paying for long tails.
_MAX_CANDIDATE_URLS: Final = 5


def _rank_candidate_urls(hits: list[TavilyHit], name: str, max_urls: int) -> list[str]:
    """Return up to ``max_urls`` URLs from ``hits``, ordered best-first.

    Each URL appears at most once. Priority:
        1. Primary — title+snippet carries a death keyword (real obituary,
           regardless of host).
        2. External fallback — name-matching hit on a host the project
           doesn't control.
        3. Self-published fallback — name-matching hit on a host the
           project does control. Worst signal, kept as last resort.

    Hits that don't mention ``name`` in title/snippet are skipped entirely.
    """
    primary: list[str] = []
    fallback_external: list[str] = []
    fallback_self: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        if hit.url in seen:
            continue
        if not _hit_mentions(hit, name):
            continue
        seen.add(hit.url)
        if _has_death_keyword(f"{hit.title} {hit.snippet}"):
            primary.append(hit.url)
        elif _is_self_published(hit.url, name):
            fallback_self.append(hit.url)
        else:
            fallback_external.append(hit.url)
    return (primary + fallback_external + fallback_self)[:max_urls]


# Outcome of one Tavily pass. ``_search_for_evidence`` looks at the last
# pass's outcome to label the consolidated ``RECALL_REJECTED_NO_EVIDENCE``
# event: ``transport_error`` if Tavily was unreachable, ``no_name_match``
# otherwise (covers both ``no_hits`` and hits-without-name-match — the
# dashboard sees one final reason per dropped suggestion).
type _PassOutcome = Literal["ok", "no_hits", "transport_error"]


async def _gather_hits(
    suggestion: RecallSuggestion,
    query: str,
    *,
    tavily_search: TavilySearchFn,
    limit: int,
) -> tuple[list[TavilyHit], _PassOutcome]:
    """Run one Tavily search; return all hits + transport outcome.

    No selection here: ranking happens in ``_rank_candidate_urls`` after both
    passes' hits are pooled, so the caller can walk multiple URLs at L2/L3
    when the first one fails.
    """
    try:
        hits = await tavily_search(query, limit)
    except (httpx.HTTPError, SSRFBlockedError) as exc:
        logger.info("recall_verify: L0 Tavily transport failure for %r: %r", suggestion.name, exc)
        return [], "transport_error"
    return hits, ("ok" if hits else "no_hits")


def _has_primary_hit(hits: list[TavilyHit], name: str) -> bool:
    """True when ``hits`` contains a name-matching hit with a death keyword in title/snippet."""
    return any(
        _hit_mentions(hit, name) and _has_death_keyword(f"{hit.title} {hit.snippet}")
        for hit in hits
    )


async def _search_for_evidence(
    suggestion: RecallSuggestion,
    *,
    tavily_search: TavilySearchFn,
    limit: int,
) -> list[str]:
    """L0: gather an ordered list of candidate URLs to probe at L2/L3.

    Returns up to ``_MAX_CANDIDATE_URLS`` URLs, best-first, drawn from:
      - ``suggestion.evidence_url`` if Opus supplied one (placed first),
      - hits from the status-shaped query (pass 1),
      - hits from the status-blind query (pass 2 — only runs when pass 1
        produced no primary death-keyword hit, preserving the original
        cost-saving short-circuit).

    Two passes because Opus over-labels live companies as
    ``bruised``/``struggling`` and the precise query then can't surface them.
    L5 is the alive/dead arbiter; pass 2 routes more candidates to it.

    Empty list = drop. ``RECALL_REJECTED_NO_EVIDENCE`` fires once per drop.
    ``transport_error`` wins over ``no_name_match`` when pass 2 also failed
    by transport (signal: Tavily was unreachable, not that nothing exists).
    Returning a non-empty list lets the caller try multiple URLs at L2/L3 so
    a single 403/anti-bot host doesn't kill a suggestion that has a usable
    second-best citation.
    """
    urls: list[str] = []
    if suggestion.evidence_url is not None:
        urls.append(suggestion.evidence_url)
        _emit_event(SpanEvent.RECALL_L0_PROVIDED_BY_RECALL_LLM)
        logger.info(
            "recall_verify: L0 short-circuit (LLM-provided URL) for %r: %s",
            suggestion.name,
            suggestion.evidence_url,
        )

    primary_query = _build_status_shaped_query(suggestion)
    hits1, outcome1 = await _gather_hits(
        suggestion, primary_query, tavily_search=tavily_search, limit=limit
    )

    pass2_ran = False
    outcome2: _PassOutcome = "ok"
    if _has_primary_hit(hits1, suggestion.name):
        pooled = hits1
    else:
        fallback_query = f'"{suggestion.name}" {suggestion.category} {suggestion.failure_year}'
        hits2, outcome2 = await _gather_hits(
            suggestion, fallback_query, tavily_search=tavily_search, limit=limit
        )
        pass2_ran = True
        pooled = hits1 + hits2

    ranked = _rank_candidate_urls(pooled, suggestion.name, _MAX_CANDIDATE_URLS)
    for url in ranked:
        if url not in urls:
            urls.append(url)

    if not urls:
        worst_outcome = outcome2 if pass2_ran else outcome1
        reason = "transport_error" if worst_outcome == "transport_error" else "no_name_match"
        logger.info("recall_verify: L0 dropped %r — %s (both passes)", suggestion.name, reason)
        _emit_event(SpanEvent.RECALL_REJECTED_NO_EVIDENCE, attributes={"reason": reason})
        return urls

    if pass2_ran and suggestion.evidence_url is None:
        # Trace signal: how often pass-2 saves a suggestion that pass-1 missed.
        # Skipped on the evidence_url path because RECALL_L0_PROVIDED_BY_RECALL_LLM
        # already fired for it.
        _emit_event(SpanEvent.RECALL_L0_NAME_ONLY_FALLBACK_RECOVERED)
        logger.info(
            "recall_verify: L0 pass-2 fallback recovered %d candidate URL(s) for %r: %s",
            len(ranked),
            suggestion.name,
            ranked,
        )
    return urls


async def _run_l4_wayback(
    *,
    suggestion: RecallSuggestion,
    seed: RawEntry,
    wayback: Enricher,
) -> tuple[VerificationTier, str | None]:
    """L4: enrich via Wayback when a homepage is present. Emit the verified event.

    Returns ``(tier, wayback_body)``; body is non-None only when the snapshot
    anchored on the company name. Skipped when ``homepage_url is None`` (the
    news URL would archive an unrelated article).
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

    HEAD often 401/403/405's on paywalled or anti-bot citation hosts even when
    GET works, so we never drop on HEAD — the result only annotates the
    eventual drop event.
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

    ``safe_get`` skips robots.txt (unlike Wayback's ``_fetch``); the evidence
    URL is a third-party citation, vendor robots policy doesn't apply.
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
    """Run the L2 HEAD→GET ladder + L3 classify; return the cleaned body or ``None``.

    Tavily ``/extract`` fires once on L2 GET 4xx/transport failure or L3
    body-too-short. Anchor failures don't retry.
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


async def verify_suggestion(  # noqa: PLR0913 - verifier signature carries L0-L5 deps + the deathness bundle
    suggestion: RecallSuggestion,
    *,
    discovered_urls: list[str],
    wayback: Enricher,
    llm: LLMClient,
    extract: ExtractFn,
    deathness: DeathnessConfig,
) -> tuple[RawEntry, VerificationTier, _AdmitVerdict] | None:
    """Run L2-L5 against one suggestion across one or more candidate URLs.

    Walks ``discovered_urls`` in priority order, attempting L2/L3 on each
    until one returns a usable body — a single 403 / anti-bot host doesn't
    drop the suggestion when a workable second-best URL exists. L4/L5 run
    once on the first L3-passing body, so L5 LLM cost stays bounded at one
    call per suggestion regardless of list length.

    ``extract`` fires once per URL on direct-GET 4xx or body-too-short.
    ``RawEntry.url`` falls back to the chosen evidence URL when
    ``homepage_url`` is None so the persisted point always has provenance.

    Returns ``None`` when every URL fails L2/L3 or when L5 rules the company
    alive / low-confidence.
    """
    homepage = suggestion.homepage_url
    evidence: str | None = None
    evidence_body: str | None = None
    for candidate in discovered_urls:
        body = await _l2_l3_fetch_body(name=suggestion.name, evidence=candidate, extract=extract)
        if body is not None:
            evidence = candidate
            evidence_body = body
            break
    if evidence is None or evidence_body is None:
        return None
    # url is identity, not provenance. When ``homepage_url is None`` the only
    # URL we have is the citation host (e.g. a news outlet), which is a domain
    # that hosts content about many unrelated companies. Letting that domain
    # flow into the resolver via ``entry.url`` makes tier-1 win on the citation
    # host and collapses unrelated recall suggestions into one canonical_id.
    # Leaving url=None forces the resolver to tier-2 keyed on suggestion.name.
    # The evidence URL is still preserved in the persisted body (see
    # ``_combine_recall_body``); only payload.sources loses it for these
    # entries, which is a cosmetic cost on the rendered report.
    # markdown_text=None AND raw_html=None matter — WaybackEnricher.enrich
    # short-circuits if either body is already populated, so the seed has to
    # arrive empty for L4 to do its enrichment pass.
    seed = RawEntry(
        source=SOURCE_LLM_RECALL,
        source_id=recall_source_id(suggestion),
        url=homepage,
        # The suggestion's name is trusted (LLM-emitted and L1-validated);
        # threading it here keeps the resolver from keying tier-2 on the
        # opaque sha256 source_id.
        title=suggestion.name,
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
        deathness=deathness,
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


@dataclass(frozen=True, slots=True)
class VerifiedEntry:
    """One suggestion that passed L0-L5.

    Carries the triple persistence needs: the seed ``RawEntry``, the
    Wayback tier label, and the L5 admit verdict (``"dead"`` | ``"struggling"``).
    Persistence lives pipeline-side and reads all three fields.
    """

    entry: RawEntry
    tier: VerificationTier
    verdict: Literal["dead", "struggling"]


# Decorator lives at the fan-out level so the trace gets one
# ``stage.recall_verify`` parent with N child spans, not N siblings with no
# parent. Suggestions, wayback, and llm handles never go to span attrs:
# CLAUDE.md forbids prompt/response bodies in tracing, and the evidence
# body can be sizeable.
@observe(
    name="stage.recall_verify",
    ignore_inputs=["suggestions", "wayback", "llm", "tavily_search", "extract"],
    ignore_output=True,
)
async def verify_all(  # noqa: PLR0913 - leaf helper; recall fan-out takes every dep at this seam
    suggestions: list[RecallSuggestion],
    *,
    wayback: Enricher,
    llm: LLMClient,
    tavily_search: TavilySearchFn,
    extract: ExtractFn,
    tavily_recall_max_results: int,
    deathness: DeathnessConfig,
    concurrency: int = _DEFAULT_CONCURRENCY,
) -> list[VerifiedEntry]:
    """Verify each suggestion under a capacity limiter; return accepted entries.

    ``gather_resilient`` isolates per-suggestion failures so a transient
    outage on one citation host doesn't poison the batch. ``_search_for_evidence``
    runs inside the same ``_one`` closure so a single 403 host doesn't kill
    siblings. Returned list contains only suggestions that passed L0-L5.
    """
    limiter = anyio.CapacityLimiter(concurrency)

    async def _one(s: RecallSuggestion) -> VerifiedEntry | None:
        async with limiter:
            discovered = await _search_for_evidence(
                s, tavily_search=tavily_search, limit=tavily_recall_max_results
            )
            if not discovered:
                return None
            verified = await verify_suggestion(
                s,
                discovered_urls=discovered,
                wayback=wayback,
                llm=llm,
                extract=extract,
                deathness=deathness,
            )
        if verified is None:
            return None
        entry, tier, verdict = verified
        return VerifiedEntry(entry=entry, tier=tier, verdict=verdict)

    results = await gather_resilient(*(_one(s) for s in suggestions))
    return [r for r in results if isinstance(r, VerifiedEntry)]
