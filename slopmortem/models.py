"""Pydantic v2 models shared across the pipeline: facets, candidates, synthesis output."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter, model_validator

# Closed-enum facet fields whose values MUST be in taxonomy.yml. Free-form
# fields like sub_sector, product_type, price_point, founding_year, and
# failure_year stay open and aren't enum-validated.
_CLOSED_FACET_FIELDS: tuple[str, ...] = (
    "sector",
    "business_model",
    "customer_type",
    "geography",
    "monetization",
)

_TAXONOMY_PATH = Path(__file__).resolve().parent / "corpus" / "taxonomy.yml"


@cache
def _load_taxonomy() -> dict[str, frozenset[str]]:
    """Load ``taxonomy.yml`` once, returning each closed-enum field as a frozenset."""
    # yaml.safe_load is loosely typed; narrow at the dict boundary.
    raw = cast(
        "dict[str, list[Any]]",  # pyright: ignore[reportExplicitAny]
        yaml.safe_load(_TAXONOMY_PATH.read_text()),
    )
    return {field: frozenset(raw[field]) for field in _CLOSED_FACET_FIELDS}


# Dynamic Literal types built from taxonomy.yml at module-load. Pydantic emits
# them as ``"enum": [...]`` in the JSON schema, which Anthropic's grammar-
# constrained sampler enforces — so Haiku can't return ``geography="japan"``
# instead of ``apac``, or ``customer_type="b2c"`` instead of ``consumer``.
_TAX = _load_taxonomy()
_SECTOR_VALUES: tuple[str, ...] = tuple(sorted(_TAX["sector"]))
_BUSINESS_MODEL_VALUES: tuple[str, ...] = tuple(sorted(_TAX["business_model"]))
_CUSTOMER_TYPE_VALUES: tuple[str, ...] = tuple(sorted(_TAX["customer_type"]))
_GEOGRAPHY_VALUES: tuple[str, ...] = tuple(sorted(_TAX["geography"]))
_MONETIZATION_VALUES: tuple[str, ...] = tuple(sorted(_TAX["monetization"]))

# Pydantic introspects the *runtime* Literal to emit JSON-schema ``enum``
# constraints. basedpyright can't expand a tuple at type-check time, so fall
# back to ``str`` for static analysis. The runtime Literal still enforces the
# closed set via Pydantic's validator.
if TYPE_CHECKING:
    SectorLit = str
    BusinessModelLit = str
    CustomerTypeLit = str
    GeographyLit = str
    MonetizationLit = str
else:
    SectorLit = Literal[*_SECTOR_VALUES]
    BusinessModelLit = Literal[*_BUSINESS_MODEL_VALUES]
    CustomerTypeLit = Literal[*_CUSTOMER_TYPE_VALUES]
    GeographyLit = Literal[*_GEOGRAPHY_VALUES]
    MonetizationLit = Literal[*_MONETIZATION_VALUES]


class PerspectiveScore(BaseModel):
    """One similarity-perspective score (0-10) with the LLM's rationale."""

    score: float
    rationale: str


class SimilarityScores(BaseModel):
    """Closed set of similarity perspectives the reranker scores against."""

    business_model: PerspectiveScore
    market: PerspectiveScore
    gtm: PerspectiveScore
    stage_scale: PerspectiveScore

    def mean(self) -> float:
        """Mean of the four perspective scores. Used by post-rerank and post-synth filters."""
        return (
            self.business_model.score + self.market.score + self.gtm.score + self.stage_scale.score
        ) / 4


class Facets(BaseModel):
    """Facets extracted from an input pitch. Closed-key half pins the taxonomy schema.

    Closed enums are typed as ``Literal[*taxonomy_values]`` so Pydantic emits a
    JSON-schema ``enum`` constraint that Anthropic's grammar-constrained
    sampler enforces. Out-of-enum values can't reach this class; the LLM
    can't generate them in the first place.
    """

    sector: SectorLit
    business_model: BusinessModelLit
    customer_type: CustomerTypeLit
    geography: GeographyLit
    monetization: MonetizationLit
    sub_sector: str | None = None
    product_type: str | None = None
    price_point: str | None = None
    founding_year: int | None = None
    failure_year: int | None = None


class LLMSynthesis(BaseModel):
    """Fields the LLM emits for one candidate.

    failure_date, lifespan_months, and sources are intentionally missing. The
    dates are derived from CandidatePayload in stages.synthesize; sources are
    passed through from CandidatePayload because the LLM never sees provenance
    URLs (the body is plain text after extract_clean), so asking for them
    produced empty or hallucinated lists that the host filter dropped.
    """

    candidate_id: str
    name: str
    one_liner: str
    similarity: SimilarityScores
    why_similar: str
    where_diverged: str
    failure_causes: list[str]
    lessons_for_input: list[str]


class Synthesis(BaseModel):
    """LLMSynthesis fields plus dates and sources derived from ``CandidatePayload``."""

    candidate_id: str
    name: str
    one_liner: str
    failure_date: date | None
    lifespan_months: int | None
    similarity: SimilarityScores
    why_similar: str
    where_diverged: str
    failure_causes: list[str]
    lessons_for_input: list[str]
    sources: list[str]
    injection_detected: bool = False
    # Mirrors ``CandidatePayload.source`` so the renderer can flag llm_recall
    # candidates without re-reading the qdrant payload. None on syntheses
    # written before the field existed.
    source: str | None = None
    # Mirrors ``CandidatePayload.deathness_verdict`` so the renderer can pick
    # forward-looking framing for ``"struggling"`` admits. None on syntheses
    # written before the verdict existed (legacy crunchbase/web rows are
    # already-curated post-mortems and render with the default dead framing).
    deathness_verdict: Literal["dead", "struggling"] | None = None

    @classmethod
    def from_llm(  # noqa: PLR0913 - one kwarg per typed-payload field threaded past the LLM
        cls,
        llm_synth: LLMSynthesis,
        *,
        founding_date: date | None,
        failure_date: date | None,
        sources: list[str],
        injection_detected: bool = False,
        source: str | None = None,
        deathness_verdict: Literal["dead", "struggling"] | None = None,
    ) -> Synthesis:
        lifespan = _months_between(founding_date, failure_date)
        return cls(
            candidate_id=llm_synth.candidate_id,
            name=llm_synth.name,
            one_liner=llm_synth.one_liner,
            failure_date=failure_date,
            lifespan_months=lifespan,
            similarity=llm_synth.similarity,
            why_similar=llm_synth.why_similar,
            where_diverged=llm_synth.where_diverged,
            failure_causes=llm_synth.failure_causes,
            lessons_for_input=llm_synth.lessons_for_input,
            sources=sources,
            injection_detected=injection_detected,
            source=source,
            deathness_verdict=deathness_verdict,
        )


def _months_between(founding: date | None, failure: date | None) -> int | None:
    """Whole months between two dates; ``None`` if missing or delta is negative."""
    if founding is None or failure is None:
        return None
    months = (failure.year - founding.year) * 12 + (failure.month - founding.month)
    return months if months >= 0 else None


class CandidatePayload(BaseModel):
    """Persisted candidate doc: body, facets, provenance, text id.

    ``sources`` is URL-only (empty when the upstream entry had no URL, e.g. CSV
    imports). ``provenance_id`` is the synthetic ``"<source>:<source_id>"``
    audit string that always identifies where the doc came from.

    Splitting these stops the synthetic id from leaking into the synth host
    allowlist: ``urlparse("curated:Celsius Network").hostname is None`` used
    to drop every cited URL.
    """

    name: str
    summary: str
    body: str
    facets: Facets
    founding_date: date | None
    failure_date: date | None
    founding_date_unknown: bool
    failure_date_unknown: bool
    provenance: Literal["curated_real", "scraped", "synthesized"]
    slop_score: float
    sources: list[str]
    provenance_id: str = ""
    text_id: str
    # None for non-recall sources; ``recall_persist`` sets it for source=llm_recall.
    # Carries the verifier's L4 outcome ("wayback_anchored" beats "evidence_only"
    # because a corroborated snapshot is harder to fabricate than a single citation).
    verification_tier: Literal["wayback_anchored", "evidence_only"] | None = None
    # None for non-recall sources; the L5 verifier sets it on admit. ``"dead"``
    # is a terminal verdict (shutdown, bankruptcy, fire-sale acquisition);
    # ``"struggling"`` is ongoing distress (layoffs, restructuring) without
    # ceased operations. Synthesis weights the two differently — terminal
    # citations are stronger evidence than distress signals.
    deathness_verdict: Literal["dead", "struggling"] | None = None
    # Raw ``RawEntry.source`` ("crunchbase_csv", "llm_recall", …). Kept alongside
    # ``provenance`` (curated/scraped/synthesized 3-way) because synthesize and
    # render branch on the finer split — only ``llm_recall`` gets the recall tag.
    # None for qdrant rows written before the field existed.
    source: str | None = None


class Candidate(BaseModel):
    """A retrieval hit: canonical id, retrieval score, and the persisted payload."""

    canonical_id: str
    score: float
    payload: CandidatePayload
    alias_canonicals: list[str] = Field(default_factory=list)


class InputContext(BaseModel):
    """The user's pitch under analysis: name, description, and an optional recency filter."""

    name: str
    description: str
    years_filter: int | None = None


class ScoredCandidate(BaseModel):
    """LLM rerank output for a single candidate: perspective scores plus a free-text rationale."""

    candidate_id: str
    perspective_scores: SimilarityScores
    rationale: str


class LlmRerankResult(BaseModel):
    """Wrapper for the rerank stage's array output, so the schema is a single object."""

    ranked: list[ScoredCandidate]


_HTTP_URL_ADAPTER: TypeAdapter[HttpUrl] = TypeAdapter(HttpUrl)


class RecallSuggestion(BaseModel):
    """One LLM-recalled comparable: company name plus the citation that proves failure.

    URL fields are typed ``str`` (not ``HttpUrl``) on purpose: ``HttpUrl`` emits
    ``format: "uri"`` plus ``minLength``/``maxLength`` in JSON Schema, all of
    which OpenAI's strict ``response_format`` mode rejects. Parse-equivalent
    validation runs in ``_validate_urls`` below, so the wire contract stays
    "well-formed http(s) URL" even though the schema sent upstream is just
    ``"type": "string"``.
    """

    name: str
    category: str
    status: Literal["dead", "absorbed", "struggling", "bruised"]
    homepage_url: str
    failure_year: int = Field(ge=1990, le=2030)
    evidence_url: str
    one_liner: str

    @model_validator(mode="after")
    def _validate_urls(self) -> RecallSuggestion:
        # Same shape ``HttpUrl`` would have enforced on the field directly.
        _HTTP_URL_ADAPTER.validate_python(self.homepage_url)
        _HTTP_URL_ADAPTER.validate_python(self.evidence_url)
        return self


class RecallSuggestionList(BaseModel):
    """Wrapper for the recall stage's array output, so the schema is a single object.

    Mirrors ``LlmRerankResult``: OpenRouter strict ``json_schema`` mode rejects
    array roots. Wrap the list under ``suggestions`` so ``to_strict_response_schema``
    emits an object schema the API will accept.
    """

    suggestions: list[RecallSuggestion]


class MergeState(StrEnum):
    """Lifecycle of an entity-resolution merge between two candidates."""

    PENDING = "pending"
    COMPLETE = "complete"
    ALIAS_BLOCKED = "alias_blocked"
    RESOLVER_FLIPPED = "resolver_flipped"


class PipelineMeta(BaseModel):
    """Run metadata attached to the final ``Report`` for cost, latency, and budget bookkeeping."""

    K_retrieve: int
    N_synthesize: int
    min_similarity_score: float
    models: dict[str, str]
    cost_usd_total: float
    latency_ms_total: int
    trace_id: str | None
    budget_remaining_usd: float
    budget_exceeded: bool
    filtered_pre_synth: int = 0
    filtered_post_synth: int = 0
    # ``coverage_gap`` is the predicate result (survivors < N_synthesize after
    # rerank+min_similarity) and is computed on every query. ``recall_used``
    # flips only after a verified entry landed and the post-recall rerank ran
    # (not on attempt). ``recall_persisted_count`` counts suggestions that passed
    # L1-L4 verification AND the slop classifier.
    coverage_gap: bool = False
    recall_used: bool = False
    recall_persisted_count: int = 0


class ConsolidatedRisk(BaseModel):
    """One pitch-applicable risk consolidated across comparables.

    Attributes:
        summary: Imperative one-liner for the founder. Canonical wording the
            LLM picks when merging paraphrases.
        applies_because: Why this risk hits THIS pitch. Must reference a
            concrete element (asset class, scale, customer type, product
            feature). Empty string is invalid.
        raised_by: candidate_ids that emitted any lesson contributing to this
            risk. Always non-empty for kept risks.
        severity: "high" | "medium" | "low". At most 4 highs per report.
    """

    summary: str
    applies_because: str
    raised_by: list[str]
    severity: Literal["high", "medium", "low"]


class TopRisks(BaseModel):
    """Pitch-applicable consolidated risks, sorted severity desc then raised_by-count desc."""

    risks: list[ConsolidatedRisk] = Field(default_factory=list)
    injection_detected: bool = False


class _LLMConsolidatedRisk(BaseModel):
    summary: str
    applies_because: str
    raised_by: list[str]
    severity: Literal["high", "medium", "low"]


class LLMTopRisksConsolidation(BaseModel):
    """LLM-facing wrapper for the consolidate-risks stage's JSON output."""

    top_risks: list[_LLMConsolidatedRisk] = Field(default_factory=list)
    injection_detected: bool = False


class Report(BaseModel):
    """The user-visible output: input echo, synthesized candidates, and pipeline meta."""

    input: InputContext
    generated_at: datetime
    candidates: list[Synthesis]
    pipeline_meta: PipelineMeta
    top_risks: TopRisks = Field(default_factory=TopRisks)


class ToolSpec(BaseModel):
    """Spec for an LLM-callable tool: name, description, arg model, and async impl."""

    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[..., Awaitable[Any]]  # pyright: ignore[reportExplicitAny]  # tools return varied payloads
    model_config = ConfigDict(arbitrary_types_allowed=True)


class RawEntry(BaseModel):
    """A scraped raw document before canonicalization: source attribution plus bytes."""

    source: str
    source_id: str
    url: str | None
    title: str | None = None
    raw_html: str | None = None
    markdown_text: str | None = None
    fetched_at: datetime
    # True when ``markdown_text`` was synthesized by the LLM pitch filler
    # (no primary HTML body backing it). Threads through to
    # ``CandidatePayload.provenance="synthesized"`` at payload assembly.
    synthesized: bool = False
    # Set by HaikuTitlePreFilter when the HN title fails the cheap "is this a
    # death narrative?" gate. Downstream enrichers (pitch filler) and the
    # ingest classify loop short-circuit on this flag so rejected entries
    # never trigger a Tavily call or a slop classifier call.
    title_pre_filter_rejected: bool = False


class AliasEdge(BaseModel):
    """Edge in the alias graph: links a canonical to a parent, acquirer, or rebrand target."""

    canonical_id: str
    alias_kind: Literal["acquired_by", "rebranded_to", "pivoted_from", "parent_of", "subsidiary_of"]
    target_canonical_id: str
    evidence_source_id: str
    confidence: float


class PendingReviewRow(BaseModel):
    """A row in the entity-resolution ``pending_review`` queue."""

    pair_key: str
    similarity_score: float | None
    haiku_decision: str | None
    haiku_rationale: str | None
    raw_section_heads: str | None


class ReclassifyReport(BaseModel):
    """Result of a ``slopmortem ingest --reclassify`` pass."""

    total: int
    declassified: int
    still_slop: int
    errors: int
