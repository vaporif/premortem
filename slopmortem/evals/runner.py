r"""Eval runner: drive a JSONL dataset through the pipeline and diff against a baseline.

Baseline JSON shape (normative)::

    {
      "version": 1,
      "rows": {
        "<row_id>": {
          "candidates_count": <int>,
          "assertions": {
            "<candidate_id>": {
              "where_diverged_nonempty": true,
              "all_sources_in_allowed_domains": true,
              "lifespan_months_positive": true,
              "claims_grounded_in_body": true
            }
          }
        }
      }
    }

Regression semantics
--------------------

A run is a **regression** (exit 1) iff:

- An assertion that was ``true`` in the baseline is ``false`` now, OR
- A row present in the baseline produces zero candidates now AND the baseline
  had a non-zero ``candidates_count``.

Forward-compat warnings (exit 0):

- A row in the current run is missing from the baseline.
- A candidate in the current row is missing from the baseline.
- An assertion is missing from the baseline for a known candidate.

A truncated row (``BudgetExceededError`` mid-run → partial Report) is **not**
a runner failure on its own. Assertions apply only to the Synthesis values
that made it through. A row with ``candidates_count=0`` matched against a
baseline that also has ``candidates_count=0`` passes.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

import anyio
from rich.console import Console

from slopmortem.budget import Budget
from slopmortem.config import load_config
from slopmortem.evals.assertions import (
    all_sources_in_allowed_domains,
    claims_grounded_in_body,
    lifespan_months_positive,
    where_diverged_nonempty,
)
from slopmortem.evals.cassettes import (
    CassetteFormatError,
    load_row_fakes,
)
from slopmortem.evals.qdrant_setup import setup_ephemeral_qdrant
from slopmortem.llm import EMBED_DIMS, NoCannedEmbeddingError, NoCannedResponseError
from slopmortem.models import (
    InputContext,
    Synthesis,
)
from slopmortem.pipeline import run_query

# Eval-time host allowlist for ``all_sources_in_allowed_domains`` in live
# mode (no in-memory corpus to widen the set from payload sources).
# web.archive.org is deliberately excluded: Wayback proxies arbitrary URLs
# and would bypass host-level allowlist semantics.
_FIXED_HOST_ALLOWLIST: frozenset[str] = frozenset({"news.ycombinator.com"})

if TYPE_CHECKING:
    from collections.abc import Mapping

    from slopmortem.models import Report

# Module-level so tests can monkeypatch it at a tmp tree without touching
# the committed fixtures.
_CASSETTE_ROOT: Path = Path("tests/fixtures/cassettes/evals")

_BASELINE_VERSION = 1
_ROW_ID_HASH_PREFIX = 8

_ASSERTION_NAMES: tuple[str, ...] = (
    "where_diverged_nonempty",
    "all_sources_in_allowed_domains",
    "lifespan_months_positive",
    "claims_grounded_in_body",
)


def _load_dataset(path: Path) -> list[InputContext]:
    """Parse a JSONL dataset into ``InputContext`` rows; empty lines skipped."""
    rows: list[InputContext] = []
    text = path.read_text()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parsed: object = json.loads(line)  # pyright: ignore[reportAny]
        rows.append(InputContext.model_validate(parsed))
    return rows


def _row_id(ctx: InputContext) -> str:
    """Stable baseline key: ``ctx.name`` if set, else ``sha1(description)[:8]``."""
    if ctx.name:
        return ctx.name
    return hashlib.sha1(ctx.description.encode(), usedforsecurity=False).hexdigest()[
        :_ROW_ID_HASH_PREFIX
    ]


def _verify_unique_row_ids(rows: list[InputContext]) -> list[str]:
    """Compute row ids; ``ValueError`` if any collide."""
    ids = [_row_id(r) for r in rows]
    seen: set[str] = set()
    dup: list[str] = []
    for rid in ids:
        if rid in seen:
            dup.append(rid)
        seen.add(rid)
    if dup:
        msg = f"duplicate row_ids in dataset: {sorted(set(dup))}"
        raise ValueError(msg)
    return ids


def _validate_scope(scope: str | None, row_ids: list[str]) -> None:
    """Exit 2 if ``scope`` is set and not in ``row_ids``."""
    if scope is None or scope in row_ids:
        return
    print(  # noqa: T201 — CLI surface
        f"--scope {scope!r} not in dataset; valid scopes: {sorted(row_ids)}",
        file=sys.stderr,
    )
    sys.exit(2)


def _allowed_hosts_for_candidate(s: Synthesis) -> set[str]:
    """Union the fixed allowlist with the candidate's own ``Synthesis.sources``."""
    hosts: set[str] = set(_FIXED_HOST_ALLOWLIST)
    for url in s.sources:
        host = urlparse(url).hostname
        if host is not None:
            hosts.add(host)
    return hosts


def _score_synthesis(
    s: Synthesis,
    *,
    bodies_map: Mapping[str, str | None],
) -> dict[str, bool]:
    """Apply every assertion in ``_ASSERTION_NAMES`` to ``s``."""
    allowed = _allowed_hosts_for_candidate(s)
    body = bodies_map.get(s.candidate_id)
    return {
        "where_diverged_nonempty": where_diverged_nonempty(s),
        "all_sources_in_allowed_domains": all_sources_in_allowed_domains(s, allowed),
        "lifespan_months_positive": lifespan_months_positive(s),
        "claims_grounded_in_body": True if body is None else claims_grounded_in_body(s, body),
    }


def _score_report(
    report: Report,
    *,
    bodies_map: Mapping[str, str | None],
) -> dict[str, object]:
    """Per-row results-dict in the baseline shape."""
    assertions: dict[str, dict[str, bool]] = {}
    for s in report.candidates:
        assertions[s.candidate_id] = _score_synthesis(s, bodies_map=bodies_map)
    return {
        "candidates_count": len(report.candidates),
        "assertions": assertions,
    }


async def _run_cassettes(
    rows: list[InputContext],
    row_ids: list[str],
    scope_filter: str | None,
) -> dict[str, dict[str, object]]:
    """Run every row in cassette mode and return per-row scored results.

    Per-row failures (missing cassette dir, ``NoCanned*Error``) log
    ``FAIL <row_id>: …`` and write ``candidates_count=0``. Run-level failures
    (missing fixture, ``CassetteFormatError``) print to stderr and exit 2.
    """
    cfg = load_config()
    fixture_path = Path("tests/fixtures/corpus_fixture.jsonl")
    # One-shot pre-flight; anyio.Path for a single stat() isn't worth it.
    if not fixture_path.exists():  # noqa: ASYNC240
        print(  # noqa: T201 — CLI surface
            f"missing {fixture_path}; run `just eval-record-corpus` first",
            file=sys.stderr,
        )
        sys.exit(2)
    dim = EMBED_DIMS[cfg.embed_model_id]

    budget = Budget(cap_usd=2.0)
    results: dict[str, dict[str, object]] = {}
    try:
        async with setup_ephemeral_qdrant(fixture_path, dim=dim) as corpus:
            for ctx, rid in zip(rows, row_ids, strict=True):
                if scope_filter is not None and rid != scope_filter:
                    continue
                scope_dir = _CASSETTE_ROOT / rid
                if not scope_dir.exists() or not any(scope_dir.iterdir()):
                    print(f"FAIL {rid}: no cassettes")  # noqa: T201 — CLI surface
                    results[rid] = {"candidates_count": 0, "assertions": {}}
                    continue

                fake_llm, fake_embed, cassette_sparse = load_row_fakes(scope_dir, cfg)

                try:
                    report = await run_query(
                        ctx,
                        llm=fake_llm,
                        embedding_client=fake_embed,
                        corpus=corpus,
                        config=cfg,
                        budget=budget,
                        sparse_encoder=cassette_sparse,
                    )
                except (NoCannedResponseError, NoCannedEmbeddingError) as exc:
                    print(f"FAIL {rid}: cassette miss — {exc}")  # noqa: T201 — CLI surface
                    results[rid] = {"candidates_count": 0, "assertions": {}}
                    continue

                bodies_map: dict[str, str | None] = {}
                for s in report.candidates:
                    try:
                        bodies_map[s.candidate_id] = await corpus.get_post_mortem(s.candidate_id)
                    except (KeyError, FileNotFoundError):
                        bodies_map[s.candidate_id] = None
                results[rid] = _score_report(report, bodies_map=bodies_map)
    except CassetteFormatError as exc:
        print(f"cassette format error: {exc}", file=sys.stderr)  # noqa: T201 — CLI surface
        sys.exit(2)
    return results


async def _run_live(rows: list[InputContext], row_ids: list[str]) -> dict[str, dict[str, object]]:
    """Run every row through real production deps. May spend real money."""
    # Lazy import so cassette mode doesn't drag prod deps in (Qdrant client,
    # OpenAI SDK). Mirrors the CLI boot path so live evals get the same wiring.
    from slopmortem.corpus import set_query_corpus  # noqa: PLC0415
    from slopmortem.deps import build_deps  # noqa: PLC0415

    cfg = load_config()
    llm, embedder, corpus, budget, sparse_encoder = build_deps(cfg)
    set_query_corpus(corpus)

    results: dict[str, dict[str, object]] = {}
    for ctx, rid in zip(rows, row_ids, strict=True):
        report = await run_query(
            ctx,
            llm=llm,
            embedding_client=embedder,
            corpus=corpus,
            config=cfg,
            budget=budget,
            sparse_encoder=sparse_encoder,
        )
        bodies_map: dict[str, str | None] = {}
        for s in report.candidates:
            try:
                bodies_map[s.candidate_id] = await corpus.get_post_mortem(s.candidate_id)
            except (KeyError, FileNotFoundError):
                bodies_map[s.candidate_id] = None
        results[rid] = _score_report(report, bodies_map=bodies_map)
    return results


def _diff_against_baseline(
    current: dict[str, dict[str, object]],
    baseline: dict[str, object],
) -> tuple[list[str], list[str]]:
    """Compute (regressions, warnings) between current and baseline.

    Empty baseline (``{}``) means "no baseline, every row new" — emits only
    forward-compat warnings.
    """
    regressions: list[str] = []
    warnings: list[str] = []

    raw_rows: object = baseline.get("rows", {}) if baseline else {}
    if not isinstance(raw_rows, dict):
        warnings.append("baseline.rows is not a dict; treating as empty")
        raw_rows = {}
    baseline_rows = cast("dict[str, dict[str, object]]", raw_rows)

    for row_id, cur_row in current.items():
        if row_id not in baseline_rows:
            warnings.append(f"row {row_id!r} not in baseline; recording for next run")
            continue
        base_row = baseline_rows[row_id]
        regressions.extend(_diff_row(row_id, cur_row, base_row))

    return regressions, warnings


def _diff_row(
    row_id: str,
    cur_row: dict[str, object],
    base_row: dict[str, object],
) -> list[str]:
    """Regression messages for a single (current, baseline) row pair."""
    out: list[str] = []
    base_count_obj = base_row.get("candidates_count", 0)
    cur_count_obj = cur_row.get("candidates_count", 0)
    base_count = int(base_count_obj) if isinstance(base_count_obj, int) else 0
    cur_count = int(cur_count_obj) if isinstance(cur_count_obj, int) else 0
    if base_count > 0 and cur_count == 0:
        out.append(f"row {row_id!r}: produced 0 candidates (baseline had {base_count})")

    base_assertions_obj = base_row.get("assertions", {})
    cur_assertions_obj = cur_row.get("assertions", {})
    if not isinstance(base_assertions_obj, dict) or not isinstance(cur_assertions_obj, dict):
        return out
    base_assertions = cast("dict[str, dict[str, bool]]", base_assertions_obj)
    cur_assertions = cast("dict[str, dict[str, bool]]", cur_assertions_obj)

    for cand_id, base_results in base_assertions.items():
        cur_results = cur_assertions.get(cand_id)
        if cur_results is None:
            # Baseline had a candidate that the current run didn't synthesize.
            # Only a regression if the baseline asserted something true.
            if any(bool(v) for v in base_results.values()):
                out.append(f"row {row_id!r} candidate {cand_id!r}: missing from current run")
            continue
        for name, base_value in base_results.items():
            if not base_value:
                continue
            cur_value = cur_results.get(name)
            if cur_value is False:
                msg = (
                    f"row {row_id!r} candidate {cand_id!r}:"
                    f" {name} regressed (baseline=true, current=false)"
                )
                out.append(msg)
    return out


def _load_baseline(path: Path) -> dict[str, object]:
    """Load the baseline JSON; ``{}`` if the file is missing or empty."""
    if not path.exists():
        return {}
    text = path.read_text().strip()
    if not text:
        return {}
    parsed: object = json.loads(text)  # pyright: ignore[reportAny]
    if not isinstance(parsed, dict):
        msg = f"baseline at {path} must be a JSON object, got {type(parsed).__name__}"
        raise TypeError(msg)
    return cast("dict[str, object]", parsed)


def _serialize_results(results: dict[str, dict[str, object]]) -> dict[str, object]:
    return {"version": _BASELINE_VERSION, "rows": results}


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="slopmortem.evals.runner",
        description=(
            "Run a JSONL eval dataset through the synthesis pipeline and "
            "compare per-row assertion results against a recorded baseline. "
            "Default mode replays committed cassettes against an ephemeral Qdrant "
            "collection (FakeLLMClient + FakeEmbeddingClient + setup_ephemeral_qdrant); "
            "--live wires real production deps. CI runs cassette mode only."
        ),
    )
    _ = p.add_argument(
        "--dataset",
        type=Path,
        required=True,
        help="Path to a JSONL dataset; each line is an InputContext.",
    )
    _ = p.add_argument(
        "--baseline",
        type=Path,
        required=True,
        help=(
            "Path to baseline.json. Missing/empty file is tolerated "
            "(treated as no baseline, every row is new)."
        ),
    )
    _ = p.add_argument(
        "--live",
        action="store_true",
        help=(
            "Use real production deps via slopmortem.deps.build_deps. "
            "Requires env keys + Qdrant; will spend real money. CI does not run this."
        ),
    )
    _ = p.add_argument(
        "--record",
        action="store_true",
        help=(
            "Re-record cassettes against the live API via the recording helper. "
            "Requires `tests/fixtures/corpus_fixture.jsonl` to exist; run "
            "`just eval-record-corpus` first if missing."
        ),
    )
    _ = p.add_argument(
        "--write-baseline",
        action="store_true",
        help=(
            "Write the current run's results to --baseline instead of comparing. "
            "Use this to bootstrap a baseline file."
        ),
    )
    _ = p.add_argument(
        "--scope",
        type=str,
        default=None,
        help=(
            "Filter to one row by name (record or replay). Without --scope, "
            "every row in the dataset runs."
        ),
    )
    _ = p.add_argument(
        "--max-cost-usd",
        type=float,
        default=2.0,
        help=(
            "Cost ceiling per recording session ($). Only consulted in --record mode. "
            "Override if a re-record legitimately needs more."
        ),
    )
    return p


def _run_record(
    *,
    dataset_path: Path,
    scope: str | None,
    max_cost_usd: float,
) -> None:
    """Dispatch to the recording helper; ``sys.exit`` on completion or error."""
    # Lazy: eager import would drag the recording wrappers and Rich widgets
    # into every cassette-mode replay, which is the common path.
    from slopmortem.evals import recording_helper  # noqa: PLC0415
    from slopmortem.evals.recording_progress import NullRecordProgress  # noqa: PLC0415
    from slopmortem.evals.render import (  # noqa: PLC0415
        RichRecordProgress,
        render_record_footer,
    )

    all_rows = _load_dataset(dataset_path)
    rows = all_rows
    if scope is not None:
        rows = [r for r in all_rows if _row_id(r) == scope]
        if not rows:
            valid = [_row_id(r) for r in all_rows]
            print(  # noqa: T201 — CLI surface
                f"unknown scope {scope!r}; valid: {valid}",
                file=sys.stderr,
            )
            sys.exit(2)
    cfg = load_config()
    output_dir = Path("tests/fixtures/cassettes/evals")
    corpus_fixture_path = Path("tests/fixtures/corpus_fixture.jsonl")
    if not corpus_fixture_path.exists():
        print(  # noqa: T201 — CLI surface
            f"missing {corpus_fixture_path}; run `just eval-record-corpus` first",
            file=sys.stderr,
        )
        sys.exit(2)

    progress_ctx: contextlib.AbstractContextManager[RichRecordProgress | None] = (
        RichRecordProgress() if sys.stderr.isatty() else contextlib.nullcontext()
    )
    with progress_ctx as bar:
        sink = bar if bar is not None else NullRecordProgress()

        async def _do_record() -> recording_helper.RecordResult:
            return await recording_helper.record_cassettes_for_inputs(
                inputs=rows,
                output_dir=output_dir,
                corpus_fixture_path=corpus_fixture_path,
                config=cfg,
                max_cost_usd=max_cost_usd,
                progress=sink,
            )

        result = anyio.run(_do_record)
        # Footer routes through the bar's console so the panel renders under
        # the live progress region instead of fighting it.
        footer_console = bar.console if bar is not None else Console(stderr=True)
        render_record_footer(
            footer_console,
            total_cost_usd=result.total_cost_usd,
            max_cost_usd=max_cost_usd,
            rows_total=result.rows_total,
            rows_succeeded=result.rows_succeeded,
            cassettes_written=result.cassettes_written,
        )
    sys.exit(0)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: parse args, drive the run, exit 0 on no regression else 1."""
    parser = _build_argparser()
    ns = parser.parse_args(argv)
    # argparse builds Namespace attrs dynamically (typed as ``Any``); narrow
    # each at the boundary with explicit casts.
    dataset_path = cast("Path", ns.dataset)
    baseline_path = cast("Path", ns.baseline)
    live = cast("bool", ns.live)
    record = cast("bool", ns.record)
    write_baseline = cast("bool", ns.write_baseline)
    scope = cast("str | None", ns.scope)
    max_cost_usd = cast("float", ns.max_cost_usd)

    if record:
        _run_record(dataset_path=dataset_path, scope=scope, max_cost_usd=max_cost_usd)

    rows = _load_dataset(dataset_path)
    row_ids = _verify_unique_row_ids(rows)
    _validate_scope(scope, row_ids)

    if live:
        results = anyio.run(_run_live, rows, row_ids)
    else:
        results = anyio.run(_run_cassettes, rows, row_ids, scope)

    serialized = _serialize_results(results)

    if write_baseline:
        baseline_path.write_text(json.dumps(serialized, indent=2, sort_keys=True) + "\n")
        print(f"wrote baseline to {baseline_path}")  # noqa: T201 — CLI surface
        sys.exit(0)

    baseline = _load_baseline(baseline_path)

    for row_id, row_results in results.items():
        assertions_obj = row_results.get("assertions", {})
        if not isinstance(assertions_obj, dict):
            continue
        assertions = cast("dict[str, dict[str, bool]]", assertions_obj)
        for cand_id, results_for_cand in assertions.items():
            ok = all(results_for_cand.values())
            mark = "PASS" if ok else "FAIL"
            print(f"{mark} {row_id} {cand_id}")  # noqa: T201 — CLI surface

    regressions, warnings = _diff_against_baseline(results, baseline)
    for w in warnings:
        print(f"WARN {w}")  # noqa: T201 — CLI surface

    if regressions:
        for r in regressions:
            print(f"REGRESSION {r}", file=sys.stderr)  # noqa: T201 — CLI surface
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
