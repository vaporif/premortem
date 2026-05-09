# Recall fallback improvements

Follow-up to the `pivot` branch's LLM recall fallback (`docs/plans/2026-05-08-llm-recall-fallback.md`). The branch shipped the end-to-end flow but left several known-weak spots. This plan addresses seven of them; three more (skip-second-rerank, TTL re-verify, A/B in cassette mode) are deferred per user direction.

## Goal

Close the verification gap that lets "real but still alive" companies poison the corpus, surface llm_recall provenance to synthesis and the user, give the coverage-gap predicate observability and regression coverage, and add per-source slop tuning headroom.

## Workstreams

| Task | Concern addressed | Touches |
|------|-------------------|---------|
| 1 | L5 deathness gate after L4 | `stages/recall_verify.py` (+prompt, config, SpanEvent) |
| 2 | Always-log RECALL_GAP_SCORE | `stages/llm_recall.py`, `pipeline.py`, `tracing/events.py` |
| 3 | Down-weight llm_recall at retrieval | `corpus/_qdrant_store.py`, `config.py` |
| 4 | Per-source slop threshold + borderline event | `config.py`, `ingest/_ingest.py`, `tracing/events.py` |
| 5 | Coverage-gap predicate eval expansion | `tests/fixtures/coverage_gate/`, `test_coverage_gate.py` |
| 6 | Provenance to synth prompt + rendered MD | `models.py`, `stages/synthesize.py`, prompt, `render.py` |

Tasks are independent at the file level. Recommend implementing 1 → 2 → 3 → 4 → 5 → 6 in order — each ships as one commit so review stays bounded.

## Architecture decisions

**L5 as a separate Haiku call, not folded into the existing slop classifier.** The slop classifier answers "is this text a death narrative?" L5 answers "is the named company actually dead?" Different question, different prompt shape. Folding them would make both fuzzier. The cost is one extra Haiku call per verified suggestion (~$0.0002), which is rounding error against the Opus recall call upstream.

**`source` carried on `CandidatePayload` as `str | None = None`.** Existing qdrant payloads don't have the field; making it required would refuse to validate them. The `provenance_id` field already encodes `<source>:<source_id>` but parsing it at every read site is brittle. A typed field is honest and forward-compatible.

**Down-weight at retrieval, not at synthesis.** The synthesis stage already has too many knobs. Pushing the soft preference into the FormulaQuery means rerank sees the right relative ordering and the rest of the pipeline is unchanged.

**RECALL_GAP_SCORE as event attributes, not a metric.** The Laminar setup already routes events; adding a metrics pipeline would dwarf the change. `qualifying`/`required` attributes give enough signal for offline calibration.

**Per-source slop threshold defaults to None (= use slop_threshold).** Default behavior unchanged. Operators who want to tune recall-source acceptance separately set `recall_slop_threshold` in `slopmortem.local.toml`. Borderline event fires regardless so we accumulate data for tuning.

## Tech stack

Existing — no new dependencies. Pydantic v2, anyio, Laminar `@observe`, basedpyright strict.

## Pre-flight

```bash
just lint
just typecheck
just test
```

All must be green before starting. Re-run after each task.

---

## Task 1 — L5 deathness gate

**Why this exists.** L1-L4 verify a URL exists (HEAD), serves content (GET), mentions the company name and a death keyword (anchor scan), and ideally has a Wayback snapshot (L4). None of that distinguishes "real company that died" from "real company that's still alive but had layoffs once." Opus can confidently emit any name from training data; verification as written passes a non-trivial fraction of those. L5 is the missing question: did this company actually die? Drop suggestions where Haiku says no or isn't confident.

**Files**
- Create: `slopmortem/llm/prompts/recall_deathness.j2`
- Modify: `slopmortem/stages/recall_verify.py`
- Modify: `slopmortem/tracing/events.py`
- Modify: `slopmortem/config.py`
- Modify: `slopmortem/pipeline.py` (thread llm + model into the recall branch's persist closure)
- Test: `tests/stages/test_recall_verify.py`

**Steps**

- [x] **Step 1.1 — Add config knobs.** In `slopmortem/config.py` add three fields near the recall block:
  ```python
  model_recall_deathness: str = "anthropic/claude-haiku-4.5"
  max_tokens_recall_deathness: int = Field(default=128, ge=1)
  recall_deathness_min_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
  ```

- [x] **Step 1.2 — Add SpanEvent.** In `slopmortem/tracing/events.py` add to the recall block:
  ```python
  RECALL_REJECTED_L5_NOT_DEAD = "recall.rejected_l5_not_dead"
  RECALL_REJECTED_L5_LOW_CONFIDENCE = "recall.rejected_l5_low_confidence"
  ```

- [x] **Step 1.3 — Author the prompt.** Create `slopmortem/llm/prompts/recall_deathness.j2`:
  ```jinja
  {% block system -%}
  You judge whether a company is actually defunct based on a verified evidence document. Return a single JSON object with three fields:
  - died: true if the document clearly establishes the company shut down, was acquired in distress (acqui-hire, fire-sale, distressed M&A), wound down operations, or went bankrupt. false if the company is still operating, had only a partial setback (one round of layoffs while still selling), or the document is ambiguous.
  - confidence: 0.0 to 1.0. How sure you are.
  - evidence_quote: a short verbatim quote from the document supporting your judgment, or empty string if you can't pull one.

  Set died=false if the document is about an acquisition that the company survived as a going concern. Set died=true if the acquisition was distressed or the brand was wound down post-deal.

  Return JSON only, no prose outside it.
  {%- endblock %}
  {% block user -%}
  Company: {{ name }}
  Claimed status: {{ status }}
  Claimed failure year: {{ failure_year }}

  <untrusted_document source="evidence">
  {{ body }}
  </untrusted_document>

  Did this company actually die? Return JSON.
  {%- endblock %}
  ```

- [x] **Step 1.4 — Write failing test (deathness=false drops).** Add to `tests/stages/test_recall_verify.py`:
  ```python
  async def test_l5_drops_when_not_dead(...):
      """L5 deathness=false → suggestion rejected even if L1-L4 pass."""
      fake_llm = FakeLLMClient(
          responses_by_template={
              "recall_deathness": '{"died": false, "confidence": 0.95, "evidence_quote": "raised series C"}',
          }
      )
      result = await verify_suggestion(suggestion, wayback=fake_wayback, llm=fake_llm, ...)
      assert result is None
  ```
  Use the existing test scaffolding for L1-L4 to seed a passing prefix. Pattern off existing tests in the file.

- [x] **Step 1.5 — Run test, expect failure.**
  ```bash
  uv run pytest tests/stages/test_recall_verify.py::test_l5_drops_when_not_dead -v
  ```
  Expected: function signature mismatch (no `llm` kwarg yet).

- [x] **Step 1.6 — Implement `_l5_deathness_judgment` helper in `recall_verify.py`.**
  ```python
  class _DeathnessJudgment(BaseModel):
      died: bool
      confidence: float = Field(ge=0.0, le=1.0)
      evidence_quote: str

  async def _l5_deathness_judgment(
      *,
      suggestion: RecallSuggestion,
      body: str,
      llm: LLMClient,
      model: str,
      max_tokens: int,
  ) -> _DeathnessJudgment | None:
      """Returns None on transport/parse failure (treated as drop, conservatively)."""
      blocks = render_blocks(
          "recall_deathness",
          name=suggestion.name,
          status=suggestion.status,
          failure_year=suggestion.failure_year,
          body=body[:8000],  # match slop classifier's char budget
      )
      try:
          result = await llm.complete(
              blocks["user"],
              system=blocks["system"],
              model=model,
              response_format={
                  "type": "json_schema",
                  "json_schema": {
                      "name": "DeathnessJudgment",
                      "schema": to_strict_response_schema(_DeathnessJudgment),
                      "strict": True,
                  },
              },
              max_tokens=max_tokens,
          )
      except (httpx.HTTPError, RuntimeError) as exc:
          logger.info("recall_verify: L5 LLM call failed: %r", exc)
          return None
      try:
          return _DeathnessJudgment.model_validate_json(result.text)
      except ValidationError as exc:
          logger.info("recall_verify: L5 invalid response: %r", exc)
          return None
  ```

- [x] **Step 1.7 — Wire L5 between L4 and the return statement** in `verify_suggestion`. Add the LLM kwargs to the signature, then after the L4 block:
  ```python
  judgment = await _l5_deathness_judgment(
      suggestion=suggestion,
      body=body,
      llm=llm,
      model=model_recall_deathness,
      max_tokens=max_tokens_recall_deathness,
  )
  if judgment is None:
      # Conservative: treat parse/transport failure as a drop.
      _emit_event(SpanEvent.RECALL_REJECTED_L5_LOW_CONFIDENCE)
      return None
  if not judgment.died:
      logger.info(
          "recall_verify: L5 ruled %r not dead (confidence=%.2f)",
          suggestion.name, judgment.confidence,
      )
      _emit_event(SpanEvent.RECALL_REJECTED_L5_NOT_DEAD)
      return None
  if judgment.confidence < min_confidence:
      logger.info(
          "recall_verify: L5 confidence %.2f below threshold %.2f for %r",
          judgment.confidence, min_confidence, suggestion.name,
      )
      _emit_event(SpanEvent.RECALL_REJECTED_L5_LOW_CONFIDENCE)
      return None
  ```

- [x] **Step 1.8 — Update `verify_and_persist_all` signature** to accept and forward `llm`, `model_recall_deathness`, `max_tokens_recall_deathness`, `min_confidence`. Add to `@observe(ignore_inputs=...)` so the LLM handle never lands in span attrs.

- [x] **Step 1.9 — Update `_run_recall_branch` in `pipeline.py`** to pass the new kwargs through:
  ```python
  verified = await verify_and_persist_all(
      suggestions,
      wayback=wb,
      persist=_persist,
      llm=llm,
      model_recall_deathness=config.model_recall_deathness,
      max_tokens_recall_deathness=config.max_tokens_recall_deathness,
      min_confidence=config.recall_deathness_min_confidence,
  )
  ```

- [x] **Step 1.10 — Add covering tests** in `tests/stages/test_recall_verify.py`:
  - `test_l5_drops_when_not_dead` (Step 1.4 — should already be there)
  - `test_l5_drops_when_low_confidence` — `died=true, confidence=0.5` with default 0.7 threshold → drops
  - `test_l5_passes_at_high_confidence` — `died=true, confidence=0.85` → returns the entry+tier tuple
  - `test_l5_drops_on_parse_failure` — LLM returns invalid JSON → drops (conservative)
  - `test_l5_drops_on_transport_failure` — LLM raises httpx.HTTPError → drops

- [x] **Step 1.11 — Run the new tests.**
  ```bash
  uv run pytest tests/stages/test_recall_verify.py -v
  ```
  Expected: all pass.

- [x] **Step 1.12 — Run full suite.**
  ```bash
  just test
  just typecheck
  just lint
  ```

- [ ] **Step 1.13 — Commit.** `recall: L5 deathness gate over verified bodies`.

---

## Task 2 — Always log RECALL_GAP_SCORE

**Why this exists.** The coverage-gap predicate is the trigger. When `enable_llm_recall=False` (the default) the pipeline doesn't even compute it, so we have no signal on how often it would have fired. When recall is enabled, only the binary fire/don't-fire is logged, not the underlying score. Calibrating the predicate (Task 5 also touches this) needs the numeric data first.

**Files**
- Modify: `slopmortem/stages/llm_recall.py` — extend predicate to expose the score
- Modify: `slopmortem/pipeline.py` — compute + emit on every query
- Modify: `slopmortem/tracing/events.py`
- Test: `tests/stages/test_coverage_gate.py`, `tests/test_pipeline_recall_fallback.py`

**Steps**

- [ ] **Step 2.1 — Add SpanEvent.**
  ```python
  RECALL_GAP_SCORE = "recall.gap_score"
  ```
  Carries attributes `qualifying: int`, `required: int`, `pitch_sector: str`.

- [ ] **Step 2.2 — Refactor predicate.** Extend `slopmortem/stages/llm_recall.py`:
  ```python
  @dataclass(frozen=True)
  class CoverageGapResult:
      qualifying: int
      required: int

      @property
      def gap(self) -> bool:
          return self.qualifying < self.required


  def compute_coverage_gap(
      *,
      retrieved: list[Candidate],
      ranked: list[ScoredCandidate],
      pitch_sector: str,
      min_similarity_score: float,
      n_synthesize: int,
  ) -> CoverageGapResult:
      # ... existing body, but accumulate `qualifying` and return CoverageGapResult(qualifying, n_synthesize) ...


  def detect_coverage_gap(...) -> bool:
      """Back-compat shim — keep signature; delegate to compute_coverage_gap."""
      return compute_coverage_gap(...).gap
  ```

- [ ] **Step 2.3 — Write failing test.** In `tests/stages/test_coverage_gate.py`:
  ```python
  def test_compute_returns_qualifying_count():
      retrieved = [_candidate(f"c{i}", "crypto_web3") for i in range(3)]
      ranked = [_scored(f"c{i}", 4.5) for i in range(3)]
      result = compute_coverage_gap(
          retrieved=retrieved,
          ranked=ranked,
          pitch_sector="crypto_web3",
          min_similarity_score=4.0,
          n_synthesize=5,
      )
      assert result.qualifying == 3
      assert result.required == 5
      assert result.gap is True
  ```
  Run, expect failure.

- [ ] **Step 2.4 — Implement and re-run.** Test should pass.

- [ ] **Step 2.5 — Update pipeline.run_query** to compute on every query, regardless of `enable_llm_recall`:
  ```python
  # Replace the existing `if config.enable_llm_recall: coverage_gap = detect_coverage_gap(...)` block with:
  gap_result = compute_coverage_gap(
      retrieved=retrieved,
      ranked=reranked.ranked,
      pitch_sector=facets.sector,
      min_similarity_score=config.min_similarity_score,
      n_synthesize=config.N_synthesize,
  )
  coverage_gap = gap_result.gap
  if Laminar.is_initialized():
      Laminar.event(
          name=str(SpanEvent.RECALL_GAP_SCORE),
          attributes={
              "qualifying": str(gap_result.qualifying),
              "required": str(gap_result.required),
              "pitch_sector": facets.sector,
          },
      )
  ```
  Note: Laminar event attribute values are stringly-typed for portability; cast ints to str.

- [ ] **Step 2.6 — Update gate-fired logic** so `gate_fired` still requires `enable_llm_recall AND coverage_gap` — the always-log change is observability only, not behavior.

- [ ] **Step 2.7 — Add pipeline test.** In `tests/test_pipeline_recall_fallback.py`:
  ```python
  async def test_gap_score_event_emitted_when_recall_disabled(monkeypatch, caplog):
      """Gap score event fires on every query, including when enable_llm_recall=False."""
      events = _capture_laminar_events(monkeypatch)
      config = _config(enable_llm_recall=False)
      await run_query(input_ctx=..., config=config, ...)
      gap_events = [e for e in events if e["name"] == "recall.gap_score"]
      assert len(gap_events) == 1
      assert "qualifying" in gap_events[0]["attributes"]
  ```
  Pattern off existing event-capture tests in the file.

- [ ] **Step 2.8 — Run tests + full suite.**
  ```bash
  uv run pytest tests/stages/test_coverage_gate.py tests/test_pipeline_recall_fallback.py -v
  just test
  just typecheck
  ```

- [ ] **Step 2.9 — Commit.** `recall: always log gap score for calibration`.

---

## Task 3 — Down-weight llm_recall at retrieval

**Why this exists.** A vertical that started thin and got llm_recall fills will keep returning those fills even after crawlers eventually cover it. There's no preference signal saying "if a crawler-sourced entry and an llm_recall entry both match, prefer the crawler one." A multiplicative score factor on llm_recall in the FormulaQuery solves this without changing rerank or synth.

**Design notes.**

- The original suggestion's "*only* when ≥k crawler entries match" qualifier is a global property of the result set, not per-doc, so it can't live inside `FormulaQuery`. Always-on demote is correct in steady state: when crawlers are abundant they outrank; when only llm_recall entries exist they're still returned (rerank uses relative ordering, so a uniform score shift doesn't drop them).
- The Qdrant docs surface `MultExpression([constant, Condition])` examples for additive boosts (the existing facet code uses this shape). Multiplying `$score` itself by a per-doc factor needs `$score` *inside* a `MultExpression`. The public prose docs don't demonstrate that composition, but `MultExpression.mult` is typed as `list[Expression]` and `$score` is a variable Expression, so it should compose. **Verify at Step 3.5** by reading `qdrant_client.models.MultExpression`'s typed signature before implementing — if the Python type rejects a string variable in the list, fall back to additive penalty (subtract `(1 - factor) × typical_score` as a constant) and add a one-line note in the code explaining why.

**Files**
- Modify: `slopmortem/config.py`
- Modify: `slopmortem/corpus/_qdrant_store.py`
- Modify: `slopmortem/deps.py` (build_deps wires the factor into QdrantCorpus)
- Test: `tests/corpus/test_qdrant_store.py`

**Steps**

- [ ] **Step 3.1 — Add config knob.**
  ```python
  recall_score_factor: float = Field(default=0.9, ge=0.0, le=1.0)
  ```
  1.0 = no down-weight; 0.0 would zero out llm_recall scores entirely (don't recommend).

- [ ] **Step 3.2 — Extend QdrantCorpus.__init__** to accept and store `recall_score_factor: float = 1.0` (1.0 default keeps existing test setups unaffected).

- [ ] **Step 3.3 — Write failing test (`requires_qdrant` marker).** In `tests/corpus/test_qdrant_store.py`:
  ```python
  @pytest.mark.requires_qdrant
  async def test_llm_recall_score_downweighted(...):
      """Two entries identical except for source — llm_recall scores ~factor × curated."""
      # Seed two entries with identical bodies + facets, source=curated and source=llm_recall.
      # Run a query that matches both equally on dense+sparse.
      # Assert llm_recall.score / curated.score is close to recall_score_factor (within tol).
  ```

- [ ] **Step 3.4 — Run test, expect failure** (sources rank equally today).

- [ ] **Step 3.5 — Verify `$score`-in-`Mult` composition.** Open `qdrant_client.models.MultExpression` (its Pydantic model) and confirm the `mult` field accepts string variables alongside constants and Filters. If yes, proceed with multiplicative; if the type forbids it, fall back to additive penalty and document the fallback inline.

- [ ] **Step 3.6 — Extend FormulaQuery in `QdrantCorpus.query`.** Append a multiplicative term that scales `$score` by `recall_score_factor` when `source=="llm_recall"`. Reading: when source matches, the new term equals `$score × (factor - 1)` (negative); summed with the existing `$score` term gives `$score × factor`. When source doesn't match, the Filter evaluates to 0 and the term contributes nothing.
  ```python
  if self._recall_score_factor < 1.0:
      formula_terms.append(
          MultExpression(
              mult=[
                  "$score",
                  self._recall_score_factor - 1.0,  # negative delta
                  FieldCondition(key="source", match=MatchValue(value="llm_recall")),
              ]
          )
      )
  ```
  Same outer shape as the existing facet boost at `_qdrant_store.py:170-178` — sum of `$score` + per-doc deltas.

- [ ] **Step 3.7 — Plumb through `build_deps`.** In `slopmortem/deps.py`, pass `recall_score_factor=config.recall_score_factor` to QdrantCorpus.

- [ ] **Step 3.8 — Run test, expect pass.**

- [ ] **Step 3.9 — Add unit test for backward compatibility.** Default `recall_score_factor=1.0` produces identical scores to the pre-change code:
  ```python
  def test_recall_score_factor_one_is_neutral():
      # FormulaQuery with factor=1.0 yields the same scores as the pre-change formula.
  ```

- [ ] **Step 3.10 — Run full suite.**

- [ ] **Step 3.11 — Commit.** `retrieve: soft down-weight on llm_recall sources`.

---

## Task 4 — Per-source slop threshold + borderline event

**Why this exists.** `HaikuSlopClassifier` was tuned against HN obituaries and Crunchbase rows. LLM-recall content is shaped differently — Wayback marketing copy, TechCrunch acquisition pieces, founder farewell blogs. The default `slop_threshold=0.7` may reject too many or too few in this distribution. We don't have 100 hand-labeled docs to retune it properly; what we *can* do is add the override knob and log borderline scores so future tuning has data.

**Files**
- Modify: `slopmortem/config.py`
- Modify: `slopmortem/ingest/_ingest.py` (find the threshold gate in `_classify_phase`)
- Modify: `slopmortem/tracing/events.py`
- Test: `tests/ingest/test_orchestration.py`

**Steps**

- [ ] **Step 4.1 — Add config knob.**
  ```python
  recall_slop_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
  ```
  None means "use slop_threshold for all sources."

- [ ] **Step 4.2 — Add SpanEvent.**
  ```python
  RECALL_SLOP_BORDERLINE = "recall.slop_borderline"
  ```
  Carries `slop_score` and `effective_threshold` attributes.

- [ ] **Step 4.3 — Read `_classify_phase`** in `slopmortem/ingest/_ingest.py` to locate the threshold comparison. Confirm it's a single `if score > config.slop_threshold` site or several.

- [ ] **Step 4.4 — Add helper.** In the same file (or `_slop_gate.py` if it lives there cleaner):
  ```python
  def _effective_slop_threshold(entry: RawEntry, config: Config) -> float:
      if entry.source == SOURCE_LLM_RECALL and config.recall_slop_threshold is not None:
          return config.recall_slop_threshold
      return config.slop_threshold
  ```

- [ ] **Step 4.5 — Write failing test (default unchanged).** In `tests/ingest/test_orchestration.py`:
  ```python
  async def test_recall_slop_threshold_default_uses_global():
      """recall_slop_threshold=None → llm_recall entries gated by slop_threshold (default 0.7)."""
      # Seed RawEntry(source="llm_recall") with body that scores 0.6.
      # Default config (recall_slop_threshold=None, slop_threshold=0.7).
      # Entry passes (score 0.6 < threshold 0.7).
  ```
  Run, confirm pass (default behavior).

- [ ] **Step 4.6 — Write failing test (override active).**
  ```python
  async def test_recall_slop_threshold_override_quarantines():
      """recall_slop_threshold=0.5 → llm_recall entry scoring 0.6 quarantines."""
      # Same seed as above.
      # config.recall_slop_threshold=0.5.
      # Entry quarantines (score 0.6 > recall threshold 0.5).
  ```
  Run, expect failure (no override path yet).

- [ ] **Step 4.7 — Replace direct `config.slop_threshold` reads** in `_classify_phase` with `_effective_slop_threshold(entry, config)`. Wire borderline event emission:
  ```python
  effective = _effective_slop_threshold(entry, config)
  if entry.source == SOURCE_LLM_RECALL and 0.4 <= score < effective:
      _emit(SpanEvent.RECALL_SLOP_BORDERLINE, {
          "slop_score": f"{score:.3f}",
          "effective_threshold": f"{effective:.3f}",
      })
  if score > effective:
      # quarantine path
  ```

- [ ] **Step 4.8 — Run tests, expect pass.**

- [ ] **Step 4.9 — Run full suite.**

- [ ] **Step 4.10 — Commit.** `ingest: per-source slop threshold for recall + borderline event`.

---

## Task 5 — Coverage-gap predicate eval expansion

**Why this exists.** Three calibration fixtures (`hacken`, `splunk_ot`, `crypto_web3_sparse`) are not regression coverage. Predicate logic shifts (e.g., adding `pitch_sector="other"` short-circuit) need broader cases to catch unintended interactions. Adding ~12 fixtures + the existing 3 + the inline tests gets to ~20 cases — meaningful coverage without becoming a maintenance burden.

**Files**
- Create: ~12 JSON fixtures in `tests/fixtures/coverage_gate/`
- Modify: `tests/stages/test_coverage_gate.py` — extend parametrize

**Steps**

- [ ] **Step 5.1 — Author 4 "should fire" fixtures** matching real failure shapes:
  - `wrong_sector_high_quality.json` — 5 candidates with mean=6.0 in sector=security, pitch_sector=crypto_web3. Expected: fire (qualifying=0).
  - `mostly_in_sector_low_quality.json` — 5 candidates in-sector but mean=2.0 (under 4.0 threshold). Expected: fire.
  - `borderline_one_qualifying.json` — 1 in-sector with mean=4.5, 4 out-of-sector with mean=8.0. Expected: fire (qualifying=1 < N=5).
  - `mixed_quality_two_qualifying.json` — 2 in-sector mean=5.0, 3 in-sector mean=3.5. Expected: fire (qualifying=2).

- [ ] **Step 5.2 — Author 4 "should not fire" fixtures**:
  - `exact_n_qualifying.json` — exactly 5 in-sector candidates with mean=4.5. Expected: quiet (qualifying=N=5).
  - `over_n_qualifying.json` — 7 in-sector candidates with mean=6.0. Expected: quiet.
  - `pitch_sector_other_quality_pass.json` — pitch_sector=other, 5 candidates mixed sectors all mean=5.0. Expected: quiet (sector check skipped).
  - `sector_other_in_candidates.json` — pitch_sector=fintech, 5 candidates with sector=other and mean=4.5. Expected: quiet (catch-all matches).

- [ ] **Step 5.3 — Author 4 "edge geometry" fixtures**:
  - `ranked_id_not_in_retrieved.json` — rerank emits a candidate_id absent from retrieved. Predicate treats as miss; combined with 4 valid in-sector qualifiers should fire (qualifying=4).
  - `exact_min_similarity_bound.json` — 5 candidates with mean exactly 4.0 (=min_similarity_score). Expected: depends on `<` vs `<=` semantics — pin the current behavior (`<` means 4.0 qualifies).
  - `n_synthesize_one.json` — N_synthesize=1, 1 in-sector qualifying candidate. Expected: quiet. (This requires parameterizing the test on n_synthesize too — acceptable change to the parametrize.)
  - `empty_ranked_nonempty_retrieved.json` — retrieved has 5 entries, ranked is empty. Expected: fire (qualifying=0). Catches the case where rerank failed silently.

- [ ] **Step 5.4 — Extend `test_calibration_fixture` parametrize.**
  ```python
  @pytest.mark.parametrize("name", [
      "hacken", "splunk_ot", "crypto_web3_sparse",
      "wrong_sector_high_quality", "mostly_in_sector_low_quality",
      "borderline_one_qualifying", "mixed_quality_two_qualifying",
      "exact_n_qualifying", "over_n_qualifying",
      "pitch_sector_other_quality_pass", "sector_other_in_candidates",
      "ranked_id_not_in_retrieved", "exact_min_similarity_bound",
      "n_synthesize_one", "empty_ranked_nonempty_retrieved",
  ])
  ```
  If `n_synthesize_one` requires per-fixture overrides, extend the loader to read optional `n_synthesize` and `min_similarity_score` keys from the JSON and thread them into the call.

- [ ] **Step 5.5 — Run.**
  ```bash
  uv run pytest tests/stages/test_coverage_gate.py -v
  ```
  All fixtures pass. If a fixture's expected outcome contradicts the predicate, fix the fixture (the predicate is the spec) — don't tweak the predicate.

- [ ] **Step 5.6 — Run full suite.**

- [ ] **Step 5.7 — Commit.** `stages: expand coverage_gate calibration fixtures`.

---

## Task 6 — Provenance threading

**Why this exists.** Synthesis treats every candidate identically. An llm_recall entry — a company name Opus pulled from training data and we verified against one news article — is weighted the same as a Crunchbase obituary on a known dead company. Synthesis should be told. The user reading the report should see it. Provenance is captured in the journal but never reaches the prompt or the markdown.

**Files**
- Modify: `slopmortem/models.py` — add `source` to `CandidatePayload` and `Synthesis`
- Modify: `slopmortem/ingest/_ingest.py` (or wherever `_build_payload` lives) — populate `source=entry.source`
- Modify: `slopmortem/stages/synthesize.py` — thread to from_llm + prompt kwargs
- Modify: `slopmortem/llm/prompts/synthesize.j2` — surface in trusted facts + add steering line
- Modify: `slopmortem/render.py` — render `[recall: verified]` line on llm_recall candidates
- Test: `tests/stages/test_render.py`, `tests/test_synthesis_tools.py`

**Steps**

- [ ] **Step 6.1 — Find `_build_payload`.** Likely in `slopmortem/ingest/_journal_writes.py` or `slopmortem/ingest/_fan_out.py`. Read it and confirm where `CandidatePayload(...)` is constructed during ingest.

- [ ] **Step 6.2 — Add `source` field to `CandidatePayload`.**
  ```python
  # In CandidatePayload, near verification_tier:
  # Source string per RawEntry.source. None for payloads written before this field
  # was added (qdrant rows persist across schema bumps; default None keeps validation
  # green on legacy rows).
  source: str | None = None
  ```

- [ ] **Step 6.3 — Populate `source` at payload assembly.** In `_build_payload` (Task 6.1), pass `source=entry.source`.

- [ ] **Step 6.4 — Add `source` field to `Synthesis`.**
  ```python
  # In Synthesis:
  source: str | None = None
  ```
  Update `Synthesis.from_llm` signature to accept and pass it through.

- [ ] **Step 6.5 — Update `synthesize()` in `stages/synthesize.py`** to forward `source=candidate.payload.source` into `Synthesis.from_llm`.

- [ ] **Step 6.6 — Update `synthesize_prompt_kwargs`** to include `source=candidate.payload.source or "unknown"`.

- [ ] **Step 6.7 — Update `synthesize.j2`** trusted facts block:
  ```jinja
  Trusted facts (typed pipeline data — prefer these over prose):
  - name: {{ candidate_name }}
  - source: {{ source }}
  ...
  ```
  Add a steering line in the system block, after the existing rules:
  ```
  - If `source` is `llm_recall`, this candidate was named by an LLM from training data and verified against the live web — its similarity to the pitch is less anchored than crawler-sourced candidates. Be conservative in `why_similar`; favor concrete claims that show up in the document body.
  ```

- [ ] **Step 6.8 — Write failing render test.** In `tests/stages/test_render.py`:
  ```python
  def test_render_marks_recall_provenance():
      syn = Synthesis(..., source="llm_recall", ...)
      out = _render_candidate(syn)
      assert "recall" in out.lower()  # tag visible in the candidate section
  ```
  Run, expect failure.

- [ ] **Step 6.9 — Update `_render_candidate` in `render.py`** to insert a tag line right after the `## {syn.name}` header when `syn.source == "llm_recall"`:
  ```python
  parts: list[str] = [f"## {syn.name}"]
  if syn.source == "llm_recall":
      parts.append("*Source: LLM recall (verified against live web)*")
  parts.extend(["", _strip_markdown_links(syn.one_liner), ...])
  ```

- [ ] **Step 6.10 — Run render test, expect pass.**

- [ ] **Step 6.11 — Add a test that the synth prompt receives source.** In `tests/test_synthesis_tools.py` or wherever prompt_kwargs is tested:
  ```python
  def test_synthesize_prompt_kwargs_includes_source():
      cand = _candidate_with_source("llm_recall")
      kwargs = synthesize_prompt_kwargs(cand, pitch="x")
      assert kwargs["source"] == "llm_recall"
  ```

- [ ] **Step 6.12 — Run full suite + typecheck.**

- [ ] **Step 6.13 — Commit.** `synthesize: thread source provenance to prompt + render`.

---

## Cross-cutting checks (after all tasks)

- [ ] `just lint` clean
- [ ] `just typecheck` clean (basedpyright strict)
- [ ] `UV_CACHE_DIR=/tmp/uv-cache uv run lint-imports` — 7/7 contracts kept
- [ ] `just test` clean (offline-friendly cassettes; no live API)
- [ ] `just eval` clean (cassette replay; no recording needed for these changes since they don't modify pinned models)

If any pinned model id moved, re-record cassettes — but none of these tasks change `model_*` defaults, so this should not be needed.

## What's out of scope

- **Skip the second retrieve+rerank when persisted_count is small** (#6 from the suggestion list). User explicitly requested keeping the whole flow.
- **TTL re-verify on llm_recall entries** (#7). Deferred — needs schema migration on the journal.
- **A/B comparison in cassette mode** (#10). Deferred — needs eval-runner extension.

These three are documented here so the next iteration knows what's still on the table.

## Notes for the implementer

- Each task is one commit. Use the project's terse commit style (`recall: ...`, `retrieve: ...`, etc.). No `Co-Authored-By` trailers.
- Don't bump pinned models. None of these tasks need to.
- `slopmortem.toml` defaults are public surface; if a task adds a config key, document it there too. Personal overrides go in `slopmortem.local.toml` (gitignored).
- L5 LLM call counts against the per-query budget. Verify the budget tracker sees it (it should, via the shared `Budget` injected into the LLMClient).
- All new SpanEvents need to be added to the closed StrEnum in `tracing/events.py` — the tracing layer rejects free-form strings.
