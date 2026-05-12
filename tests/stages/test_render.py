"""Tests for ``slopmortem.render``: pure markdown emit, autolink and image stripping, no I/O."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from slopmortem.models import (
    ConsolidatedRisk,
    InputContext,
    PerspectiveScore,
    PipelineMeta,
    Report,
    SimilarityScores,
    Synthesis,
    TopRisks,
)
from slopmortem.render import _render_candidate, render

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion


def _scores(value: float = 5.0) -> SimilarityScores:
    return SimilarityScores(
        business_model=PerspectiveScore(score=value, rationale="bm rationale"),
        market=PerspectiveScore(score=value, rationale="market rationale"),
        gtm=PerspectiveScore(score=value, rationale="gtm rationale"),
        stage_scale=PerspectiveScore(score=value, rationale="stage rationale"),
    )


def _synthesis_clean() -> Synthesis:
    return Synthesis(
        candidate_id="acme-corp",
        name="Acme",
        one_liner="B2B fintech for SMB invoicing.",
        failure_date=date(2023, 1, 1),
        lifespan_months=60,
        similarity=_scores(7.0),
        why_similar="Both target SMB invoicing.",
        where_diverged="New pitch is web-first; Acme was mobile-only.",
        failure_causes=["CAC > LTV", "long sales cycles"],
        lessons_for_input=["target larger ACVs", "avoid SMB churn"],
        sources=["https://acme.com/postmortem", "https://news.ycombinator.com/item?id=1"],
    )


def _synthesis_with_attacker_links() -> Synthesis:
    """Synthesis whose prose deliberately includes inline links and an image.

    The renderer must strip them before they reach markdown, otherwise
    a terminal or markdown viewer would render them as one-click attacker URLs
    or as an exfil pixel.
    """
    return Synthesis(
        candidate_id="beta-co",
        name="BetaCo",
        one_liner="A/B-test SaaS for marketers.",
        failure_date=None,
        lifespan_months=None,
        similarity=_scores(4.0),
        why_similar="They served the same SMB cohort.",
        where_diverged=(
            "BetaCo was acquired in 2021. See [click here](https://attacker.com) "
            "and ![pwn](https://attacker.com/x.png) for details. "
            "Reference-style: [docs][ref]."
        ),
        failure_causes=["pivoted too late"],
        lessons_for_input=["[click](https://attacker.com)", "be honest"],
        sources=["https://betaco.example/blog"],
    )


def _report() -> Report:
    return Report(
        input=InputContext(name="newco", description="A B2B fintech for SMB invoicing"),
        generated_at=datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC),
        candidates=[_synthesis_clean(), _synthesis_with_attacker_links()],
        pipeline_meta=PipelineMeta(
            K_retrieve=30,
            N_synthesize=5,
            min_similarity_score=4.0,
            models={"rerank": "anthropic/claude-sonnet-4.6"},
            cost_usd_total=0.42,
            latency_ms_total=1234,
            trace_id="trace-abc",
            budget_remaining_usd=1.58,
            budget_exceeded=False,
        ),
        top_risks=TopRisks(
            risks=[
                ConsolidatedRisk(
                    summary="target larger ACVs",
                    applies_because="pitch sells the same SMB invoicing motion as Acme.",
                    raised_by=["acme-corp"],
                    severity="high",
                ),
                ConsolidatedRisk(
                    summary="be honest",
                    applies_because="pitch claims a moat without naming one.",
                    raised_by=["beta-co"],
                    severity="medium",
                ),
            ]
        ),
    )


def _structural_keys(md: str) -> list[str]:
    """Extract heading lines and labelled fields so the snapshot is structural-only."""
    keys: list[str] = []
    for line in md.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or re.match(r"^[A-Z][A-Za-z _-]+:\s*$", stripped):
            keys.append(stripped)
    return keys


def test_render_strips_autolinks_and_images(snapshot: SnapshotAssertion) -> None:
    md = render(_report())

    assert not re.search(r"\[[^\]]+\]\([^)]+\)", md)  # no inline links
    assert not re.search(r"\[[^\]]+\]\[[^\]]+\]", md)  # no reference-style links
    assert "![" not in md  # no images

    assert _structural_keys(md) == snapshot


def test_render_emits_one_section_per_candidate() -> None:
    md = render(_report())
    # Two candidate names; the renderer puts each one in a level-2 heading.
    assert "## Acme" in md or "Acme" in md
    assert "BetaCo" in md
    # Footer carries pipeline_meta.
    assert "trace-abc" in md
    assert "0.42" in md or "$0.42" in md
    assert "1234" in md


def test_render_keeps_sources_as_plain_text() -> None:
    md = render(_report())
    # Sources list URLs but does NOT wrap them in ``[]()`` markdown link syntax.
    assert "https://acme.com/postmortem" in md
    assert "https://news.ycombinator.com/item?id=1" in md
    # Defense-in-depth: even if a synthesis somehow contained an attacker URL
    # in ``where_diverged`` prose, the autolink stripper killed it.
    assert "[click here]" not in md
    assert "[click](" not in md


def test_render_emits_top_risks_section_when_present() -> None:
    md = render(_report())
    assert "## Top risks across all comparables" in md
    # Numbered list items render with severity tag, applies_because, raised_by.
    assert "[HIGH]" in md
    assert "[MEDIUM]" in md
    assert "Applies because:" in md
    assert "Raised by:" in md
    # The total denominator is the candidate count (2 in this fixture).
    assert "(1/2)" in md


def test_render_omits_top_risks_when_empty() -> None:
    report = _report()
    report_no_risks = report.model_copy(update={"top_risks": TopRisks(risks=[])})
    md = render(report_no_risks)
    assert "## Top risks across all comparables" not in md


def test_render_emits_banner_when_no_candidates_pass_threshold() -> None:
    """Empty candidates list with a finished run renders the threshold banner."""
    report = _report().model_copy(update={"candidates": [], "top_risks": TopRisks(risks=[])})
    md = render(report)
    assert "No comparables passed similarity threshold 4.0" in md
    assert "min_similarity_score" in md
    assert "## Acme" not in md
    assert "## BetaCo" not in md


def test_render_skips_banner_on_budget_exceeded() -> None:
    """Budget-truncated runs skip the banner — the empty candidates have a different cause."""
    report = _report().model_copy(update={"candidates": [], "top_risks": TopRisks(risks=[])})
    report = report.model_copy(
        update={"pipeline_meta": report.pipeline_meta.model_copy(update={"budget_exceeded": True})}
    )
    md = render(report)
    assert "No comparables passed similarity threshold" not in md


def test_render_footer_includes_min_similarity_score() -> None:
    md = render(_report())
    assert "min_similarity_score: 4.0" in md


def test_render_footer_includes_recall_flags_when_set() -> None:
    """``coverage_gap`` / ``recall_used`` / ``recall_persisted_count`` only render when non-default.

    The footer must surface all three when set so eval and ops can attribute
    extra cost and corpus growth to the recall path; default reports keep
    the footer terse.
    """
    report = _report().model_copy(
        update={
            "pipeline_meta": _report().pipeline_meta.model_copy(
                update={
                    "coverage_gap": True,
                    "recall_used": True,
                    "recall_persisted_count": 2,
                }
            )
        }
    )
    md = render(report)
    assert "- coverage_gap: True" in md
    assert "- recall_used: True" in md
    assert "- recall_persisted_count: 2" in md


def test_render_footer_omits_recall_flags_when_default() -> None:
    """Default ``PipelineMeta`` (recall not used) keeps the three lines out of the footer."""
    md = render(_report())
    assert "coverage_gap" not in md
    assert "recall_used" not in md
    assert "recall_persisted_count" not in md


def _synthesis_with_source(source: str | None) -> Synthesis:
    return Synthesis(
        candidate_id="recall-co",
        name="RecallCo",
        one_liner="LLM-recalled comparable.",
        failure_date=date(2022, 6, 1),
        lifespan_months=24,
        similarity=_scores(5.0),
        why_similar="They served the same niche.",
        where_diverged="Different geo footprint.",
        failure_causes=["regulation"],
        lessons_for_input=["watch the regulator"],
        sources=["https://recall.example/post"],
        source=source,
    )


def test_render_marks_recall_provenance() -> None:
    """``source == "llm_recall"`` surfaces a provenance tag in the rendered candidate.

    The tag warns the reader that the comparable was named by an LLM and only
    web-verified — weaker grounding than a crawler-sourced obit.
    """
    out = _render_candidate(_synthesis_with_source("llm_recall"))
    assert "Source: LLM recall (verified against live web)" in out


def test_render_omits_recall_tag_for_other_sources() -> None:
    """Crawler-sourced candidates render without the recall provenance line."""
    out = _render_candidate(_synthesis_with_source("crunchbase_csv"))
    assert "Source: LLM recall" not in out


def test_render_omits_recall_tag_when_source_missing() -> None:
    """Legacy syntheses (``source=None``) render without the recall provenance line."""
    out = _render_candidate(_synthesis_with_source(None))
    assert "Source: LLM recall" not in out


def _synthesis_struggling() -> Synthesis:
    """Synthesis whose source candidate carries L5 ``deathness_verdict="struggling"``.

    Mirrors ``_synthesis_clean`` (curated obit) so the test only varies the
    verdict; the renderer's struggling branch is what's under test.
    """
    return Synthesis(
        candidate_id="impaired-co",
        name="ImpairedCo",
        one_liner="SMB invoicing vendor mid-restructuring.",
        failure_date=date(2025, 11, 1),
        lifespan_months=84,
        similarity=_scores(6.0),
        why_similar="Same SMB invoicing motion as the pitch.",
        where_diverged="Pitch is greenfield; ImpairedCo is mid-pivot.",
        failure_causes=["active layoffs", "missed Q3 revenue"],
        lessons_for_input=["watch burn early", "avoid SMB-heavy ARR mix"],
        sources=["https://news.example/impairedco-layoffs"],
        deathness_verdict="struggling",
    )


def test_render_struggling_uses_distress_heading() -> None:
    """Struggling vendors get a forward-looking heading + "Distress observed:" label.

    The default "## Name" + "Failure date:" framing reads as a post-mortem;
    a still-operating but impaired vendor must not be presented as dead.
    """
    out = _render_candidate(_synthesis_struggling())
    assert "## ImpairedCo — currently struggling" in out
    assert "Distress observed: 2025-11-01" in out
    # Lifespan is omitted for struggling vendors — open-ended runway is not
    # a number worth printing alongside layoff signals.
    assert "Lifespan:" not in out


def test_render_dead_keeps_default_failure_heading() -> None:
    """Default ``deathness_verdict=None`` (or ``"dead"``) keeps the post-mortem framing."""
    out = _render_candidate(_synthesis_clean())
    assert "## Acme" in out
    assert "currently struggling" not in out
    assert "Failure date: 2023-01-01" in out
    assert "Lifespan: 60 months" in out


def test_render_is_pure_no_io() -> None:
    """``render`` must not touch the filesystem.

    Static check on the source. Cheaper and more deterministic than trying
    to monkeypatch ``open`` or ``Path`` at runtime through syrupy's own I/O.
    """
    src = Path(__file__).resolve().parents[2] / "slopmortem" / "render.py"
    text = src.read_text()
    assert "open(" not in text, "render.py must not call open()"
    assert "Path(" not in text, "render.py must not construct Path objects"
