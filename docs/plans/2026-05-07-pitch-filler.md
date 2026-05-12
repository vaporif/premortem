# LLM Pitch Filler — Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax — these are **persistent durable state**, not visual decoration. The executor edits the plan file in place: `- [ ]` → `- [x]` the instant a step verifies, before moving on. On resume (new session, crash, takeover), the executor scans existing `- [x]` marks and skips them — these steps are NOT redone.

**Goal:** Replace the dumb Tavily-extract + Wayback enricher fallback chain for HN-Algolia entries with an LLM-driven pitch filler. Each URL-only stub is researched by Haiku 4.5 with a `tavily_search` tool — the model finds the right entity, synthesizes a faithful pitch + cause-of-death narrative from press coverage / founder posts, cites the sources it used, and self-reports confidence. The dumb enrichers (`TavilyEnricher`, `WaybackEnricher`) remain as opt-in flags but are no longer auto-enabled on `hn_algolia`.

**Why:** The dumb enricher chain has structural limits validated against this corpus (probe data from 2026-05-07):
1. Tavily `/extract` on the original URL fails for ~50% of HN entries — the underlying blog/host is gone (Lytro, Turbine Labs, Sparkwise, Rdio).
2. Wayback often has no snapshot for those same dead URLs.
3. Even when extraction succeeds, output is frequently chrome-padded (TechCrunch, The Verge, The Hustle results were 70% nav/ads in the first 800 chars) — slop classify catches some, but signal-to-noise in the corpus body is low.
4. A naive `/search` fallback can't distinguish primary post-mortems from competitor commentary (probe surfaced Kilo Code's promotional LinkedIn post for "Sunsetting Supermaven"; only LLM judgment correctly excludes it).

A Haiku probe (run 2026-05-07 against Lytro and Supermaven) showed the model:
- Correctly identifies the right entity vs. URL domain
- Excludes promotional/competitor content from cited sources
- Synthesizes a clean ~500-700-word pitch + cause-of-death narrative
- Cites real URLs (no hallucinated citations)
- Self-reports confidence with concrete reasoning
- Costs ~$0.009-$0.012 per entry → ~$5-7 for full 566-entry corpus, well under `max_cost_usd_per_ingest=$15`

**Architecture:** One new module `slopmortem/ingest/_pitch_filler.py` exposing `HaikuPitchFiller` that implements the existing `Enricher` Protocol (`async def enrich(entry: RawEntry) -> RawEntry`). It runs as the *last* enricher in the chain, so existing skip-guards (`if raw_html or markdown_text already populated, return`) make it a natural fallback — if Tavily-extract or Wayback already filled the body, the filler skips. Constructor takes `LLMClient`, `model` (default `anthropic/claude-haiku-4.5`), and a `Budget` reference so per-entry overruns short-circuit the run.

The HN Algolia source is changed to populate a new `RawEntry.title: str | None` field so the filler has the title to search with. The CLI's existing `hn_algolia → auto-enable Wayback+Tavily` block (added 2026-05-07) is replaced with `hn_algolia → auto-enable pitch filler`. Wayback and Tavily-extract remain available via the existing `--enrich-wayback` and `--tavily-enrich` flags but are no longer implied by the source set; users who want the cheap dumb path can still opt in. `--enable-defillama` continues to imply `--tavily-enrich` (its flow depends on Tavily-extract on a Wayback-anchored URL — different from the HN flow).

**Tech Stack:** Python 3.13, anyio, pydantic v2, OpenRouterClient (`tools=` parameter, strict-JSON `response_format`), basedpyright strict, pytest with FakeLLMClient.

## Execution Strategy

**Subagents** — default; no spec override. Tasks 1-5 must run sequentially (Task 2 depends on Task 1's `RawEntry.title` field; Task 5 depends on Tasks 2-4). Task 6 (tests) is the polish pass; per-component tests can be batched.

## Task Dependency Graph

- Task 1 [AFK]: `RawEntry.title` + hn_algolia population → depends on `none` → batch 1
- Task 2 [AFK]: `HaikuPitchFiller` module → depends on `Task 1` → batch 2
- Task 3 [AFK]: `pitch_filler.jinja` prompt template → depends on `none` (parallelizable with 1) → batch 1
- Task 4 [AFK]: Config keys → depends on `none` (parallelizable) → batch 1
- Task 5 [AFK]: CLI wiring + auto-enable swap → depends on `Tasks 2, 3, 4` → batch 3
- Task 6 [AFK]: Tests → depends on `Task 5` → batch 4

## Agent Assignments

- Task 1: `RawEntry.title` field → python-development:python-pro
- Task 2: `HaikuPitchFiller` module → python-development:python-pro
- Task 3: Prompt template → python-development:python-pro
- Task 4: Config keys → python-development:python-pro
- Task 5: CLI wiring → python-development:python-pro
- Task 6: Tests → python-development:python-pro

---

## File Structure

**New:**
- `slopmortem/ingest/_pitch_filler.py` — `HaikuPitchFiller` class implementing the `Enricher` Protocol. Reads `entry.title`, `entry.url`, derives `expected_domain` from the URL, calls `llm.complete(...)` with a single registered `tavily_search` ToolSpec and strict-JSON `response_format`. Parses output `{pitch_markdown, confidence, source_urls, entity_match_reason}`. Returns the entry with `markdown_text` populated (and a sentinel `raw_html=""` to signal "synthesized — no HTML") when `confidence == "high"`; returns the entry untouched otherwise.
- `slopmortem/llm/prompts/pitch_filler.jinja` — system + user template. System block instructs the model on: ground-truth domain check, primary-vs-secondary distinction, refusal when uncertain, single tool call, strict JSON output. User block carries `{title}`, `{url}`, `{domain}`.
- `slopmortem/llm/_pitch_filler_tools.py` — module-private `tavily_search` ToolSpec wired specifically for the filler (richer payload than the synthesis-time `tavily_search` in `corpus/_tools_impl.py`: returns `{results: [{url, title, raw_content}]}` with `raw_content` truncated to 2500 chars per result, `include_raw_content=true`, basic depth, max_results=5). Kept separate from the synthesis tool to avoid coupling two callers to one shape.

**Modified:**
- `slopmortem/models.py` — add `title: str | None = None` to `RawEntry`. Default keeps all other sources working unchanged. Also extend `CandidatePayload.provenance` Literal to include `"synthesized"` (alongside existing `"curated_real"` and `"scraped"`).
- `slopmortem/corpus/sources/hn_algolia.py` — `_hit_to_entry` populates `title=hit.get("title")` on the emitted `RawEntry`. Other source code paths unchanged.
- `slopmortem/config.py` — add four keys:
  - `model_pitch_filler: str = "anthropic/claude-haiku-4.5"` — model used by the filler.
  - `enable_pitch_filler: bool = False` — opt-in, but auto-set to `True` when `hn_algolia` is in the resolved source list (mirrors the `enable_defillama → tavily_enrich = True` pattern).
  - `max_tokens_pitch_filler: int = 1500` — synthesis output cap.
  - `pitch_filler_max_chars_per_result: int = 2500` — truncation for tool results so prompts stay bounded.
  - Add a cross-field validator: `enable_pitch_filler=True` requires non-empty `tavily_api_key` (same shape as `enable_tavily_synthesis` validator at config.py:120).
- `slopmortem/cli/_ingest_cmd.py`:
  - **Remove** the `if any(isinstance(s, HNAlgoliaSource) for s in sources): enrich_wayback = True; tavily_enrich = True` block added 2026-05-07.
  - **Add** an analogous block: `if any(isinstance(s, HNAlgoliaSource) for s in sources): enable_pitch_filler = True` (preceded by the same `TAVILY_API_KEY` fail-fast check, since the filler tool needs it).
  - When `enable_pitch_filler=True`, instantiate `HaikuPitchFiller(llm=llm, model=config.model_pitch_filler, budget=budget, ...)` and append it to `enrichers` *after* any other enrichers (so it only fires for entries that earlier enrichers couldn't fill).
  - Help text on `--enrich-wayback` / `--tavily-enrich` updated to remove the "auto-enabled when hn_algolia is in the source list" claim (no longer true).
  - Add `--enable-pitch-filler` / `--no-pitch-filler` flag for explicit override.
- `slopmortem/ingest/_helpers.py` — extend `_build_payload` (or its caller) so when the entry came from the filler (signalled by the `raw_html=""` sentinel or a new `RawEntry.synthesized: bool` flag — pick one, see Decisions below), the resulting `CandidatePayload.provenance="synthesized"`. Else logic unchanged.
- `slopmortem/llm/__init__.py` — re-export `HaikuPitchFiller`.

**New tests:**
- `tests/ingest/test_pitch_filler.py` — `HaikuPitchFiller.enrich()` against a `FakeLLMClient` that returns canned `{pitch_markdown, confidence: "high"|"low", source_urls, entity_match_reason}` JSON. Cases:
  - `confidence="high"` → entry returned with `markdown_text` populated.
  - `confidence="low"` → entry returned untouched (empty body → downstream skip).
  - `confidence="medium"` → entry returned untouched (gate is high-only by design).
  - Skip-guard: pre-filled `markdown_text` → enrich is a no-op (no LLM call).
  - Skip-guard: `entry.title is None` → no-op (the filler can't search without a title).
  - Malformed JSON output → no-op + warning log (per-entry isolation, never raises).
  - Cited source URL not in tool results → kept (we don't enforce — the citation is descriptive, not a ground truth).

**Modified tests:**
- `tests/sources/test_hn_algolia_yaml.py` — assert that `RawEntry.title` is populated by `_hit_to_entry` on every emitted entry (and that the title matches the hit's `title` field).
- `tests/test_cli_ingest.py`:
  - Remove `test_ingest_default_auto_enables_enrichers_for_hn_algolia` (no longer current behavior).
  - Add `test_ingest_default_auto_enables_pitch_filler_for_hn_algolia` — assert `HaikuPitchFiller` is in the enricher list when no flags passed (and `WaybackEnricher` / `TavilyEnricher` are NOT, since auto-enable was removed).
  - Update `test_ingest_default_without_tavily_key_fails` — error message now should reference pitch filler, not Tavily extract.

---

## Pros and Cons of Key Decisions

**Filler as Enricher vs. separate post-enrichment stage in `_classify_phase`:**
- Pros of Enricher: zero changes to `_classify_phase._one()`. The existing skip-guard contract (`if body already populated, return`) makes the filler a natural last-resort. Composable: with `[TavilyEnricher, WaybackEnricher, HaikuPitchFiller]` order, the cheap paths run first, the expensive LLM agent fires only when both fail. Mirrors the Wayback no-clobber fix's design intent.
- Pros of separate stage: clearer architectural separation between "dumb fetchers" and "LLM agents".
- Cons of Enricher: the LLMClient + Budget dep is now wired into a class that previously didn't need them. But: the Enricher Protocol doesn't constrain construction args — only `enrich(entry) -> entry` — so this is purely an internal detail of the new class.
- **Pick Enricher.** The compositional skip-guard is exactly the contract we want; reusing it costs less than writing a new pre-classify stage. The "clean separation" argument is aesthetic; the ingest pipeline already mixes concerns (`HaikuSlopClassifier` is also LLM-backed and lives in `ingest/_impls.py`).

**Haiku 4.5 vs Sonnet 4.6 for the filler:**
- Pros of Haiku: ~$0.01/entry → $5-7 full corpus. Within budget. Probe-validated on the Kilo Code commentary case — Haiku correctly excluded it from sources.
- Pros of Sonnet: stronger judgment on entity ambiguity (e.g. multiple companies sharing a name); more reliable refusal calibration.
- Cons of Haiku: weaker on contested-attribution cases. Mitigated by (a) the slop classifier downstream catching pitches that don't describe a real dead company, (b) the confidence gate (only `"high"` is accepted), (c) the prompt's explicit `expected_domain` ground truth that anchors entity selection.
- **Pick Haiku.** The probe demonstrated capability sufficiency. The model is configurable (`model_pitch_filler`), so a future tuning pass can flip to Sonnet if a real run shows >5% wrong-entity bodies in the corpus (detection: spot-check the `entity_match_reason` field against the URL domain).

**Output shape — markdown body vs structured pitch JSON:**
- Pros of markdown: feeds the existing `extract_facets` + `summarize` stages with no new shape. The synthesized markdown body looks like every other body in the corpus.
- Pros of structured JSON: free facet extraction; cleaner provenance.
- **Pick markdown body.** The existing facet/summarize pipeline does the structured extraction job already. Doing it in two places fights skip_key cache reuse.

**`raw_html` sentinel vs `RawEntry.synthesized` flag for provenance:**
- Pros of sentinel (`raw_html=""`): zero schema change, signal is "this body has no HTML backing".
- Pros of explicit flag: unambiguous, type-safe, doesn't overload `raw_html`'s meaning.
- **Pick explicit flag.** Add `RawEntry.synthesized: bool = False`. `raw_html` already has documented semantics ("the original HTML if available"); overloading it would make the codebase harder to reason about. The flag threads through to `CandidatePayload.provenance="synthesized"` in `_build_payload`.

**Single tool call (`tavily_search` only) vs multi-step research (`tavily_search` + `tavily_extract`):**
- Pros of single: deterministic budget (~1 search call, ~1 synthesis turn = ~$0.01). Probe showed a single search result set is enough for synthesis.
- Pros of multi-step: model could chase down specific URLs, dig into competitor disclosures, multi-hop reason.
- Cons of multi-step: cost balloons (each `tavily_extract` is another LLM round-trip), latency goes from ~5s to ~30s/entry, and the current probe never showed a case where multi-step would help that single-search wouldn't.
- **Pick single-call.** `max_tool_turns=2` (one tool call + one final assistant turn). If a future probe shows multi-step is necessary for some long tail, this is a knob to flip; today it's premature.

**Confidence gate at `"high"` only vs accepting `"medium"`:**
- Pros of `"high"` only: avoids polluting the corpus with low-confidence pitches that may misattribute.
- Pros of accepting `"medium"`: more entries make it through.
- Cons of `"high"` only: ~5-15% of entries with genuinely good content but not airtight evidence may be skipped. Same outcome as today (skipped at classify).
- **Pick `"high"` only.** False negatives (skipped recoverable entries) are recoverable by a future tuning pass; false positives (wrong entity stored under right canonical_id) are corpus poison and hard to detect after the fact.

**Auto-enable on hn_algolia vs purely opt-in:**
- Pros of auto-enable: matches the intent ("hn_algolia entries can't be useful without a body"). Keeps `just ingest-all` working without flag-juggling.
- Pros of opt-in: explicit cost gate. Operator must opt into LLM calls during ingest.
- **Pick auto-enable.** Mirrors the `enable_defillama → tavily_enrich = True` pattern at `_ingest_cmd.py:324-334`. The cost is bounded by `max_cost_usd_per_ingest`, which is the right place to enforce a budget — adding flag friction here doesn't add real safety.

**Drop the dumb-enricher auto-enable entirely vs keep it as a fallback:**
- Pros of drop: simpler. No mode confusion ("which enricher fires first?"). Aligned with the design's stated goal of replacing the dumb path.
- Pros of keep: cheaper for entries that *would* succeed via Tavily-extract (~50% of HN URLs per probe). Saves LLM cost.
- Cons of keep: today the mixed mode means an entry might hit Tavily-extract (chrome-padded body), get past the slop gate, and store a noisy body — instead of going through the filler that would have produced clean markdown.
- **Pick drop.** User direction is explicit ("forget about tavily extract and wayback"). The cost difference (~$5 saved out of ~$10 total at full corpus scale) doesn't justify keeping two parallel paths and the body-quality regression. The flags (`--enrich-wayback`, `--tavily-enrich`) remain available for users who want the cheap path on demand; they're just no longer implied by the default source set.

---

## Task 1: Add `RawEntry.title` field + populate in HN Algolia source

**Why:** The pitch filler needs the original HN story title to search with. Currently the title is dropped at the source after the 2026-05-07 source fix. Re-fetching it via the per-item HN Algolia API at filler time is one extra HTTP call per entry; preserving it on the model is a one-line cost.

**API verification:**
- `RawEntry` is a Pydantic model; adding an optional field with a default is backward-compatible for all existing callers and tests.

### Steps

- [x] **Step 1.1:** Add `title: str | None = None` to `RawEntry` in `slopmortem/models.py`.
- [x] **Step 1.2:** Update `_hit_to_entry` in `slopmortem/corpus/sources/hn_algolia.py` to populate `title=str(hit.get("title") or "") or None` (empty title → `None`, non-empty stored as-is).
- [x] **Step 1.3:** Update `tests/sources/test_hn_algolia_yaml.py` — extend an existing fixture's assertions to check `e.title == "RethinkDB is shutting down"` (or whatever the cassette's first hit title is).
- [x] **Step 1.4:** Run `just lint` and `just typecheck` — clean.
- [x] **Step 1.5:** Run `just test tests/sources/test_hn_algolia_yaml.py` — green.

**Done when:** `RawEntry.title` is populated for HN Algolia entries; existing tests still pass; one new assertion proves the title plumbing.

---

## Task 2: Add `HaikuPitchFiller` module

**Why:** Core component. Implements the Enricher Protocol so it composes with the existing chain without changes to `_classify_phase`.

### Steps

- [x] **Step 2.1:** Create `slopmortem/llm/_pitch_filler_tools.py` exporting `build_pitch_filler_tavily_tool() -> ToolSpec`. The wrapped async fn POSTs to `https://api.tavily.com/search` with `{api_key, query, max_results: 5, include_raw_content: True, search_depth: "basic"}`, truncates each `raw_content` to `pitch_filler_max_chars_per_result`, returns JSON `{results: [{url, title, raw_content, score}]}`. ToolSpec args: `q: str, limit: int = 5`.
- [x] **Step 2.2:** Create `slopmortem/ingest/_pitch_filler.py`. `HaikuPitchFiller` class:
  - `__init__(self, *, llm: LLMClient, model: str, budget: Budget, max_tokens: int = 1500, max_chars_per_result: int = 2500)`.
  - `async def enrich(entry: RawEntry) -> RawEntry`.
  - Skip-guards: return entry untouched if `entry.markdown_text` non-empty, or `entry.raw_html` non-empty, or `entry.url is None`, or `entry.title is None`.
  - Derive `expected_domain` from `urlparse(entry.url).netloc` (strip `www.` prefix).
  - Render prompt via `render_prompt("pitch_filler", title=entry.title, url=entry.url, domain=expected_domain)`.
  - Call `llm.complete(prompt, system=<system block from template>, tools=[tool], model=self.model, response_format=<strict JSON schema>, max_tokens=self.max_tokens, extra_body={"prompt_template_sha": prompt_template_sha("pitch_filler")})`.
  - Parse output. On `confidence == "high"` and non-empty `pitch_markdown`: return `entry.model_copy(update={"markdown_text": pitch_markdown, "synthesized": True})`. Otherwise return `entry` unchanged.
  - Wrap LLM call in `try: ... except (httpx.HTTPError, json.JSONDecodeError, ValidationError) as exc:` — log warning, return entry unchanged. Never raise (per-entry isolation contract).
- [x] **Step 2.3:** Add `synthesized: bool = False` field to `RawEntry` in `slopmortem/models.py`. *(Also extended `CandidatePayload.provenance` Literal to include `"synthesized"` per File Structure §Modified.)*
- [x] **Step 2.4:** Re-export `HaikuPitchFiller` from `slopmortem/ingest/__init__.py`.
- [x] **Step 2.5:** Run `just lint` and `just typecheck` — clean.

**Done when:** Module exists, lints, typechecks, importable.

---

## Task 3: Pitch filler prompt template

**Why:** The probe established a working prompt; this step canonicalizes it as a Jinja template tracked by `prompt_template_sha` (so cache-invalidation behaves correctly when the prompt changes).

### Steps

- [x] **Step 3.1:** Create `slopmortem/llm/prompts/pitch_filler.jinja`. System block: probe's verbatim system prompt (entity-domain ground truth, primary-vs-secondary distinction, single-search rule, JSON schema with `pitch_markdown`, `confidence`, `source_urls`, `entity_match_reason`, `low → empty pitch`). User block: `HN title: {{ title }}\nOriginal URL: {{ url }}\nURL domain (ground-truth entity host): {{ domain }}\n\nSearch the web and synthesize a faithful pitch.` *(File created at `slopmortem/llm/prompts/pitch_filler.j2` — `.j2` matches the existing loader extension; plan typo.)*
- [x] **Step 3.2:** Verify `render_prompt("pitch_filler", ...)` works (Jinja env discovers the template via the existing loader).
- [x] **Step 3.3:** Run `prompt_template_sha("pitch_filler")` — record the SHA in a comment at the top of the .jinja file for human reference (the SHA is what `extra_body` carries to OpenRouter; bump triggers cache miss). *(Documented as a runtime-computed value rather than an embedded hash that drifts with every prompt edit; current sha: `8a315c9408f21960`.)*

**Done when:** Template renders; SHA is stable.

---

## Task 4: Config keys

**Why:** Exposes the model, budget, and gating knobs as config + env-var overridable.

### Steps

- [x] **Step 4.1:** Add to `slopmortem/config.py`:
  - `model_pitch_filler: str = "anthropic/claude-haiku-4.5"`
  - `enable_pitch_filler: bool = False`
  - `max_tokens_pitch_filler: int = 1500`
  - `pitch_filler_max_chars_per_result: int = 2500`
- [x] **Step 4.2:** Add cross-field validator: `enable_pitch_filler=True` requires `tavily_api_key.get_secret_value()` non-empty. Error message: `"enable_pitch_filler=True requires tavily_api_key (the filler's tavily_search tool needs the Tavily API key)"`. Mirror the existing `enable_tavily_synthesis` validator at `config.py:118-122`.
- [x] **Step 4.3:** Add the keys to the documented surface in `slopmortem.toml` (tracked defaults file) with brief inline comments, mirroring the format around the existing `model_facet`, `enable_tavily_synthesis` blocks.
- [x] **Step 4.4:** Run `just lint` and `just typecheck` — clean.

**Done when:** Config loads; validator fires when expected.

---

## Task 5: CLI wiring + auto-enable swap

**Why:** Replace the hn_algolia auto-enable for Wayback+Tavily with auto-enable for pitch filler. Drops the now-misleading help-text claims on the dumb-enricher flags.

### Steps

- [x] **Step 5.1:** In `slopmortem/cli/_ingest_cmd.py`, **remove** the auto-enable block:
  ```python
  if any(isinstance(s, HNAlgoliaSource) for s in sources):
      if not os.environ.get("TAVILY_API_KEY"):
          raise typer.BadParameter(...)
      enrich_wayback = True
      tavily_enrich = True
  ```
- [x] **Step 5.2:** **Add** a new auto-enable block in the same location (after the `--only-source` filter resolves the source list):
  ```python
  if any(isinstance(s, HNAlgoliaSource) for s in sources):
      if not os.environ.get("TAVILY_API_KEY"):
          msg = (
              "hn_algolia source requires TAVILY_API_KEY: HN entries are URL-only "
              "stubs whose pitches are synthesized by the LLM-driven pitch filler "
              "using tavily_search. Set TAVILY_API_KEY in .env, or use --only-source "
              "on a source whose entries already carry their body (e.g. crunchbase_csv)."
          )
          raise typer.BadParameter(msg)
      enable_pitch_filler = True
  ```
- [x] **Step 5.3:** Update help text on `--enrich-wayback` and `--tavily-enrich` to drop the "Auto-enabled when hn_algolia is in the source list" claim.
- [x] **Step 5.4:** Add `--enable-pitch-filler` / `--no-pitch-filler` typer flag (default `False`, but the auto-enable block flips it to `True` for `hn_algolia`).
- [x] **Step 5.5:** When `enable_pitch_filler=True`, instantiate `HaikuPitchFiller(llm=llm, model=config.model_pitch_filler, budget=budget, max_tokens=config.max_tokens_pitch_filler, max_chars_per_result=config.pitch_filler_max_chars_per_result)` and append it to the `enrichers` list **after** any other enrichers (so dumb fetchers run first if also enabled).
- [x] **Step 5.6:** Update `_helpers.py` `_build_payload` (or its caller) so when `entry.synthesized is True`, `provenance="synthesized"`. Else preserve existing logic.
- [x] **Step 5.7:** Run `just lint` and `just typecheck` — clean.

**Done when:** CLI wires the filler; auto-enable swap is in place; help text updated.

---

## Task 6: Tests

**Why:** Lock the per-component contracts and the integration assembly.

### Steps

- [x] **Step 6.1:** Create `tests/ingest/test_pitch_filler.py`:
  - Build a `FakeLLMClient` that records the request and returns a canned response. *(Used a focused `_StubLLM` instead of the cassette-keyed `FakeLLMClient` — the filler's contract is independent of the cassette key shape, and the stub is simpler to reason about.)*
  - Test `confidence="high"` → entry returned with `markdown_text` populated and `synthesized=True`.
  - Test `confidence="low"` → entry returned untouched.
  - Test `confidence="medium"` → entry returned untouched (gate is high-only).
  - Test pre-filled `markdown_text` → no LLM call.
  - Test `entry.title is None` → no LLM call.
  - Test malformed JSON output → no-op + WARNING log captured (no exception bubbles).
- [x] **Step 6.2:** Update `tests/sources/test_hn_algolia_yaml.py` — add `assert e.title == ...` on at least one fixture path. *(Done as part of Task 1.)*
- [x] **Step 6.3:** Update `tests/test_cli_ingest.py`:
  - Remove `test_ingest_default_auto_enables_enrichers_for_hn_algolia` (no longer current behavior).
  - Add `test_ingest_default_auto_enables_pitch_filler_for_hn_algolia` — assert `HaikuPitchFiller` appears in the enricher list when default flags are passed; assert `WaybackEnricher` and `TavilyEnricher` do NOT.
  - Update `test_ingest_default_without_tavily_key_fails` — error message references pitch filler.
  - Update `test_only_source_crunchbase_skips_enricher_auto_enable` — also assert the pitch filler is NOT in the enricher list when `--only-source crunchbase_csv` is passed.
- [x] **Step 6.4:** Run `just lint`, `just typecheck`, `just test` — all green. *(Pre-existing failures in `tests/ingest/test_orchestration.py` and `test_per_entry_isolation.py` are environmental — sandbox `PermissionError` on `/private/tmp` paths, unrelated to this diff. Confirmed by stashing changes and re-running on a clean tree.)*
- [ ] **Step 6.5:** Run a small live slice to confirm end-to-end:
  ```sh
  uv run slopmortem ingest --only-source hn_algolia --limit 5 --dry-run
  ```
  Expect: 5 entries seen, ~1-3 with `synthesized=True` and high confidence, the rest skipped (confidence below threshold or filler errored on per-entry isolation). Log lines should include `pitch filler: kept ...` / `pitch filler: refused (confidence=low) ...`. *(Deferred — needs live API keys and Qdrant; can't run in this environment.)*

**Done when:** All tests pass; small live slice confirms wiring.

---

## Out of scope (deferred)

- **Backfill of the 566 existing stub files.** Re-running `just ingest-all` after this lands will overwrite stubs in place via the existing skip_key/content_hash mechanism (different `markdown_text` → different `content_hash` → different `skip_key` → no skip). No special `--re-enrich` flag. Tail entries that the filler also can't recover (sub-confidence) stay as stubs on disk — same orphan story as today; trivially detectable via `grep -lE "^hn_object_id:" post_mortems/raw/hn_algolia/*.md`.
- **Multi-tool agent.** The probe established that single-search synthesis is sufficient for the corpus we've validated. If a future probe shows multi-step (search → extract → search) is needed for some long tail, that's a separate plan.
- **Sonnet escalation on low-confidence.** Two-tier (Haiku-first, Sonnet for low-confidence) would lift recovery rate but doubles wiring complexity and the cost-per-corpus-refresh climbs. Defer until a real run shows the Haiku-only baseline is too lossy.
- **Provenance-aware retrieval.** Downstream stages currently treat all bodies equally. Once `provenance="synthesized"` exists, a future plan could deprioritize synthesized bodies vs. primary sources at rerank time. Out of scope here — the value is independent of the filler's introduction.
- **Filler over Crunchbase / Curated.** Those sources already carry bodies. The filler's skip-guard handles this naturally (pre-filled `markdown_text` → no-op), so it's safe to leave on globally. Just no auto-enable for non-hn_algolia source sets.
