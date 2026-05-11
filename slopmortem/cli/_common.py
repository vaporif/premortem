"""Shared helpers used by 2+ subcommand modules.

Lives here so subcommand files can import without forming circular dependencies
through ``cli/__init__.py``. The leading underscore signals package-private;
the import-linter contract enforces it.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import typer
from lmnr import Laminar
from rich.panel import Panel

from slopmortem.cli_progress import RichPhaseProgress
from slopmortem.pipeline import QueryPhase
from slopmortem.tracing import init_tracing

if TYPE_CHECKING:
    from rich.console import Console

    from slopmortem.config import Config
    from slopmortem.models import Report

# ``__all__`` flags these underscore-prefixed names as intentional package-private
# exports so basedpyright stops reporting reportPrivateUsage at the import sites
# in ``_*_cmd.py``.
__all__ = [
    "_QUERY_PHASE_LABELS",
    "RichQueryProgress",
    "_maybe_init_tracing",
    "_maybe_setup_logging",
    "_render_query_footer",
]


def _maybe_setup_logging() -> None:
    """Configure stdlib logging from ``SLOPMORTEM_LOG`` env var.

    Off by default so library use of the CLI module doesn't hijack the root
    logger. Set ``SLOPMORTEM_LOG=info`` (or ``debug``) to see per-entry ingest
    progress (tavily fill lines, ingest save lines).
    Third-party loggers (httpx, lmnr) are pinned to WARNING so the slopmortem
    signal isn't drowned out.
    """
    level_name = os.environ.get("SLOPMORTEM_LOG", "").strip().lower()
    if not level_name:
        return
    level = getattr(logging, level_name.upper(), None)
    if not isinstance(level, int):
        return
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "lmnr", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _maybe_init_tracing(config: Config) -> None:
    """Opt-in Laminar init gated on ``enable_tracing`` and a non-empty API key."""
    base_url = config.lmnr_base_url or None
    init_tracing(
        base_url=base_url,
        allow_remote=bool(config.lmnr_allow_remote),
    )
    if not config.enable_tracing:
        return
    api_key = config.lmnr_project_api_key.get_secret_value()
    if not api_key:
        typer.echo(
            "slopmortem: LMNR_PROJECT_API_KEY missing; tracing disabled",
            err=True,
        )
        return
    Laminar.initialize(project_api_key=api_key, base_url=base_url)


_QUERY_PHASE_LABELS: dict[QueryPhase, str] = {
    QueryPhase.FACET_EXTRACT: "Extracting facets",
    QueryPhase.RETRIEVE: "Retrieving candidates",
    QueryPhase.RERANK: "Reranking candidates",
    QueryPhase.RECALL: "Recalling from memory",
    QueryPhase.SYNTHESIZE: "Synthesizing post-mortems",
}


class RichQueryProgress(RichPhaseProgress[QueryPhase]):
    def __init__(self) -> None:
        super().__init__(_QUERY_PHASE_LABELS)


def _render_query_footer(console: Console, report: Report) -> None:
    meta = report.pipeline_meta
    parts = [
        f"cost=${meta.cost_usd_total:.4f}",
        f"latency={meta.latency_ms_total}ms",
        f"synthesized={len(report.candidates)}",
    ]
    if meta.filtered_pre_synth > 0:
        parts.append(f"filtered_pre_synth={meta.filtered_pre_synth}")
    if meta.trace_id:
        parts.append(f"trace={meta.trace_id}")
    if meta.budget_exceeded:
        parts.append("[bold red]budget_exceeded[/bold red]")
    console.print(
        Panel(
            " • ".join(parts),
            title="[bold cyan]done[/bold cyan]",
            title_align="left",
            border_style="cyan",
            expand=False,
        )
    )
