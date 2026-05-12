"""Coverage check: every seed-dataset sector must have at least one matching corpus entry.

Runs in milliseconds, no Qdrant, no LLM. Catches the case where someone
bumps the corpus fixture without noticing the seed dataset still expects
matching sectors. If this test fails, either expand the inputs YAML and
re-record, or update the seed.
"""

from __future__ import annotations

import json
from pathlib import Path

# Best-effort sector inference from the seed pitch description. Keep the
# mapping tight — the goal is "is there any sector overlap at all", not a
# full taxonomy classifier. If a description is too generic to infer, leave
# it out of the assertion set.
_SEED_SECTORS: dict[str, str] = {
    "ledgermint": "fintech",
    "gridspring": "climate_energy",
    "kappa-cli": "devtools",
    "yume-tutor": "edtech",
    "smolpark": "social_communication",
    "shardbright": "gaming",
    # kakikaki = b2c marketplace; could plausibly map to media_content,
    # social_communication, or retail_ecommerce — too ambiguous to assert on.
    # lastmile-iq = B2B fleet-dispatch SaaS. The closest fixture entries
    # (webvan, kozmo-com, boo-com) all classified as retail_ecommerce because
    # they were 90s/00s e-commerce delivery, not fleet-routing software. No
    # clean logistics_supply_chain match exists on Wikipedia — too ambiguous
    # to assert on.
}


def _seed_names() -> set[str]:
    out: set[str] = set()
    with Path("tests/evals/datasets/seed.jsonl").open() as f:
        for line in f:
            row = json.loads(line)
            out.add(row["name"])
    return out


def _fixture_sectors() -> set[str]:
    out: set[str] = set()
    with Path("tests/fixtures/corpus_fixture.jsonl").open() as f:
        for line in f:
            row = json.loads(line)
            facets = row.get("payload", {}).get("facets") or {}
            sector = facets.get("sector")
            if isinstance(sector, str):
                out.add(sector)
    return out


def test_seed_dataset_unchanged() -> None:
    """Guard the _SEED_SECTORS map against silent seed-dataset edits."""
    expected = set(_SEED_SECTORS) | {"kakikaki", "lastmile-iq"}
    assert _seed_names() == expected, "seed.jsonl drifted; update _SEED_SECTORS in this test"


def test_every_inferred_sector_has_a_corpus_entry() -> None:
    """Each sector represented by the seed has at least one fixture entry in that sector."""
    fixture_sectors = _fixture_sectors()
    missing = {seed for seed, sector in _SEED_SECTORS.items() if sector not in fixture_sectors}
    assert not missing, (
        "corpus fixture has no entries for sectors needed by these seed pitches: "
        f"{sorted(missing)}.\n"
        f"Fixture sectors present: {sorted(fixture_sectors)}.\n"
        f"Re-run Task 2 of docs/plans/2026-05-02-corpus-fixture-rebuild.md to add coverage."
    )
