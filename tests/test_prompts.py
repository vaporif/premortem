from __future__ import annotations

import json
from pathlib import Path

from slopmortem.llm import prompt_template_sha, render_blocks, render_prompt
from slopmortem.models import Facets, LlmRerankResult, LLMTopRisksConsolidation, Synthesis

FIXTURES = Path(__file__).parent / "fixtures" / "prompts"


def test_facet_extract_renders():
    out = render_prompt("facet_extract", description="we sell scrap metal")
    assert "scrap metal" in out
    assert "<untrusted_document" in out
    assert "fintech" in out


def test_facet_extract_paired_schema_loads():
    schema = Facets.model_json_schema()
    assert "sector" in schema["properties"]


def test_llm_rerank_renders_with_candidates():
    out = render_prompt(
        "llm_rerank",
        pitch="medical scribing for dermatologists",
        facets={"sector": "healthtech"},
        top_n=1,
        candidates=[
            {"candidate_id": "a.com", "summary": "ehr integration vendor", "name": "A"},
        ],
    )
    assert "a.com" in out
    schema = LlmRerankResult.model_json_schema()
    assert "ranked" in schema["properties"]


def test_synthesize_renders_inlined_body():
    out = render_prompt(
        "synthesize",
        pitch="x",
        candidate_id="a.com",
        candidate_name="A",
        candidate_body="<full markdown>",
        source="llm_recall",
        deathness_status="dead",
        founding_date=None,
        failure_date=None,
        sub_sector=None,
        customer_type="smb",
        geography="us",
        monetization="subscription_recurring",
        product_type=None,
        price_point=None,
    )
    assert "<untrusted_document" in out
    assert "</untrusted_document>" in out
    assert "<full markdown>" in out
    assert "Trusted facts" in out
    assert "customer_type: smb" in out
    assert "source: llm_recall" in out
    assert "deathness_status: dead" in out
    schema = Synthesis.model_json_schema()
    assert "where_diverged" in schema["properties"]


def test_consolidate_risks_renders():
    out = render_prompt(
        "consolidate_risks",
        pitch="MedScribe is an AI medical scribing app for US dermatology clinics.",
        lessons=[
            {
                "candidate_id": "scribetech.com",
                "candidate_name": "ScribeTech",
                "lesson": "Pick a specialty Epic and athena are slow to bundle into.",
            },
        ],
        candidate_ids=["scribetech.com", "ambient-ai.example"],
    )
    assert "MedScribe is an AI medical scribing" in out
    assert "candidate_id=scribetech.com" in out
    assert "Pick a specialty" in out
    assert "scribetech.com, ambient-ai.example" in out
    schema = LLMTopRisksConsolidation.model_json_schema()
    assert "top_risks" in schema["properties"]
    assert "injection_detected" in schema["properties"]


def test_consolidate_risks_fixture_matches_schema():
    fx = json.loads((FIXTURES / "consolidate_risks.json").read_text())
    LLMTopRisksConsolidation.model_validate(fx["expected_output"])
    out = render_prompt("consolidate_risks", **fx["vars"])
    assert fx["vars"]["pitch"] in out
    for item in fx["vars"]["lessons"]:
        assert item["lesson"] in out


def test_summarize_renders_with_source_id():
    out = render_prompt(
        "summarize",
        source_id="hn:42",
        body="acme failed because regulators",
    )
    assert "acme failed" in out
    assert 'source="hn:42"' in out


def test_render_blocks_splits_system_and_user():
    blocks = render_blocks("facet_extract", description="we sell scrap metal")
    assert set(blocks.keys()) == {"system", "user"}
    assert "fintech" in blocks["system"]
    assert "scrap metal" in blocks["user"]
    assert "fintech" not in blocks["user"]


def test_prompt_template_sha_is_deterministic():
    a = prompt_template_sha("facet_extract")
    b = prompt_template_sha("facet_extract")
    assert a == b
    assert len(a) == 16


def test_facet_extract_fixture_matches_schema():
    fx = json.loads((FIXTURES / "facet_extract.json").read_text())
    Facets.model_validate(fx["expected_output"])
    out = render_prompt("facet_extract", **fx["vars"])
    assert fx["vars"]["description"] in out


def test_llm_rerank_fixture_matches_schema():
    fx = json.loads((FIXTURES / "llm_rerank.json").read_text())
    LlmRerankResult.model_validate(fx["expected_output"])
    out = render_prompt("llm_rerank", **fx["vars"])
    for c in fx["vars"]["candidates"]:
        assert c["candidate_id"] in out


def test_synthesize_fixture_matches_schema():
    fx = json.loads((FIXTURES / "synthesize.json").read_text())
    Synthesis.model_validate(fx["expected_output"])
    out = render_prompt("synthesize", **fx["vars"])
    assert fx["vars"]["candidate_body"].splitlines()[0] in out


def test_summarize_fixture_renders():
    fx = json.loads((FIXTURES / "summarize.json").read_text())
    out = render_prompt("summarize", **fx["vars"])
    assert fx["vars"]["body"].split(".")[0] in out


def test_prompt_template_sha_is_cached():
    prompt_template_sha.cache_clear()
    _ = prompt_template_sha("facet_extract")
    _ = prompt_template_sha("facet_extract")
    info = prompt_template_sha.cache_info()
    assert info.hits == 1
    assert info.misses == 1
