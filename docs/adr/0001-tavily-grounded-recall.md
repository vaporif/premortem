# ADR 0001: Tavily-Grounded Recall Over Opus Parametric

## Status

Accepted — 2026-05-08

## Context

The recall fallback stage fires when the existing corpus has no usable comparables for a pitch's vertical (canonical fixture: the Hacken/Extractor pitch in vendor-side Web3 security, where retrieval surfaces general cyber-threat-intel deaths like Norse Corp and Carbon Black instead of Web3-native firms like Hexagate, CipherTrace, Coinfirm, Halborn).

The original design (`docs/specs/2026-05-08-llm-recall-fallback-design.md`, first draft) used a single Opus call to name candidate comparables from the model's training memory, then verified each against URL HEAD + Wayback snapshot.

Stated user priority: precision > recall > cost; latency unbounded. Net new false positives going into the corpus must round to zero — corpus entries are persisted permanently and there is no eviction tooling (a `--purge-source` mechanism does not exist; emergency recovery is `slopmortem nuke`).

Single-shot Opus has four observable failure modes against this priority:

1. **Recall ceiling.** Opus reliably names 5–10 vendors per niche; the long-tail (regional, smaller, older) is missed. Long-tail is exactly the population the recall feature exists to surface.
2. **Temporal blind spot.** Failures within ~6–18 months of the model cutoff are weakly represented in training memory.
3. **Vertical framing bias.** "Web3 security" reduces to "smart contract auditors"; adjacent sub-verticals (on-chain monitoring, key management, AML/compliance, insurance) are missed unless the prompt forces fan-out.
4. **Hallucinated citation surface.** Even with a verifier downstream, Opus frequently emits plausible-but-fabricated `homepage_url` and `evidence_url` values. The verifier catches them, but Opus is the most expensive model in the family, so each dropped suggestion costs the most money to drop.

## Decision

Replace single-shot Opus with Tavily-grounded extraction:

1. Build 3–4 templated Tavily search queries from the pitch's facets (`sector`, `sub_sector`, `product_type`).
2. Send them in parallel to Tavily with `include_raw_content=True`. Merge and dedupe results by canonical URL.
3. Send merged results to Sonnet for extraction. Sonnet returns `RecallSuggestion[]` where each suggestion cites one merged-result URL and a literal `evidence_quote` from that result's body.
4. Verify suggestions at three deterministic layers (schema → URL HEAD → quote substring-anchored in body). The literal quote requirement is load-bearing: it forces the extractor to ground claims in concrete article passages, not summaries.
5. Persist verified suggestions through the existing ingest tail (slop classifier → facet extractor → journal → qdrant). Trust downstream stages (slop, rerank, synthesise) to handle residual precision filtering.

Tavily-grounded means the **web** is the recall corpus, not a single model's parametric memory.

## Consequences

### Positive

- Recall ceiling collapses. Tavily indexes long-tail companies that aren't in any single model's parametric memory.
- Temporal blind spots eliminated. Tavily indexes current web.
- `evidence_url` is a real article from search results (not an LLM-invented URL). L3 hallucination rate drops to near-zero, freeing the verifier to focus on quote-anchoring (a stronger signal).
- Sonnet (extraction) is roughly 10× cheaper than Opus (parametric recall). Per-gap-firing cost drops from ~$0.10 to ~$0.02–0.05.
- Vertical framing bias is mitigated by the 3–4 query fan-out: each query templates over a different facet axis (`sub_sector`, `product_type`, `sector`).

### Negative

- Adds dependency on the Tavily API. Project already gates Tavily behind an API key (`enable_tavily_synthesis`, `enable_pitch_filler` already require it); the recall path inherits the same gate.
- One additional pipeline step (search → extract) compared to single-shot. Search calls run in parallel so wall-clock impact is small.
- Tavily coverage of niche verticals is variable. When Tavily returns nothing relevant, extraction returns empty and the report flags `coverage_gap=True`. Some pitches will surface no recall hits even after the gate fires.
- Query template tuning becomes load-bearing for recall quality. A bad query template silently degrades recall on the canonical fixture without breaking any test.

### Neutral

- The verifier's role narrows from "defend against hallucination" to "anchor extraction in cited evidence." Three deterministic layers (schema, URL HEAD, quote substring) are sufficient because the evidence is grounded.
- The slop classifier and rerank scoring already provide downstream precision filtering for any source. Recall entries flow through the same path, no parallel quality stack required.

## Alternatives Considered

### Single-shot Opus from training memory

The original design. Rejected for the four failure modes listed in Context above. The verifier was paying to catch hallucinations that grounding would prevent in the first place; net cost was higher and recall ceiling was a hard cap.

### Multi-framing Opus fan-out

Issue 3–4 Opus calls with different sub-vertical framings; dedupe by name. Considered as a cheap diversity boost over single-shot.

Rejected because it doesn't solve the recall ceiling (still parametric) or the hallucination surface (still no grounded `evidence_url`), and pays roughly 3× the per-firing cost of Tavily-grounded for marginal recall lift.

### Hybrid: Tavily-first, Opus-fallback when Tavily yields nothing

Considered as a recall-completeness measure for niches Tavily indexes poorly.

Rejected because the Opus fallback would re-introduce the precision risk this decision exists to eliminate. The user's explicit priority is precision over recall; having a fallback that occasionally persists hallucinated-but-verified entries trades exactly the wrong way. If empirical recall shows too many gaps where Tavily returns zero, revisit in v2.

### Iterative recall (multi-pass with negative examples)

Run Opus, verify, then run again excluding names already found. Considered as a recall expansion.

Rejected because it compounds hallucination risk across passes (each pass weakens the calibration for what counts as "in-vertical") and breaks the deterministic-budget property of single-pass.

## References

- Spec: `docs/specs/2026-05-08-llm-recall-fallback-design.md`
- Original (now-stale) plan: `docs/plans/2026-05-08-llm-recall-fallback.md`
- Project conventions: `CLAUDE.md` (precision priority, fakes-over-mocks, journal idempotency, ingest tail invariants)
