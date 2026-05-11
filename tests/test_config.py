from __future__ import annotations

import pytest
from pydantic import ValidationError

from slopmortem.config import Config, load_config


def test_defaults_load_from_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "slopmortem.toml").write_text("""\
K_retrieve = 30
N_synthesize = 5
ingest_concurrency = 20
facet_boost = 0.01
rrf_k = 60
slop_threshold = 0.7
max_doc_tokens = 50000
tier3_calibration_band = [0.65, 0.85]
max_cost_usd_per_query = 2.00
max_cost_usd_per_ingest = 15.00
openrouter_base_url = "https://openrouter.ai/api/v1"
model_facet = "anthropic/claude-haiku-4.5"
model_summarize = "anthropic/claude-haiku-4.5"
model_rerank = "anthropic/claude-sonnet-4.6"
model_synthesize = "anthropic/claude-sonnet-4.6"
""")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    c = load_config()
    assert c.K_retrieve == 30
    assert c.N_synthesize == 5
    assert c.K_retrieve >= c.N_synthesize
    assert c.openrouter_api_key.get_secret_value() == "sk-or-v1-test"


def test_typo_in_toml_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "slopmortem.toml").write_text("K_retreive = 30\n")  # typo
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    with pytest.raises(ValidationError, match="extra"):  # extra="forbid"
        load_config()


def test_k_retrieve_must_gte_n_synthesize(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "slopmortem.toml").write_text("K_retrieve = 3\nN_synthesize = 5\n")
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    with pytest.raises(ValidationError, match="K_retrieve"):
        load_config()


def test_env_overrides_local_toml(tmp_path, monkeypatch):
    """Env vars beat slopmortem.local.toml -- standard 12-factor."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "slopmortem.toml").write_text("max_cost_usd_per_query = 1.0\n")
    (tmp_path / "slopmortem.local.toml").write_text("max_cost_usd_per_query = 2.0\n")
    monkeypatch.setenv("MAX_COST_USD_PER_QUERY", "5.0")
    cfg = load_config()
    assert cfg.max_cost_usd_per_query == 5.0


def test_local_toml_overrides_main_toml(tmp_path, monkeypatch):
    """slopmortem.local.toml beats slopmortem.toml when env is unset."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "slopmortem.toml").write_text("max_cost_usd_per_query = 1.0\n")
    (tmp_path / "slopmortem.local.toml").write_text("max_cost_usd_per_query = 2.0\n")
    monkeypatch.delenv("MAX_COST_USD_PER_QUERY", raising=False)
    cfg = load_config()
    assert cfg.max_cost_usd_per_query == 2.0


def test_dotenv_overrides_local_toml(tmp_path, monkeypatch):
    """`.env` beats slopmortem.local.toml (env tier > toml tier)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "slopmortem.local.toml").write_text("max_cost_usd_per_query = 2.0\n")
    (tmp_path / ".env").write_text("MAX_COST_USD_PER_QUERY=7.0\n")
    monkeypatch.delenv("MAX_COST_USD_PER_QUERY", raising=False)
    cfg = load_config()
    assert cfg.max_cost_usd_per_query == 7.0


_FIELD_REJECTS: list[tuple[str, object, str]] = [
    ("slop_threshold", 1.5, "slop_threshold"),
    ("slop_threshold", -0.1, "slop_threshold"),
    ("min_similarity_score", 11.0, "min_similarity_score"),
    ("min_similarity_score", -0.1, "min_similarity_score"),
    ("cache_read_ratio_threshold", 1.5, "cache_read_ratio_threshold"),
    ("cache_read_ratio_threshold", -0.1, "cache_read_ratio_threshold"),
    ("K_retrieve", 0, "K_retrieve"),
    ("N_synthesize", 0, "N_synthesize"),
    ("ingest_concurrency", 0, "ingest_concurrency"),
    ("rrf_k", 0, "rrf_k"),
    ("max_doc_tokens", 0, "max_doc_tokens"),
    ("cache_read_ratio_probe_n", 0, "cache_read_ratio_probe_n"),
    ("max_tokens_facet", 0, "max_tokens_facet"),
    ("max_tokens_summarize", 0, "max_tokens_summarize"),
    ("max_tokens_rerank", 0, "max_tokens_rerank"),
    ("max_tokens_synthesize", 0, "max_tokens_synthesize"),
    ("max_tokens_consolidate", 0, "max_tokens_consolidate"),
    ("max_tokens_slop_judge", 0, "max_tokens_slop_judge"),
    ("max_tokens_tiebreaker", 0, "max_tokens_tiebreaker"),
    ("facet_boost", -0.01, "facet_boost"),
    ("retry_max_attempts", -1, "retry_max_attempts"),
    ("retry_initial_backoff", -0.1, "retry_initial_backoff"),
    ("tavily_calls_per_synthesis", -1, "tavily_calls_per_synthesis"),
    ("max_cost_usd_per_query", 0.0, "max_cost_usd_per_query"),
    ("max_cost_usd_per_ingest", 0.0, "max_cost_usd_per_ingest"),
    ("max_cost_usd_per_query", -1.0, "max_cost_usd_per_query"),
    ("qdrant_port", 0, "qdrant_port"),
    ("qdrant_port", 70000, "qdrant_port"),
]


@pytest.mark.parametrize(("field", "bad", "match"), _FIELD_REJECTS)
def test_field_constraint_rejects(tmp_path, monkeypatch, field, bad, match):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError, match=match):
        Config(**{field: bad})


def test_embedding_provider_literal_rejects_unknown(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError, match="embedding_provider"):
        Config(embedding_provider="ollama")  # pyright: ignore[reportArgumentType]


def test_tier3_band_inverted_rejected(tmp_path, monkeypatch):
    """lo > hi swaps merge/split direction silently."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError, match="tier3_calibration_band"):
        Config(tier3_calibration_band=(0.85, 0.65))


def test_tier3_band_out_of_unit_interval_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError, match="tier3_calibration_band"):
        Config(tier3_calibration_band=(-0.1, 0.85))
    with pytest.raises(ValidationError, match="tier3_calibration_band"):
        Config(tier3_calibration_band=(0.65, 1.5))


def test_tier3_band_equal_endpoints_accepted(tmp_path, monkeypatch):
    """lo == hi degenerates to a hard threshold — legal, collapses the tiebreaker band."""
    monkeypatch.chdir(tmp_path)
    cfg = Config(tier3_calibration_band=(0.7, 0.7))
    assert cfg.tier3_calibration_band == (0.7, 0.7)


def test_tavily_enabled_without_key_rejected(tmp_path, monkeypatch):
    """Fail at load, not late inside the synthesis tool call."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with pytest.raises(ValidationError, match="tavily_api_key"):
        Config(enable_tavily_synthesis=True)


def test_tavily_disabled_does_not_require_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    # All three Tavily call sites must be off — recall L0 defaults on per the
    # search-then-verify contract, so it has to be explicitly disabled to make
    # the no-key Config legal.
    cfg = Config(enable_tavily_recall_search=False)
    assert cfg.enable_tavily_synthesis is False
    assert cfg.enable_tavily_recall_search is False
    assert cfg.tavily_api_key.get_secret_value() == ""


def test_openai_provider_without_key_rejected(tmp_path, monkeypatch):
    """Fail at load, not inside the embedder factory."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValidationError, match="openai_api_key"):
        Config(embedding_provider="openai", embed_model_id="text-embedding-3-small")


def test_fastembed_provider_does_not_require_openai_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = Config()
    assert cfg.embedding_provider == "fastembed"
    assert cfg.openai_api_key.get_secret_value() == ""


def test_title_pre_filter_config_defaults() -> None:
    c = Config()
    assert c.enable_title_pre_filter is False
    assert c.model_title_pre_filter == "anthropic/claude-haiku-4.5"
    assert c.max_tokens_title_pre_filter == 16


def test_min_similarity_score_after_recall_default(tmp_path, monkeypatch):
    """Recall-branch synthesis floor defaults to 3.0 (below the 4.0 corpus-normal)."""
    monkeypatch.chdir(tmp_path)
    cfg = Config()
    assert cfg.min_similarity_score_after_recall == 3.0
    assert cfg.min_similarity_score_after_recall <= cfg.min_similarity_score


def test_min_similarity_score_after_recall_validator_rejects_above_normal(tmp_path, monkeypatch):
    """Setting the recall floor above the corpus-normal floor is rejected at load."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError, match="min_similarity_score_after_recall"):
        Config(min_similarity_score=4.0, min_similarity_score_after_recall=5.0)


def test_recall_max_tavily_calls_default_is_3(tmp_path, monkeypatch):
    """Per-recall Tavily call budget defaults to 3 — covers most niches without bloating latency."""
    monkeypatch.chdir(tmp_path)
    cfg = Config()
    assert cfg.recall_max_tavily_calls == 3


def test_recall_max_tavily_calls_rejects_above_cap(tmp_path, monkeypatch):
    """Cap of 20 keeps a runaway tool loop from blowing the whole budget."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError, match="recall_max_tavily_calls"):
        Config(recall_max_tavily_calls=21)


def test_recall_max_tavily_calls_rejects_negative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError, match="recall_max_tavily_calls"):
        Config(recall_max_tavily_calls=-1)
