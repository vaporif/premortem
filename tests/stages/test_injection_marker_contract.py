"""Pin the three independent copies of the injection-marker literal to the same string.

Drift between the prompt template, the synthesize-stage post-processor, and the
SpanEvent enum silently breaks injection detection — there is no other test
that fails when only one of the three is renamed.
"""

from __future__ import annotations

from slopmortem.llm.prompts import _PROMPT_DIR
from slopmortem.stages.synthesize import _INJECTION_MARKER
from slopmortem.tracing.events import SpanEvent

_PROMPT_PATH = _PROMPT_DIR / "synthesize.j2"


def test_injection_marker_matches_span_event() -> None:
    assert SpanEvent.PROMPT_INJECTION_ATTEMPTED.value == _INJECTION_MARKER


def test_injection_marker_present_in_prompt_template() -> None:
    prompt_text = _PROMPT_PATH.read_text(encoding="utf-8")
    assert _INJECTION_MARKER in prompt_text, (
        f"synthesize.j2 must instruct the LLM to emit the literal "
        f"{_INJECTION_MARKER!r} in where_diverged; not found in prompt body"
    )
