"""Shared helpers used by 2+ subcommand modules.

Lives here so subcommand files can import without forming circular dependencies
through ``cli/__init__.py``. The leading underscore signals package-private;
the import-linter contract enforces it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from typing import TYPE_CHECKING

import typer
from lmnr import Laminar
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel

from slopmortem.cli_progress import RichPhaseProgress
from slopmortem.pipeline import QueryPhase
from slopmortem.tracing import init_tracing

if TYPE_CHECKING:
    from collections.abc import Callable

    from slopmortem.config import Config
    from slopmortem.models import Report

# Shared between ``_maybe_setup_logging``'s ``RichHandler`` and every
# ``RichPhaseProgress`` instance the CLI builds. Live's render-height
# tracking only stays consistent when log lines and bar refreshes go
# through the same ``Console``: ``Console.print`` acquires Live's lock
# and prints above the live region. A stdlib ``StreamHandler`` (or a
# second ``Console(stderr=True)``) would write past Live's redirect
# proxy and orphan a copy of the phase table per log line.
_STDERR_CONSOLE = Console(stderr=True)


# ``__all__`` flags these underscore-prefixed names as intentional package-private
# exports so basedpyright stops reporting reportPrivateUsage at the import sites
# in ``_*_cmd.py``.
__all__ = [
    "_QUERY_PHASE_LABELS",
    "_STDERR_CONSOLE",
    "RichQueryProgress",
    "_maybe_init_tracing",
    "_maybe_setup_logging",
    "_render_query_footer",
    "progress_context",
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
    # RichHandler routes records through ``_STDERR_CONSOLE.print``, which is
    # Live-aware: it pauses the live region, prints above it, and resumes.
    # A plain ``StreamHandler`` would capture the original ``sys.stderr`` at
    # construction time and bypass Live's redirect proxy, leaving one stranded
    # copy of the phase table per emitted record.
    handler = RichHandler(
        console=_STDERR_CONSOLE,
        show_path=False,
        markup=False,
        rich_tracebacks=False,
    )
    # ``force=True`` so we still install our handler if an earlier import
    # (Laminar, test runner) already touched the root logger.
    logging.basicConfig(level=level, format="%(name)s: %(message)s", handlers=[handler], force=True)
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
        super().__init__(_QUERY_PHASE_LABELS, console=_STDERR_CONSOLE)


def progress_context[T](
    factory: Callable[[], contextlib.AbstractContextManager[T]],
) -> contextlib.AbstractContextManager[T | None]:
    """TTY-gated factory for the Rich phase bar. ``None`` when the bar is suppressed.

    Three independent gates: ``SLOPMORTEM_NO_PROGRESS`` env escape hatch,
    Python's fileno-level TTY check on stderr, and Rich's own ``is_terminal``
    probe. Non-tty environments (redirected stderr, CI without a pty) fall
    back to ``nullcontext`` — stdlib logging still surfaces the same info via
    ``SLOPMORTEM_LOG=info``.
    """
    if os.environ.get("SLOPMORTEM_NO_PROGRESS"):
        return contextlib.nullcontext()
    if not sys.stderr.isatty():
        return contextlib.nullcontext()
    if not Console(stderr=True).is_terminal:
        return contextlib.nullcontext()
    return factory()


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
