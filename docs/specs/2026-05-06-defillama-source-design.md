# DefiLlama Source — Design

**Status:** approved-pending-review
**Date:** 2026-05-06
**Branch context:** `expand-sources` — sibling of the Tavily News source
spec (`docs/specs/2026-05-06-tavily-news-source-design.md`) and its
implementation plan (`docs/plans/2026-05-06-defillama-source.md`).

## Goal

Add a `Source` adapter that consumes DefiLlama's `/protocols` and
`/protocol/{slug}` JSON endpoints, identifies dead/zombie protocols via
peak-relative TVL trajectory, anchors each entry's URL to a peak-era
Wayback snapshot, and emits a `RawEntry` whose body is filled by the
existing `TavilyEnricher` from the original (live-era) pitch text.
Fills the crypto-native long-tail gap that the existing curated YAML,
HN Algolia, and Crunchbase CSV sources miss — without depending on RSS
feeds whose tail-window primitive can't reach historical data.

**Coverage caveat — empirically grounded.** A live probe on 2026-05-06
against `/protocols` returned **7,451 protocols total**, of which
**1,621** sit in the shortlist band (`0 < tvl < $100K`). Within a
random 9-candidate sample, 5 classified `dead`, 4 classified
`never_launched` (peak below the $1M floor) — zero `alive` /
`too_early`. Of the 5 dead, 4 had Wayback coverage and 1 didn't
(`dexfinance-bsc`, no peak-era snapshots). Extrapolated yield: ~727
emit-eligible candidates from the full shortlist; capped at the default
`max_emit=300` per run. Real numbers will drift as protocols come and
go — re-probe before tuning thresholds.

**Recall vs depth caveat.** This source produces **structured death
signals** (peak/current TVL, peak date, ratio, days since peak) plus a
peak-era pitch URL — but the pitch body quality is bimodal. Validated:

- **Content-rich snapshots** (Primitive Finance,
  `https://web.archive.org/web/20220525072306/https://primitive.xyz/`)
  yield real pitch text — *"Derivatives Without Counterparties. Earn
  fees without lockups. Other derivative platforms lock collateral
  until maturity. On Primitive, collateral and earned yield can be
  redeemed on demand."* — exactly the corpus shape the synthesize
  stage needs.
- **SPA snapshots** (`valasfinance.com`) render to ~20 chars of
  static text — Tavily extraction returns nearly nothing usable.

The source's `seed_markdown` (name, category, chain, peak/current TVL,
peak date, DefiLlama's own one-sentence description) carries the entry
when Tavily extraction is thin; this is load-bearing, not a
fallback. The slop classifier and embeddings get something to work with
either way.

## Architecture

One module under `slopmortem/corpus/sources/` implementing the existing
`Source` Protocol (`fetch() -> AsyncIterable[RawEntry]`). Two helpers
live in the same module: `classify_death(chain_tvls)` decides
dead/alive/never-launched/too-early from the merged TVL series, and
`wayback_snapshot_near(url, target_date)` resolves the live URL to a
peak-era `web.archive.org` snapshot via the CDX API. The adapter
routes through `safe_get`, `respect_robots`, and `throttle_for` so the
SSRF guard, robots policy, and per-host token bucket apply uniformly.

The source's emitted `RawEntry.url` points at the Wayback snapshot, so
the existing `TavilyEnricher` extracts the original pitch body without
any new enricher. Wired into `_ingest_cmd.py` behind an opt-in
`--enable-defillama` flag, which **implies** `--tavily-enrich` and
fails at startup if `TAVILY_API_KEY` is unset (without Tavily, this
source produces empty entries — Wayback HTML alone is unusable). The
default `just ingest` remains bit-identical to current behaviour.

## Tech Stack

Python 3.13, `anyio`, `httpx` (via `safe_get`), `pydantic` v2 (for
`RawEntry`), `pytest` + `pytest-recording` (vcrpy cassettes),
`basedpyright` strict.

## Execution Strategy

**Subagents** — default; no spec override. Two sequential tasks: source
adapter + helpers + tests, then CLI wiring + reliability rank +
exports. Task 2 imports the class produced by Task 1, so they cannot
run in parallel.

## Task Dependency Graph

- Task 1 [AFK]: DefiLlama source + helpers + tests → depends on `none` → batch 1
- Task 2 [AFK]: CLI wiring + reliability rank + exports → depends on `Task 1` → batch 2

## Agent Assignments

- Task 1: Source + helpers + tests + re-exports → python-development:python-pro
- Task 2: CLI wiring + reliability rank + exports → python-development:python-pro
- Polish: post-implementation-polish → general-purpose

---

## File Structure

**New:**

- `slopmortem/corpus/sources/defillama.py` — `DefiLlamaSource` plus two
  module-level helpers (`classify_death`, `wayback_snapshot_near`).

**Modified:**

- `slopmortem/corpus/sources/_names.py` — add
  `SOURCE_DEFILLAMA: Final = "defillama"` alongside the existing
  source-name constants. The literal string `"defillama"` is the
  identifier used in `RawEntry.source`, the
  `_RELIABILITY_RANK` key, and the `--only-source` argument once the
  Tavily News spec's generic flag lands.
- `slopmortem/corpus/sources/__init__.py` — re-export `DefiLlamaSource`.
- `slopmortem/cli/_ingest_cmd.py` — add `--enable-defillama` (no
  per-source CLI override flags; thresholds are constructor kwargs
  only); thread it into `_run_ingest`; assert `TAVILY_API_KEY` is set
  when the source is enabled and silently flip `tavily_enrich = True`.
- `slopmortem/ingest/_helpers.py` — extend `_RELIABILITY_RANK` with
  `defillama → 3`. Slots between `crunchbase_csv` (rank 2) and the
  dead-letter rank 9. Coordination caveat: if the Tavily News spec
  lands first, that source claims rank 4; rank 3 stays open for
  DefiLlama.

**New tests:**

- `tests/sources/test_defillama.py` — pure-fn tests for
  `classify_death` (dead, alive, never_launched, too_early,
  recently_dipped, dedupes_intra_day_duplicates) and
  `wayback_snapshot_near` (closest 200 picked, returns None on no 200,
  widens window on miss); mocked-`safe_get` integration tests for the
  full `fetch()` flow (emits dead with Wayback URL, skips dead without
  coverage, max_emit cap); cassette round-trip test (skipped without
  `RECORD=1`).

**Modified tests:**

- `tests/ingest/test_reliability_rank.py` — add `(SOURCE_DEFILLAMA, 3)`
  parametrize case.
- `tests/test_cli_ingest.py` — add a regression test asserting that
  `--enable-defillama` without `TAVILY_API_KEY` exits non-zero with a
  message naming the env var.

## Pipeline (`fetch()`)

1. **Bulk fetch.** GET `https://api.llama.fi/protocols` (one call). The
   live response is a JSON array of dicts with fields: `id`, `name`,
   `slug`, `url`, `description`, `chain`, `category`, `tvl`,
   `listedAt`, plus a `chainTvls` summary object that's not used here.
2. **Shortlist.** Keep rows with
   `0 < tvl < shortlist_tvl_ceiling_usd` (default `$100K`) AND a
   non-empty `slug` AND a non-empty `url`. The ceiling intentionally
   sits above the typical zombie tail (validated: Primitive sits at
   $58K = 3.4% of its $1.72M peak — a $10K floor would have missed
   it). Live 2026-05-06: 1,621 of 7,451 protocols pass.
3. **Per-candidate detail.** For each shortlisted row, GET
   `https://api.llama.fi/protocol/{slug}` and read `chainTvls`. Live
   shape: `chainTvls[<chain>]` is a *dict* with keys `tvl`,
   `tokensInUsd`, `tokens`; the daily series lives at
   `chainTvls[<chain>]["tvl"]` as `[{date, totalLiquidityUSD}, ...]`,
   not a bare list. **Schema misread is a one-line bug that silently
   yields zero entries** — `_merge_chain_series` must descend into
   `["tvl"]`. Within a chain, dedupe on `date` with last-write-wins
   (DefiLlama can ship multiple intra-day points for "today";
   Primitive's 2026-05-06 series shipped two end-of-day rows, $58,678
   and $58,168 — naive summing inflates current to $117K and trips the
   5%-of-peak threshold backwards).
4. **Classify.** `classify_death(chain_tvls, threshold_pct=0.05,
   peak_floor_usd=$1M, min_days_since_peak=180)` returns one of
   `dead`, `alive`, `never_launched`, `too_early`, `unknown`. Dead
   requires **all four**: peak ≥ $1M floor, days-since-peak ≥ 180,
   current TVL ≤ 5% of peak, AND trailing-90-day mean ≤ 5% of peak
   (the last condition rules out "recently dipped" candidates).
5. **Resolve Wayback.** For dead candidates,
   `wayback_snapshot_near(live_url, peak_date)` queries the Wayback
   CDX API (`https://web.archive.org/cdx/search/cdx?url=...&from=...&to=...`)
   in a ±30-day window around peak (widens to ±180 days on miss),
   filters `statuscode == "200"`, picks the snapshot closest to
   `peak_date`. Skip the candidate entirely if no usable snapshot
   exists. Wayback CDX is intermittently flaky (504s, 503s, read
   timeouts in live testing); the narrow→wide window fallback is the
   only retry mechanism — observed ~20% loss rate on dead candidates.
6. **Emit.** Yield one `RawEntry` per surviving candidate with:
   - `source = "defillama"`
   - `source_id = slug`
   - `url = wayback_snapshot_url` (peak-era 200-status snapshot)
   - `markdown_text` = seed text carrying name / category / chain /
     peak-tvl / peak-date / current-tvl / DefiLlama's `description`
     (so the row is non-empty even if Tavily later extracts nothing
     useful from an SPA snapshot)
   - `raw_html = None`
   - `fetched_at = datetime.now(UTC)`
7. **Cap.** `max_emit` (default `300`) bounds per-run yield. Each
   emitted entry costs ~$0.005 Tavily extract + ~$0.0008 Haiku slop
   call (~$0.006/entry). Default 300 → ~$1.75 per run. Detail-call
   volume is bounded by the same cap: at observed ~50% dead-rate in
   the shortlist, ~600 detail calls fill 300 emissions before
   short-circuit.

`fetch()` is structured as `bulk → shortlist → for each: (detail,
classify, wayback?, yield)` — yields lazily, not collect-then-rank.
DefiLlama imposes no scoring, so emission order matches shortlist
order; cassette stability is preserved by the deterministic shortlist
sort (TVL ascending — deepest zombies first).

## CLI Surface

### `--enable-defillama`

Boolean opt-in. Default `False`. When set:

- Asserts `TAVILY_API_KEY` is exported. If missing, raises
  `typer.BadParameter` with a message naming both the missing env var
  AND the implicating flag. Reads via
  `os.environ.get("TAVILY_API_KEY", "")` inside the wiring code (matches
  the `TavilyEnricher` and `_tools_impl._tavily_api_key()` pattern);
  `slopmortem/config.py`'s `tavily_api_key: SecretStr` is not threaded
  through.
- **Implies `--tavily-enrich`.** Silently flips `tavily_enrich = True`
  inside `_run_ingest` before the source list is built. Without
  Tavily extraction, Wayback HTML is unusable raw input; the slop
  classifier and embeddings would both choke on it.
- Appends `DefiLlamaSource()` to the source list at the end.
  Insertion position relative to Tavily News (`--enable-tavily-news`)
  is irrelevant because both are opt-in: the existing list is
  `[Curated, HN, Crunchbase?, DefiLlama?, TavilyNews?]` where each
  `?`-marked source appears only when its enable-flag is set.

### Per-source CLI overrides

**None.** All death-classifier knobs (`dead_threshold_pct`,
`peak_floor_usd`, `min_days_since_peak`, `shortlist_tvl_ceiling_usd`,
`max_emit`) live as constructor kwargs only. CLI exposure can wait
until an operator wants to tune them at runtime — defer until that
need lands.

### Standalone runs via `--only-source defillama`

Once the Tavily News spec's generic `--only-source NAME` flag lands,
`slopmortem ingest --only-source defillama` Just Works (auto-enables
the flag, filters the source list to one entry). No additional wiring
required from this spec — just register `defillama` in the
`_SOURCE_REGISTRY` table that Tavily News introduces.

Until that flag exists, the standalone workaround is to (a) empty the
curated YAML for the run and (b) skip `--crunchbase-csv`. Curated and
HN both run unconditionally, so DefiLlama won't be the *only* source
without intervention. Acceptable as a temporary state — Tavily News
spec ships first or alongside.

## Error Handling

Per CLAUDE.md ("don't add bare `except Exception: pass`", "per-entry
failures log and continue, run-level failures short-circuit"):

- **Per-detail failures** — HTTP 4xx/5xx, timeout, JSON parse error,
  payload not a dict, `chainTvls` missing or wrong type. Logged at
  WARN with the slug, then dropped.
- **Per-Wayback failures** — narrow CDX window 5xx → continue to wide
  window; wide window 5xx → log INFO and skip the candidate. No
  per-source retry decorator (project rule: retry policy lives in the
  LLM client). The narrow→wide cascade *is* the retry mechanism.
- **Per-classify failures** — verdict `unknown` (empty series) or
  `never_launched` / `alive` / `too_early`: logged at INFO, skipped.
  `current_tvl=0` is **not** a failure — it's the canonical dead
  signal.
- **Run-level failures** — only `TAVILY_API_KEY` missing at startup
  (`typer.BadParameter`). No mid-run abort even if every Wayback call
  fails — the source just yields nothing and logs a single WARN
  (`defillama: 0 entries after wayback resolution`).

`gather_resilient` is **not used** here. The shape is sequential
(bulk → for-each-shortlisted: detail → classify → wayback →
yield), not fan-out. Per-candidate independence is enforced by the
loop structure, not by a fan-out primitive.

## Tracing

`fetch()` is wrapped with
`@observe(name="defillama.fetch", ignore_inputs=[], ignore_output=True)`.
Per-candidate attributes (`slug`, `chain`, `category`, `peak_tvl`,
`peak_date`, `current_tvl`, `ratio_pct`, `verdict`, `snap_offset_days`)
emit as custom span events via `slopmortem.tracing.events.SpanEvent`.
No API key to redact — DefiLlama and Wayback both unauthenticated.

## Testing

| Test | What it asserts |
|------|------------------|
| `test_classify_death_dead_zombie` | Primitive-shaped series (peak $1.7M 2022-05-31, current $58K) → `dead` |
| `test_classify_death_alive` | Constant healthy TVL → `alive` |
| `test_classify_death_never_launched` | Peak below $1M floor → `never_launched` |
| `test_classify_death_too_early` | Peak last week → `too_early` |
| `test_classify_death_recently_dipped` | One-day blip to $0 with healthy 90-day mean → not `dead` |
| `test_classify_death_dedupes_intra_day_duplicates` | Two same-date points keep last value, not summed (locks in the Primitive 2026-05-06 regression) |
| `test_wayback_picks_closest_200` | Multiple snapshots → closest to peak_date with `statuscode=200` wins; non-200 ignored |
| `test_wayback_returns_none_when_no_200` | Only non-200 rows → `None` |
| `test_emits_dead_protocol_with_wayback_url` | End-to-end happy path: bulk → shortlist → detail → classify → wayback → `RawEntry.url` is the snapshot URL, not the live URL |
| `test_skips_when_no_wayback_coverage` | Dead candidate with empty CDX response → 0 entries |
| `test_max_emit_caps_yield` | 10 dead candidates available, `max_emit=3` → exactly 3 emitted |
| `test_enable_defillama_without_tavily_key_fails` | `--enable-defillama` without `TAVILY_API_KEY` → exit ≠ 0, output mentions the env var AND the flag |
| `test_reliability_rank` | `(SOURCE_DEFILLAMA, 3)` |
| `test_defillama_round_trip` (cassette, `pytest.mark.vcr`) | One real run recorded under `tests/sources/cassettes/test_defillama/test_defillama_round_trip.yaml` with `max_emit=5`. Skip-without-cassette pattern: `if not CASSETTE_FILE.exists() and not os.environ.get("RECORD"): pytest.skip(...)`. |

Test fixtures use a `_series` helper that wraps daily points under
`{"tvl": [...]}` so they match the live `chainTvls[<chain>]` shape.
Naive `{"Ethereum": [...]}` fixtures pass against the broken
production code and fail against the correct one — that mistake
shipped in an early plan draft and was caught only by live API
validation. Spec-level reminder: the chain payload is a dict, not
a list.

## Pros and Cons of Key Decisions

**Peak-relative death detection vs raw TVL floor:**

- Pros of peak-relative: catches zombies that sit well above zero.
  Validated: Primitive Finance at $58K current / $1.72M peak — a raw
  $10K or $50K floor misses it; a 5%-of-peak rule catches it cleanly.
- Pros of raw floor: one bulk API call, no per-candidate fan-out,
  simpler.
- Cons of peak-relative: requires per-candidate `/protocol/{slug}`
  calls. Live 2026-05-06 cost: up to ~600 detail calls per run before
  `max_emit=300` short-circuits. Free unmetered endpoint.
- **Pick peak-relative.** The point of this source is to surface dead
  protocols; a detection rule that misses zombies (the dominant
  failure mode in DeFi) defeats the purpose.

**Internal Wayback resolution vs the existing optional `WaybackEnricher`:**

- Pros of internal: `RawEntry.url` already points at the snapshot, so
  `TavilyEnricher` extracts the right page in one pass; the source's
  contract becomes "yields entries that are immediately usable"
  without runtime flag dances.
- Pros of external: keeps source modules thin; one place handles
  archive resolution.
- Cons of external: the existing `WaybackEnricher` runs uniformly on
  every entry; for DefiLlama specifically we need the snapshot anchored
  to the *peak TVL date*, which only the source has access to (peak
  comes from `chainTvls`). Generalising that into the enricher leaks
  DefiLlama-specific semantics upward.
- **Pick internal.** The Wayback target date is a DefiLlama-specific
  signal; resolution belongs in the source. Validated: peak-era
  snapshots return real pitch text; end-of-life snapshots return
  death notices that aren't useful pitch corpus.

**Tavily implication and key check:**

- Pros: catches misconfiguration at startup instead of producing 300
  silent SPA-degraded entries.
- Cons: couples two CLI flags; operators who deliberately want
  source-without-body can't get it.
- **Pick implication + assertion.** No realistic use case for
  "DefiLlama without Tavily" — Wayback HTML pre-extraction is
  unreadable. Error message names both the missing env var and the
  implicating flag.

**Shortlist TVL ceiling (default $100K):**

- Pros: drastically reduces per-protocol detail calls (only candidates
  plausibly near the dead threshold get fetched). Live ratio: 1,621
  shortlist of 7,451 total = 22% kept.
- Cons: misses "10%-of-peak" deaths where current TVL is high in
  absolute terms (e.g. $5M current / $50M peak). Those exist but are
  rarer and more contentious to call dead.
- **$100K ceiling.** Captures all clean zombies validated so far
  (Primitive at $58K) without scanning the long alive tail.
  Constructor kwarg, tunable later if a future spec wants to widen.

**Last-write-wins dedupe on duplicate timestamps:**

- Pros of last-write-wins: matches DefiLlama's apparent intent (later
  intra-day point overrides earlier estimate); keeps `current_tvl`
  honest; passes the Primitive 2026-05-06 regression.
- Pros of summing: simpler one-liner.
- Pros of first-write-wins: mathematically same as last-write-wins
  modulo ordering; ordering is unstable.
- **Pick last-write-wins.** The bug mode it prevents (Primitive
  classifying as `alive` because $58K + $58K > 5% threshold) is
  silent and load-bearing. Lock in via the
  `test_classify_death_dedupes_intra_day_duplicates` regression test.

**`max_emit=300` default vs higher / unbounded:**

- Pros of 300: bounds Tavily + Haiku spend at ~$1.75 per run;
  matches the "first useful chunk" principle from the project's eval
  budget rules; keeps cassettes shootable.
- Pros of higher: the shortlist supports ~727 emit-eligible
  candidates; raising the cap to 1000 captures the full long tail
  per run.
- Cons of higher: 3-5× cost without commensurate corpus gain
  (diminishing returns past the head of the dead-zombie distribution).
- **Pick 300.** Constructor kwarg; an operator wanting the full
  long-tail bumps it without code change.

**Opt-in CLI flag vs always-on:**

- Pros of opt-in: existing `just ingest` output stays bit-identical;
  cassette/eval baselines don't shift; new source doesn't quietly add
  Tavily-credit cost; matches Tavily News and the project's
  cassette-discipline rule from `CLAUDE.md`.
- Pros of always-on: zero ops friction, works out of the box.
- Cons of always-on: silently bumps Tavily credit consumption; users
  who happen to have `TAVILY_API_KEY` set for the existing enricher
  get DefiLlama costs they didn't consent to.
- **Pick opt-in.** Same logic as `--enable-tavily-news`.

**Reliability rank `3` vs `2` vs `4`:**

- Pros of `3`: on-chain TVL is primary data — more reliable than
  derived reporting (Tavily News at 4) but less curated than
  hand-vetted post-mortems (Curated 0).
- Pros of `2`: TVL is mechanically observable; arguably as reliable
  as Crunchbase's batch CSV.
- Pros of `4`: TVL spikes can mislead (wash trading, reflexive
  bridges); maybe news reporting is better-attributed.
- **Pick `3`.** Slots cleanly: Curated 0, HN 1, Crunchbase CSV 2,
  DefiLlama 3, Tavily News 4, dead-letter 9.
  **Coordination caveat:** if Tavily News ships first and claims
  rank 3, bump DefiLlama up to keep primary-data sources ahead of
  derived reporting.

---

## Out of Scope

- **Per-source budget caps.** Adding spend limits or rate caps per
  adapter requires touching `slopmortem/budget.py`. Defer until a
  real overrun lands. The `max_emit` cap is a yield-count proxy, not
  a true budget.
- **Pivot detection.** Some protocols don't die outright — they pivot
  (Opyn v1 → Squeeth) or merge (Ribbon → Aevo). `classify_death` will
  mark these `alive` because TVL legitimately moved or stayed under a
  new product wrapping the same contracts. Surfacing these as "the
  original product is dead" requires either an LLM check on Wayback
  homepage diffs or a `curated.py` override list. Defer to either a
  curated override or a future spec that adds a homepage-diff
  classifier.
- **Death-classifier tunables surfaced as CLI flags.**
  `dead_threshold_pct`, `peak_floor_usd`, `min_days_since_peak`, and
  `shortlist_tvl_ceiling_usd` are constructor kwargs only. CLI
  exposure can wait until an operator actually wants to tune them at
  runtime.
- **Wayback CDX retry decorator.** Observed ~20% loss rate on dead
  candidates due to CDX flakiness (504s, 503s, read timeouts). Adding
  per-source retry would violate the project rule that retry policy
  lives in the LLM client. The narrow→wide window fallback is the
  intentional retry mechanism. Revisit if loss rate climbs above
  ~50% sustained.
- **SPA snapshot rendering.** Some snapshots (`valasfinance.com`)
  render to nearly empty static HTML because the page is a JS-driven
  SPA. The seed `markdown_text` carries DefiLlama's `description`
  field as a backstop, which is sufficient for slop classification
  and embedding. Extracting JS-rendered content would require a
  headless browser hop — out of scope here.
- **RSS-based crypto sources** (rekt.news, web3isgoinggreat.com).
  Deferred indefinitely: RSS exposes only a publisher-controlled
  tail window with no pagination, so one-shot yield is tiny and
  historical reach is zero. Worth revisiting only if/when scheduled
  ingest lands and the project decides to accrete recent items over
  months.
- **Re-recording the eval cassettes.** The new source expands the
  corpus, but the eval runner is gated by curated post-mortems plus
  the existing two sources (per `docs/cassettes.md`). Eval cassettes
  don't need re-recording for this change. Verify with `just eval`
  after Task 2 — divergence is a separate plan.
- **Updating `docs/architecture.md`.** Add a one-line note pointing to
  this spec once it lands. Full re-write isn't warranted.
- **`--only-source defillama` flag.** Belongs to the Tavily News spec
  as a generic mechanism. Once that lands, DefiLlama gets standalone
  invocation for free; spec'ing it here would duplicate work and
  fragment ownership.
