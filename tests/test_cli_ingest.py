"""CLI tests for ``slopmortem ingest``, covering wiring assembly and orchestrator dispatch."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

from typer.testing import CliRunner

from slopmortem.budget import Budget
from slopmortem.cli import app
from slopmortem.cli._ingest_cmd import _default_curated_yaml

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


async def _fake_deps(*_args: object, **_kwargs: object) -> tuple[Any, ...]:
    """Return six MagicMock placeholders matching ``_build_ingest_deps``'s tuple shape.

    Async because ``_build_ingest_deps`` is async; it awaits the journal's
    ``init()`` to create the sqlite schema.
    """
    return (
        MagicMock(name="llm"),
        MagicMock(name="embed"),
        MagicMock(name="corpus"),
        Budget(cap_usd=1.0),
        MagicMock(name="journal"),
        MagicMock(name="slop"),
    )


def test_default_curated_yaml_resolves_to_existing_file() -> None:
    """``Path(__file__)``-anchored helper must survive moves between subpackages."""
    path = _default_curated_yaml()
    assert path.is_file(), f"curated YAML missing at {path}"


def test_ingest_dry_run_dispatches_to_orchestrator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--dry-run path: wiring is assembled and ingest() is called with dry_run=True."""
    fake_ingest = AsyncMock(return_value=MagicMock(dry_run=True, processed=0))
    monkeypatch.setattr("slopmortem.cli._ingest_cmd.ingest", fake_ingest)
    # Block real Qdrant / OpenRouter / OpenAI / sqlite construction.
    monkeypatch.setattr("slopmortem.cli._ingest_cmd._build_ingest_deps", _fake_deps)
    runner = CliRunner()
    result = runner.invoke(app, ["ingest", "--dry-run", "--post-mortems-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert fake_ingest.await_count == 1
    await_args = fake_ingest.await_args
    assert await_args is not None
    kwargs = await_args.kwargs
    assert kwargs["dry_run"] is True
    assert kwargs["force"] is False
    assert kwargs["post_mortems_root"] == tmp_path


def test_ingest_tavily_enrich_appends_enricher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--tavily-enrich now wires a real TavilyEnricher into the enrichers list."""
    captured: dict[str, object] = {}

    async def fake_ingest(**kwargs: object) -> object:
        captured["enrichers"] = kwargs["enrichers"]
        return MagicMock(dry_run=True, processed=0)

    monkeypatch.setattr("slopmortem.cli._ingest_cmd.ingest", fake_ingest)
    monkeypatch.setattr("slopmortem.cli._ingest_cmd._build_ingest_deps", _fake_deps)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["ingest", "--dry-run", "--tavily-enrich", "--post-mortems-root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    enrichers = captured["enrichers"]
    assert isinstance(enrichers, list)
    enricher_classnames = [type(e).__name__ for e in enrichers]
    assert "TavilyEnricher" in enricher_classnames


def test_ingest_with_crunchbase_csv_appends_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When --crunchbase-csv is given, the sources list includes CrunchbaseCsvSource."""
    captured: dict[str, object] = {}

    async def fake_ingest(**kwargs: object) -> object:
        captured["sources"] = kwargs["sources"]
        return MagicMock(dry_run=True, processed=0)

    monkeypatch.setattr("slopmortem.cli._ingest_cmd.ingest", fake_ingest)
    monkeypatch.setattr("slopmortem.cli._ingest_cmd._build_ingest_deps", _fake_deps)
    csv = tmp_path / "cb.csv"
    csv.write_text("name,description\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "ingest",
            "--dry-run",
            "--crunchbase-csv",
            str(csv),
            "--post-mortems-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    sources = captured["sources"]
    assert isinstance(sources, list)
    source_classnames = [type(s).__name__ for s in sources]
    assert "CrunchbaseCsvSource" in source_classnames
    assert "CuratedSource" in source_classnames
    assert "HNAlgoliaSource" in source_classnames


def test_enable_tavily_news_without_api_key_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr("slopmortem.cli._ingest_cmd._build_ingest_deps", _fake_deps)
    runner = CliRunner()
    result = runner.invoke(app, ["ingest", "--enable-tavily-news", "--dry-run"])
    assert result.exit_code != 0, result.output
    combined = result.output + (result.stderr or "")
    assert "TAVILY_API_KEY" in combined


def test_enable_defillama_without_tavily_key_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr("slopmortem.cli._ingest_cmd._build_ingest_deps", _fake_deps)
    runner = CliRunner()
    result = runner.invoke(app, ["ingest", "--enable-defillama", "--dry-run"])
    assert result.exit_code != 0, result.output
    combined = result.output + (result.stderr or "")
    assert "TAVILY_API_KEY" in combined


def test_only_source_tavily_news_runs_in_isolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    async def fake_ingest(**kwargs: object) -> object:
        captured["sources"] = kwargs["sources"]
        return MagicMock(dry_run=True, processed=0)

    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-key")
    monkeypatch.setattr("slopmortem.cli._ingest_cmd.ingest", fake_ingest)
    monkeypatch.setattr("slopmortem.cli._ingest_cmd._build_ingest_deps", _fake_deps)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "ingest",
            "--dry-run",
            "--only-source",
            "tavily_news",
            "--post-mortems-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    sources = captured["sources"]
    assert isinstance(sources, list)
    classnames = [type(s).__name__ for s in sources]
    assert classnames == ["TavilyNewsSource"]


def test_only_source_unknown_name_lists_valid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("slopmortem.cli._ingest_cmd._build_ingest_deps", _fake_deps)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "ingest",
            "--dry-run",
            "--only-source",
            "definitely-not-a-source",
            "--post-mortems-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0, result.output
    combined = result.output + (result.stderr or "")
    assert "tavily_news" in combined
    assert "curated" in combined
