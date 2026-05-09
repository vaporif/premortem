"""Config loader: TOML, env, and secrets, validated on load."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, override

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


class Config(BaseSettings):
    """All knobs slopmortem reads at startup.

    Precedence (highest wins): env > .env > slopmortem.local.toml >
    slopmortem.toml (tracked defaults) > built-in defaults.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="forbid",
        toml_file=("slopmortem.toml", "slopmortem.local.toml"),
    )

    K_retrieve: int = Field(default=30, ge=1)
    N_synthesize: int = Field(default=5, ge=1)
    min_similarity_score: float = Field(default=4.0, ge=0.0, le=10.0)
    strict_sector_filter: bool = False
    strict_sector_filter_excludes_other: bool = False
    ingest_concurrency: int = Field(default=20, ge=1)
    facet_boost: float = Field(default=0.01, ge=0.0)
    rrf_k: int = Field(default=60, ge=1)
    slop_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    max_doc_tokens: int = Field(default=8000, ge=1)
    tier3_calibration_band: tuple[float, float] = (0.65, 0.85)
    max_cost_usd_per_query: float = Field(default=2.00, gt=0.0)
    max_cost_usd_per_ingest: float = Field(default=15.00, gt=0.0)

    cache_read_ratio_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    cache_read_ratio_probe_n: int = Field(default=5, ge=1)

    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    model_facet: str = "anthropic/claude-haiku-4.5"
    model_summarize: str = "anthropic/claude-haiku-4.5"
    model_rerank: str = "anthropic/claude-sonnet-4.6"
    model_synthesize: str = "anthropic/claude-sonnet-4.6"
    model_consolidate: str = "anthropic/claude-sonnet-4.6"
    model_pitch_filler: str = "anthropic/claude-haiku-4.5"
    model_title_pre_filter: str = "anthropic/claude-haiku-4.5"

    # Recall fires automatically when the coverage-gap predicate trips
    # (survivors < N_synthesize after rerank+min_similarity). ``force_llm_recall``
    # fires recall on every query, bypassing the predicate. Useful for cassette
    # recording, eval calibration, and probes against thin/new corpora. Spend is
    # gated by the pipeline-level ``Budget`` shared across every LLM call — there
    # is no per-stage cap.
    force_llm_recall: bool = False
    model_recall: str = "anthropic/claude-opus-4-7"
    max_tokens_recall: int = Field(default=4096, ge=1)
    recall_max_suggestions_per_pitch: int = Field(default=8, ge=1, le=20)

    # L5 deathness gate: a verified URL with the right keywords still doesn't
    # prove the company actually died (could be a layoff article about a still-
    # running firm). Haiku reads the body and answers died/confidence; we drop
    # below ``recall_deathness_min_confidence`` or on died=false.
    model_recall_deathness: str = "anthropic/claude-haiku-4.5"
    max_tokens_recall_deathness: int = Field(default=128, ge=1)
    recall_deathness_min_confidence: float = Field(default=0.7, ge=0.0, le=1.0)

    # Per-stage output caps. OpenRouter holds upfront credit for the model's
    # max output, so leaving these unset reserves the full 64K Anthropic
    # ceiling and surfaces as HTTP 402 on low-balance keys. Values sized to
    # each stage's actual output shape plus slack.
    max_tokens_facet: int = Field(default=2000, ge=1)
    max_tokens_summarize: int = Field(default=1500, ge=1)
    max_tokens_rerank: int = Field(default=4000, ge=1)
    max_tokens_synthesize: int = Field(default=16000, ge=1)
    max_tokens_consolidate: int = Field(default=2048, ge=1)
    max_tokens_slop_judge: int = Field(default=64, ge=1)
    max_tokens_tiebreaker: int = Field(default=256, ge=1)
    max_tokens_pitch_filler: int = Field(default=1500, ge=1)
    max_tokens_title_pre_filter: int = Field(default=16, ge=1)

    embedding_provider: Literal["fastembed", "openai"] = "fastembed"
    embed_model_id: str = "nomic-ai/nomic-embed-text-v1.5"
    embed_cache_dir: Path | None = None
    retry_max_attempts: int = Field(default=3, ge=0)
    retry_initial_backoff: float = Field(default=1.0, ge=0.0)

    taxonomy_version: str = "v2"
    reliability_rank_version: str = "v1"

    enable_tavily_synthesis: bool = False
    enable_pitch_filler: bool = False
    enable_title_pre_filter: bool = False
    pitch_filler_max_chars_per_result: int = Field(default=2500, ge=1)
    enable_tracing: bool = False
    strict_deaths: bool = False

    tavily_calls_per_synthesis: int = Field(default=2, ge=0)

    openrouter_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    tavily_api_key: SecretStr = SecretStr("")
    laminar_api_key: SecretStr = SecretStr("")

    # Infrastructure knobs populated from env (LMNR_*, QDRANT_*,
    # POST_MORTEMS_ROOT, MERGE_JOURNAL_PATH). Declared here so the CLI can read
    # them off Config instead of os.environ, and so extra="forbid" doesn't
    # reject .env entries for them.
    lmnr_project_api_key: SecretStr = SecretStr("")
    lmnr_base_url: str = ""
    lmnr_allow_remote: str = ""
    qdrant_host: str = "localhost"
    # 16333 (not 6333) so the project's docker-compose Qdrant doesn't collide
    # with any pre-existing 6333 instance on the dev box. The container still
    # serves 6333 internally; only the host-side publish port is bumped.
    qdrant_port: int = Field(default=16333, ge=1, le=65535)
    qdrant_collection: str = "slopmortem"
    post_mortems_root: str = "./post_mortems"
    merge_journal_path: str = ""

    @model_validator(mode="after")
    def _check_k_ge_n(self) -> Config:
        if self.K_retrieve < self.N_synthesize:
            msg = f"K_retrieve ({self.K_retrieve}) must be >= N_synthesize ({self.N_synthesize})"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_tier3_band(self) -> Config:
        lo, hi = self.tier3_calibration_band
        if not (0.0 <= lo <= hi <= 1.0):
            msg = (
                f"tier3_calibration_band must satisfy 0.0 <= lo <= hi <= 1.0, "
                f"got {self.tier3_calibration_band}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_required_api_keys(self) -> Config:
        if self.enable_tavily_synthesis and not self.tavily_api_key.get_secret_value():
            msg = "enable_tavily_synthesis=True requires tavily_api_key"
            raise ValueError(msg)
        if self.enable_pitch_filler and not self.tavily_api_key.get_secret_value():
            msg = (
                "enable_pitch_filler=True requires tavily_api_key "
                "(the filler's tavily_search tool needs the Tavily API key)"
            )
            raise ValueError(msg)
        if self.embedding_provider == "openai" and not self.openai_api_key.get_secret_value():
            msg = 'embedding_provider="openai" requires openai_api_key'
            raise ValueError(msg)
        return self

    @classmethod
    @override
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """TOML sits below env+dotenv (12-factor).

        The second ``toml_file`` entry applies last so ``local.toml`` overrides
        tracked defaults.
        """
        toml_files: list[Path] = [
            p
            for name in ("slopmortem.toml", "slopmortem.local.toml")
            if (p := Path.cwd() / name).exists()
        ]
        toml = TomlConfigSettingsSource(settings_cls, toml_file=toml_files or None)
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            toml,
            file_secret_settings,
        )


def load_config() -> Config:
    return Config()
