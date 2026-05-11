"""CLI tests for ``slopmortem query`` recall wiring.

Asserts the default path always builds ``RecallDeps`` (recall fires
automatically when the coverage gap predicate trips) and that
``--force-llm-recall`` flips ``Config.force_llm_recall``. ``run_query`` and
``build_deps`` are patched so the test never builds a real LLM/Qdrant/embedder
and never spends any LLM credit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from typer.testing import CliRunner

from slopmortem.budget import Budget
from slopmortem.cli import app
from slopmortem.cli._query_cmd import _progress_context
from slopmortem.models import (
    InputContext,
    PerspectiveScore,
    PipelineMeta,
    Report,
    SimilarityScores,
    Synthesis,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from slopmortem.config import Config


def _fixture_report() -> Report:
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
        input=InputContext(name="x", description="A pitch", years_filter=None),
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
    return object(), object(), object(), Budget(cap_usd=0.0), object()


def _noop_set_corpus(_corpus: object) -> None:
    return


def _patch_query_seams(
    monkeypatch: pytest.MonkeyPatch,
    *,
    captured: dict[str, Any],
    tmp_path: Path,
    stub_recall_deps: bool = True,
) -> None:
    """Wire fakes for the ``query`` CLI so the test never builds real deps.

    ``captured`` collects the ``Config`` ``run_query`` was called with so
    the test can assert on the recall flag. ``stub_recall_deps=False`` keeps
    the real ``_build_recall_deps`` so a test can verify the real journal
    /classifier construction path runs (used by the always-on contract test).
    """

    async def _fake_run_query(input_ctx: InputContext, **kwargs: Any) -> Report:
        captured["config"] = kwargs["config"]
        captured["recall_deps"] = kwargs.get("recall_deps")
        report = _fixture_report()
        return report.model_copy(update={"input": input_ctx})

    async def _stub_build_recall_deps(*_args: object, **_kwargs: object) -> object:
        # Return a sentinel so the CLI passes a non-None deps object; tests
        # that care about identity assert against this value.
        return _RECALL_DEPS_STUB

    monkeypatch.setattr("slopmortem.cli._query_cmd.build_deps", _build_fake_deps)
    monkeypatch.setattr("slopmortem.cli._query_cmd.set_query_corpus", _noop_set_corpus)
    monkeypatch.setattr("slopmortem.cli._query_cmd.run_query", _fake_run_query)
    if stub_recall_deps:
        monkeypatch.setattr("slopmortem.cli._query_cmd._build_recall_deps", _stub_build_recall_deps)
    monkeypatch.chdir(tmp_path)


_RECALL_DEPS_STUB = object()


def test_force_llm_recall_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--force-llm-recall`` flips ``Config.force_llm_recall`` to True."""
    captured: dict[str, Any] = {}
    _patch_query_seams(monkeypatch, captured=captured, tmp_path=tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["query", "A pitch", "--force-llm-recall", "--stdout"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")

    cfg = captured["config"]
    assert cfg.force_llm_recall is True


def test_recall_deps_built_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Without flags, ``force_llm_recall`` stays False but ``RecallDeps`` is built.

    Predicate-driven recall fires inside ``run_query``; the CLI's job is to
    always provide deps so the branch can run.
    """
    captured: dict[str, Any] = {}
    _patch_query_seams(monkeypatch, captured=captured, tmp_path=tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["query", "A pitch", "--stdout"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")

    cfg = captured["config"]
    assert cfg.force_llm_recall is False
    assert captured["recall_deps"] is _RECALL_DEPS_STUB


def test_query_cmd_calls_maybe_setup_logging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``query`` honors ``SLOPMORTEM_LOG`` by going through ``_maybe_setup_logging``.

    The bug this locks: ``query`` previously didn't call the helper, so
    ``SLOPMORTEM_LOG=info just query`` was a silent no-op. Spy on the
    helper rather than asserting on captured logs — root-logger config has
    been touched by other tests in the suite and is brittle to assert on.
    """
    captured: dict[str, Any] = {}
    _patch_query_seams(monkeypatch, captured=captured, tmp_path=tmp_path)

    calls: list[bool] = []

    def _spy() -> None:
        calls.append(True)

    monkeypatch.setattr("slopmortem.cli._query_cmd._maybe_setup_logging", _spy)
    runner = CliRunner()
    result = runner.invoke(app, ["query", "A pitch", "--stdout"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert calls == [True]


def test_debug_retrieve_skips_recall_deps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--debug-retrieve`` returns before deps construction.

    Patches ``MergeJournal`` and ``HaikuSlopClassifier`` to a sentinel that
    raises on instantiation; the test passes if neither is touched.
    """

    class _Forbidden:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            msg = "debug-retrieve must not build recall deps"
            raise AssertionError(msg)

    async def _fake_debug_retrieve(*_args: object, **_kwargs: object) -> None:
        return

    monkeypatch.setattr("slopmortem.cli._query_cmd.build_deps", _build_fake_deps)
    monkeypatch.setattr("slopmortem.cli._query_cmd.set_query_corpus", _noop_set_corpus)
    monkeypatch.setattr("slopmortem.cli._query_cmd._debug_retrieve", _fake_debug_retrieve)
    monkeypatch.setattr("slopmortem.corpus.MergeJournal", _Forbidden)
    monkeypatch.setattr("slopmortem.ingest.HaikuSlopClassifier", _Forbidden)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["query", "A pitch", "--debug-retrieve"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")


def test_progress_context_returns_nullcontext_when_env_disables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SLOPMORTEM_NO_PROGRESS=1`` short-circuits even when every TTY probe says yes.

    Forces both ``sys.stderr.isatty`` and Rich's ``Console.is_terminal`` to
    True so the env-var gate is the only thing that can flip the result —
    proves the escape hatch isn't shadowed by the other checks.
    """
    monkeypatch.setenv("SLOPMORTEM_NO_PROGRESS", "1")
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setattr("rich.console.Console.is_terminal", True)
    with _progress_context() as bar:
        assert bar is None


def test_progress_context_returns_nullcontext_when_stderr_not_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-TTY stderr (piped/redirected) disables the bar."""
    monkeypatch.delenv("SLOPMORTEM_NO_PROGRESS", raising=False)
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    with _progress_context() as bar:
        assert bar is None


def test_progress_context_returns_nullcontext_when_rich_rejects_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Python TTY check True + Rich ``is_terminal`` False = still disabled.

    This is the pty-but-not-cursor-positionable case (``just`` wrapper, some
    CI shells) that caused the splatting bug.
    """
    monkeypatch.delenv("SLOPMORTEM_NO_PROGRESS", raising=False)
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setattr("rich.console.Console.is_terminal", False)
    with _progress_context() as bar:
        assert bar is None
