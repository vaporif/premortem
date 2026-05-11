# Recall Search-Then-Verify Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or `/team-feature` to implement this plan task-by-task, per the Execution Strategy below. Steps use checkbox (`- [ ]`) syntax — these are **persistent durable state**, not visual decoration. The executor edits the plan file in place: `- [ ]` → `- [x]` the instant a step verifies, before moving on. On resume (new session, crash, takeover), the executor scans existing `- [x]` marks and skips them — these steps are NOT redone. TodoWrite mirrors this state in-session; the plan file is the source of truth across sessions.

**Goal:** Stop trusting Opus's *article* URLs in the recall stage. Opus is reliable at remembering *which* startups died but unreliable at *citing where* the news lives — it fabricates Coindesk/Block paths that 404. The current verifier correctly drops these hallucinations, but it also drops the real signal that hides behind them (CipherTrace, Forta, etc. are real entities the verifier never gets to confirm because the cited article URL is invented). Replace Opus-supplied article URLs with Tavily-discovered URLs and feed those through the existing L2→L3→L4→L5 verifier. Keep `homepage_url` — homepage roots hallucinate at a much lower rate than specific article paths (short, repeated across training data, fact-checkable via DNS), and a dead vendor's expired homepage is positive death-signal that Wayback turns into the marketing-copy substrate retrieval needs.

**Architecture:** The recall LLM's contract loses one field. `RecallSuggestion` keeps `name`, `category`, `status`, `failure_year` (still required `int`, [1990, 2030]), `one_liner`, `homepage_url` (now optional — `str | None`) and drops `evidence_url`. A new **search head** (`_search_for_evidence`) sits between `llm_recall` and `verify_suggestion`, replacing the recall LLM as the source of the citation URL. The existing gate ladder gains one rung at the top:

- **L0 (new) — search head.** Per suggestion, issue one Tavily search shaped by status and year (see the Search query template note below). Drop the suggestion when the search returns no hits — equivalent to "the article URL would have 404'd anyway, save the L2 round-trip".
- **L1 — Pydantic schema.** Unchanged.
- **L2 — HEAD→GET liveness on the discovered URL.** Same code path; only the URL source changes.
- **L3 — body extraction + name+death-keyword anchors.** Unchanged.
- **L4 — Wayback enrichment.** Pure enricher (never decreases standing). Seed `RawEntry.url` becomes `suggestion.homepage_url` when present; when the recall LLM didn't supply one, **L4 short-circuits** with no enrichment and no Wayback round-trip.
- **L5 — Haiku deathness judgment.** Unchanged; this is the final arbiter that the discovered article describes *this company's* death/distress event, not a different unrelated event involving a similarly-named entity.

**`RawEntry.url` when homepage is absent:** `RawEntry.url` is non-optional and is the canonical source-URL persisted to the Qdrant point. When `suggestion.homepage_url is None`, the seed `RawEntry` is constructed with `url=discovered_url` (the Tavily-discovered article URL) so the persisted point still has provenance. The L4 Wayback step keys off `suggestion.homepage_url is not None`, not off `RawEntry.url`, so the "article URL as `RawEntry.url`" case never triggers Wayback against a news article URL.

**Verification tiers stay two-valued** (`wayback_anchored`, `evidence_only`) — splitting the type churns the persistence chain (`CandidatePayload`, `_build_payload`, persist callback) for one event-level distinction. Instead, distinguish Wayback-skipped from Wayback-attempted-but-empty at the **span-event level**: `RECALL_VERIFIED_EVIDENCE_ONLY` gains a `wayback_attempted: bool` attribute (False when `homepage_url is None`, True when Wayback ran and returned no anchoring snapshot).

**Dedup key (`_recall_source_id`).** Prefer `(name, homepage_url)` when homepage is present; fall back to `(name,)` (name-only) when homepage is absent — *not* `(name, registrable_domain(discovered_url))`. Rationale: across runs, the same homepage-less company will often surface via different citation hosts (Coindesk one run, Block one run), and domain-keyed fallback would create duplicate Qdrant points for the same vendor. Name-only fallback risks merging two distinct startups with the same name, but that's vanishingly rare per pitch and `alias_graph` already collapses obvious name collisions at persist time. Document the trade-off in the helper docstring.

**Tech Stack:** Python 3.13, anyio (no bare asyncio), httpx (existing `safe_post` for Tavily), pydantic v2 (strict, no `Any` leaks), tldextract (existing via `corpus._domain`), basedpyright strict, pytest with `asyncio_mode="auto"` + `pytest-xdist`.

## Execution Strategy

**Subagents** — Tasks land sequentially because each step changes the recall contract shape that the next step builds on. Task 0 is a pre-flight: prove that Tavily reliably returns a valid evidence URL for known dead Web3 startups (CipherTrace, BlockFi, Celsius, FTX, Voyager Digital) so we don't ship a search step that returns "(no results)" for the population it exists to surface. If Task 0 fails (< 4 of 5 return a usable hit), the search query template is the bottleneck — pause and iterate on the query shape before Tasks 1–5. The two-stage review gate (spec compliance, then code quality) applies per task.

## Task Dependency Graph

- [x] Task 0 [AFK]: Tavily evidence-discovery pre-flight (5 known-dead Web3 companies; ≥4 must return a usable hit) → depends on `none` → batch 0
- [x] Task 1 [AFK]: Tavily structured-search helper (`tavily_search_structured` returning `list[TavilyHit]`, not formatted string) → depends on `Task 0` → batch 1
- [x] Task 2 [AFK]: `RecallSuggestion` schema shrink (drop `evidence_url`; relax `homepage_url` to `str | None`); `llm_recall.j2` prompt update; cassette re-record for `llm_recall` → depends on `Task 1` → batch 2
- [x] Task 3 [AFK]: Verifier search head (`_search_for_evidence`); plumb discovered URL through `verify_suggestion`; L4 Wayback stays, gated on `homepage_url is not None`; `_recall_source_id` keeps the homepage key with a name-only fallback when homepage is absent → depends on `Task 2` → batch 3
- [ ] Task 4 [AFK]: Pipeline wiring (`RecallDeps` adds `tavily_search` callable; CLI builds it from config; eval recorder fakes it) → depends on `Task 3` → batch 4
- [ ] Task 5 [AFK]: Telemetry + cassette re-record for `recall_deathness` (URL bodies will differ); test sweep across recall_verify / recall_persist / pipeline_recall_fallback test files → depends on `Task 4` → batch 5
- [ ] Polish: post-implementation-polish → depends on `Tasks 1-5` → batch 6

## Agent Assignments

- Task 0: Tavily evidence-discovery pre-flight → python-development:python-pro
- Task 1: Tavily structured-search helper → python-development:python-pro
- Task 2: `RecallSuggestion` schema shrink + prompt + cassette → python-development:python-pro
- Task 3: Verifier search head + L4 gate + dedup rekey → python-development:python-pro
- Task 4: Pipeline wiring → python-development:python-pro
- Task 5: Telemetry + cassette + test sweep → python-development:python-pro
- Polish: post-implementation-polish → general-purpose

---

## File Structure

**New:**

- `tests/stages/test_recall_search_preflight.py` — Task 0 test (cassette-backed). For each of `CipherTrace`, `BlockFi`, `Celsius Network`, `FTX`, `Voyager Digital` paired with their failure year, render the search query template, hit Tavily (or cassette), assert at least one of the top-3 results has the company name in title or snippet. Task 0 explicitly compares **three query-syntax variants** against the same five companies — pipe-OR (`shutdown|closed|...`), comma-OR (`shutdown, closed, ...`), and natural prose (`shutdown or closed or ...`) — and picks the variant with the highest hit rate. Tavily is natural-language; pipe-OR is not documented as a supported operator, so the prose variant is the safe baseline. Gates the whole plan.
- `tests/stages/test_recall_search_head.py` — Task 3 tests: search returns zero hits → drop with `RECALL_REJECTED_NO_EVIDENCE` event; search returns hits but none mention the name → drop; search returns valid hit → passes discovered URL into existing L2; multiple Coindesk hits about the same company → first valid wins.
- `tests/fixtures/cassettes/recall/tavily_search_preflight.yaml` — Task 0 cassette: live recordings of the five known-dead searches.
- `tests/fixtures/cassettes/recall/tavily_search_e2e.yaml` — Task 3+5 cassette: search responses for whatever pitch the end-to-end tests run against.

**Modified:**

- `slopmortem/models.py` — Task 2. Drop `evidence_url` from `RecallSuggestion`. Relax `homepage_url` to `homepage_url: str | None = None`. Update `_validate_constraints` to validate the homepage when present (existing `_HTTP_URL_ADAPTER` call, but guarded `if self.homepage_url is not None`) and drop the evidence_url half. The docstring's "URL fields are typed ``str`` ..." paragraph stays accurate for the homepage; trim the evidence half.
- `slopmortem/llm/prompts/llm_recall.j2` — Task 2. Remove the evidence_url output instruction; keep homepage_url as optional with a hard "do not fabricate — leave null if you do not remember the exact homepage" line. The recall_max_suggestions_per_pitch cap stays. Add a short explanation that the citation is discovered downstream so the recall LLM has no reason to fabricate one.
- `slopmortem/corpus/tavily.py` — Task 1 (**new module, non-leaf**). Defines `TavilyHit` (`BaseModel` with `title`, `url`, `snippet`, `published_date: str | None`) and `tavily_search_structured(q: str, limit: int) -> list[TavilyHit]`. Keeps the structured shape off the leaf-private `_tools_impl.py` so `pipeline.py`/`stages/recall_verify.py` can import it without crossing the import-linter boundary the L2 comment in `recall_verify.py` already calls out.
- `slopmortem/corpus/_tools_impl.py` — Task 1 (light touch). Keep `tavily_search_async` (formatted-string output for the LLM tool loop) untouched; internally refactor its HTTP body parse to share the new `TavilyHit` parser, then re-stringify. Don't change `tavily_search_async`'s signature — synthesis tools and the pitch filler are called inside the LLM tool loop where any shape change costs cassettes.
- `slopmortem/stages/recall_verify.py` — Task 3. New `_search_for_evidence(suggestion, *, tavily_search)` that builds the query and returns `str | None` (the discovered article URL). `verify_suggestion` gains a `discovered_url: str` parameter that replaces the old `suggestion.evidence_url` references. `RawEntry.url` is constructed as `suggestion.homepage_url if suggestion.homepage_url is not None else discovered_url` so the persisted entry always has a canonical URL. L4 Wayback's *gate* is `suggestion.homepage_url is not None` (independent of `RawEntry.url`); when the homepage is absent, L4 short-circuits, tier stays `evidence_only`, and `RECALL_VERIFIED_EVIDENCE_ONLY` is emitted with `wayback_attempted="false"`. When the homepage is present and Wayback runs but returns no anchoring snapshot, tier is still `evidence_only` but the event carries `wayback_attempted="true"`. Wayback remains a *pure enricher* — it never drops a candidate, never decreases a score; success bumps the tier from `evidence_only` to `wayback_anchored` and appends the snapshot body to the persisted body, failure or absence leaves both unchanged. `_recall_source_id(suggestion)` returns `(name, homepage_url)` when homepage is present and `(name,)` (name-only) otherwise — the helper's docstring documents the false-merge trade-off and the reason for not falling back to the discovered URL's domain. **Update the module docstring** to describe L0 (search head) plus the existing L1–L5 ladder, and to note that L4 short-circuits when `homepage_url is None`.
- `slopmortem/stages/llm_recall.py` — Task 2. Update the success-path log line (added two turns ago) — drop the year/status tuple references that depended on dropped fields, keep `name` and `status`. The `cap` slice + ValidationError handling are unchanged.
- `slopmortem/pipeline.py` — Task 4. `RecallDeps` adds `tavily_search: TavilySearchFn` (typed Protocol mirroring `tavily_search_structured`). `_run_recall_branch` threads it through to `verify_and_persist_all`. CLI builds it in `deps.py` lazily so non-recall queries don't reach for `TAVILY_API_KEY`.
- `slopmortem/deps.py` — Task 4. Add a small builder that returns a `TavilySearchFn` closure over `tavily_search_structured`. Same conditional shape as the existing `tavily_synthesis` wiring: raise on missing key only when recall is enabled.
- `slopmortem/config.py` — Task 4. Add `enable_tavily_recall_search: bool = True` (default on; this is the new contract). Add `tavily_recall_max_results: int = Field(default=5, ge=1, le=10)` so the search-result cutoff is config-tunable. The existing `tavily_calls_per_synthesis` is unchanged — recall and synthesis have independent quotas.
- `slopmortem/tracing/events.py` — Task 3. New `RECALL_REJECTED_NO_EVIDENCE = "recall.rejected_no_evidence"` (search returned zero hits or zero name-matching hits). No event values change; the existing `RECALL_VERIFIED_EVIDENCE_ONLY` event gains a `wayback_attempted: str` attribute at the emit site in `recall_verify.py` (Laminar attribute values are strings, hence `"true"`/`"false"`). Remove nothing.
- `slopmortem/evals/recording.py` — Task 5. Add the Tavily fake wrapper so `--record` runs capture Tavily responses into the eval cassette, matching the existing LLM/embedding wrapper pattern.
- `slopmortem/evals/recording_helper.py` — Task 5. Same.
- `slopmortem/llm/prompts/llm_recall.j2.prompt_sha` — Task 2 (touched indirectly via the Jinja render registry). Note for the executor: the cassette key includes the prompt SHA, so any Jinja change requires re-recording the affected cassettes.
- `tests/stages/test_llm_recall.py` — Task 2. Update fixtures: `RecallSuggestion(name=..., category=..., status=..., failure_year=..., one_liner=...)` — no URLs. The "dropped invalid response" case stays valid (any other ValidationError still fires).
- `tests/stages/test_recall_verify.py` — Task 3. All existing fixtures that construct `RecallSuggestion` need URL fields stripped. Tests that inject canned L2/L3 HTTP responses now key on the search-discovered URL — easiest path: parametrize the fixture so the cassette / fake search returns a known URL the L2/L3 fakes are already keyed on.
- `tests/stages/test_recall_verify_l2_l4_bprime.py`, `tests/stages/test_recall_verify_l3_hygiene.py`, `tests/stages/test_recall_verify_l5_tristate.py` — Task 5. Same shape: drop URLs from `RecallSuggestion`, parametrize fixtures around the discovered URL.
- `tests/stages/test_recall_retrieval_survival.py` — Task 5. Adjust the verified-entry fixture: homepage-bearing case keeps the existing dedup id; add a parametrized case where `homepage_url is None` so the registrable-domain-of-evidence fallback is covered too.
- `tests/stages/test_recall_persist.py`, `tests/stages/test_recall_persist_dedup_event.py`, `tests/stages/test_recall_persist_gap_closures.py` — Task 5. The dedup key change is the only material shift; existing tests should keep passing once `RecallSuggestion` fixtures are updated.
- `tests/test_pipeline_recall_fallback.py` — Task 4. Add a `FakeTavilySearch` (returns canned hits) and inject through `RecallDeps`. Existing recall-path assertions stay; the wiring is what's new.
- `slopmortem.toml` — Task 4. Document `enable_tavily_recall_search` and `tavily_recall_max_results` in the recall section with the same comment style as the other recall knobs.
- `docs/architecture.md` — Task 5 (or Polish). Update the recall section: "The recall LLM returns candidate names; Tavily search anchors each to a real citation; verifier proves the citation. URL hallucinations no longer reach the verifier — they fail at the search step or get replaced by a real URL."

---

## Pre-flight (read before starting any task)

Project conventions that bite if missed:

- `uv` for everything. `just install`, `just test`, `just lint`, `just typecheck`. Don't invoke `pip` or `python -m venv`.
- Strict basedpyright with `reportAny="error"`. No `# type: ignore` to silence. Use `cast` with a one-line comment if a third-party stub is missing.
- Pydantic v2 only. `BaseModel`, `model_validator(mode="after")`. `StrEnum` or `Literal[*_TAXONOMY_VALUES]` for closed sets. **Avoid** `Field(ge=, le=)` on strict-schema-bound models — both OpenAI and Anthropic strict response_format reject `minimum`/`maximum` on numeric types. Use `model_validator(mode="after")` for numeric bounds, mirroring `RecallSuggestion._validate_constraints` and `_DeathnessJudgment._validate_confidence`.
- Anyio, not bare asyncio. `gather_resilient` at fan-out points; the search step fans out across suggestions and must use it.
- Fakes over mocks. `FakeTavilySearch` follows the `FakeLLMClient` / `FakeSlopClassifier` pattern. New tests must not import `unittest.mock`.
- Tests must be parallel-safe (`pytest-xdist`). Filesystem state lives in `tmp_path`. No `/tmp` direct writes.
- Cassettes in `tests/fixtures/cassettes/...`. If a cassette test raises `NoCannedResponseError`, the prompt SHA or model changed — re-record the affected scope only, don't widen the matcher. See `docs/cassettes.md`.
- New SpanEvents go in the `slopmortem/tracing/events.py` StrEnum. Free-form strings get rejected.
- `slopmortem.toml` is the documented default surface; document new keys there. Personal overrides go in `slopmortem.local.toml` (gitignored).
- Tavily key is `SecretStr`. Don't log it, don't include it in span attrs (CLAUDE.md). The closure builder in `deps.py` handles read-once.

## Design invariants (do not violate)

1. **Wayback is a pure enricher.** It may *increase* a candidate's standing — bumping `verification_tier` from `evidence_only` to `wayback_anchored` and contributing marketing-copy body that helps retrieval embedding match future pitches — but it must *never* decrease one. Absence of a Wayback snapshot, transport failure, or empty body all leave the candidate at `evidence_only` and proceed; none of those signals can cause a drop. This is why the v1 Tavily search step replaces the article URL only — Tavily is in the proof path (no hit ⇒ drop), Wayback is not (no snapshot ⇒ continue).
2. **The homepage is provenance, not corroboration.** A dead homepage doesn't drop a candidate; a live homepage doesn't admit one. The article URL discovered by Tavily is the citation; the homepage is the Wayback seed and a retrieval substrate. Recall_verify must not L2-gate the homepage.
3. **L5 is the event-match arbiter.** Tavily can legitimately surface an article that mentions the company but covers a *different event* than the suggestion claims (e.g., the suggestion says `status=dead, failure_year=2022` and Tavily returns a 2024 layoff article about the same name). L0–L4 cannot tell those apart; only L5's Haiku judgment of `verdict in {"dead", "struggling", "alive"}` against the article body decides. Treat any L5 transport/parse failure as a drop — false admits in this path are worse than false drops.

## Cost

- **Per recall firing:** up to `cap` (~8) suggestions × 1 Tavily search = ~8 Tavily credits added per query that triggers the recall fallback. L0 drops (no-hit suggestions) save one downstream L2 GET + L5 Haiku call each, so net spend is bounded by `min(suggestions, hits) × (Tavily + L2 + L5)` vs the current `suggestions × (L2 + L5)`.
- **Eval delta (`just eval`):** depends on the recall-fallback firing rate across the eval set. Measure on first cassette re-record and report in Task 5's verification note; if it exceeds the existing eval cost ceiling, gate L0 behind a per-query suggestion cap (`tavily_recall_max_results × min(suggestions, k)`).
- **Recording (`just eval-record`):** one extra Tavily call per surviving suggestion. The `~$2 per record` estimate in `justfile` already includes some Tavily spend for the pitch filler; budget another ~$0.50–$1 for recall-side searches.

## Open questions (resolve before Task 3)

1. **Search query template — syntax and shape.** Two axes to resolve together in Task 0:
   - *Syntax:* pipe-OR (`shutdown|closed|...`), comma-OR (`shutdown, closed, ...`), or natural prose (`shutdown or closed or ...`). Tavily is a natural-language search; the prose form is the safe baseline. Task 0 records all three against the pre-flight five and picks the best hit rate.
   - *Shape:* status-shaped (`"<name>" shutdown OR closed OR acquired <year>`) vs status-blind (`"<name>" startup death <year>`). Status-shaped likely beats status-blind on precision for `dead`/`absorbed` but may miss `struggling` cases where layoff news doesn't use the dead-set keywords. The status-shaped variant should branch on `RecallSuggestion.status`: `dead`/`absorbed` use the terminal keyword set, `struggling`/`bruised` use the distress keyword set.
   - `failure_year` is required `int` on `RecallSuggestion` ([1990, 2030] per `models.py:301`) so it's always present at the search step — no null-handling branch needed.
2. **Multi-hit selection.** When Tavily returns up to `tavily_recall_max_results` (default 5) hits:
   - Primary: first hit whose **title or snippet** (not snippet alone — snippets are ~150–200 chars and routinely lack death keywords even when the article body has them) contains both the company name AND any keyword from `_DEATH_KEYWORDS`.
   - Fallback: first hit whose title or snippet contains the name. The hit still has to clear L3's body-level anchor check, so a name-only hit can still get dropped downstream — better than a no-hit drop here.
   - Punt date-proximity (preferring `published_date` near `failure_year`) to a follow-up; the L5 Haiku already handles event-match.
3. **Homepage discovery as a follow-up.** When the recall LLM returns `homepage_url=null`, v1 skips Wayback entirely. A follow-up could discover the homepage via a second Tavily call (`"<name>" official site`) or by extracting outbound links from the article body. Not in scope for v1 because the cost/benefit isn't measurable until the v1 search path is on the eval set.

---

(Task sections to be filled in by the team-lead at dispatch time, per the verifier-hardening plan's pattern.)
