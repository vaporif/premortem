"""Calibration tests for the coverage-gate predicate that triggers LLM recall."""

from __future__ import annotations

import json
from dataclasses import dataclass
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
from slopmortem.stages.coverage_gap import compute_coverage_gap

FIXTURES = Path(__file__).parent.parent / "fixtures" / "coverage_gate"


@dataclass(frozen=True)
class _Fixture:
    retrieved: list[Candidate]
    ranked: list[ScoredCandidate]
    pitch_sector: str
    expected: bool
    n_synthesize: int
    min_similarity_score: float


def _load(name: str) -> _Fixture:
    # n_synthesize / min_similarity_score are optional per-fixture overrides
    # so calibration cases can pin a non-default predicate threshold.
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    retrieved = [Candidate.model_validate(item) for item in data["retrieved"]]
    ranked = [ScoredCandidate.model_validate(item) for item in data["ranked"]]
    return _Fixture(
        retrieved=retrieved,
        ranked=ranked,
        pitch_sector=data["pitch_sector"],
        expected=bool(data["expected_gate"]),
        n_synthesize=int(data.get("n_synthesize", 5)),
        min_similarity_score=float(data.get("min_similarity_score", 4.0)),
    )


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


@pytest.mark.parametrize(
    "name",
    [
        "hacken",
        "splunk_ot",
        "crypto_web3_sparse",
        "wrong_sector_high_quality",
        "mostly_in_sector_low_quality",
        "borderline_one_qualifying",
        "mixed_quality_two_qualifying",
        "exact_n_qualifying",
        "over_n_qualifying",
        "pitch_sector_other_quality_pass",
        "sector_other_in_candidates",
        "ranked_id_not_in_retrieved",
        "exact_min_similarity_bound",
        "n_synthesize_one",
        "empty_ranked_nonempty_retrieved",
    ],
)
def test_calibration_fixture(name: str) -> None:
    fx = _load(name)
    result = compute_coverage_gap(
        retrieved=fx.retrieved,
        ranked=fx.ranked,
        pitch_sector=fx.pitch_sector,
        min_similarity_score=fx.min_similarity_score,
        n_synthesize=fx.n_synthesize,
    ).gap
    assert result is fx.expected


def test_zero_candidates_fires() -> None:
    assert (
        compute_coverage_gap(
            retrieved=[],
            ranked=[],
            pitch_sector="crypto_web3",
            min_similarity_score=4.0,
            n_synthesize=5,
        ).gap
        is True
    )


def test_one_in_sector_high_quality_match_fires() -> None:
    # One survivor under strict filter: 1 < N_synthesize=5, so fire. Regression
    # for the case the unified trigger was added to catch.
    retrieved = [_candidate("only_one", "crypto_web3")]
    ranked = [_scored("only_one", 5.0)]
    assert (
        compute_coverage_gap(
            retrieved=retrieved,
            ranked=ranked,
            pitch_sector="crypto_web3",
            min_similarity_score=4.0,
            n_synthesize=5,
        ).gap
        is True
    )


def test_five_in_sector_high_quality_matches_quiet() -> None:
    retrieved = [_candidate(f"c{i}", "crypto_web3") for i in range(5)]
    ranked = [_scored(f"c{i}", 4.5) for i in range(5)]
    assert (
        compute_coverage_gap(
            retrieved=retrieved,
            ranked=ranked,
            pitch_sector="crypto_web3",
            min_similarity_score=4.0,
            n_synthesize=5,
        ).gap
        is False
    )


def test_pitch_sector_other_skips_sector_check() -> None:
    # pitch_sector="other" — count quality-only — five high-quality candidates
    # of any sector → quiet.
    sectors = ["fintech", "security", "healthtech", "edtech", "biotech"]
    retrieved = [_candidate(f"c{i}", s) for i, s in enumerate(sectors)]
    ranked = [_scored(f"c{i}", 4.5) for i in range(5)]
    assert (
        compute_coverage_gap(
            retrieved=retrieved,
            ranked=ranked,
            pitch_sector="other",
            min_similarity_score=4.0,
            n_synthesize=5,
        ).gap
        is False
    )


def test_wrong_vertical_noise_fires() -> None:
    # Five high-mean (>=6.0) candidates but all in the wrong sector → qualifying=0 → fire.
    retrieved = [_candidate(f"c{i}", "security") for i in range(5)]
    ranked = [_scored(f"c{i}", 6.0) for i in range(5)]
    assert (
        compute_coverage_gap(
            retrieved=retrieved,
            ranked=ranked,
            pitch_sector="crypto_web3",
            min_similarity_score=4.0,
            n_synthesize=5,
        ).gap
        is True
    )


def test_compute_returns_qualifying_count() -> None:
    # compute_coverage_gap exposes the underlying qualifying count so the
    # pipeline can emit it on every query for predicate calibration, not just
    # when the gate fires.
    retrieved = [_candidate(f"c{i}", "crypto_web3") for i in range(3)]
    ranked = [_scored(f"c{i}", 4.5) for i in range(3)]
    result = compute_coverage_gap(
        retrieved=retrieved,
        ranked=ranked,
        pitch_sector="crypto_web3",
        min_similarity_score=4.0,
        n_synthesize=5,
    )
    assert result.qualifying == 3
    assert result.required == 5
    assert result.gap is True
