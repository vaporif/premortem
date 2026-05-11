"""Task 0 recorder: probe Tavily for 5 known-dead Web3 startups across 3 query variants.

Writes a single YAML cassette consumed by ``tests/stages/test_recall_search_preflight.py``.
Idempotent: refuses to overwrite an existing cassette (delete it to re-record).

Usage:
    TAVILY_API_KEY=... uv run python scripts/record_tavily_preflight.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, cast

import httpx
import yaml

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("record_tavily_preflight")

_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_MAX_RESULTS = 5
_INTER_CALL_SLEEP_S = 0.2
_HTTP_TIMEOUT_S = 30.0

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CASSETTE_PATH = (
    _REPO_ROOT / "tests" / "fixtures" / "cassettes" / "recall" / "tavily_search_preflight.yaml"
)

type Variant = Literal["pipe", "comma", "prose"]
_VARIANTS: tuple[Variant, ...] = ("pipe", "comma", "prose")


class Company(NamedTuple):
    name: str
    failure_year: int


_COMPANIES: tuple[Company, ...] = (
    Company("CipherTrace", 2024),
    Company("BlockFi", 2023),
    Company("Celsius Network", 2022),
    Company("FTX", 2022),
    Company("Voyager Digital", 2022),
)


def _build_query(variant: Variant, name: str, year: int) -> str:
    if variant == "pipe":
        return f'"{name}" shutdown|closed|bankrupt|"Chapter 11" {year}'
    if variant == "comma":
        return f'"{name}" shutdown, closed, bankrupt, Chapter 11 {year}'
    return f'"{name}" shutdown or closed or bankrupt or "Chapter 11" {year}'


def _tavily_api_key() -> str:
    key = os.environ.get("TAVILY_API_KEY", "")
    if not key:
        msg = "TAVILY_API_KEY not set in env"
        raise RuntimeError(msg)
    return key


async def _tavily_search(
    client: httpx.AsyncClient, api_key: str, query: str
) -> list[dict[str, str | None]]:
    resp = await client.post(
        _TAVILY_SEARCH_URL,
        json={
            "api_key": api_key,
            "query": query,
            "max_results": _MAX_RESULTS,
        },
    )
    resp.raise_for_status()
    payload_any: object = resp.json()  # pyright: ignore[reportAny]
    if not isinstance(payload_any, dict):
        msg = "unexpected Tavily payload shape (not an object)"
        raise TypeError(msg)
    payload = cast("Mapping[str, object]", payload_any)
    raw_results: object = payload.get("results")
    if raw_results is None:
        raw_results = []
    if not isinstance(raw_results, list):
        msg = "unexpected Tavily 'results' shape (not a list)"
        raise TypeError(msg)
    raw_list = cast("list[object]", raw_results)
    results: list[dict[str, str | None]] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        hit = cast("Mapping[str, object]", raw)
        results.append(
            {
                "title": _coerce_str(hit.get("title")),
                "url": _coerce_str(hit.get("url")),
                "content": _coerce_str(hit.get("content")),
                "published_date": _maybe_str(hit.get("published_date")),
            }
        )
    return results


def _coerce_str(v: object) -> str:
    return str(v) if v is not None else ""


def _maybe_str(v: object) -> str | None:
    if v is None:
        return None
    return str(v)


def _is_usable_hit(name: str, hit: Mapping[str, str | None]) -> bool:
    needle = name.casefold()
    title = (hit.get("title") or "").casefold()
    content = (hit.get("content") or "").casefold()
    return needle in title or needle in content


def _hit_rate_by_variant(queries: Sequence[Mapping[str, object]]) -> dict[str, str]:
    counts: dict[str, list[bool]] = {v: [] for v in _VARIANTS}
    for entry in queries:
        variant_obj = entry["variant"]
        name_obj = entry["company"]
        if not isinstance(variant_obj, str) or not isinstance(name_obj, str):
            continue
        results = entry["results"]
        if not isinstance(results, list):
            continue
        results_list = cast("list[object]", results)
        any_usable = any(
            _is_usable_hit(name_obj, cast("Mapping[str, str | None]", r))
            for r in results_list
            if isinstance(r, dict)
        )
        counts[variant_obj].append(any_usable)
    return {v: f"{sum(hits)}/{len(hits)}" for v, hits in counts.items()}


async def _record() -> None:
    if _CASSETTE_PATH.exists():
        logger.info(
            "cassette already exists at %s — refusing to overwrite (delete to re-record)",
            _CASSETTE_PATH,
        )
        return

    _CASSETTE_PATH.parent.mkdir(parents=True, exist_ok=True)

    api_key = _tavily_api_key()
    queries: list[dict[str, object]] = []
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
        for variant in _VARIANTS:
            for company in _COMPANIES:
                query = _build_query(variant, company.name, company.failure_year)
                logger.info("[%s] %s: %s", variant, company.name, query)
                results = await _tavily_search(client, api_key, query)
                queries.append(
                    {
                        "variant": variant,
                        "company": company.name,
                        "failure_year": company.failure_year,
                        "query": query,
                        "results": results,
                    }
                )
                await asyncio.sleep(_INTER_CALL_SLEEP_S)

    cassette: dict[str, object] = {
        "schema_version": "1.0",
        "recorded_at": dt.datetime.now(dt.UTC).isoformat(),
        "queries": queries,
    }

    tmp = _CASSETTE_PATH.with_suffix(_CASSETTE_PATH.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        yaml.safe_dump(cassette, fp, sort_keys=False, allow_unicode=True, width=120)
    tmp.replace(_CASSETTE_PATH)
    logger.info("wrote %s", _CASSETTE_PATH)

    print("\n=== Per-variant hit rates (>=4/5 needed to pass the gate) ===")
    for variant, rate in _hit_rate_by_variant(queries).items():
        print(f"  {variant:6s} {rate}")


def main() -> int:
    try:
        asyncio.run(_record())
    except (RuntimeError, httpx.HTTPError):
        logger.exception("recorder failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
