# Title Pre-Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or `/team-feature` to implement this plan task-by-task, per the Execution Strategy below. Steps use checkbox (`- [ ]`) syntax — these are **persistent durable state**, not visual decoration. The executor edits the plan file in place: `- [ ]` → `- [x]` the instant a step verifies, before moving on. On resume (new session, crash, takeover), the executor scans existing `- [x]` marks and skips them — these steps are NOT redone. TodoWrite mirrors this state in-session; the plan file is the source of truth across sessions.

**Goal:** Cut Tavily credit burn on `hn_algolia` ingest by rejecting obvious non-death-narrative titles with a cheap Haiku-on-title call before the expensive pitch filler runs.

**Architecture:** New `HaikuTitlePreFilter` enricher that runs first in the enricher chain. It asks Haiku 4.5 a yes/no question about the HN title alone (no Tavily, no body), and on "no" sets a new `RawEntry.title_pre_filter_rejected: bool` flag. Both the pitch filler and the ingest classify loop honor that flag and short-circuit, so rejected entries never reach Tavily and never reach the slop classifier.

**Tech Stack:** Python 3.13, anyio, pydantic v2, OpenRouterClient with strict-JSON `response_format`, basedpyright strict, pytest with a stub LLM mirroring the pitch-filler test pattern.

## Execution Strategy

**Subagents** — default; no spec override. Tasks 1–6 must run sequentially because each task's tests assume prior task changes are present (Task 2 depends on Task 1's new `RawEntry` field; Task 4 depends on Task 3's prompt; Task 5 reuses Task 2's enricher class; Task 6 wires everything together).

## Task Dependency Graph

- Task 1 [AFK]: `RawEntry.title_pre_filter_rejected` field → depends on `none` → batch 1
- Task 2 [AFK]: prompt template `title_pre_filter.j2` → depends on `none` → batch 1 (parallel with Task 1)
- Task 3 [AFK]: config keys → depends on `none` → batch 1 (parallel with Task 1)
- Task 4 [AFK]: `HaikuTitlePreFilter` enricher module → depends on `Tasks 1, 2, 3` → batch 2
- Task 5 [AFK]: pitch filler + ingest skip-guards honor the flag → depends on `Task 1` → batch 2 (parallel with Task 4)
- Task 6 [AFK]: CLI wiring + auto-enable for `hn_algolia` → depends on `Tasks 3, 4, 5` → batch 3
- Polish: post-implementation-polish → depends on `Task 6` → batch 4

## Agent Assignments

- Task 1: `RawEntry` field → python-development:python-pro
- Task 2: prompt template → python-development:python-pro
- Task 3: config keys → python-development:python-pro
- Task 4: enricher module → python-development:python-pro
- Task 5: skip-guard wiring → python-development:python-pro
- Task 6: CLI wiring → python-development:python-pro
- Polish: post-implementation-polish → general-purpose

---

## File Structure

**New:**
- `slopmortem/ingest/_title_pre_filter.py` — `HaikuTitlePreFilter` class implementing the `Enricher` Protocol. Reads `entry.title`, calls `llm.complete` with strict-JSON `{"decision": "yes"|"no"}` response shape, sets `entry.title_pre_filter_rejected=True` on "no". Skip-guards on missing title, pre-filled body, and exhausted budget. Returns the entry unchanged on any LLM/parse failure (per-entry isolation).
- `slopmortem/llm/prompts/title_pre_filter.j2` — single-block prompt with system + user blocks. Asks: given an HN story title, is this likely a post about a startup that has shut down or is shutting down? Reply yes or no.
- `tests/ingest/test_title_pre_filter.py` — unit tests against a stub LLM (mirrors `tests/ingest/test_pitch_filler.py` pattern).

**Modified:**
- `slopmortem/models.py:327-340` — add `title_pre_filter_rejected: bool = False` to `RawEntry`. Default keeps existing sources working unchanged.
- `slopmortem/ingest/_pitch_filler.py:66-90` — extend `_should_skip` to return `True` when `entry.title_pre_filter_rejected` is set.
- `slopmortem/ingest/_ingest.py:139-167` — when `enriched.title_pre_filter_rejected` is set, count as a skip with reason `title_pre_filter_rejected` instead of falling through to the "empty body" skip. Surfaces the rejection in run stats.
- `slopmortem/config.py:51,64,75-77,127-132` — add `enable_title_pre_filter: bool = False`, `model_title_pre_filter: str = "anthropic/claude-haiku-4.5"`, `max_tokens_title_pre_filter: int = 16`. Extend the existing required-keys validator to require `OPENROUTER_API_KEY` (already required for any LLM stage, so this is implicit; no new validator needed).
- `slopmortem/cli/_ingest_cmd.py:96-106,433-443,445-464` — add `--enable-title-pre-filter / --no-title-pre-filter` Typer option (mirrors the `enable_pitch_filler` option at 96-106), auto-enable for `hn_algolia` inside the existing `if any(isinstance(s, HNAlgoliaSource) ...)` block at 433-443, prepend `HaikuTitlePreFilter(...)` to the enricher list at 445-464 **before** the pitch filler so rejected entries skip the pitch filler's Haiku call and its `tavily_search` tool invocation.
- `slopmortem/ingest/__init__.py` — re-export `HaikuTitlePreFilter`.

**Decisions:**
- **New `RawEntry` field vs sentinel value.** Auto-selected — no downsides compared to alternatives. A bool field reads clearly in tests and doesn't conflict with the existing `synthesized` flag. Sentinels in `markdown_text` or `title` are hackier and would defeat the existing pitch-filler skip-guards in subtle ways.
- **Strict JSON schema with enum on `decision`.** Pros: rejects malformed model output deterministically; same pattern as the slop classifier. Cons: tiny overhead vs free-form yes/no text. Picked because the project's other stages all use strict JSON.
- **`max_tokens=16`.** The model returns one token plus JSON brackets. 16 is small enough to fail fast on runaway output without ever clipping a valid response.
- **Auto-enable for `hn_algolia`.** Pros: matches the existing `enable_pitch_filler` auto-enable pattern, so users get protection by default. Cons: adds a Haiku call per HN entry. At Haiku 4.5 pricing ($1/MTok in, $5/MTok out — see `slopmortem/llm/prices.yml`) and ~150 input + ~5 output tokens per title, this is roughly $0.0002/entry, ~$0.40 for the full 2192-entry HN backlog — negligible against the Tavily credits saved. Net: enable.
- **Tie-breaking direction in the prompt.** Pick `"yes"` when uncertain. A `"no"` here drops the entry from the corpus entirely — it's never classified, never journaled, never written. A `"yes"` lets the entry continue; if it isn't actually a death narrative, the pitch filler's `confidence != "high"` gate (`_pitch_filler.py:151`) catches it at a cost of one Haiku turn + one `tavily_search` call (~2 credits). Asymmetric: false negatives shrink the corpus permanently for this run; false positives waste a small fixed cost downstream. Recall > precision at this stage.

---

### Task 1: Add `RawEntry.title_pre_filter_rejected` field

**Files:**
- Modify: `slopmortem/models.py:327-340`
- Test: `tests/test_models.py` (existing file — append a test)

- [x] **Step 1: Write the failing test**

Append to `tests/test_models.py`:

```python
def test_raw_entry_title_pre_filter_rejected_defaults_false() -> None:
    from datetime import UTC, datetime
    from slopmortem.models import RawEntry
    e = RawEntry(
        source="hn_algolia",
        source_id="x",
        url="https://example.com",
        fetched_at=datetime.now(UTC),
    )
    assert e.title_pre_filter_rejected is False


def test_raw_entry_title_pre_filter_rejected_can_be_set() -> None:
    from datetime import UTC, datetime
    from slopmortem.models import RawEntry
    e = RawEntry(
        source="hn_algolia",
        source_id="x",
        url="https://example.com",
        fetched_at=datetime.now(UTC),
        title_pre_filter_rejected=True,
    )
    assert e.title_pre_filter_rejected is True
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_models.py::test_raw_entry_title_pre_filter_rejected_defaults_false tests/test_models.py::test_raw_entry_title_pre_filter_rejected_can_be_set -v`
Expected: FAIL — `unexpected keyword argument 'title_pre_filter_rejected'`.

- [x] **Step 3: Add the field**

Edit `slopmortem/models.py` at the `RawEntry` class (around line 327-341). Add after `synthesized: bool = False`:

```python
    # Set by HaikuTitlePreFilter when the HN title fails the cheap "is this a
    # death narrative?" gate. Downstream enrichers (pitch filler) and the
    # ingest classify loop short-circuit on this flag so rejected entries
    # never trigger a Tavily call or a slop classifier call.
    title_pre_filter_rejected: bool = False
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_models.py -v`
Expected: PASS, including both new tests and all existing `RawEntry` tests.

- [x] **Step 5: Run lint and typecheck**

Run: `just lint && just typecheck`
Expected: clean.

- [x] **Step 6: Commit**

`git add slopmortem/models.py tests/test_models.py && git commit -m "models: RawEntry.title_pre_filter_rejected"`

---

### Task 2: Create the `title_pre_filter.j2` prompt

**Files:**
- Create: `slopmortem/llm/prompts/title_pre_filter.j2`

- [x] **Step 1: Write the prompt file**

Create `slopmortem/llm/prompts/title_pre_filter.j2`:

```jinja2
{# template_sha is computed at runtime via prompt_template_sha("title_pre_filter") #}
{% block system -%}
You filter Hacker News story titles for a startup-death corpus.

Decide whether the title likely refers to a SPECIFIC company, startup, or business venture that has DIED or is DYING — meaning it shut down, went bankrupt, was dissolved, or was acquired and absorbed/extinguished.

Answer `decision = "yes"` for titles like:
- "Lytro is shutting down"
- "Farewell from Mattermark"
- "Why Rdio failed"
- "Quibi shuts down after six months"
- "Vine is dead"

Answer `decision = "no"` for titles like:
- "Show HN: my new startup"
- "Ask HN: how do you handle X?"
- "10 reasons startups fail" (generic listicle)
- "Big Co lays off 10% of workforce" (still operating)
- "Review: the new MacBook"
- "Rumors that X is shutting down" (speculation, not confirmed)
- "X is not shutting down" (denial)
- "How to avoid the mistakes that killed X" (lessons piece, not obituary)

When uncertain, prefer `"yes"`. A `"no"` drops the entry permanently for this run (no classify, no journal, no write); a `"yes"` lets the pitch filler's confidence gate catch a non-death title at the cost of one Haiku turn + one `tavily_search` (~2 Tavily credits). Recall matters more than precision at this stage — the death-narrative corpus is the bottleneck, not Tavily credits.

Output schema (return a single JSON object, no prose outside it):
{
  "decision": "yes" | "no"
}
{%- endblock %}
{% block user -%}
HN title: {{ title }}

Is this likely a post about a specific startup that has shut down or is shutting down?
{%- endblock %}
```

- [x] **Step 2: Verify the template parses**

Run: `uv run python -c "from slopmortem.llm import render_blocks, prompt_template_sha; print(prompt_template_sha('title_pre_filter')); print(render_blocks('title_pre_filter', title='Lytro is shutting down'))"`
Expected: 16-char SHA prints, then `{'system': '...', 'user': 'HN title: Lytro is shutting down\n\nIs this likely...'}`.

- [x] **Step 3: Commit**

`git add slopmortem/llm/prompts/title_pre_filter.j2 && git commit -m "prompt: title_pre_filter"`

---

### Task 3: Add config keys

**Files:**
- Modify: `slopmortem/config.py:46-77`
- Test: `tests/test_config.py` (append)

- [x] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_title_pre_filter_config_defaults() -> None:
    from slopmortem.config import Config
    c = Config()
    assert c.enable_title_pre_filter is False
    assert c.model_title_pre_filter == "anthropic/claude-haiku-4.5"
    assert c.max_tokens_title_pre_filter == 16
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_title_pre_filter_config_defaults -v`
Expected: FAIL — `'Config' object has no attribute 'enable_title_pre_filter'` (or pydantic validation error).

- [x] **Step 3: Add the config keys**

In `slopmortem/config.py`, add these lines near the existing `model_pitch_filler` (~line 51), `max_tokens_pitch_filler` (~line 64), and `enable_pitch_filler` (~line 76):

```python
    model_title_pre_filter: str = "anthropic/claude-haiku-4.5"
    max_tokens_title_pre_filter: int = Field(default=16, ge=1)
    enable_title_pre_filter: bool = False
```

- [x] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py::test_title_pre_filter_config_defaults -v`
Expected: PASS.

- [x] **Step 5: Lint + typecheck**

Run: `just lint && just typecheck`
Expected: clean.

- [x] **Step 6: Commit**

`git add slopmortem/config.py tests/test_config.py && git commit -m "config: title pre-filter keys"`

---

### Task 4: Implement `HaikuTitlePreFilter`

**Files:**
- Create: `slopmortem/ingest/_title_pre_filter.py`
- Modify: `slopmortem/ingest/__init__.py`
- Test: `tests/ingest/test_title_pre_filter.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/ingest/test_title_pre_filter.py`:

```python
"""Unit tests for ``HaikuTitlePreFilter`` — cheap title-only Haiku gate."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from slopmortem.budget import Budget
from slopmortem.ingest import HaikuTitlePreFilter
from slopmortem.llm.client import CompletionResult
from slopmortem.models import RawEntry

_TEST_MODEL = "anthropic/claude-haiku-4.5"


@dataclass
class _StubLLM:
    text: str = ""
    raise_exc: BaseException | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    async def complete(  # noqa: PLR0913
        self,
        prompt: str,
        *,
        system: str | None = None,
        tools: list[Any] | None = None,
        model: str | None = None,
        cache: bool = False,
        response_format: dict[str, Any] | None = None,
        extra_body: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        single_tool_call: bool = False,
    ) -> CompletionResult:
        self.calls.append(
            {
                "prompt": prompt,
                "system": system,
                "model": model,
                "response_format": response_format,
                "extra_body": extra_body,
                "max_tokens": max_tokens,
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return CompletionResult(text=self.text, stop_reason="stop")


def _make_entry(
    *,
    title: str | None = "Lytro is shutting down",
    url: str | None = "https://blog.lytro.com/farewell",
    markdown_text: str | None = None,
) -> RawEntry:
    return RawEntry(
        source="hn_algolia",
        source_id="42",
        url=url,
        title=title,
        markdown_text=markdown_text,
        fetched_at=datetime.now(UTC),
    )


def _filter(*, llm: _StubLLM) -> HaikuTitlePreFilter:
    return HaikuTitlePreFilter(
        llm=llm,
        model=_TEST_MODEL,
        budget=Budget(cap_usd=1.0),
        max_tokens=16,
    )


@pytest.mark.anyio
async def test_yes_passes_entry_through_unchanged() -> None:
    llm = _StubLLM(text=json.dumps({"decision": "yes"}))
    out = await _filter(llm=llm).enrich(_make_entry())
    assert out.title_pre_filter_rejected is False
    assert len(llm.calls) == 1


@pytest.mark.anyio
async def test_no_sets_rejected_flag() -> None:
    llm = _StubLLM(text=json.dumps({"decision": "no"}))
    out = await _filter(llm=llm).enrich(_make_entry(title="Show HN: my new startup"))
    assert out.title_pre_filter_rejected is True
    assert len(llm.calls) == 1


@pytest.mark.anyio
async def test_skips_when_body_pre_filled() -> None:
    llm = _StubLLM(text=json.dumps({"decision": "no"}))
    out = await _filter(llm=llm).enrich(_make_entry(markdown_text="already enriched"))
    assert out.title_pre_filter_rejected is False
    assert len(llm.calls) == 0


@pytest.mark.anyio
async def test_skips_when_title_missing() -> None:
    llm = _StubLLM(text=json.dumps({"decision": "no"}))
    out = await _filter(llm=llm).enrich(_make_entry(title=None))
    assert out.title_pre_filter_rejected is False
    assert len(llm.calls) == 0


@pytest.mark.anyio
async def test_malformed_json_returns_entry_unchanged() -> None:
    llm = _StubLLM(text="not json at all")
    out = await _filter(llm=llm).enrich(_make_entry())
    assert out.title_pre_filter_rejected is False
    assert len(llm.calls) == 1


@pytest.mark.anyio
async def test_invalid_decision_value_returns_entry_unchanged() -> None:
    llm = _StubLLM(text=json.dumps({"decision": "maybe"}))
    out = await _filter(llm=llm).enrich(_make_entry())
    assert out.title_pre_filter_rejected is False
    assert len(llm.calls) == 1


@pytest.mark.anyio
async def test_budget_exhausted_skips() -> None:
    llm = _StubLLM(text=json.dumps({"decision": "no"}))
    budget = Budget(cap_usd=0.0)  # nothing remaining
    f = HaikuTitlePreFilter(llm=llm, model=_TEST_MODEL, budget=budget, max_tokens=16)
    out = await f.enrich(_make_entry())
    assert out.title_pre_filter_rejected is False
    assert len(llm.calls) == 0


@pytest.mark.anyio
async def test_llm_runtime_error_returns_entry_unchanged() -> None:
    llm = _StubLLM(raise_exc=RuntimeError("boom"))
    out = await _filter(llm=llm).enrich(_make_entry())
    assert out.title_pre_filter_rejected is False


@pytest.mark.anyio
async def test_budget_exceeded_propagates() -> None:
    from slopmortem.budget import BudgetExceededError
    llm = _StubLLM(raise_exc=BudgetExceededError("over"))
    with pytest.raises(BudgetExceededError):
        await _filter(llm=llm).enrich(_make_entry())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ingest/test_title_pre_filter.py -v`
Expected: FAIL — `cannot import name 'HaikuTitlePreFilter'`.

- [ ] **Step 3: Implement the enricher**

Create `slopmortem/ingest/_title_pre_filter.py`:

```python
"""Cheap title-only Haiku gate that runs before the pitch filler.

Most HN-Algolia hits the phrase search returns are not actually startup
death narratives — denials, listicles, generic essays, Show HN posts.
Asking Haiku a yes/no question on the title alone (no body, no Tavily)
cuts pitch-filler invocations and Tavily credit burn substantially.
Rejected entries get ``title_pre_filter_rejected=True``; downstream
enrichers and the ingest classify loop short-circuit on the flag.

Per-entry isolation contract: log and return the entry unchanged on every
recoverable failure (HTTP, JSON parse, schema mismatch).
``BudgetExceededError`` is the one fatal class — it propagates so the
orchestrator can short-circuit the run.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

import httpx
from pydantic import BaseModel, ValidationError

from slopmortem.budget import BudgetExceededError
from slopmortem.llm import (
    prompt_template_sha,
    render_blocks,
    to_strict_response_schema,
)

if TYPE_CHECKING:
    from slopmortem.budget import Budget
    from slopmortem.llm import LLMClient
    from slopmortem.models import RawEntry

__all__ = ["HaikuTitlePreFilter"]

logger = logging.getLogger(__name__)


class _TitlePreFilterOutput(BaseModel):
    decision: Literal["yes", "no"]


@dataclass
class HaikuTitlePreFilter:
    """[Enricher] Title-only Haiku gate that flips a skip flag for non-death titles."""

    llm: LLMClient
    model: str
    budget: Budget
    max_tokens: int = 16

    def _should_skip(self, entry: RawEntry) -> bool:
        if entry.markdown_text is not None and entry.markdown_text.strip():
            return True
        if entry.raw_html is not None and entry.raw_html.strip():
            return True
        if not entry.title:
            return True
        if self.budget.remaining <= 0.0:
            logger.info(
                "title pre-filter: skipped %s:%s (budget exhausted)",
                entry.source,
                entry.source_id,
            )
            return True
        return False

    async def enrich(self, entry: RawEntry) -> RawEntry:
        if self._should_skip(entry):
            return entry

        blocks = render_blocks("title_pre_filter", title=entry.title)
        system_block = blocks.get("system", "")
        user_block = blocks.get("user", "")

        try:
            result = await self.llm.complete(
                user_block,
                system=system_block,
                model=self.model,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "TitlePreFilterOutput",
                        "schema": to_strict_response_schema(_TitlePreFilterOutput),
                        "strict": True,
                    },
                },
                extra_body={"prompt_template_sha": prompt_template_sha("title_pre_filter")},
                max_tokens=self.max_tokens,
            )
        except BudgetExceededError:
            raise
        except (httpx.HTTPError, RuntimeError) as exc:
            logger.warning(
                "title pre-filter: LLM call failed for %s:%s: %r",
                entry.source,
                entry.source_id,
                exc,
            )
            return entry

        try:
            parsed_obj = cast("object", json.loads(result.text))
            output = _TitlePreFilterOutput.model_validate(parsed_obj)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning(
                "title pre-filter: malformed output for %s:%s: %r",
                entry.source,
                entry.source_id,
                exc,
            )
            return entry

        if output.decision == "no":
            logger.info(
                "title pre-filter: rejected %s:%s title=%r",
                entry.source,
                entry.source_id,
                entry.title,
            )
            return entry.model_copy(update={"title_pre_filter_rejected": True})
        return entry
```

- [ ] **Step 4: Re-export from `slopmortem.ingest`**

In `slopmortem/ingest/__init__.py`, add the import directly after the existing `HaikuPitchFiller` re-export:

```python
from slopmortem.ingest._title_pre_filter import HaikuTitlePreFilter as HaikuTitlePreFilter
```

…and insert `"HaikuTitlePreFilter"` into `__all__` in alphabetical order (between `HaikuSlopClassifier` and `InMemoryCorpus` — note the existing list keeps the underscore-prefixed `_Point` last, so do not append at the end).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/ingest/test_title_pre_filter.py -v`
Expected: all PASS.

- [ ] **Step 6: Lint + typecheck**

Run: `just lint && just typecheck`
Expected: clean.

- [ ] **Step 7: Commit**

`git add slopmortem/ingest/_title_pre_filter.py slopmortem/ingest/__init__.py tests/ingest/test_title_pre_filter.py && git commit -m "title pre-filter: HaikuTitlePreFilter enricher"`

---

### Task 5: Pitch filler + ingest honor the rejected flag

**Files:**
- Modify: `slopmortem/ingest/_pitch_filler.py:66-90`
- Modify: `slopmortem/ingest/_ingest.py:139-167`
- Test: `tests/ingest/test_pitch_filler.py`, `tests/ingest/test_orchestration.py`

- [ ] **Step 1: Write the failing test for pitch filler skip**

Append to `tests/ingest/test_pitch_filler.py`:

```python
@pytest.mark.anyio
async def test_skips_when_title_pre_filter_rejected() -> None:
    llm = _StubLLM(text=_high_response())
    entry = _make_entry()
    entry = entry.model_copy(update={"title_pre_filter_rejected": True})
    out = await _filler(llm=llm).enrich(entry)
    assert out.markdown_text is None
    assert out.synthesized is False
    assert len(llm.calls) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ingest/test_pitch_filler.py::test_skips_when_title_pre_filter_rejected -v`
Expected: FAIL — pitch filler still calls the LLM and populates `markdown_text`.

- [ ] **Step 3: Add the skip-guard**

In `slopmortem/ingest/_pitch_filler.py`, at the top of `_should_skip` (line 66, before the existing checks):

```python
        if entry.title_pre_filter_rejected:
            return True
```

- [ ] **Step 4: Run pitch-filler tests**

Run: `uv run pytest tests/ingest/test_pitch_filler.py -v`
Expected: all PASS.

- [ ] **Step 5: Make ingest classify loop count rejected entries**

Read `slopmortem/ingest/_ingest.py` around line 156–166 (the empty-body skip) to see the existing skip-counting pattern. Insert a check **before** the empty-body skip:

```python
            if enriched.title_pre_filter_rejected:
                logger.info(
                    "ingest: title pre-filter rejected %s:%s title=%r",
                    entry.source,
                    entry.source_id,
                    entry.title,
                )
                result.skipped += 1
                progress.advance_phase(IngestPhase.CLASSIFY)
                return None
```

- [ ] **Step 6: Add an orchestration test**

Append to `tests/ingest/test_orchestration.py` a test that runs the ingest end-to-end with a `HaikuTitlePreFilter` that rejects every entry, asserts `result.skipped == N`, and asserts the slop classifier was never called. (Mirror the existing fake-classifier patterns used in that file.)

- [ ] **Step 7: Run the full ingest test suite**

Run: `uv run pytest tests/ingest/ -v`
Expected: all PASS.

- [ ] **Step 8: Lint + typecheck**

Run: `just lint && just typecheck`
Expected: clean.

- [ ] **Step 9: Commit**

`git add slopmortem/ingest/_pitch_filler.py slopmortem/ingest/_ingest.py tests/ingest/ && git commit -m "ingest: honor title_pre_filter_rejected in skip-guards"`

---

### Task 6: CLI wiring

**Files:**
- Modify: `slopmortem/cli/_ingest_cmd.py:115-125,477,479-498`
- Test: `tests/test_cli.py` (or `tests/cli/test_ingest_cmd.py` — match existing pattern)

- [ ] **Step 1: Add the Typer flag**

In `slopmortem/cli/_ingest_cmd.py` directly after the existing `enable_pitch_filler` Typer option (currently `:96-106`), add:

```python
    enable_title_pre_filter: Annotated[
        bool,
        typer.Option(
            "--enable-title-pre-filter/--no-title-pre-filter",
            help=(
                "Enable the cheap title-only Haiku gate that rejects HN entries "
                "whose titles aren't startup-death narratives. Saves Tavily credits "
                "by short-circuiting before the pitch filler. Auto-enabled when "
                "hn_algolia is in the source list."
            ),
        ),
    ] = False,
```

Thread it through `_run_ingest`'s parameter list (the kwargs block currently around `:309-332`) and through the `functools.partial(_run_ingest, ...)` call site (currently `:240-263`).

- [ ] **Step 2: Auto-enable for `hn_algolia`**

In the existing `if any(isinstance(s, HNAlgoliaSource) for s in sources):` block (currently `_ingest_cmd.py:433-443`, ending with `enable_pitch_filler = True`), add `enable_title_pre_filter = True` next to the `enable_pitch_filler = True` assignment.

- [ ] **Step 3: Prepend the enricher**

In the `enrichers: list[Enricher] = []` block (currently `_ingest_cmd.py:445-464`), insert the title pre-filter **before** the existing `if enable_pitch_filler:` block. The current code shape is:

```python
    enrichers: list[Enricher] = []
    # Cheap fetch-chain enrichers temporarily disabled (WaybackEnricher,
    # TavilyEnricher /extract). Re-enable by reinstating the imports + flags.
    if enable_pitch_filler:
        from slopmortem.ingest import HaikuPitchFiller  # noqa: PLC0415
        enrichers.append(HaikuPitchFiller(...))
```

Insert the new gate above it:

```python
    enrichers: list[Enricher] = []
    if enable_title_pre_filter:
        from slopmortem.ingest import HaikuTitlePreFilter  # noqa: PLC0415

        enrichers.append(
            HaikuTitlePreFilter(
                llm=llm,
                model=config.model_title_pre_filter,
                budget=budget,
                max_tokens=config.max_tokens_title_pre_filter,
            )
        )
    # Cheap fetch-chain enrichers temporarily disabled (WaybackEnricher,
    # TavilyEnricher /extract). Re-enable by reinstating the imports + flags.
    if enable_pitch_filler:
        from slopmortem.ingest import HaikuPitchFiller  # noqa: PLC0415
        enrichers.append(HaikuPitchFiller(...))
```

Ordering is load-bearing: the title pre-filter must run before the pitch filler, because the pitch filler is the stage that issues the `tavily_search` tool call (~2 Tavily credits/entry). `TavilyEnricher` (the `/extract`-based body fetcher) is currently disabled, but Tavily search is still reachable via the pitch filler tool, so the gate has real cost-saving value.

- [ ] **Step 4: Update help text on `--enable-pitch-filler`**

The existing help (currently `_ingest_cmd.py:101-104`) says "Auto-enabled when hn_algolia is in the source list." Append: "Title pre-filter (auto-enabled too) drops most non-death titles before this stage runs, so Tavily credit burn is roughly proportional to the title-pass rate."

- [ ] **Step 5: Add a CLI auto-enable test**

`tests/test_cli_ingest.py` does not currently assert on the `enable_pitch_filler` auto-enable wiring, so there is no precedent to copy — pick the lightest path that proves the auto-enable fired.

Lightest path: extract the auto-enable + enricher-construction block out of `_run_ingest` into a small pure helper (e.g. `_build_enrichers(sources, *, enable_pitch_filler, enable_title_pre_filter, llm, config, budget) -> tuple[list[Enricher], bool, bool]` returning the enricher list plus the post-auto-enable flag values), then unit-test that helper directly:

```python
def test_hn_algolia_auto_enables_title_pre_filter() -> None:
    enrichers, pf, tpf = _build_enrichers(
        sources=[HNAlgoliaSource(...)],
        enable_pitch_filler=False,
        enable_title_pre_filter=False,
        llm=_stub_llm(), config=load_config(), budget=Budget(cap_usd=1.0),
    )
    assert tpf is True
    assert pf is True
    assert isinstance(enrichers[0], HaikuTitlePreFilter)  # ordering: pre-filter first
    assert any(isinstance(e, HaikuPitchFiller) for e in enrichers[1:])
```

If the worker decides the extraction is too invasive, fall back to a smoke test that calls the Typer command via `CliRunner` with `--dry-run --only-source hn_algolia`, `TAVILY_API_KEY=stub` set, and asserts the run exits 0 — this only proves nothing crashes, not that the flag flipped, but it's better than no test.

- [ ] **Step 6: Run the full test suite**

Run: `just test`
Expected: all PASS.

- [ ] **Step 7: Lint + typecheck**

Run: `just lint && just typecheck`
Expected: clean.

- [ ] **Step 8: Smoke test with `--limit 5` (manual, optional)**

If you have ~10 Tavily credits free:

```
TAVILY_API_KEY=<your-key> uv run slopmortem ingest \
  --only-source hn_algolia --limit 5
```

Watch the log for `title pre-filter: rejected …` lines on titles that aren't death narratives, and confirm those entries never trigger a `pitch filler tavily_search` line.

- [ ] **Step 9: Commit**

`git add slopmortem/cli/_ingest_cmd.py tests/ && git commit -m "cli: wire title pre-filter, auto-enable for hn_algolia"`

---

### Polish

- [ ] **Step 1: Run post-implementation polish**

Dispatch the `post-implementation-polish` skill on the diff produced by Tasks 1–6.

- [ ] **Step 2: Address any findings, recommit if needed**

Each polish-driven fix lands as its own commit so blame stays useful.

- [ ] **Step 3: Final lint/typecheck/test sweep**

Run: `just lint && just typecheck && just test`
Expected: clean.
