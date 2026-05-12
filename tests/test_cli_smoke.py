"""CLI smoke tests via ``typer.testing.CliRunner``.

Covers Task 10 plan steps:
- 10.7 ``replay --dataset`` with no fixture exits with code 2.
- CLI smoke for ``query`` with ``run_query`` monkeypatched to return a fixture
  Report.

``slopmortem.deps.build_deps`` is the monkeypatch seam, so the smoke tests
don't need real Qdrant / OpenRouter / OpenAI credentials.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from typer.testing import CliRunner

from slopmortem.budget import Budget
from slopmortem.cli import app
from slopmortem.models import (
    InputContext,
    PerspectiveScore,
    PipelineMeta,
    Report,
    SimilarityScores,
    Synthesis,
)

if TYPE_CHECKING:
    import pytest

    from slopmortem.config import Config


def _fixture_report(*, name: str = "Foo") -> Report:
    sim = SimilarityScores(
        business_model=PerspectiveScore(score=7.0, rationale="match"),
        market=PerspectiveScore(score=6.0, rationale="match"),
        gtm=PerspectiveScore(score=5.0, rationale="match"),
        stage_scale=PerspectiveScore(score=4.0, rationale="match"),
    )
    syn = Synthesis(
        candidate_id="acme",
        name="Acme",
        one_liner="One-line summary.",
        failure_date=None,
        lifespan_months=None,
        similarity=sim,
        why_similar="Similar.",
        where_diverged="Diverged.",
        failure_causes=["one"],
        lessons_for_input=["one"],
        sources=[],
    )
    return Report(
        input=InputContext(name=name, description="A pitch", years_filter=None),
        generated_at=datetime.now(UTC),
        candidates=[syn],
        pipeline_meta=PipelineMeta(
            K_retrieve=30,
            N_synthesize=5,
            min_similarity_score=4.0,
            models={"facet": "f", "rerank": "r", "synthesize": "s"},
            cost_usd_total=0.0,
            latency_ms_total=0,
            trace_id=None,
            budget_remaining_usd=2.0,
            budget_exceeded=False,
        ),
    )


def _build_fake_deps(_config: Config) -> tuple[object, object, object, Budget, object]:
    """Stand-in for `slopmortem.deps.build_deps`; returns inert objects."""
    # Bare ``object()`` instances are fine here. The smoke test patches
    # ``run_query``, so these are never called.
    return object(), object(), object(), Budget(cap_usd=0.0), object()


def _noop_set_corpus(_corpus: object) -> None:
    """Stand-in for `slopmortem.corpus.set_query_corpus`."""
    return


def test_query_smoke_renders_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """``slopmortem query --stdout`` runs end-to-end with a fake ``run_query``.

    Checks typer wiring: arg parsing, dispatch through ``_query``, render to
    stdout via ``--stdout``, with ``build_deps`` as the seam.
    """

    async def _fake_run_query(input_ctx: InputContext, **_kwargs: Any) -> Report:
        return _fixture_report(name=input_ctx.name)

    # Swap dep-construction so the test doesn't need real OPENROUTER_API_KEY etc.
    monkeypatch.setattr("slopmortem.cli._query_cmd.build_deps", _build_fake_deps)
    monkeypatch.setattr("slopmortem.cli._query_cmd.set_query_corpus", _noop_set_corpus)
    monkeypatch.setattr("slopmortem.cli._query_cmd.run_query", _fake_run_query)

    runner = CliRunner()
    result = runner.invoke(app, ["query", "Some pitch text", "--name", "MyStartup", "--stdout"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    # The rendered report's title should contain the input name.
    assert "MyStartup" in result.stdout


def test_query_smoke_default_unnamed(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``--name`` is omitted, the rendered report uses ``(unnamed)``."""

    async def _fake_run_query(input_ctx: InputContext, **_kwargs: Any) -> Report:
        return _fixture_report(name=input_ctx.name)

    monkeypatch.setattr("slopmortem.cli._query_cmd.build_deps", _build_fake_deps)
    monkeypatch.setattr("slopmortem.cli._query_cmd.set_query_corpus", _noop_set_corpus)
    monkeypatch.setattr("slopmortem.cli._query_cmd.run_query", _fake_run_query)

    runner = CliRunner()
    result = runner.invoke(app, ["query", "A pitch", "--stdout"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "(unnamed)" in result.stdout


def test_query_default_writes_to_runs_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Default (no --stdout) writes the report under .slopmortem/runs/ and echoes the path."""

    async def _fake_run_query(input_ctx: InputContext, **_kwargs: Any) -> Report:
        return _fixture_report(name=input_ctx.name)

    monkeypatch.setattr("slopmortem.cli._query_cmd.build_deps", _build_fake_deps)
    monkeypatch.setattr("slopmortem.cli._query_cmd.set_query_corpus", _noop_set_corpus)
    monkeypatch.setattr("slopmortem.cli._query_cmd.run_query", _fake_run_query)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["query", "A pitch", "--name", "MyStartup"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")

    out_path = Path(result.stdout.strip())
    assert out_path.parent == Path(".slopmortem/runs")
    assert out_path.suffix == ".md"
    assert "mystartup" in out_path.name  # slug from --name
    full_path = tmp_path / out_path
    assert full_path.exists()
    assert "MyStartup" in full_path.read_text(encoding="utf-8")


def test_query_empty_candidates_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty candidate list prints a not-found markdown to stdout and exits with code 1."""

    async def _fake_run_query(input_ctx: InputContext, **_kwargs: Any) -> Report:
        return _fixture_report(name=input_ctx.name).model_copy(update={"candidates": []})

    monkeypatch.setattr("slopmortem.cli._query_cmd.build_deps", _build_fake_deps)
    monkeypatch.setattr("slopmortem.cli._query_cmd.set_query_corpus", _noop_set_corpus)
    monkeypatch.setattr("slopmortem.cli._query_cmd.run_query", _fake_run_query)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["query", "A pitch"])
    assert result.exit_code == 1
    assert "No matching post-mortems found" in result.stdout
    # No file should have been written.
    assert not (tmp_path / ".slopmortem" / "runs").exists()


def test_replay_missing_dataset_exits_with_code_2() -> None:
    """``replay`` against a missing dataset exits cleanly with code 2."""
    runner = CliRunner()
    result = runner.invoke(app, ["replay", "does-not-exist"])
    assert result.exit_code == 2
    # Error goes to stderr; click's CliRunner merges streams unless
    # ``mix_stderr=False`` is set. Either way "no dataset" lands in the
    # captured output.
    combined = (result.stdout or "") + (result.stderr or "")
    assert "no dataset" in combined
