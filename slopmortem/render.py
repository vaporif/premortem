"""Markdown renderer for `Report`.

Strips clickable links and images from prose as a second line of defense
behind the synthesize-stage URL allowlist: prose like ``where_diverged``
doesn't pass through that filter, and a one-click attacker URL or exfil
pixel in the rendered output is unacceptable.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slopmortem.models import (
        PerspectiveScore,
        PipelineMeta,
        Report,
        SimilarityScores,
        Synthesis,
        TopRisks,
    )

# Inline link must NOT be greedy across lines.
_INLINE_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_REF_LINK = re.compile(r"\[([^\]]+)\]\[[^\]]+\]")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")


def _strip_markdown_links(text: str) -> str:
    # Order matters: images use ``![alt](url)``, which the inline-link pattern
    # would match after the leading ``!`` if it ran first.
    text = _IMAGE.sub(r"\1", text)
    text = _INLINE_LINK.sub(r"\1", text)
    return _REF_LINK.sub(r"\1", text)


def _fmt_score_row(label: str, score: PerspectiveScore) -> str:
    rationale = _strip_markdown_links(score.rationale)
    return f"| {label} | {score.score:.1f} | {rationale} |"


def _render_similarity_table(sim: SimilarityScores) -> str:
    rows = [
        "| Perspective | Score | Rationale |",
        "| --- | --- | --- |",
        _fmt_score_row("business_model", sim.business_model),
        _fmt_score_row("market", sim.market),
        _fmt_score_row("gtm", sim.gtm),
        _fmt_score_row("stage_scale", sim.stage_scale),
    ]
    return "\n".join(rows)


def _render_bullets(items: list[str]) -> str:
    return "\n".join(f"- {_strip_markdown_links(item)}" for item in items)


def _render_candidate(syn: Synthesis) -> str:
    failure_date_str = syn.failure_date.isoformat() if syn.failure_date else "unknown"
    lifespan_str = f"{syn.lifespan_months} months" if syn.lifespan_months is not None else "unknown"
    parts: list[str] = [f"## {syn.name}"]
    # llm_recall comparables are LLM-named and web-verified — weaker grounding
    # than a crawler-sourced obit. Tag them so the reader discounts accordingly.
    if syn.source == "llm_recall":
        parts.append("*Source: LLM recall (verified against live web)*")
    parts.extend(
        [
            "",
            _strip_markdown_links(syn.one_liner),
            "",
            f"Failure date: {failure_date_str}",
            f"Lifespan: {lifespan_str}",
            "",
            "Similarity:",
            "",
            _render_similarity_table(syn.similarity),
            "",
            "Why similar:",
            "",
            _strip_markdown_links(syn.why_similar),
            "",
            "Where diverged:",
            "",
            _strip_markdown_links(syn.where_diverged),
            "",
            "Failure causes:",
            "",
            _render_bullets(syn.failure_causes),
            "",
            "Lessons:",
            "",
            _render_bullets(syn.lessons_for_input),
            "",
            "Sources:",
            "",
            "\n".join(syn.sources),
        ]
    )
    return "\n".join(parts)


def _render_top_risks(top_risks: TopRisks, candidates: list[Synthesis]) -> str:
    # Unknown ids fall back to the raw id string — defensive, the consolidator
    # only sees ids from the same syntheses list.
    id_to_name = {c.candidate_id: c.name for c in candidates}
    total = len(candidates)
    lines: list[str] = ["## Top risks across all comparables", ""]
    for idx, risk in enumerate(top_risks.risks, start=1):
        names = ", ".join(id_to_name.get(cid, cid) for cid in risk.raised_by)
        lines.append(f"{idx}. [{risk.severity.upper()}] {_strip_markdown_links(risk.summary)}")
        lines.append(f"   Applies because: {_strip_markdown_links(risk.applies_because)}")
        lines.append(f"   Raised by: {names} ({len(risk.raised_by)}/{total})")
        lines.append("")
    return "\n".join(lines)


def _render_footer(meta: PipelineMeta) -> str:
    models_block = "\n".join(f"- {role}: {model}" for role, model in sorted(meta.models.items()))
    lines: list[str] = [
        "---",
        "",
        "Pipeline meta:",
        "",
        f"- cost_usd_total: {meta.cost_usd_total:.4f}",
        f"- latency_ms_total: {meta.latency_ms_total}",
        f"- trace_id: {meta.trace_id or 'none'}",
        f"- budget_remaining_usd: {meta.budget_remaining_usd:.4f}",
        f"- budget_exceeded: {meta.budget_exceeded}",
        f"- K_retrieve: {meta.K_retrieve}",
        f"- N_synthesize: {meta.N_synthesize}",
        f"- min_similarity_score: {meta.min_similarity_score:.1f}",
    ]
    # Recall fallback signals: only render when non-default so existing
    # report shapes don't grow noisy lines on every query.
    if meta.coverage_gap:
        lines.append(f"- coverage_gap: {meta.coverage_gap}")
    if meta.recall_used:
        lines.append(f"- recall_used: {meta.recall_used}")
    if meta.recall_persisted_count:
        lines.append(f"- recall_persisted_count: {meta.recall_persisted_count}")
    lines.extend(
        [
            "",
            "Models:",
            "",
            models_block,
        ]
    )
    return "\n".join(lines)


def _render_no_comparables_banner(meta: PipelineMeta) -> str:
    threshold = meta.min_similarity_score
    dropped = meta.filtered_pre_synth + meta.filtered_post_synth
    if dropped > 0:
        return (
            f"No comparables passed similarity threshold {threshold:.1f}. "
            f"{dropped} candidate(s) were filtered out. Try lowering "
            f"min_similarity_score in slopmortem.toml."
        )
    return (
        f"No comparables passed similarity threshold {threshold:.1f}. "
        "The pitch may be outside the corpus, or the threshold may be too strict "
        "(min_similarity_score in slopmortem.toml)."
    )


def render(report: Report) -> str:
    """Render *report* to plain markdown, stripping clickable links.

    Inline links, reference-style links, and image markdown are stripped from
    every prose field. Sources go out as plain URLs, one per line, so no
    clickable autolink reaches a markdown viewer.
    """
    sections: list[str] = [
        f"# Slopmortem report for {report.input.name}",
        "",
        f"Pitch: {_strip_markdown_links(report.input.description)}",
        "",
        f"Generated: {report.generated_at.isoformat()}",
        "",
    ]
    if not report.candidates and not report.pipeline_meta.budget_exceeded:
        sections.append(_render_no_comparables_banner(report.pipeline_meta))
        sections.append("")
    if report.top_risks.risks:
        sections.append(_render_top_risks(report.top_risks, report.candidates))
    for syn in report.candidates:
        sections.append(_render_candidate(syn))
        sections.append("")
    sections.append(_render_footer(report.pipeline_meta))
    return "\n".join(sections)
