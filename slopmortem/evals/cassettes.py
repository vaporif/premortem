"""Cassette loaders, writers, slugifier, error types, and schema-version policy.

Key derivation lives in ``slopmortem.llm.cassettes``. Recording wrappers and
replay loaders both import from here so record and replay can't disagree on
disk shape.

Schema version is ``<major>.<minor>``: reader hard-fails on major mismatch,
accepts any minor at the same major. Minor bumps are additive, and
``extra="ignore"`` lets unknown future fields deserialize cleanly.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from slopmortem.llm import (
    FakeEmbeddingClient,
    FakeLLMClient,
    FakeResponse,
    embed_cassette_key,
)
from slopmortem.llm import (
    NoCannedEmbeddingError as NoCannedEmbeddingError,  # noqa: PLC0414 - explicit re-export for back-compat
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from slopmortem.config import Config
    from slopmortem.llm import CompletionResult


_SCHEMA_MAJOR = 1
_SCHEMA_MINOR = 0
CASSETTE_SCHEMA_VERSION = f"{_SCHEMA_MAJOR}.{_SCHEMA_MINOR}"

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]")


class CassetteFormatError(Exception):
    """Cassette JSON unparseable or fails type validation."""


class CassetteSchemaError(Exception):
    """Cassette ``schema_version`` is unparseable or major-mismatched."""


class DuplicateCassetteError(Exception):
    """Two cassette files in the same scope dir resolved to the same key."""


class RecordingBudgetExceededError(Exception):
    """``RecordingLLMClient`` accumulated cost would exceed ``max_cost_usd``."""

    def __init__(self, *, spent: float, limit: float) -> None:
        super().__init__(f"recording cost ceiling exceeded: spent={spent:.4f} limit={limit:.4f}")
        self.spent = spent
        self.limit = limit


def _slugify_model(model: str) -> str:
    """Replace any character outside ``[A-Za-z0-9._-]`` with ``_`` (filenames only)."""
    return _SLUG_RE.sub("_", model)


def _check_schema_version(version: object, *, path: Path) -> None:
    """Enforce ``major == reader_major``; any minor at the same major is accepted."""
    if not isinstance(version, str) or "." not in version:
        msg = f"cassette {path} has unparseable schema_version={version!r}"
        raise CassetteSchemaError(msg)
    try:
        major_str, _ = version.split(".", 1)
        major = int(major_str)
    except ValueError as exc:
        msg = f"cassette {path} has unparseable schema_version={version!r}"
        raise CassetteSchemaError(msg) from exc
    if major != _SCHEMA_MAJOR:
        msg = (
            f"cassette {path} schema major mismatch: file={major}, "
            f"reader={_SCHEMA_MAJOR}; cassette must be re-recorded"
        )
        raise CassetteSchemaError(msg)


_FROZEN_IGNORE = ConfigDict(frozen=True, extra="ignore", protected_namespaces=())
_IGNORE = ConfigDict(extra="ignore", protected_namespaces=())


class LlmCassette(BaseModel):
    """LLM cassette: flat consumer-facing shape, Pydantic-validated on load."""

    model_config = _FROZEN_IGNORE

    template_sha: str
    model: str
    prompt_hash: str
    text: str
    stop_reason: str
    cost_usd: float
    cache_read_tokens: int | None
    cache_creation_tokens: int | None
    prompt_preview: str
    system_preview: str
    tools_present: list[str]
    response_format_present: bool


class EmbeddingCassette(BaseModel):
    model_config = _FROZEN_IGNORE

    model: str
    text_hash: str
    vector: list[float]
    text_preview: str


class SparseCassette(BaseModel):
    """Sparse embedding cassette (Qdrant/bm25)."""

    model_config = _FROZEN_IGNORE

    model: str
    text_hash: str
    indices: list[int]
    values: list[float]
    text_preview: str


# Read-side envelope models mirror the on-disk JSON. ``extra="ignore"`` lets
# a future writer add fields (logprobs, trace_id, …) without breaking older
# readers.


class _LlmKey(BaseModel):
    model_config = _IGNORE
    template_sha: str
    model: str
    prompt_hash: str


class _LlmResponse(BaseModel):
    model_config = _IGNORE
    text: str
    stop_reason: str = "stop"
    cost_usd: float = 0.0
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None


class _LlmDebug(BaseModel):
    model_config = _IGNORE
    prompt_preview: str = ""
    system_preview: str = ""
    tools_present: list[str] = Field(default_factory=list)
    response_format_present: bool = False


class _LlmEnvelope(BaseModel):
    model_config = _IGNORE
    schema_version: str
    key: _LlmKey
    response: _LlmResponse
    request_debug: _LlmDebug = Field(default_factory=_LlmDebug)


class _EmbedKey(BaseModel):
    model_config = _IGNORE
    model: str
    text_hash: str


class _EmbedResponse(BaseModel):
    model_config = _IGNORE
    vector: list[float] | None = None  # dense
    indices: list[int] | None = None  # sparse
    values: list[float] | None = None  # sparse


class _EmbedEnvelope(BaseModel):
    model_config = _IGNORE
    schema_version: str
    key: _EmbedKey
    response: _EmbedResponse


_LLM_ADAPTER: TypeAdapter[_LlmEnvelope] = TypeAdapter(_LlmEnvelope)
_EMBED_ADAPTER: TypeAdapter[_EmbedEnvelope] = TypeAdapter(_EmbedEnvelope)


def write_llm_cassette(cas: LlmCassette, out_dir: Path, *, stage: str) -> Path:
    """Write to ``<out_dir>/<stage>__<slug(model)>__<prompt_hash>.json``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{stage}__{_slugify_model(cas.model)}__{cas.prompt_hash}.json"
    path = out_dir / fname
    path.write_text(
        json.dumps(
            {
                "schema_version": CASSETTE_SCHEMA_VERSION,
                "key": {
                    "template_sha": cas.template_sha,
                    "model": cas.model,
                    "prompt_hash": cas.prompt_hash,
                },
                "response": {
                    "text": cas.text,
                    "stop_reason": cas.stop_reason,
                    "cost_usd": cas.cost_usd,
                    "cache_read_tokens": cas.cache_read_tokens,
                    "cache_creation_tokens": cas.cache_creation_tokens,
                },
                "request_debug": {
                    "prompt_preview": cas.prompt_preview,
                    "system_preview": cas.system_preview,
                    "tools_present": cas.tools_present,
                    "response_format_present": cas.response_format_present,
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return path


def write_embedding_cassette(cas: EmbeddingCassette, out_dir: Path) -> Path:
    """Write to ``<out_dir>/embed__<slug(model)>__<text_hash>.json``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"embed__{_slugify_model(cas.model)}__{cas.text_hash}.json"
    path = out_dir / fname
    path.write_text(
        json.dumps(
            {
                "schema_version": CASSETTE_SCHEMA_VERSION,
                "key": {"model": cas.model, "text_hash": cas.text_hash},
                "response": {"vector": cas.vector},
                "request_debug": {
                    "text_preview": cas.text_preview,
                    "vector_dim": len(cas.vector),
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return path


def write_sparse_cassette(cas: SparseCassette, out_dir: Path) -> Path:
    """Write to ``<out_dir>/embed__<slug>__<text_hash>.json`` (sparse variant)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"embed__{_slugify_model(cas.model)}__{cas.text_hash}.json"
    path = out_dir / fname
    path.write_text(
        json.dumps(
            {
                "schema_version": CASSETTE_SCHEMA_VERSION,
                "key": {"model": cas.model, "text_hash": cas.text_hash},
                "response": {"indices": cas.indices, "values": cas.values},
                "request_debug": {
                    "text_preview": cas.text_preview,
                    "nnz": len(cas.indices),
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return path


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        raw: object = json.loads(path.read_text())  # pyright: ignore[reportAny]
    except json.JSONDecodeError as exc:
        msg = f"cassette {path} is not valid JSON: {exc}"
        raise CassetteFormatError(msg) from exc
    if not isinstance(raw, dict):
        msg = f"cassette {path} top-level must be an object"
        raise CassetteFormatError(msg)
    # JSON object keys are always strings; cast keeps static type honest.
    return cast("dict[str, object]", raw)


def load_llm_cassettes(
    scope_dir: Path,
) -> Mapping[tuple[str, str, str], LlmCassette]:
    """Load every ``<stage>__*.json`` (non-``embed__``) under ``scope_dir``.

    Bad types raise ``CassetteFormatError`` with the path here, not a
    confusing ``TypeError`` later at use site.
    """
    out: dict[tuple[str, str, str], LlmCassette] = {}
    if not scope_dir.exists():
        return out
    for path in sorted(scope_dir.glob("*.json")):
        if path.name.startswith("embed__"):
            continue
        raw = _read_json_object(path)
        _check_schema_version(raw.get("schema_version"), path=path)
        try:
            env = _LLM_ADAPTER.validate_python(raw)
        except ValidationError as exc:
            msg = f"cassette {path} failed schema validation: {exc}"
            raise CassetteFormatError(msg) from exc
        cas = LlmCassette(
            template_sha=env.key.template_sha,
            model=env.key.model,
            prompt_hash=env.key.prompt_hash,
            text=env.response.text,
            stop_reason=env.response.stop_reason,
            cost_usd=env.response.cost_usd,
            cache_read_tokens=env.response.cache_read_tokens,
            cache_creation_tokens=env.response.cache_creation_tokens,
            prompt_preview=env.request_debug.prompt_preview,
            system_preview=env.request_debug.system_preview,
            tools_present=env.request_debug.tools_present,
            response_format_present=env.request_debug.response_format_present,
        )
        key = (cas.template_sha, cas.model, cas.prompt_hash)
        if key in out:
            msg = f"duplicate cassette key {key!r} in {scope_dir}"
            raise DuplicateCassetteError(msg)
        out[key] = cas
    return out


def load_embedding_cassettes(
    scope_dir: Path,
) -> tuple[
    Mapping[tuple[str, str], list[float]],
    Mapping[tuple[str, str], tuple[list[int], list[float]]],
]:
    """Load every ``embed__*.json`` under ``scope_dir``.

    Splits sparse vs dense by response shape.
    """
    dense: dict[tuple[str, str], list[float]] = {}
    sparse: dict[tuple[str, str], tuple[list[int], list[float]]] = {}
    if not scope_dir.exists():
        return dense, sparse
    for path in sorted(scope_dir.glob("embed__*.json")):
        raw = _read_json_object(path)
        _check_schema_version(raw.get("schema_version"), path=path)
        try:
            env = _EMBED_ADAPTER.validate_python(raw)
        except ValidationError as exc:
            msg = f"cassette {path} failed schema validation: {exc}"
            raise CassetteFormatError(msg) from exc
        model = env.key.model
        text_hash = env.key.text_hash
        if env.response.indices is not None and env.response.values is not None:
            key_s = (model, text_hash)
            if key_s in sparse:
                msg = f"duplicate sparse cassette key {key_s!r} in {scope_dir}"
                raise DuplicateCassetteError(msg)
            sparse[key_s] = (env.response.indices, env.response.values)
        elif env.response.vector is not None:
            key_d = (model, text_hash)
            if key_d in dense:
                msg = f"duplicate dense cassette key {key_d!r} in {scope_dir}"
                raise DuplicateCassetteError(msg)
            dense[key_d] = env.response.vector
        else:
            msg = f"embed cassette {path} response has neither vector nor (indices, values)"
            raise CassetteFormatError(msg)
    return dense, sparse


def load_row_fakes(
    scope_dir: Path,
    cfg: Config,
) -> tuple[FakeLLMClient, FakeEmbeddingClient, Callable[[str], dict[int, float]]]:
    """Build the ``(fake_llm, fake_embed, sparse_encoder)`` triple for one row from cassettes.

    The sparse encoder closure raises ``NoCannedEmbeddingError`` on miss; the
    LLM/dense fakes raise their own miss errors at call time.
    """
    llm_canned: dict[tuple[str, str, str], FakeResponse | CompletionResult] = {
        k: FakeResponse(
            text=v.text,
            stop_reason=v.stop_reason,
            cost_usd=v.cost_usd,
            cache_read_tokens=v.cache_read_tokens,
            cache_creation_tokens=v.cache_creation_tokens,
        )
        for k, v in load_llm_cassettes(scope_dir).items()
    }
    dense_canned, sparse_canned = load_embedding_cassettes(scope_dir)
    fake_llm = FakeLLMClient(canned=llm_canned, default_model=cfg.model_synthesize)
    fake_embed = FakeEmbeddingClient(model=cfg.embed_model_id, canned=dense_canned)

    def cassette_sparse(text: str) -> dict[int, float]:
        key = embed_cassette_key(text=text, model="Qdrant/bm25")
        if key not in sparse_canned:
            msg = f"no sparse cassette for {key!r}"
            raise NoCannedEmbeddingError(msg)
        idx, vals = sparse_canned[key]
        return dict(zip(idx, vals, strict=True))

    return fake_llm, fake_embed, cassette_sparse
