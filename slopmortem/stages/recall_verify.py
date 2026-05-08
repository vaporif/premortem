"""Verifier for LLM-recalled suggestions: gates L1-L4 before persistence.

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
from lmnr import observe

from slopmortem.concurrency import gather_resilient
from slopmortem.http import SSRFBlockedError, safe_get, safe_head
from slopmortem.models import RawEntry, RecallSuggestion

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from slopmortem.corpus.sources.base import Enricher


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

_DEATH_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        # Terminal: the company is gone.
        "shutdown",
        "shut down",
        "closed",
        "defunct",
        "dissolved",
        "bankrupt",
        "bankruptcy",
        "acquired",
        "acquisition",
        "wound down",
        "ceased",
        "going out of business",
        # Distress: still operating but visibly hurting.
        "layoffs",
        "layoff",
        "restructuring",
        "struggling",
        "missed payroll",
        "downsizing",
        "troubled",
    }
)


type VerificationTier = Literal["wayback_anchored", "evidence_only"]


_SLUG_RE: Final = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    return _SLUG_RE.sub("-", name.lower()).strip("-")


def _recall_source_id(suggestion: RecallSuggestion) -> str:
    """Stable id keyed on (name, evidence_url).

    Two suggestions for the same company citing the same article collapse to
    one ``source_id``; differing citations stay distinct. The 12-char hash
    suffix is plenty for collision avoidance at the recall stage's per-pitch
    cap (~8 suggestions). Slug prefix keeps journal rows greppable.
    """
    fingerprint = f"{suggestion.name.lower()}|{suggestion.evidence_url}"
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12]
    return f"{_slugify(suggestion.name)}-{digest}"


def _body_anchors_name_and_death(name: str, body: str) -> bool:
    haystack = body.lower()
    if name.lower() not in haystack:
        return False
    return any(kw in haystack for kw in _DEATH_KEYWORDS)


# Drop suggestion bodies and URLs from span attrs: CLAUDE.md forbids
# prompt/response bodies in tracing, and the evidence body can be sizeable.
@observe(name="stage.recall_verify", ignore_inputs=["suggestion"], ignore_output=True)
async def verify_suggestion(
    suggestion: RecallSuggestion,
    *,
    wayback: Enricher,
) -> tuple[RawEntry, VerificationTier] | None:
    """Run L1-L4 against one suggestion. Returns ``None`` if any gate drops."""
    homepage = str(suggestion.homepage_url)
    evidence = str(suggestion.evidence_url)
    # L2: HEAD both URLs.
    for url in (homepage, evidence):
        try:
            head_resp = await safe_head(url, timeout=_FETCH_TIMEOUT_S)
        except (SSRFBlockedError, httpx.HTTPError) as exc:
            logger.info("recall_verify: L2 HEAD failed for %s: %r", url, exc)
            return None
        if head_resp.status_code >= _HTTP_BAD_REQUEST:
            logger.info("recall_verify: L2 HEAD %s for %s", head_resp.status_code, url)
            return None
    # L3: GET evidence body — primary anchor.
    # Note: ``safe_get`` does NOT consult robots.txt (unlike Wayback's
    # ``_fetch``). Recall's reason for existence is to surface vendors whose
    # own sites are robots-blocked or vanished; the evidence URL is a
    # third-party citation, so robots policy on the *vendor* doesn't apply.
    try:
        evidence_resp = await safe_get(evidence, timeout=_FETCH_TIMEOUT_S)
    except (SSRFBlockedError, httpx.HTTPError) as exc:
        logger.info("recall_verify: L3 GET failed for %s: %r", evidence, exc)
        return None
    if evidence_resp.status_code >= _HTTP_BAD_REQUEST:
        logger.info("recall_verify: L3 GET %s for %s", evidence_resp.status_code, evidence)
        return None
    evidence_body = evidence_resp.text
    if not _body_anchors_name_and_death(suggestion.name, evidence_body):
        logger.info(
            "recall_verify: L3 body lacks name+death anchor for %r at %s",
            suggestion.name,
            evidence,
        )
        return None
    # L4: optional Wayback corroboration.
    tier: VerificationTier = "evidence_only"
    body = evidence_body
    # markdown_text=None AND raw_html=None matter — WaybackEnricher.enrich
    # short-circuits if either body is already populated.
    seed = RawEntry(
        source="llm_recall",
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
    # Body lands on markdown_text. _entry_summary_text already prefers
    # markdown_text over raw_html; leaving raw_html=None avoids a second
    # extract pass downstream.
    final = seed.model_copy(update={"markdown_text": body})
    return final, tier


async def verify_and_persist_all(
    suggestions: list[RecallSuggestion],
    *,
    wayback: Enricher,
    persist: Callable[[RawEntry, VerificationTier], Awaitable[None]],
    concurrency: int = _DEFAULT_CONCURRENCY,
) -> list[RawEntry]:
    """Verify each suggestion under a capacity limiter; persist accepted entries.

    ``gather_resilient`` keeps a sibling failure from cancelling the rest —
    a transient outage on one citation host shouldn't poison the whole batch.
    Returned list contains only the entries that passed L1-L4 AND persisted
    cleanly; per-suggestion exceptions land in the logs instead.
    """
    limiter = anyio.CapacityLimiter(concurrency)

    async def _one(s: RecallSuggestion) -> RawEntry | None:
        async with limiter:
            verified = await verify_suggestion(s, wayback=wayback)
        if verified is None:
            return None
        entry, tier = verified
        await persist(entry, tier)
        return entry

    results = await gather_resilient(*(_one(s) for s in suggestions))
    return [r for r in results if isinstance(r, RawEntry)]
