"""Entity resolution: tier-1, tier-2, tier-3 canonical_id derivation.

Tiered IDs:

- Tier 1: registrable_domain (via ``tldextract``). Demoted on
  ``platform_domains.yml``, recycled-domain founding-year delta, or
  parent/subsidiary suffix delta.
- Tier 2: ``{normalized_name}::{sector}``.
- Tier 3: dense-embedding cosine similarity against the existing tier-2
  canonical, with a Haiku tiebreaker inside the calibration band
  ``[0.65, 0.85]``.

Atomicity contracts:

- Resolver-flip precheck runs first. A previously-bound
  ``(source, source_id)`` lands as ``resolver_flipped`` in its terminal
  state — no transient ``pending`` row, no ingest of the new canonical
  (``--reconcile`` owns repair).
- Alias precheck runs before any ingest write. With an alias hint,
  ``MergeJournal.upsert_alias_blocked`` writes the alias edge and the
  journal row in one SQLite transaction; on failure both roll back.

Tier-3 decisions cache in a module-private ``tier3_decisions`` SQLite table
sharing the merge journal's ``db_path``. Cache key is lex-sorted so
``(A, B)`` and ``(B, A)`` collapse to one row.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlparse

import tldextract
import yaml
from anyio import to_thread

from slopmortem._time import utcnow_iso
from slopmortem.corpus._db import connect
from slopmortem.llm import prompt_template_sha, render_prompt
from slopmortem.models import MergeState
from slopmortem.tracing import SpanEvent

if TYPE_CHECKING:
    import sqlite3

    from slopmortem.corpus._merge import MergeJournal
    from slopmortem.llm import EmbeddingClient, LLMClient
    from slopmortem.models import AliasEdge, RawEntry


_PLATFORM_DOMAINS_YAML = Path(__file__).resolve().parent / "sources" / "platform_domains.yml"
_CORPORATE_HIERARCHY_YAML = (
    Path(__file__).resolve().parent / "sources" / "corporate_hierarchy_overrides.yml"
)

# Corporate suffixes that flag parent/subsidiary disambiguation.
_CORPORATE_SUFFIXES = (
    "holdings",
    "group",
    "corp",
    "corporation",
    "ltd",
    "limited",
    "llc",
    "inc",
    "incorporated",
    "co",
    "company",
    "plc",
    "gmbh",
    "ag",
    "sa",
    "sas",
)
_CORPORATE_SUFFIXES_RE = re.compile(rf"\s+({'|'.join(_CORPORATE_SUFFIXES)})\b\.?$", re.IGNORECASE)

# Founding-year delta beyond this demotes tier-1 to tier-2 (recycled domain).
_RECYCLED_DOMAIN_YEAR_DELTA = 10

# Similarity inside this band triggers the Haiku tiebreaker; outside auto-decides.
_DEFAULT_TIER3_BAND: tuple[float, float] = (0.65, 0.85)

_TIEBREAKER_PROMPT_NAME = "tier3_tiebreaker"


@dataclass(frozen=True, slots=True)
class ResolveResult:
    """Outcome of a resolve attempt.

    For ``resolver_flipped``, ``canonical_id`` is the NEW id; the rebind is
    not written here — the repair pass owns it.
    """

    canonical_id: str
    action: Literal["create", "merge", "resolver_flipped", "alias_blocked"]
    prior_canonical_id: str | None = None
    span_events: list[str] = field(default_factory=list)


def _load_platform_domains() -> frozenset[str]:
    with _PLATFORM_DOMAINS_YAML.open("r", encoding="utf-8") as fh:
        data = cast("dict[str, Any]", yaml.safe_load(fh) or {})  # pyright: ignore[reportExplicitAny]
    domains_obj: object = data.get("domains") or []
    if not isinstance(domains_obj, list):
        return frozenset()
    domains_list = cast("list[object]", domains_obj)
    return frozenset(str(d).lower() for d in domains_list)


def _load_corporate_hierarchy_overrides() -> dict[str, list[str]]:
    """Map parent canonical → list of subsidiary canonicals (empty in v1)."""
    with _CORPORATE_HIERARCHY_YAML.open("r", encoding="utf-8") as fh:
        data = cast("dict[str, Any]", yaml.safe_load(fh) or {})  # pyright: ignore[reportExplicitAny]
    overrides_obj: object = data.get("overrides") or []
    if not isinstance(overrides_obj, list):
        return {}
    out: dict[str, list[str]] = {}
    overrides_list = cast("list[object]", overrides_obj)
    for raw in overrides_list:
        if not isinstance(raw, dict):
            continue
        entry = cast("dict[str, Any]", raw)  # pyright: ignore[reportExplicitAny]
        parent_obj: object = entry.get("parent")
        subs_obj: object = entry.get("subsidiaries")
        if not isinstance(parent_obj, str) or not isinstance(subs_obj, list):
            continue
        subs_list = cast("list[object]", subs_obj)
        out[parent_obj] = [str(s) for s in subs_list]
    return out


_PLATFORM_DOMAINS: frozenset[str] = _load_platform_domains()
_HIERARCHY_OVERRIDES: dict[str, list[str]] = _load_corporate_hierarchy_overrides()


def _registrable_domain(url: str) -> str:
    extracted = tldextract.extract(url)
    if not extracted.domain or not extracted.suffix:
        host = urlparse(url).hostname or ""
        return host.lower()
    return f"{extracted.domain}.{extracted.suffix}".lower()


def _normalize_name(name: str) -> str:
    """Lowercase, strip a trailing corporate suffix, and collapse whitespace."""
    no_suffix = _CORPORATE_SUFFIXES_RE.sub("", name).strip()
    return re.sub(r"\s+", " ", no_suffix.lower())


def _strip_corporate_suffix(name: str) -> tuple[str, str | None]:
    """Return ``(normalized_stem, suffix_or_None)`` for the suffix-delta heuristic."""
    match = _CORPORATE_SUFFIXES_RE.search(name)
    suffix = match.group(1).lower() if match else None
    stem = re.sub(r"\s+", " ", _CORPORATE_SUFFIXES_RE.sub("", name).strip().lower())
    return stem, suffix


def _tier2_canonical(name: str, sector: str) -> str:
    return f"{_normalize_name(name)}::{sector.lower()}"


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _pair_key(canonical_a: str, canonical_b: str) -> str:
    """Lex-sorted pair joined by U+0001 SOH so ``(A, B)`` and ``(B, A)`` collapse."""
    lo, hi = sorted((canonical_a, canonical_b))
    return f"{lo}{hi}"


_TIER3_DECISIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tier3_decisions (
    pair_key                TEXT PRIMARY KEY,
    decision                TEXT NOT NULL,
    rationale               TEXT,
    haiku_model_id          TEXT NOT NULL,
    tiebreaker_prompt_hash  TEXT NOT NULL,
    decided_at              TEXT NOT NULL
)
"""


def _ensure_tier3_table_sync(db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.execute(_TIER3_DECISIONS_SCHEMA)


def _read_founding_year_sync(db_path: Path, registrable_domain: str) -> int | None:
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            SELECT founding_year FROM founding_year_cache
             WHERE registrable_domain = ?
               AND founding_year IS NOT NULL
             ORDER BY rowid ASC LIMIT 1
            """,
            (registrable_domain,),
        )
        # sqlite3 Row indexing returns Any; column is INTEGER NULL.
        row = cast("sqlite3.Row | None", cur.fetchone())
        return None if row is None else cast("int | None", row["founding_year"])


def _write_founding_year_sync(
    db_path: Path,
    registrable_domain: str,
    content_sha256: str,
    founding_year: int | None,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO founding_year_cache
              (registrable_domain, content_sha256, founding_year)
            VALUES (?, ?, ?)
            ON CONFLICT(registrable_domain, content_sha256) DO UPDATE SET
              founding_year = excluded.founding_year
            """,
            (registrable_domain, content_sha256, founding_year),
        )


def _read_tier3_decision_sync(
    db_path: Path,
    pair_key: str,
    haiku_model_id: str,
    tiebreaker_prompt_hash: str,
) -> str | None:
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            SELECT decision FROM tier3_decisions
             WHERE pair_key = ?
               AND haiku_model_id = ?
               AND tiebreaker_prompt_hash = ?
            """,
            (pair_key, haiku_model_id, tiebreaker_prompt_hash),
        )
        # sqlite3 Row indexing returns Any; decision column is TEXT NOT NULL.
        row = cast("sqlite3.Row | None", cur.fetchone())
        return None if row is None else cast("str", row["decision"])


def _write_tier3_decision_sync(  # noqa: PLR0913 — keyword-only cache write
    db_path: Path,
    pair_key: str,
    decision: str,
    rationale: str,
    haiku_model_id: str,
    tiebreaker_prompt_hash: str,
    decided_at: str,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO tier3_decisions
              (pair_key, decision, rationale, haiku_model_id, tiebreaker_prompt_hash, decided_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(pair_key) DO UPDATE SET
              decision = excluded.decision,
              rationale = excluded.rationale,
              haiku_model_id = excluded.haiku_model_id,
              tiebreaker_prompt_hash = excluded.tiebreaker_prompt_hash,
              decided_at = excluded.decided_at
            """,
            (
                pair_key,
                decision,
                rationale,
                haiku_model_id,
                tiebreaker_prompt_hash,
                decided_at,
            ),
        )


def _write_pending_review_sync(  # noqa: PLR0913 — keyword-only review write
    db_path: Path,
    pair_key: str,
    similarity_score: float,
    haiku_decision: str,
    haiku_rationale: str,
    raw_section_heads: str,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO pending_review
              (pair_key, similarity_score, haiku_decision, haiku_rationale, raw_section_heads)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(pair_key) DO UPDATE SET
              similarity_score = excluded.similarity_score,
              haiku_decision = excluded.haiku_decision,
              haiku_rationale = excluded.haiku_rationale,
              raw_section_heads = excluded.raw_section_heads
            """,
            (
                pair_key,
                similarity_score,
                haiku_decision,
                haiku_rationale,
                raw_section_heads,
            ),
        )


def _candidate_tier1_id(url: str) -> tuple[str, bool]:
    """Return ``(registrable_domain, is_platform)``."""
    domain = _registrable_domain(url)
    return domain, domain in _PLATFORM_DOMAINS


async def _decide_tier3(  # noqa: PLR0913 — keyword-only tier-3 decision API
    *,
    db_path: Path,
    canonical_existing: str,
    canonical_new: str,
    similarity: float,
    band: tuple[float, float],
    name_existing: str,
    name_new: str,
    section_head_existing: str,
    section_head_new: str,
    llm_client: LLMClient | None,
    haiku_model_id: str,
    max_tokens: int | None = None,
) -> tuple[str, str]:
    """Auto-decide outside the band; cached Haiku tiebreaker inside."""
    if similarity >= band[1]:
        return "same", "auto: similarity above upper band"
    if similarity <= band[0]:
        return "different", "auto: similarity below lower band"
    if llm_client is None:
        # In-band, no LLM: conservative, do NOT collapse.
        return "different", "no llm: defaulting to different inside calibration band"
    pk = _pair_key(canonical_existing, canonical_new)
    tiebreaker_hash = prompt_template_sha(_TIEBREAKER_PROMPT_NAME)
    cached = await to_thread.run_sync(
        _read_tier3_decision_sync, db_path, pk, haiku_model_id, tiebreaker_hash
    )
    if cached is not None:
        return cached, "cached"
    rendered = render_prompt(
        _TIEBREAKER_PROMPT_NAME,
        name_a=name_existing,
        name_b=name_new,
        section_head_a=section_head_existing,
        section_head_b=section_head_new,
    )
    result = await llm_client.complete(
        rendered,
        model=haiku_model_id,
        extra_body={"prompt_template_sha": tiebreaker_hash},
        max_tokens=max_tokens,
    )
    decision, rationale = _parse_tiebreaker_response(result.text)
    now_iso = utcnow_iso()
    await to_thread.run_sync(
        _write_tier3_decision_sync,
        db_path,
        pk,
        decision,
        rationale,
        haiku_model_id,
        tiebreaker_hash,
        now_iso,
    )
    # Mirror to pending_review for the offline `--list-review` queue.
    section_heads = json.dumps(
        {
            "existing": section_head_existing[:200],
            "new": section_head_new[:200],
        }
    )
    await to_thread.run_sync(
        _write_pending_review_sync,
        db_path,
        pk,
        similarity,
        decision,
        rationale,
        section_heads,
    )
    return decision, rationale


def _parse_tiebreaker_response(text: str) -> tuple[str, str]:
    """Falls back to ``different`` on any parse failure."""
    try:
        payload = cast("dict[str, Any]", json.loads(text))  # pyright: ignore[reportExplicitAny]
    except json.JSONDecodeError:
        return "different", "unparseable response: defaulting to different"
    decision = str(payload.get("decision") or "different").lower()
    if decision not in {"same", "different"}:
        decision = "different"
    rationale = str(payload.get("rationale") or "")
    return decision, rationale


def _looks_tier1(canonical_id: str) -> bool:
    return "::" not in canonical_id


async def _is_parent_subsidiary_suspect(journal: MergeJournal, domain: str, new_name: str) -> bool:
    """Flag a suffix-delta when the bare domain is in the journal and the name has a corp suffix.

    No per-row display-name persistence yet, so the heuristic is conservative.
    ``corporate_hierarchy_overrides.yml`` seeds explicit parent/subsidiary pairs.
    """
    if domain in _HIERARCHY_OVERRIDES:
        return True
    rows = await journal.fetch_all()
    domain_present = any(
        row["canonical_id"] == domain
        and row["merge_state"] in (MergeState.COMPLETE.value, MergeState.PENDING.value)
        for row in rows
    )
    if not domain_present:
        return False
    _, new_suffix = _strip_corporate_suffix(new_name)
    return new_suffix is not None


async def resolve_entity(  # noqa: PLR0913 — keyword-only resolver entry point
    entry: RawEntry,
    *,
    journal: MergeJournal,
    embed_client: EmbeddingClient,
    name: str,
    sector: str,
    founding_year: int | None = None,
    alias_hint: AliasEdge | None = None,
    llm_client: LLMClient | None = None,
    haiku_model_id: str = "anthropic/claude-haiku-4.5",
    tier3_band: tuple[float, float] = _DEFAULT_TIER3_BAND,
    force_similarity: float | None = None,
    tiebreaker_max_tokens: int | None = None,
) -> ResolveResult:
    """Resolve *entry* to a canonical_id.

    - ``alias_hint`` set → alias edge + ``alias_blocked`` row written
      atomically; short-circuits.
    - ``founding_year`` drives the recycled-domain check (delta > 10).
    - ``llm_client`` only required for in-band tier-3 calls.
    - ``force_similarity`` is a test-only escape hatch.

    SQLite or LLM exceptions propagate after the journal rolls back.
    """
    # tier-3 + founding-year caches share the merge journal's sqlite file. No
    # public accessor on purpose — merge.py is read-only from this side.
    db_path = journal._db  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
    await to_thread.run_sync(_ensure_tier3_table_sync, db_path)

    span_events: list[str] = []

    if alias_hint is not None:
        await journal.upsert_alias_blocked(
            canonical_id=alias_hint.canonical_id,
            source=entry.source,
            source_id=entry.source_id,
            alias_edge=alias_hint,
        )
        span_events.append(SpanEvent.CUSTOM_ALIAS_SUSPECTED.value)
        return ResolveResult(
            canonical_id=alias_hint.canonical_id,
            action="alias_blocked",
            span_events=span_events,
        )

    domain, is_platform = _candidate_tier1_id(entry.url or "")
    candidate_id = domain
    use_tier2 = is_platform or not domain

    # Recycled-domain check: same domain + founding_year delta > 10 → demote.
    if not use_tier2 and founding_year is not None:
        cached_year = await to_thread.run_sync(_read_founding_year_sync, db_path, domain)
        if (
            cached_year is not None
            and abs(founding_year - cached_year) > _RECYCLED_DOMAIN_YEAR_DELTA
        ):
            use_tier2 = True

    # Suffix-delta: tier-1 hit but new name differs only by a corporate suffix.
    if not use_tier2 and await _is_parent_subsidiary_suspect(journal, domain, name):
        use_tier2 = True
        span_events.append(SpanEvent.PARENT_SUBSIDIARY_SUSPECTED.value)

    if use_tier2:
        candidate_id = _tier2_canonical(name, sector)

    candidate_id = await _maybe_tier3_collapse(
        db_path=db_path,
        journal=journal,
        candidate_id=candidate_id,
        entry=entry,
        embed_client=embed_client,
        name=name,
        sector=sector,
        llm_client=llm_client,
        haiku_model_id=haiku_model_id,
        tier3_band=tier3_band,
        force_similarity=force_similarity,
        is_tier2=use_tier2,
        tiebreaker_max_tokens=tiebreaker_max_tokens,
    )

    prior = await journal.lookup_canonical_for_source(entry.source, entry.source_id)
    if prior is not None and prior != candidate_id:
        await journal.upsert_resolver_flipped(
            canonical_id=candidate_id,
            source=entry.source,
            source_id=entry.source_id,
        )
        span_events.append(SpanEvent.RESOLVER_FLIP_DETECTED.value)
        return ResolveResult(
            canonical_id=candidate_id,
            action="resolver_flipped",
            prior_canonical_id=prior,
            span_events=span_events,
        )

    if founding_year is not None and _looks_tier1(candidate_id):
        # Ideally cache key would include content_sha256, but the merged content
        # doesn't exist at resolve time. v1 only needs *some* entry per domain
        # for the delta check, so source_id stands in as a per-row dedup key.
        await to_thread.run_sync(
            _write_founding_year_sync,
            db_path,
            domain,
            f"{entry.source}:{entry.source_id}",
            founding_year,
        )

    existing = await journal.fetch_all()
    is_existing = any(row["canonical_id"] == candidate_id for row in existing)
    action: Literal["create", "merge"] = "merge" if is_existing else "create"
    return ResolveResult(canonical_id=candidate_id, action=action, span_events=span_events)


async def _maybe_tier3_collapse(  # noqa: PLR0913 — keyword-only internal hop
    *,
    db_path: Path,
    journal: MergeJournal,
    candidate_id: str,
    entry: RawEntry,
    embed_client: EmbeddingClient,
    name: str,
    sector: str,
    llm_client: LLMClient | None,
    haiku_model_id: str,
    tier3_band: tuple[float, float],
    force_similarity: float | None,
    is_tier2: bool,
    tiebreaker_max_tokens: int | None = None,
) -> str:
    """Collapse a tier-2 candidate onto a same-sector tier-2 sibling when ``same`` fires.

    Only runs when ``is_tier2`` is True.
    """
    if not is_tier2:
        return candidate_id
    rows = await journal.fetch_all()
    same_sector_suffix = f"::{sector.lower()}"
    # journal.fetch_all values are sqlite-typed Any; canonical_id/merge_state are TEXT.
    siblings: list[str] = [
        cid
        for row in rows
        if (cid := cast("str", row["canonical_id"])).endswith(same_sector_suffix)
        and cid != candidate_id
        and row["merge_state"] in (MergeState.COMPLETE.value, MergeState.PENDING.value)
    ]
    if not siblings:
        return candidate_id
    sibling = sorted(siblings)[0]
    section_head_new = (entry.markdown_text or "")[:200]
    if force_similarity is not None:
        similarity = force_similarity
    else:
        embed_text_new = f"{name}\n{section_head_new}"
        embed_text_existing = f"{sibling}\n"  # we don't persist existing heads in v1
        result = await embed_client.embed([embed_text_new, embed_text_existing])
        similarity = _cosine(result.vectors[0], result.vectors[1])
    decision, _rationale = await _decide_tier3(
        db_path=db_path,
        canonical_existing=sibling,
        canonical_new=candidate_id,
        similarity=similarity,
        band=tier3_band,
        name_existing=sibling,
        name_new=name,
        section_head_existing="",
        section_head_new=section_head_new,
        llm_client=llm_client,
        haiku_model_id=haiku_model_id,
        max_tokens=tiebreaker_max_tokens,
    )
    if decision == "same":
        return str(sibling)
    return candidate_id
