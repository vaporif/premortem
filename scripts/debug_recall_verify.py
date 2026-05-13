"""Targeted re-verify: run a saved suggestion list through the L0-L5 verifier.

Bypasses Opus to eliminate sampling variance. Reads suggestions from a JSON
file in the same wire shape ``llm_recall`` returns (a ``RecallSuggestionList``
JSON object: ``{"suggestions": [...]}``) and feeds each through
``_search_for_evidence`` (L0) + ``verify_suggestion`` (L1-L5). Per-suggestion
verdicts and the rejection log lines are printed under tagged section headers.

No persist — this is read-only diagnosis. Costs ~one Tavily search +
one HEAD/GET + (sometimes) one Tavily extract + one Haiku L5 call per
suggestion. Roughly $0.01 per suggestion.

Usage:
    uv run python scripts/debug_recall_verify.py /tmp/suggestions.json
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import override

from openai import AsyncOpenAI

from slopmortem.budget import Budget
from slopmortem.config import load_config
from slopmortem.corpus.sources import WaybackEnricher
from slopmortem.deps import build_tavily_recall_extract, build_tavily_recall_search
from slopmortem.llm import OpenRouterClient
from slopmortem.models import RecallSuggestionList
from slopmortem.recall import DeathnessConfig
from slopmortem.recall._verify import verify_all


class BufferHandler(logging.Handler):
    """Capture INFO records into an in-memory list for per-suggestion replay."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    @override
    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(f"[{record.name}] {record.getMessage()}")


def _install_log_capture() -> BufferHandler:
    buf = BufferHandler()
    buf.setLevel(logging.INFO)
    for name in (
        "slopmortem.recall._verify",
        "slopmortem.recall._brainstorm",
        "slopmortem.corpus.sources.wayback",
        "slopmortem.corpus.tavily",
    ):
        log = logging.getLogger(name)
        log.setLevel(logging.INFO)
        log.addHandler(buf)
    return buf


async def main(payload: str) -> None:
    wrapper = RecallSuggestionList.model_validate_json(payload)
    suggestions = wrapper.suggestions

    config = load_config()
    sdk = AsyncOpenAI(
        api_key=config.openrouter_api_key.get_secret_value(),
        base_url=config.openrouter_base_url,
    )
    budget = Budget(cap_usd=config.max_cost_usd_per_query)
    llm = OpenRouterClient(sdk=sdk, budget=budget, model=config.model_recall_deathness)
    tavily_search = build_tavily_recall_search(config)
    tavily_extract = build_tavily_recall_extract(config)
    if tavily_search is None or tavily_extract is None:
        msg = "Tavily is disabled; set enable_tavily_recall_search and TAVILY_API_KEY"
        raise SystemExit(msg)
    wayback = WaybackEnricher()
    deathness = DeathnessConfig(
        model=config.model_recall_deathness,
        max_tokens=config.max_tokens_recall_deathness,
        min_confidence=config.recall_deathness_min_confidence,
        struggling_min_confidence=config.recall_struggling_min_confidence,
    )
    buf = _install_log_capture()

    verdicts: list[tuple[str, str, str, str]] = []
    for s in suggestions:
        buf.records.clear()
        print()
        print(f"=== {s.name} ({s.status}, claimed failure_year={s.failure_year}) ===")
        print(f"    evidence_url provided: {s.evidence_url!r}")
        print(f"    homepage_url:          {s.homepage_url!r}")

        survivors = await verify_all(
            [s],
            wayback=wayback,
            llm=llm,
            tavily_search=tavily_search,
            extract=tavily_extract,
            tavily_recall_max_results=config.tavily_recall_max_results,
            deathness=deathness,
            concurrency=1,
        )
        _replay_logs(buf)
        if not survivors:
            print("  → REJECTED (see log lines above for the specific gate)")
            verdicts.append((s.name, s.status, "REJECTED", "see logs"))
        else:
            v = survivors[0]
            print(f"  → ADMITTED tier={v.tier} verdict={v.verdict}")
            verdicts.append((s.name, s.status, "ADMITTED", f"tier={v.tier} verdict={v.verdict}"))

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for name, status, gate, detail in verdicts:
        marker = "+" if gate == "ADMITTED" else "-"
        print(f"  {marker} {name:25s} {status:11s} -> {gate:9s} {detail}")
    admitted = sum(1 for _, _, gate, _ in verdicts if gate == "ADMITTED")
    print(f"\n  {admitted} of {len(verdicts)} admitted")
    print(f"  budget spent: ${budget.spent_usd:.4f}")


def _replay_logs(buf: BufferHandler) -> None:
    for line in buf.records:
        print(f"    log: {line}")
    buf.records.clear()


if __name__ == "__main__":
    _MIN_ARGS = 2
    if len(sys.argv) < _MIN_ARGS:
        msg = "Usage: debug_recall_verify.py <suggestions.json>"
        raise SystemExit(msg)
    asyncio.run(main(Path(sys.argv[1]).read_text()))
