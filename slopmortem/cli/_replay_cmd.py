"""``slopmortem replay`` subcommand.

Iterates a JSONL evals dataset (one ``InputContext`` per line) through the
synthesis pipeline. Missing-dataset path exits 2 so CI smoke tests can probe
the wiring without a fixture corpus.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Annotated

import anyio
import typer
from lmnr import observe

from slopmortem.cli import app
from slopmortem.cli._common import (
    RichQueryProgress,
    _maybe_init_tracing,
    _render_query_footer,
)
from slopmortem.config import load_config
from slopmortem.corpus import set_query_corpus
from slopmortem.deps import build_deps
from slopmortem.models import InputContext
from slopmortem.pipeline import run_query
from slopmortem.render import render


@app.command("replay")
def replay_cmd(
    dataset: Annotated[
        str,
        typer.Argument(help="Dataset name under tests/evals/datasets/."),
    ],
) -> None:
    """Replay a JSONL evals dataset through the synthesis pipeline."""
    anyio.run(_replay, dataset)


@observe(name="cli.replay")
async def _replay(dataset: str) -> None:
    path = Path("tests/evals/datasets") / f"{dataset}.jsonl"
    if not path.exists():
        typer.echo(f"no dataset at {path}; run 'just eval-record' to generate it", err=True)
        raise typer.Exit(code=2)

    config = load_config()
    _maybe_init_tracing(config)
    llm, embedder, corpus, budget, sparse_encoder = build_deps(config)
    set_query_corpus(corpus)

    progress_ctx: contextlib.AbstractContextManager[RichQueryProgress | None] = (
        RichQueryProgress() if sys.stderr.isatty() else contextlib.nullcontext()
    )
    with progress_ctx as bar:
        for raw_line in path.read_text().splitlines():
            line = raw_line.strip()
            if not line:
                continue
            # ``json.loads`` returns ``Any`` by design; the per-site ignore
            # narrows the unknown payload to ``object``.
            # ``InputContext.model_validate`` is the strict boundary.
            row: object = json.loads(line)  # pyright: ignore[reportAny]
            ctx = InputContext.model_validate(row)
            report = await run_query(
                ctx,
                llm=llm,
                embedding_client=embedder,
                corpus=corpus,
                config=config,
                budget=budget,
                progress=bar,
                sparse_encoder=sparse_encoder,
            )
            if bar is not None:
                _render_query_footer(bar.console, report)
            typer.echo(render(report))
