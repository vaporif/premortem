"""One-shot: extract facets + call llm_recall + print Opus's raw suggestions.

Bypasses the verifier and synthesizer so we see only what Opus claims to
recall for a pitch. Costs ~one facet call (Haiku) + one recall call (Opus).
"""

from __future__ import annotations

import asyncio
import logging
import sys

from slopmortem.config import load_config
from slopmortem.deps import build_deps
from slopmortem.recall._brainstorm import llm_recall
from slopmortem.stages.facet_extract import extract_facets

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "httpcore", "lmnr", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


async def main(pitch: str) -> None:
    config = load_config()
    llm, _embedder, _corpus, _budget, _sparse = build_deps(config)
    facets = await extract_facets(
        pitch,
        llm,
        model=config.model_facet,
        max_tokens=config.max_tokens_facet,
    )
    print(f"\nFacets: sector={facets.sector!r} sub_sector={facets.sub_sector!r}")
    print(f"        product_type={facets.product_type!r}\n")
    suggestions = await llm_recall(
        pitch=pitch,
        facets=facets,
        current_top_n=[],
        llm=llm,
        model=config.model_recall,
        max_tokens=config.max_tokens_recall,
        cap=config.recall_max_suggestions_per_pitch,
        tools=[],
    )
    print(f"\n=== Opus returned {len(suggestions)} suggestion(s) ===\n")
    for i, s in enumerate(suggestions, 1):
        print(f"[{i}] {s.name} ({s.status}, {s.failure_year})")
        print(f"    category: {s.category}")
        print(f"    one_liner: {s.one_liner}")
        print(f"    homepage:  {s.homepage_url}")
        print(f"    evidence:  {s.evidence_url}\n")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
