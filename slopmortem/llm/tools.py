# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""JSON-Schema helpers for tool definitions and OpenAI strict-mode response schemas.

``jsonref`` ships no stubs; reportUnknown* is silenced file-wide and the
shape we produced (a dict from ``model_json_schema()``) is asserted at use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import jsonref

from slopmortem.models import ToolSpec

if TYPE_CHECKING:
    from pydantic import BaseModel

    from slopmortem.config import Config

__all__ = [
    "ToolSpec",
    "recall_tools",
    "synthesis_tools",
    "to_openai_input_schema",
    "to_strict_response_schema",
]


def to_openai_input_schema(
    args_model: type[BaseModel],
) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
    """Render *args_model* as an OpenAI ``parameters`` schema with ``$ref`` inlined."""
    schema = args_model.model_json_schema()
    inlined = jsonref.replace_refs(schema, proxies=False, lazy_load=False)
    if not isinstance(inlined, dict):
        msg = f"expected dict from jsonref.replace_refs, got {type(inlined).__name__}"
        raise TypeError(msg)
    for k in ("$defs", "$schema", "$id"):
        inlined.pop(k, None)
    return cast("dict[str, Any]", dict(inlined))  # pyright: ignore[reportExplicitAny]


def _force_required(node: object) -> None:
    if not isinstance(node, dict):
        return
    if node.get("type") == "object" and "properties" in node:
        node["required"] = list(node["properties"])
        node["additionalProperties"] = False
        for v in node["properties"].values():
            _force_required(v)
    for key in ("items", "anyOf", "oneOf", "allOf"):
        v = node.get(key)
        if isinstance(v, list):
            for elem in v:
                _force_required(elem)
        elif isinstance(v, dict):
            _force_required(v)


def to_strict_response_schema(
    model: type[BaseModel],
) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
    """Emit ``response_format.json_schema.schema`` for OpenAI strict mode.

    Pydantic v2 drops defaulted fields from ``required``; OpenAI strict mode
    wants every property required and nullability as ``anyOf:[T,null]``.
    """
    schema = model.model_json_schema()
    inlined = jsonref.replace_refs(schema, proxies=False, lazy_load=False)
    if not isinstance(inlined, dict):
        msg = f"expected dict from jsonref.replace_refs, got {type(inlined).__name__}"
        raise TypeError(msg)
    for k in ("$defs", "$schema", "$id"):
        inlined.pop(k, None)
    _force_required(inlined)
    return cast("dict[str, Any]", dict(inlined))  # pyright: ignore[reportExplicitAny]


def _build_bounded_tavily_pair(
    *,
    cap: int,
    label: str,
    search_description: str | None = None,
    extract_description: str | None = None,
) -> list[ToolSpec]:
    """Build the (search, extract) ToolSpec pair under one shared call quota.

    Both bounds share the same ``used``/``cap`` budget so a caller that hits
    the cap on either surface refuses on the other too. ``label`` rides in
    the refusal string so log/trace inspection tells synthesis vs recall
    apart. Descriptions fall back to the underlying tool's default — recall
    overrides them to nudge Opus on when to reach for each surface.
    """
    from slopmortem.corpus import _tools_impl  # noqa: PLC0415 - break import cycle
    from slopmortem.corpus._tools_impl import tavily_extract, tavily_search  # noqa: PLC0415

    used = 0
    refusal = f"tavily call budget exceeded ({cap} per {label}); refusing"

    async def _bounded_search(*, q: str, limit: int = 5) -> str:
        nonlocal used
        if used >= cap:
            return refusal
        used += 1
        # Attribute lookup at call time so tests can monkeypatch the impl.
        return await _tools_impl.tavily_search_async(q, limit)

    async def _bounded_extract(*, url: str) -> str:
        nonlocal used
        if used >= cap:
            return refusal
        used += 1
        return await _tools_impl.tavily_extract_async(url)

    return [
        ToolSpec(
            name=tavily_search.name,
            description=search_description or tavily_search.description,
            args_model=tavily_search.args_model,
            fn=_bounded_search,
        ),
        ToolSpec(
            name=tavily_extract.name,
            description=extract_description or tavily_extract.description,
            args_model=tavily_extract.args_model,
            fn=_bounded_extract,
        ),
    ]


def synthesis_tools(config: Config) -> list[ToolSpec]:
    """Build the synthesis tool list (Tavily inclusion is config-gated)."""
    from slopmortem.corpus._tools_impl import (  # noqa: PLC0415 - break import cycle
        get_post_mortem,
        search_corpus,
    )

    tools = [get_post_mortem, search_corpus]
    if config.enable_tavily_synthesis:
        # Per-synthesize() quota (default ≤2 calls), shared across both tools.
        tools.extend(
            _build_bounded_tavily_pair(
                cap=config.tavily_calls_per_synthesis,
                label="synthesis",
            )
        )
    return tools


def recall_tools(config: Config) -> list[ToolSpec]:
    """Tools available to the recall LLM (Opus) during candidate discovery.

    Both ``tavily_search`` and ``tavily_extract`` share a single
    ``config.recall_max_tavily_calls`` quota — Opus uses search to find
    candidate articles and extract to read the body of high-stakes picks
    before committing to a suggestion. Returns an empty list when the
    enable flag is off or the cap is 0 (recall stays training-data-only).
    """
    if not config.enable_tavily_recall_search:
        return []
    if config.recall_max_tavily_calls <= 0:
        return []
    return _build_bounded_tavily_pair(
        cap=config.recall_max_tavily_calls,
        label="recall",
        search_description=(
            "Search the live web for failed or struggling startups in this "
            "vertical. Returns title, url, snippet for the top matches. Use "
            "when your training memory is thin for the pitch's specific niche."
        ),
        extract_description=(
            "Fetch the readable body of an article URL found via tavily_search. "
            "Use sparingly — only when a search snippet alone leaves you "
            "uncertain whether the article actually describes failure/distress "
            "for the company you remember."
        ),
    )
