"""CLI tests for ``slopmortem query`` recall flag wiring.

Asserts ``--enable-llm-recall`` / ``--force-llm-recall`` flow into the
``Config`` ``run_query`` receives. ``run_query`` and ``build_deps`` are
patched so the test never builds a real LLM/Qdrant/embedder, and never
spends any LLM credit.
"""

from __future__ import annotations

from datetime import UTC, datetime
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


def _build_fake_deps(_config: Config) -> tuple[object, object, object, Budget]:
    return object(), object(), object(), Budget(cap_usd=0.0)


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
    the test can assert on the recall flags. ``stub_recall_deps=False``
    keeps the real ``_maybe_build_recall_deps`` so a test can verify the
    lazy contract (no journal / classifier when flags are off).
    """

    async def _fake_run_query(input_ctx: InputContext, **kwargs: Any) -> Report:
        captured["config"] = kwargs["config"]
        captured["recall_deps"] = kwargs.get("recall_deps")
        report = _fixture_report()
        return report.model_copy(update={"input": input_ctx})

    async def _noop_build_recall_deps(*_args: object, **_kwargs: object) -> None:
        # Skip the journal init so the test doesn't touch the filesystem.
        return None

    monkeypatch.setattr("slopmortem.cli._query_cmd.build_deps", _build_fake_deps)
    monkeypatch.setattr("slopmortem.cli._query_cmd.set_query_corpus", _noop_set_corpus)
    monkeypatch.setattr("slopmortem.cli._query_cmd.run_query", _fake_run_query)
    if stub_recall_deps:
        monkeypatch.setattr(
            "slopmortem.cli._query_cmd._maybe_build_recall_deps", _noop_build_recall_deps
        )
    monkeypatch.chdir(tmp_path)


class _ForbiddenConstruction:
    """Sentinel that raises if instantiated.

    Used to prove the lazy contract: when both recall flags are False,
    production code must NOT construct ``MergeJournal`` or
    ``HaikuSlopClassifier``. Drop one of these in via ``monkeypatch`` and a
    regression that eagerly builds the heavy deps will trip the assert.
    """

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        msg = (
            "lazy contract regressed: heavy recall dep was constructed even "
            "though both --enable-llm-recall and --force-llm-recall are off"
        )
        raise AssertionError(msg)


def test_enable_llm_recall_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--enable-llm-recall`` flips ``Config.enable_llm_recall`` to True; force stays False."""
    captured: dict[str, Any] = {}
    _patch_query_seams(monkeypatch, captured=captured, tmp_path=tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["query", "A pitch", "--enable-llm-recall", "--stdout"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")

    cfg = captured["config"]
    assert cfg.enable_llm_recall is True
    assert cfg.force_llm_recall is False


def test_force_llm_recall_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--force-llm-recall`` alone flips force=True, leaves enable=False (OR-combined)."""
    captured: dict[str, Any] = {}
    _patch_query_seams(monkeypatch, captured=captured, tmp_path=tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["query", "A pitch", "--force-llm-recall", "--stdout"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")

    cfg = captured["config"]
    assert cfg.enable_llm_recall is False
    assert cfg.force_llm_recall is True


def test_both_recall_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Both flags together set both Config bools to True."""
    captured: dict[str, Any] = {}
    _patch_query_seams(monkeypatch, captured=captured, tmp_path=tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["query", "A pitch", "--enable-llm-recall", "--force-llm-recall", "--stdout"],
    )
    assert result.exit_code == 0, result.stdout + (result.stderr or "")

    cfg = captured["config"]
    assert cfg.enable_llm_recall is True
    assert cfg.force_llm_recall is True


def test_recall_flags_default_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Without flags, both knobs stay False; recall_deps stays None.

    Exercises the REAL ``_maybe_build_recall_deps`` (not a stub) so the
    assertion bites if the lazy contract regresses. ``_ForbiddenConstruction``
    is patched in for both heavy deps; the test fails loudly if production
    instantiates them despite both flags being off.
    """
    captured: dict[str, Any] = {}
    _patch_query_seams(monkeypatch, captured=captured, tmp_path=tmp_path, stub_recall_deps=False)
    # Patch at the source modules — ``_maybe_build_recall_deps`` does
    # ``from slopmortem.corpus import MergeJournal`` / ``from slopmortem.ingest
    # import HaikuSlopClassifier`` lazily, so the rebound names there are
    # what gets resolved at call time.
    monkeypatch.setattr("slopmortem.corpus.MergeJournal", _ForbiddenConstruction)
    monkeypatch.setattr("slopmortem.ingest.HaikuSlopClassifier", _ForbiddenConstruction)

    runner = CliRunner()
    result = runner.invoke(app, ["query", "A pitch", "--stdout"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")

    cfg = captured["config"]
    assert cfg.enable_llm_recall is False
    assert cfg.force_llm_recall is False
    assert captured["recall_deps"] is None
