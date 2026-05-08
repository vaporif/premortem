"""Calibration tests for the coverage-gate predicate that triggers LLM recall."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from slopmortem.models import (
    Candidate,
    CandidatePayload,
    Facets,
    PerspectiveScore,
    ScoredCandidate,
    SimilarityScores,
)
from slopmortem.stages import detect_coverage_gap

FIXTURES = Path(__file__).parent.parent / "fixtures" / "coverage_gate"


def _load(name: str) -> tuple[list[Candidate], list[ScoredCandidate], str, bool]:
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    retrieved = [Candidate.model_validate(item) for item in data["retrieved"]]
    ranked = [ScoredCandidate.model_validate(item) for item in data["ranked"]]
    return retrieved, ranked, data["pitch_sector"], bool(data["expected_gate"])


def _facets(sector: str) -> Facets:
    return Facets(
        sector=sector,
        business_model="b2b_saas",
        customer_type="enterprise",
        geography="us",
        monetization="subscription_recurring",
    )


def _payload(*, name: str, sector: str) -> CandidatePayload:
    return CandidatePayload(
        name=name,
        summary=f"{name} summary",
        body=f"{name} body",
        facets=_facets(sector),
        founding_date=date(2015, 1, 1),
        failure_date=date(2022, 1, 1),
        founding_date_unknown=False,
        failure_date_unknown=False,
        provenance="curated_real",
        slop_score=0.0,
        sources=[],
        provenance_id=f"curated:{name}",
        text_id=name.replace(" ", "_").lower(),
    )


def _candidate(canonical_id: str, sector: str) -> Candidate:
    return Candidate(
        canonical_id=canonical_id,
        score=0.8,
        payload=_payload(name=canonical_id, sector=sector),
    )


def _scores(mean: float) -> SimilarityScores:
    """Build a SimilarityScores with all four axes equal to ``mean``."""
    return SimilarityScores(
        business_model=PerspectiveScore(score=mean, rationale="r"),
        market=PerspectiveScore(score=mean, rationale="r"),
        gtm=PerspectiveScore(score=mean, rationale="r"),
        stage_scale=PerspectiveScore(score=mean, rationale="r"),
    )


def _scored(candidate_id: str, mean: float) -> ScoredCandidate:
    return ScoredCandidate(
        candidate_id=candidate_id,
        perspective_scores=_scores(mean),
        rationale="r",
    )


@pytest.mark.parametrize("name", ["hacken", "splunk_ot", "crypto_web3_sparse"])
def test_calibration_fixture(name: str) -> None:
    retrieved, ranked, pitch_sector, expected = _load(name)
    result = detect_coverage_gap(
        retrieved=retrieved,
        ranked=ranked,
        pitch_sector=pitch_sector,
        min_similarity_score=4.0,
        n_synthesize=5,
    )
    assert result is expected


def test_zero_candidates_fires() -> None:
    assert (
        detect_coverage_gap(
            retrieved=[],
            ranked=[],
            pitch_sector="crypto_web3",
            min_similarity_score=4.0,
            n_synthesize=5,
        )
        is True
    )


def test_one_in_sector_high_quality_match_fires() -> None:
    # Strict filter cut to 1 — qualifying_count=1 < N_synthesize=5 — fire.
    # This is the regression test for the bug the unified trigger fixes.
    retrieved = [_candidate("only_one", "crypto_web3")]
    ranked = [_scored("only_one", 5.0)]
    assert (
        detect_coverage_gap(
            retrieved=retrieved,
            ranked=ranked,
            pitch_sector="crypto_web3",
            min_similarity_score=4.0,
            n_synthesize=5,
        )
        is True
    )


def test_five_in_sector_high_quality_matches_quiet() -> None:
    retrieved = [_candidate(f"c{i}", "crypto_web3") for i in range(5)]
    ranked = [_scored(f"c{i}", 4.5) for i in range(5)]
    assert (
        detect_coverage_gap(
            retrieved=retrieved,
            ranked=ranked,
            pitch_sector="crypto_web3",
            min_similarity_score=4.0,
            n_synthesize=5,
        )
        is False
    )


def test_pitch_sector_other_skips_sector_check() -> None:
    # pitch_sector="other" — count quality-only — five high-quality candidates
    # of any sector → quiet.
    sectors = ["fintech", "security", "healthtech", "edtech", "biotech"]
    retrieved = [_candidate(f"c{i}", s) for i, s in enumerate(sectors)]
    ranked = [_scored(f"c{i}", 4.5) for i in range(5)]
    assert (
        detect_coverage_gap(
            retrieved=retrieved,
            ranked=ranked,
            pitch_sector="other",
            min_similarity_score=4.0,
            n_synthesize=5,
        )
        is False
    )


def test_wrong_vertical_noise_fires() -> None:
    # Five candidates with mean >= 6.0 but all in wrong sector → qualifying=0 → fire.
    # Replaces the old vertical-axis-collapse test.
    retrieved = [_candidate(f"c{i}", "security") for i in range(5)]
    ranked = [_scored(f"c{i}", 6.0) for i in range(5)]
    assert (
        detect_coverage_gap(
            retrieved=retrieved,
            ranked=ranked,
            pitch_sector="crypto_web3",
            min_similarity_score=4.0,
            n_synthesize=5,
        )
        is True
    )
