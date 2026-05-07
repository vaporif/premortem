# ruff: noqa: FBT002 - typer signatures are bool flags with False defaults by convention.
"""``slopmortem ingest`` subcommand.

Read-only modes: ``--list-review`` shows the pending entity-resolution review
queue. ``--reconcile`` runs the six-drift-class scan with ``repair=True`` and
prints the report. ``--reclassify`` re-runs the slop classifier on every
quarantine row and routes survivors back out of the quarantine tree.
"""

from __future__ import annotations

import contextlib
import functools
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

import anyio
import typer
from lmnr import observe
from openai import AsyncOpenAI
from rich.console import Console
from rich.table import Table

from slopmortem.budget import Budget
from slopmortem.cli import app
from slopmortem.cli._common import _maybe_init_tracing
from slopmortem.cli_progress import RichPhaseProgress
from slopmortem.config import load_config
from slopmortem.corpus import (
    QdrantCorpus,
    reclassify_quarantined,
    reconcile,
    set_query_corpus,
)
from slopmortem.corpus.sources import (
    CrunchbaseCsvSource,
    CuratedSource,
    HNAlgoliaSource,
    TavilyEnricher,
    TavilyNewsSource,
    WaybackEnricher,
)
from slopmortem.ingest import INGEST_PHASE_LABELS, IngestPhase, IngestResult, ingest
from slopmortem.llm import OpenRouterClient, make_embedder

if TYPE_CHECKING:
    from slopmortem.config import Config
    from slopmortem.corpus import Corpus, MergeJournal
    from slopmortem.corpus.sources import Enricher, Source
    from slopmortem.ingest import Corpus as IngestCorpus
    from slopmortem.ingest import SlopClassifier
    from slopmortem.llm import EmbeddingClient, LLMClient


@app.command("ingest")
def ingest_cmd(  # noqa: PLR0913 - every flag mirrors the spec; user types kwargs.
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Count entries that would be ingested; write nothing.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Bypass the skip_key short-circuit.")
    ] = False,
    reconcile_flag: Annotated[
        bool,
        typer.Option(
            "--reconcile",
            help="Run the reconcile pass and apply repairs for the six drift classes.",
        ),
    ] = False,
    reclassify: Annotated[
        bool,
        typer.Option(
            "--reclassify",
            help=(
                "Re-run the slop classifier on quarantined docs; "
                "if no longer slop, route through entity resolution."
            ),
        ),
    ] = False,
    list_review: Annotated[
        bool,
        typer.Option(
            "--list-review",
            help="Print the pending_review queue and exit.",
        ),
    ] = False,
    crunchbase_csv: Annotated[
        Path | None,
        typer.Option(
            "--crunchbase-csv",
            help="Path to a Crunchbase CSV; enables the Crunchbase adapter for this run.",
        ),
    ] = None,
    enrich_wayback: Annotated[
        bool, typer.Option("--enrich-wayback", help="Enable the Wayback enricher.")
    ] = False,
    tavily_enrich: Annotated[
        bool, typer.Option("--tavily-enrich", help="Enable the Tavily enricher.")
    ] = False,
    enable_tavily_news: Annotated[
        bool,
        typer.Option(
            "--enable-tavily-news",
            help=(
                "Enable the Tavily news shutdown-event source. "
                "Requires TAVILY_API_KEY. Bodies are returned inline; "
                "no Tavily-extract hop is implied."
            ),
        ),
    ] = False,
    tavily_news_start_year: Annotated[
        int | None,
        typer.Option(
            "--tavily-news-start-year",
            help="Override year_range.start for the Tavily news source.",
        ),
    ] = None,
    tavily_news_end_year: Annotated[
        int | None,
        typer.Option(
            "--tavily-news-end-year",
            help="Override year_range.end for the Tavily news source. Defaults to current year.",
        ),
    ] = None,
    tavily_news_max_emit: Annotated[
        int | None,
        typer.Option(
            "--tavily-news-max-emit",
            help="Override the Tavily news source's max_emit cap.",
        ),
    ] = None,
    tavily_news_search_depth: Annotated[
        str | None,
        typer.Option(
            "--tavily-news-search-depth",
            help=(
                "Override search_depth for the Tavily news source: "
                "basic (1 credit) or advanced (2)."
            ),
        ),
    ] = None,
    only_source: Annotated[
        str | None,
        typer.Option(
            "--only-source",
            help=(
                "Run only the named source, auto-enabling its --enable-* flag if any. "
                "Accepts source identifiers (curated, hn_algolia, crunchbase_csv, tavily_news)."
            ),
        ),
    ] = None,
    post_mortems_root: Annotated[
        Path,
        typer.Option(
            "--post-mortems-root",
            help="Root for raw/, canonical/, quarantine/ trees.",
        ),
    ] = Path("./post_mortems"),
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            help=(
                "Process only the first N entries after gathering from all sources. "
                "Source order is curated -> HN -> Crunchbase. Useful for cheap live "
                "smoke tests; cost scales with N."
            ),
        ),
    ] = None,
) -> None:
    """Run the ingest pipeline against the configured sources."""
    anyio.run(
        functools.partial(
            _run_ingest,
            dry_run=dry_run,
            force=force,
            reconcile_flag=reconcile_flag,
            reclassify=reclassify,
            list_review=list_review,
            crunchbase_csv=crunchbase_csv,
            enrich_wayback=enrich_wayback,
            tavily_enrich=tavily_enrich,
            enable_tavily_news=enable_tavily_news,
            tavily_news_start_year=tavily_news_start_year,
            tavily_news_end_year=tavily_news_end_year,
            tavily_news_max_emit=tavily_news_max_emit,
            tavily_news_search_depth=tavily_news_search_depth,
            only_source=only_source,
            post_mortems_root=post_mortems_root,
            limit=limit,
        )
    )


async def _run_reclassify(config: Config, post_mortems_root: Path) -> None:
    journal = await _build_journal(config, post_mortems_root)
    # reclassify hits the live slop judge, so we need the LLM but neither the
    # embedder nor qdrant.
    budget = Budget(cap_usd=config.max_cost_usd_per_ingest)
    openrouter_sdk = AsyncOpenAI(
        api_key=config.openrouter_api_key.get_secret_value(),
        base_url=config.openrouter_base_url,
    )
    llm = OpenRouterClient(sdk=openrouter_sdk, budget=budget, model=config.model_summarize)
    classifier = _build_slop_classifier(
        dry_run=False,
        llm=llm,
        model=config.model_summarize,
        max_tokens=config.max_tokens_slop_judge,
    )
    report = await reclassify_quarantined(
        journal=journal,
        slop_classifier=classifier,
        post_mortems_root=post_mortems_root,
        slop_threshold=config.slop_threshold,
    )
    counts = f"total={report.total} declassified={report.declassified}"
    tail = f"still_slop={report.still_slop} errors={report.errors}"
    typer.echo(f"reclassify: {counts} {tail}")


async def _run_reconcile(config: Config, post_mortems_root: Path) -> None:
    journal = await _build_journal(config, post_mortems_root)
    corpus = await _build_ingest_corpus(config, post_mortems_root)
    report = await reconcile(
        journal=journal,
        corpus=corpus,
        post_mortems_root=post_mortems_root,
        repair=True,
    )
    typer.echo(f"reconcile: {len(report.rows)} drift findings, {len(report.applied)} repaired")
    for r in report.rows:
        typer.echo(f"  drift_class={r.drift_class}\t{r.path}\t{r.detail}")


@observe(name="cli.ingest")
async def _run_ingest(  # noqa: PLR0913, PLR0912, PLR0915, C901 - the ingest CLI surface is wide.
    *,
    dry_run: bool,
    force: bool,
    reconcile_flag: bool,
    reclassify: bool,
    list_review: bool,
    limit: int | None,
    crunchbase_csv: Path | None,
    enrich_wayback: bool,
    tavily_enrich: bool,
    enable_tavily_news: bool,
    tavily_news_start_year: int | None,
    tavily_news_end_year: int | None,
    tavily_news_max_emit: int | None,
    tavily_news_search_depth: str | None,
    only_source: str | None,
    post_mortems_root: Path,
) -> None:
    config = load_config()
    _maybe_init_tracing(config)

    # Read-only short-circuits run before the full LLM/embedder build so they
    # don't require ``OPENROUTER_API_KEY`` / ``OPENAI_API_KEY``.
    if list_review:
        journal = await _build_journal(config, post_mortems_root)
        rows = await journal.list_pending_review()
        if not rows:
            typer.echo("(no pending_review rows)")
        for r in rows:
            score = r.similarity_score
            decision = r.haiku_decision
            rationale = r.haiku_rationale
            typer.echo(f"{r.pair_key}\tscore={score}\tdecision={decision}\trationale={rationale}")
        return

    if reclassify:
        await _run_reclassify(config, post_mortems_root)
        return

    if reconcile_flag:
        await _run_reconcile(config, post_mortems_root)
        return

    if only_source is not None:
        if only_source not in _SOURCE_REGISTRY:
            valid = ", ".join(sorted(_SOURCE_REGISTRY))
            msg = f"--only-source: unknown source {only_source!r}. Valid: {valid}."
            raise typer.BadParameter(msg)
        spec = _SOURCE_REGISTRY[only_source]
        # Each opt-in source's --enable-* flag needs an explicit branch here —
        # Python's keyword-only parameter binding can't be table-driven without
        # ``locals()`` tricks. Add one when introducing a new opt-in source.
        if spec.source_class is TavilyNewsSource:
            enable_tavily_news = True
        # crunchbase_csv is gated by a path argument, not a boolean — require it explicitly.
        if spec.source_class is CrunchbaseCsvSource and crunchbase_csv is None:
            msg = "--only-source crunchbase_csv requires --crunchbase-csv PATH."
            raise typer.BadParameter(msg)

    if enable_tavily_news and not os.environ.get("TAVILY_API_KEY"):
        msg = (
            "--enable-tavily-news requires TAVILY_API_KEY: the Tavily news source "
            "calls /search and pulls article bodies via include_raw_content. "
            "Set TAVILY_API_KEY in .env or unset --enable-tavily-news."
        )
        raise typer.BadParameter(msg)

    llm, embedder, corpus, budget, journal, classifier = await _build_ingest_deps(
        config, post_mortems_root, dry_run=dry_run
    )
    # The ingest-side Corpus Protocol is wider than the query-side one used by
    # set_query_corpus; the underlying QdrantCorpus instance satisfies both.
    set_query_corpus(cast("Corpus", corpus))

    sources: list[Source] = [
        CuratedSource(yaml_path=_default_curated_yaml(), rps=3.0),
        HNAlgoliaSource(query="post-mortem", rps=5.0),
    ]
    if crunchbase_csv is not None:
        sources.append(CrunchbaseCsvSource(csv_path=crunchbase_csv))
    if enable_tavily_news:
        sources.append(
            TavilyNewsSource(
                start_year=tavily_news_start_year,
                end_year=tavily_news_end_year,
                max_emit=tavily_news_max_emit,
                search_depth=tavily_news_search_depth,
            )
        )

    if only_source is not None:
        wanted_class = _SOURCE_REGISTRY[only_source].source_class
        sources = [s for s in sources if isinstance(s, wanted_class)]
        if not sources:
            msg = (
                f"--only-source {only_source!r}: source enabled but not constructed; "
                "check that its prerequisites (e.g. --crunchbase-csv path) are present."
            )
            raise typer.BadParameter(msg)

    enrichers: list[Enricher] = []
    if enrich_wayback:
        enrichers.append(WaybackEnricher(rps=5.0))
    if tavily_enrich:
        enrichers.append(TavilyEnricher())

    # TTY-gated: attach Rich progress only when stderr is a real terminal.
    # Piped invocations (CI, ``> file``) get a quiet run.
    progress_ctx: contextlib.AbstractContextManager[RichIngestProgress | None] = (
        RichIngestProgress() if sys.stderr.isatty() else contextlib.nullcontext()
    )
    # Print escaping exceptions before Rich tears down — Progress.__exit__
    # clears the screen, so a traceback interleaved with bar redraws disappears.
    err_console = Console(stderr=True)
    try:
        with progress_ctx as bar:
            result = await ingest(
                sources=sources,
                enrichers=enrichers,
                journal=journal,
                corpus=corpus,
                llm=llm,
                embed_client=embedder,
                budget=budget,
                slop_classifier=classifier,
                config=config,
                post_mortems_root=post_mortems_root,
                dry_run=dry_run,
                force=force,
                limit=limit,
                progress=bar,
            )
    except KeyboardInterrupt:
        # BaseException, so without an explicit handler the prompt returns
        # silently with no sign the run was interrupted.
        err_console.rule("[bold yellow]ingest cancelled (Ctrl-C)", style="yellow")
        raise
    except BaseException:
        # Covers CancelledError / SystemExit / etc that ``except Exception``
        # misses. Re-raise so the caller still gets a non-zero exit.
        err_console.rule("[bold red]ingest failed", style="red")
        err_console.print_exception(show_locals=False)
        raise
    if bar is not None:
        _render_ingest_result(bar.console, result, budget)
    else:
        typer.echo(f"slopmortem ingest result: {result} cost=${budget.spent_usd:.4f}")


@dataclass(frozen=True)
class _SourceSpec:
    source_class: type[Source]


_SOURCE_REGISTRY: dict[str, _SourceSpec] = {
    "curated": _SourceSpec(source_class=CuratedSource),
    "hn_algolia": _SourceSpec(source_class=HNAlgoliaSource),
    "crunchbase_csv": _SourceSpec(source_class=CrunchbaseCsvSource),
    "tavily_news": _SourceSpec(source_class=TavilyNewsSource),
}


def _default_curated_yaml() -> Path:
    return Path(__file__).parent.parent / "corpus" / "sources" / "curated" / "post_mortems_v0.yml"


async def _build_journal(config: Config, post_mortems_root: Path) -> MergeJournal:
    """Build the merge journal, calling `MergeJournal.init`.

    ``init()`` is idempotent so calling it every CLI invocation is cheap and
    fresh dev databases work without an explicit setup step.
    """
    from slopmortem.corpus import MergeJournal  # noqa: PLC0415

    journal_path = Path(
        config.merge_journal_path or str(post_mortems_root.parent / "journal.sqlite")
    )
    journal = MergeJournal(journal_path)
    await journal.init()
    return journal


def _build_slop_classifier(
    *, dry_run: bool, llm: LLMClient, model: str, max_tokens: int | None = None
) -> SlopClassifier:
    """Pick a slop classifier based on *dry_run*.

    Dry-run uses `FakeSlopClassifier` — no API key, no LLM cost.
    Live runs use `HaikuSlopClassifier` (one Haiku call per entry).
    """
    if dry_run:
        from slopmortem.ingest import FakeSlopClassifier  # noqa: PLC0415

        return FakeSlopClassifier()
    from slopmortem.ingest import HaikuSlopClassifier  # noqa: PLC0415

    return HaikuSlopClassifier(llm=llm, model=model, max_tokens=max_tokens)


async def _build_ingest_corpus(config: Config, post_mortems_root: Path) -> IngestCorpus:
    """Build the ingest-side `QdrantCorpus`, ensuring the collection exists.

    Without ``ensure_collection`` (idempotent), ``upsert_chunk`` fails with
    ``Collection 'slopmortem' doesn't exist`` on a fresh dev box's first write.
    """
    from qdrant_client import AsyncQdrantClient  # noqa: PLC0415 - heavy dep, lazy import

    from slopmortem.corpus import ensure_collection  # noqa: PLC0415
    from slopmortem.llm import EMBED_DIMS  # noqa: PLC0415

    if config.embed_model_id not in EMBED_DIMS:
        msg = f"unknown embed model {config.embed_model_id!r}; add it to EMBED_DIMS"
        raise ValueError(msg)
    dim = EMBED_DIMS[config.embed_model_id]

    qdrant_client = AsyncQdrantClient(host=config.qdrant_host, port=config.qdrant_port)
    await ensure_collection(qdrant_client, config.qdrant_collection, dim=dim)
    return QdrantCorpus(
        client=qdrant_client,
        collection=config.qdrant_collection,
        post_mortems_root=post_mortems_root,
        facet_boost=config.facet_boost,
        rrf_k=config.rrf_k,
    )


async def _build_ingest_deps(
    config: Config,
    post_mortems_root: Path,
    *,
    dry_run: bool,
) -> tuple[LLMClient, EmbeddingClient, IngestCorpus, Budget, MergeJournal, SlopClassifier]:
    """Build the ingest-side dependency tuple.

    Mirrors `slopmortem.deps.build_deps` but caps the budget at
    ``max_cost_usd_per_ingest`` and additionally builds a journal plus the
    slop classifier. ``dry_run=True`` swaps in `FakeSlopClassifier` so
    the run needs no real API key.
    """
    budget = Budget(cap_usd=config.max_cost_usd_per_ingest)

    openrouter_sdk = AsyncOpenAI(
        api_key=config.openrouter_api_key.get_secret_value(),
        base_url=config.openrouter_base_url,
    )
    llm = OpenRouterClient(
        sdk=openrouter_sdk,
        budget=budget,
        model=config.model_facet,  # ingest uses the cheap model
    )

    embedder = make_embedder(config, budget)

    corpus = await _build_ingest_corpus(config, post_mortems_root)
    journal = await _build_journal(config, post_mortems_root)
    classifier = _build_slop_classifier(
        dry_run=dry_run,
        llm=llm,
        model=config.model_summarize,
        max_tokens=config.max_tokens_slop_judge,
    )
    return llm, embedder, corpus, budget, journal, classifier


class RichIngestProgress(RichPhaseProgress[IngestPhase]):
    def __init__(self) -> None:
        super().__init__(INGEST_PHASE_LABELS)


def _render_ingest_result(console: Console, result: IngestResult, budget: Budget) -> None:
    rows: list[tuple[str, str]] = [
        ("seen", str(result.seen)),
        ("processed", str(result.processed)),
        ("would_process", str(result.would_process)),
        ("quarantined", str(result.quarantined)),
        ("skipped", str(result.skipped)),
        ("errors", str(result.errors)),
        ("source_failures", str(result.source_failures)),
        ("cache_warmed", str(result.cache_warmed)),
        ("dry_run", str(result.dry_run)),
        ("cost_usd", f"${budget.spent_usd:.4f}"),
    ]
    table = Table(title="Ingest result", show_header=False, expand=False)
    table.add_column("Field", style="bold cyan", no_wrap=True)
    table.add_column("Value", style="white")
    for k, v in rows:
        table.add_row(k, v)
    console.print(table)
