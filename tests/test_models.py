from __future__ import annotations

from slopmortem.models import (
    Candidate,
    CandidatePayload,
    Facets,
    InputContext,
    MergeState,
    PerspectiveScore,
    ScoredCandidate,
    SimilarityScores,
)


def test_similarity_scores_strict_keys():
    SimilarityScores(
        business_model=PerspectiveScore(score=8.0, rationale="x"),
        market=PerspectiveScore(score=7.0, rationale="y"),
        gtm=PerspectiveScore(score=6.0, rationale="z"),
        stage_scale=PerspectiveScore(score=5.0, rationale="w"),
    )
    schema = SimilarityScores.model_json_schema()
    # strict-mode requirement: closed object, no additionalProperties
    assert schema.get("additionalProperties") in (False, None)
    assert set(schema["properties"].keys()) == {"business_model", "market", "gtm", "stage_scale"}


def test_facets_field_names_singular_match_taxonomy():
    Facets(
        sector="fintech",
        business_model="b2b_saas",
        customer_type="smb",
        geography="us",
        monetization="subscription_recurring",
    )
    fields = set(Facets.model_fields.keys())
    assert {"sector", "business_model", "customer_type", "geography", "monetization"} <= fields
    # NOT plural; guards against silent FormulaQuery boost mismatch.
    assert "sectors" not in fields
    assert "business_models" not in fields


def test_candidate_alias_canonicals_default_empty():
    c = Candidate(
        canonical_id="acme.com",
        score=0.5,
        payload=CandidatePayload(
            name="Acme",
            summary="s",
            body="b",
            facets=Facets(
                sector="fintech",
                business_model="b2b_saas",
                customer_type="smb",
                geography="us",
                monetization="subscription_recurring",
            ),
            founding_date=None,
            failure_date=None,
            founding_date_unknown=True,
            failure_date_unknown=True,
            provenance="curated_real",
            slop_score=0.1,
            sources=["https://acme.com"],
            text_id="0123456789abcdef",
        ),
    )
    assert c.alias_canonicals == []


def test_input_context_fields():
    ic = InputContext(name="MedScribe", description="...", years_filter=5)
    assert ic.years_filter == 5
    ic2 = InputContext(name="X", description="y")
    assert ic2.years_filter is None


def test_scored_candidate_minimal_shape():
    ScoredCandidate(
        candidate_id="acme.com",
        perspective_scores=SimilarityScores(
            business_model=PerspectiveScore(score=1, rationale="a"),
            market=PerspectiveScore(score=1, rationale="a"),
            gtm=PerspectiveScore(score=1, rationale="a"),
            stage_scale=PerspectiveScore(score=1, rationale="a"),
        ),
        rationale="one liner",
    )
    # No embedded Candidate; drift guard.
    assert "candidate" not in ScoredCandidate.model_fields


def test_merge_state_enum_values():
    assert {s.value for s in MergeState} == {
        "pending",
        "complete",
        "alias_blocked",
        "resolver_flipped",
    }
    # NOT "quarantined"; quarantined docs live in quarantine_journal (Blocker B4).
    assert not hasattr(MergeState, "QUARANTINED")


def test_similarity_scores_mean_averages_four_perspectives() -> None:
    from slopmortem.models import PerspectiveScore, SimilarityScores  # noqa: PLC0415

    scores = SimilarityScores(
        business_model=PerspectiveScore(score=8.0, rationale="x"),
        market=PerspectiveScore(score=6.0, rationale="x"),
        gtm=PerspectiveScore(score=4.0, rationale="x"),
        stage_scale=PerspectiveScore(score=2.0, rationale="x"),
    )
    assert scores.mean() == 5.0


def test_raw_entry_title_pre_filter_rejected_defaults_false() -> None:
    from datetime import UTC, datetime  # noqa: PLC0415

    from slopmortem.models import RawEntry  # noqa: PLC0415

    e = RawEntry(
        source="hn_algolia",
        source_id="x",
        url="https://example.com",
        fetched_at=datetime.now(UTC),
    )
    assert e.title_pre_filter_rejected is False


def test_raw_entry_title_pre_filter_rejected_can_be_set() -> None:
    from datetime import UTC, datetime  # noqa: PLC0415

    from slopmortem.models import RawEntry  # noqa: PLC0415

    e = RawEntry(
        source="hn_algolia",
        source_id="x",
        url="https://example.com",
        fetched_at=datetime.now(UTC),
        title_pre_filter_rejected=True,
    )
    assert e.title_pre_filter_rejected is True
