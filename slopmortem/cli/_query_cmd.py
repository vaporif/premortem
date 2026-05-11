# ruff: noqa: FBT002 - typer signatures are bool flags with False defaults by convention.
"""``slopmortem query`` subcommand.

Stage progress goes to stderr (TTY-gated); the rendered Markdown report goes to
stdout (with ``--stdout``) or to ``.slopmortem/runs/<utc-ts>-<slug>.md``.
"""

from __future__ import annotations

import contextlib
import functools
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import anyio
import typer
from lmnr import observe
from rich.console import Console

from slopmortem.cli import app
from slopmortem.cli._common import (
    RichQueryProgress,
    _maybe_init_tracing,
    _maybe_setup_logging,
    _render_query_footer,
)
from slopmortem.config import load_config
from slopmortem.corpus import set_query_corpus
from slopmortem.deps import build_deps, build_tavily_recall_extract, build_tavily_recall_search
from slopmortem.models import InputContext
from slopmortem.pipeline import RecallDeps, cutoff_iso, run_query
from slopmortem.render import render
from slopmortem.stages import extract_facets, retrieve

if TYPE_CHECKING:
    from slopmortem.config import Config
    from slopmortem.corpus import Corpus
    from slopmortem.llm import EmbeddingClient, LLMClient


_RUNS_DIR = Path(".slopmortem/runs")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SLUG_MAX = 40

_NOT_FOUND_MARKDOWN = """# No matching post-mortems found

The query returned no synthesized candidates above the similarity floor.
Try broadening the description or removing the `--years` filter."""


def _slugify(text: str) -> str:
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    return s[:_SLUG_MAX].rstrip("-") or "run"


def _query_run_path(ctx: InputContext) -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = ctx.name if ctx.name and ctx.name != "(unnamed)" else ctx.description
    return _RUNS_DIR / f"{ts}-{_slugify(base)}.md"


def _progress_context() -> contextlib.AbstractContextManager[RichQueryProgress | None]:
    """Decide whether the live Rich progress bar is safe to render.

    Rich's Live mode splats each frame as a new line when it can't do cursor
    positioning (e.g., ``just``'s pty wrapper, some CI shells). In those cases
    every tick redraws the whole phase table and the user sees the bar
    repeated many times. Disable the bar instead — stdlib logging still
    surfaces the same info via ``SLOPMORTEM_LOG=info``.

    Three independent gates: ``SLOPMORTEM_NO_PROGRESS`` env escape hatch,
    Python's fileno-level TTY check on stderr, and Rich's own ``is_terminal``
    probe (which catches the pty-but-not-cursor-positionable case).
    """
    if os.environ.get("SLOPMORTEM_NO_PROGRESS"):
        return contextlib.nullcontext()
    if not sys.stderr.isatty():
        return contextlib.nullcontext()
    if not Console(stderr=True).is_terminal:
        return contextlib.nullcontext()
    return RichQueryProgress()


@app.command("query")
def query_cmd(  # noqa: PLR0913 - every flag mirrors a knob; user types kwargs.
    description: Annotated[
        str,
        typer.Argument(help="The pitch text to analyze."),
    ],
    name: Annotated[
        str | None,
        typer.Option("--name", help="Optional name for the input pitch."),
    ] = None,
    years: Annotated[
        int | None,
        typer.Option(
            "--years",
            help="Only consider candidates whose failure_date falls within this many years.",
        ),
    ] = None,
    debug_retrieve: Annotated[
        bool,
        typer.Option(
            "--debug-retrieve",
            help=(
                "Run facet_extract + retrieve only; print the candidate list and exit. "
                "Skips rerank and synthesize so retrieval can be inspected in isolation."
            ),
        ),
    ] = False,
    to_stdout: Annotated[
        bool,
        typer.Option(
            "--stdout",
            help=(
                "Print the rendered report to stdout instead of writing it under "
                ".slopmortem/runs/. Use when piping the report into another tool."
            ),
        ),
    ] = False,
    force_llm_recall: Annotated[
        bool,
        typer.Option(
            "--force-llm-recall/--no-force-llm-recall",
            help=(
                "Fire LLM recall on every query regardless of the coverage gate. "
                "Default behaviour already fires recall when the gate trips; this "
                "flag bypasses the gate. Use for cassette recording, eval "
                "calibration, or thin/new corpora."
            ),
        ),
    ] = False,
) -> None:
    """When no candidates clear the similarity floor, exits 1 and writes no file.

    Tracing is gated on ``Config.enable_tracing`` and ``LMNR_PROJECT_API_KEY``;
    if the key is missing but tracing is enabled, the run continues untraced
    with a one-line warning on stderr.
    """
    anyio.run(
        functools.partial(
            _query,
            description=description,
            name=name,
            years=years,
            debug_retrieve=debug_retrieve,
            to_stdout=to_stdout,
            force_llm_recall=force_llm_recall,
        )
    )


@observe(name="cli.query")
async def _query(  # noqa: PLR0913 - mirrors ``query_cmd``'s flag surface.
    *,
    description: str,
    name: str | None,
    years: int | None,
    debug_retrieve: bool = False,
    to_stdout: bool = False,
    force_llm_recall: bool = False,
) -> None:
    config = load_config()
    # ``model_copy(update=...)`` skips validators; ``force_llm_recall`` does
    # not participate in cross-field validation, so this is safe. If a future
    # flag does need re-validation, switch to
    # ``Config.model_validate({**config.model_dump(), **overrides})``.
    config = config.model_copy(update={"force_llm_recall": force_llm_recall})
    _maybe_setup_logging()
    _maybe_init_tracing(config)
    llm, embedder, corpus, budget, sparse_encoder = build_deps(config)
    set_query_corpus(corpus)
    ctx = InputContext(name=name or "(unnamed)", description=description, years_filter=years)
    if debug_retrieve:
        await _debug_retrieve(ctx, llm=llm, embedder=embedder, corpus=corpus, config=config)
        return
    recall_deps = await _build_recall_deps(config, llm=llm)

    progress_ctx = _progress_context()
    err_console = Console(stderr=True)
    try:
        with progress_ctx as bar:
            report = await run_query(
                ctx,
                llm=llm,
                embedding_client=embedder,
                corpus=corpus,
                config=config,
                budget=budget,
                progress=bar,
                sparse_encoder=sparse_encoder,
                recall_deps=recall_deps,
            )
    except KeyboardInterrupt:
        err_console.rule("[bold yellow]query cancelled (Ctrl-C)", style="yellow")
        raise
    except BaseException:
        err_console.rule("[bold red]query failed", style="red")
        err_console.print_exception(show_locals=False)
        raise

    if bar is not None:
        _render_query_footer(bar.console, report)

    if not report.candidates:
        typer.echo(_NOT_FOUND_MARKDOWN)
        raise typer.Exit(code=1)

    rendered = render(report)
    if to_stdout:
        typer.echo(rendered)
        return
    out_path = _query_run_path(ctx)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _ = out_path.write_text(rendered, encoding="utf-8")
    err_console.print(f"[bold green]Report saved to[/bold green] {out_path.resolve()}")
    if not sys.stdout.isatty():
        typer.echo(str(out_path))


async def _build_recall_deps(
    config: Config,
    *,
    llm: LLMClient,
) -> RecallDeps | None:
    """Construct ``RecallDeps`` for every non-debug query, or ``None`` when recall is off.

    The recall branch can fire on any query whose coverage-gap predicate
    trips, so deps are built up front. Cold-start cost is dominated by
    ``MergeJournal.init()`` (sqlite open + table check), well under 50ms.

    Returns ``None`` when ``enable_tavily_recall_search=False`` — the L0
    search head is mandatory under the new contract, so disabling Tavily
    disables the entire recall branch. The L3 extract fallback is bundled
    under the same flag.
    """
    tavily_search = build_tavily_recall_search(config)
    extract = build_tavily_recall_extract(config)
    if tavily_search is None or extract is None:
        return None
    # Local imports keep the cold-start cost off the import path; mirrors
    # the ``_ingest_cmd.py`` pattern of deferring heavyweight deps.
    from slopmortem.corpus import MergeJournal  # noqa: PLC0415
    from slopmortem.ingest import HaikuSlopClassifier  # noqa: PLC0415

    post_mortems_root = Path(config.post_mortems_root)
    journal_path = Path(
        config.merge_journal_path or str(post_mortems_root.parent / "journal.sqlite")
    )
    journal = MergeJournal(journal_path)
    await journal.init()
    classifier = HaikuSlopClassifier(
        llm=llm,
        model=config.model_summarize,
        max_tokens=config.max_tokens_slop_judge,
    )
    return RecallDeps(
        journal=journal,
        slop_classifier=classifier,
        post_mortems_root=post_mortems_root,
        tavily_search=tavily_search,
        extract=extract,
    )


_DEBUG_SUMMARY_MAX = 200


async def _debug_retrieve(
    ctx: InputContext,
    *,
    llm: LLMClient,
    embedder: EmbeddingClient,
    corpus: Corpus,
    config: Config,
) -> None:
    facets = await extract_facets(
        ctx.description,
        llm,
        model=config.model_facet,
        max_tokens=config.max_tokens_facet,
    )
    cutoff = cutoff_iso(ctx.years_filter)
    candidates = await retrieve(
        description=ctx.description,
        facets=facets,
        corpus=corpus,
        embedding_client=embedder,
        cutoff_iso=cutoff,
        strict_deaths=config.strict_deaths,
        k_retrieve=config.K_retrieve,
    )

    typer.echo(f"# debug-retrieve  input={ctx.name!r}  cutoff={cutoff or 'none'}")
    typer.echo(f"facets.closed: sector={facets.sector} business_model={facets.business_model}")
    typer.echo(f"               customer_type={facets.customer_type} geography={facets.geography}")
    typer.echo(f"               monetization={facets.monetization}")
    typer.echo(f"facets.open:   sub_sector={facets.sub_sector} product_type={facets.product_type}")
    typer.echo(f"               price_point={facets.price_point}")
    typer.echo(f"facets.years:  founding={facets.founding_year} failure={facets.failure_year}")
    typer.echo(f"retrieved: {len(candidates)} (k_retrieve={config.K_retrieve})")
    typer.echo("")
    for i, c in enumerate(candidates, start=1):
        p = c.payload
        founded = p.founding_date.isoformat() if p.founding_date else "?"
        failed = p.failure_date.isoformat() if p.failure_date else "?"
        summary = p.summary.replace("\n", " ").strip()
        if len(summary) > _DEBUG_SUMMARY_MAX:
            summary = summary[: _DEBUG_SUMMARY_MAX - 3] + "..."
        meta = f"{p.facets.sector}/{p.facets.business_model}, founded={founded}, failed={failed}"
        typer.echo(f"[{i}] score={c.score:.4f}  {p.name}  ({meta})")
        typer.echo(f"    id={c.canonical_id}  provenance={p.provenance}  slop={p.slop_score:.2f}")
        if c.alias_canonicals:
            typer.echo(f"    aliases: {', '.join(c.alias_canonicals)}")
        if p.sources:
            extra = f" +{len(p.sources) - 1}" if len(p.sources) > 1 else ""
            typer.echo(f"    sources: {p.sources[0]}{extra}")
        elif p.provenance_id:
            typer.echo(f"    provenance: {p.provenance_id}")
        typer.echo(f"    summary: {summary}")
        typer.echo("")
