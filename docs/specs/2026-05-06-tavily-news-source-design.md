# Tavily News Source — Design

**Status:** approved-pending-review
**Date:** 2026-05-06
**Branch context:** `expand-sources` — sibling of the DefiLlama source plan
(`docs/plans/2026-05-06-defillama-source.md`).

## Goal

Add a `Source` adapter that pulls dead/struggling-startup news articles from
Tavily's `/search` endpoint with `topic=news` and explicit `start_date` /
`end_date` windows. The source covers **rolling current-window** named
shutdown events (Lilium, Cruise, Northvolt, Bench, Fisker, First Mode, …)
that the existing curated YAML, HN Algolia, and Crunchbase CSV sources miss
because those events were too late for Crunchbase's batch CSV cadence or
never made it into the Show-HN narrative cluster. Each emitted `RawEntry`
already carries the article body in `markdown_text` (Tavily returns
`raw_content` inside the search response), so no Wayback hop and no
`TavilyEnricher` hop are required.

**Coverage caveat — empirically grounded.** A 200-call probe on
2026-05-06 confirmed Tavily's news index is *effectively empty* before
2024 for these shutdown-verb queries: every quarterly window from
2020-Q1 through 2023-Q4 returned only score≤0.07 noise (industry
trade-press bulletins, unrelated Reuters/Vogue/Kotaku content) with
zero entries crossing `min_score=0.3`. Tavily honors the date filter;
it just has no matching corpus for older windows. The source's
`year_range` therefore floors at 2024 and rolls forward with the
calendar — historical long-tail discovery is a different problem and
out of scope for this adapter.

**Recall vs depth caveat.** This source produces **named-event recall**
(many distinct shutdowns surfaced per run) at the cost of **shallow
pitch density**: news articles describe the failure event, not the
company's original pitch. For household-name shutdowns (Spirit
Airlines, Cruise) the journalist assumes the reader knows what the
company did and summarises the thesis in a parenthetical at best; for
lesser-known startups (Bench, First Mode, Lilium) the model is usually
paragraph-summarised. Synthesize / consolidate_risks output from this
source will therefore be thinner than from curated post-mortems —
complementary to, not a replacement for, the curated YAML. The slop
classifier is also load-bearing here: queries like
`"startup files chapter 11"` happily surface non-startups (Spirit
Airlines is a 1980-founded public company), and the source does not
attempt to filter on startup-ness.

A second piece of work falls out of "I want to run only this source": a
generic `--only-source NAME` CLI flag that filters the constructed source
list down to a single named source and auto-enables it. Designed as a
generic mechanism so future sources (DefiLlama included) get the same
treatment for free.

## Architecture

One module under `slopmortem/corpus/sources/` implementing the existing
`Source` Protocol (`fetch() -> AsyncIterable[RawEntry]`). Configuration
lives in YAML alongside the module so operators can tune queries, year
windows, and thresholds without redeploying Python. The adapter routes
each Tavily call through `safe_post` (SSRF guard) and `throttle_for`
(per-host token bucket); fan-out across `(query, year, quarter)` triples uses
`gather_resilient` with an `anyio.CapacityLimiter` so one bad query never
takes down siblings. Wired into `_ingest_cmd.py` behind an opt-in
`--enable-tavily-news` flag plus the new generic `--only-source NAME`
filter; `--enable-tavily-news` fails at startup if `TAVILY_API_KEY` is
unset because every result depends on a Tavily call. The default
`just ingest` stays bit-identical to current behaviour.

## Tech Stack

Python 3.13, `anyio`, `httpx` (via `safe_post`), `pydantic` v2 (for
`RawEntry`), `pyyaml` for the queries file, `pytest` +
`pytest-recording` (vcrpy cassettes), `basedpyright` strict.

## Execution Strategy

**Subagents** — default; no spec override. Two sequential tasks: source
+ exports + YAML + tests, then CLI wiring + `--only-source` + reliability
rank. Task 2 imports the class produced by Task 1, so they cannot run in
parallel.

## Task Dependency Graph

- Task 1 [AFK]: Tavily news source + YAML + exports → depends on `none` → batch 1
- Task 2 [AFK]: CLI wiring + `--only-source` + reliability rank → depends on `Task 1` → batch 2

## Agent Assignments

- Task 1: Source + YAML + tests + re-exports → python-development:python-pro
- Task 2: CLI wiring + reliability rank + only-source → python-development:python-pro
- Polish: post-implementation-polish → general-purpose

---

## File Structure

**New:**

- `slopmortem/corpus/sources/tavily_news.py` — `TavilyNewsSource` plus
  module-level helpers (`_canonicalize_url`, `_build_call_descriptors`,
  `_dedup_keep_highest_score`, `_drop_mirror_hosts`, `_load_yaml`).
- `slopmortem/corpus/sources/queries/__init__.py` — empty package marker.
- `slopmortem/corpus/sources/queries/tavily_news.yml` — queries +
  defaults. Loaded at `__init__` time, overridable via constructor kwargs.
- `slopmortem/corpus/sources/mirror_domains.yml` — host blocklist for
  known aggregators / syndicated-content rehosts (peer to
  `platform_domains.yml`). Shared infra: future sources can read the
  same file. Match is suffix-based (`host == d or host.endswith("." + d)`)
  so `m.bundle.app` and `bundle.app` both drop. Seed list:
  `bundle.app` (observed mirroring TechCrunch in the 2026-05-06 probe),
  `flipboard.com`, `feedly.com`, `smartnews.com`, `inoreader.com`,
  `news.google.com`. Easy to grow as filler reappears.

**Modified:**

- `slopmortem/corpus/sources/_names.py` — add
  `SOURCE_TAVILY_NEWS: Final = "tavily_news"` alongside
  `SOURCE_DEFILLAMA`. The literal string `"tavily_news"` is the
  identifier used in `RawEntry.source`, the
  `_RELIABILITY_RANK` key, and the `--only-source` argument.
- `slopmortem/corpus/sources/__init__.py` — re-export `TavilyNewsSource`.
- `slopmortem/cli/_ingest_cmd.py` — add `--enable-tavily-news` and the
  four CLI override flags (`--tavily-news-start-year`,
  `--tavily-news-end-year`, `--tavily-news-max-emit`,
  `--tavily-news-search-depth`); add the generic `--only-source NAME`
  flag; thread both into `_run_ingest`; assert `TAVILY_API_KEY` is set
  when the source is enabled (no `tavily_enrich` implication — the body
  is in the search response).
- `slopmortem/ingest/_helpers.py` — extend `_RELIABILITY_RANK` with
  `tavily_news → 4`. Slots one below DefiLlama (rank 3) since news
  reporting is one step removed from primary on-chain data.

**New tests:**

- `tests/sources/test_tavily_news.py` — pure-fn tests for URL
  canonicalisation, dedup, score filter, max_emit cap, score-zero
  filler rejection; mocked-`safe_post` integration test for the full
  `fetch()` flow; cassette round-trip test (skipped without
  `RECORD=1`).

**Modified tests:**

- `tests/ingest/test_reliability_rank.py` — add
  `(SOURCE_TAVILY_NEWS, 4)` parametrize case.
- `tests/test_cli_ingest.py` — add three regression tests:
  `--enable-tavily-news` without `TAVILY_API_KEY` exits non-zero;
  `--only-source tavily_news` runs the source in isolation and
  auto-enables; `--only-source nonexistent` lists valid names and
  exits non-zero.

## Configuration: `tavily_news.yml`

```yaml
queries:
  - "startup shuts down"
  - "startup ceases operations"
  - "startup files for bankruptcy"
  - "company winds down"
  - "startup files chapter 11"
  - "startup lays off all staff"
  - "tech startup closes operations"
  - "startup runs out of cash"
  - "startup fails to raise funding"
  - "company shutters operations"
year_range:
  start: 2024  # Tavily news index empirically empty for these queries before 2024
  # `end` intentionally omitted: source defaults to datetime.now().year.
  # Set explicitly only for one-off historical sweeps via YAML or CLI.
max_emit: 200
min_score: 0.3
search_depth: basic     # basic = 1 credit/call, advanced = 2 credits/call
```

The YAML is loaded at `TavilyNewsSource.__init__` via
`importlib.resources` so packaging stays clean. Constructor kwargs
override anything in the file. CLI flags override constructor kwargs.

## Pipeline (`fetch()`)

1. **Materialise call descriptors.** Build the `(query, year, quarter)`
   cross product into a list of dicts with quarterly windows
   (`Q1=01-01..03-31`, `Q2=04-01..06-30`, `Q3=07-01..09-30`,
   `Q4=10-01..12-31`). With the default 10 queries × 3 years × 4
   quarters that's **120 calls → 120 credits per run on `basic`**,
   240 on `advanced`. Quarterly bucketing matters because Tavily's
   `max_results=20` cap is per-call: a single full-year window
   saturates on whichever quarter dominated the news cycle and
   silently drops the rest. Quarters force the index to surface its
   top-20 four times per year.
2. **Fan out.** Wrap each call as a coroutine that calls `safe_post`
   to `https://api.tavily.com/search` with
   `{api_key, query, topic: "news", start_date, end_date,
   search_depth, max_results: 20, include_raw_content: true}`.
   Throttle via `throttle_for("api.tavily.com", rps=1.0)`. Run all
   coroutines through `gather_resilient` under
   `anyio.CapacityLimiter(5)`. `gather_resilient` returns
   `list[T | Exception]` (it does *not* swallow failures silently);
   the source filters out `Exception` instances, logs each at WARN
   with the failed `(query, year)`, and continues with the
   successful results.
3. **Flatten + filter.** Collect every `result` row from every
   succeeded call. Drop any with `score < min_score` (default 0.3) or
   missing/non-numeric score, missing URL, missing `raw_content`.
4. **Canonicalise URLs.** `_canonicalize_url` lowercases the host,
   drops fragment, and strips query parameters in
   `{"utm_source", "utm_medium", "utm_campaign", "utm_term",
   "utm_content", "fbclid", "gclid", "ref", "ref_src", "feature",
   "_ga"}`. Trailing slashes normalised. Result: stable
   `source_id` and dedup key.
5. **Drop mirror/aggregator hosts.** `_drop_mirror_hosts` discards
   rows whose canonical host matches `mirror_domains.yml` (suffix
   match). Catches syndicated reposts of upstream reporting (e.g.
   `bundle.app` mirroring TechCrunch). Logged at DEBUG once per drop
   with the dropped host. Seed list intentionally small and
   conservative — easy to grow as filler reappears.
6. **Dedup.** Keep one row per canonical URL — the one with the
   highest Tavily `score`. Handles both within-query duplicates (the
   Fisker-11×-in-one-response case observed during API exploration)
   and cross-query duplicates (same article matching multiple
   shutdown verbs).
7. **Sort + cap.** Sort by `(score desc, published_date desc,
   canonical_url asc)`. Take the first `max_emit` (default 200).
   **`published_date` from Tavily is RFC 1123** (e.g. `Tue, 18 Nov
   2024 11:05:19 GMT`), not ISO 8601 — parse with
   `email.utils.parsedate_to_datetime` before sorting; raw string
   sort would silently misorder.
8. **Yield.** One `RawEntry` per kept row:
   - `source = "tavily_news"`
   - `source_id = canonical_url`
   - `url = canonical_url`
   - `markdown_text = raw_content` — full article body returned by Tavily
   - `raw_html = None`
   - `fetched_at = datetime.now(UTC)`

   No per-entry debug metadata is stored. `RawEntry` (`models.py:327`)
   has no `extra` field, and adding one would touch every source,
   the journal schema, and `qdrant_store`. Tavily score, matched
   query phrase, and `published_date` are logged at the call site
   instead — sufficient for debugging without expanding the model.

Collect-then-rank (steps 2-7) runs entirely in memory before any yield,
because parallel `gather_resilient` completion order is non-deterministic
and we want score-ranked emission for cassette stability and corpus
quality. Total memory footprint: ~120 calls × 20 results × ~10 KB raw
content ≈ 24 MB peak. Trivial.

## CLI Surface

### `--enable-tavily-news`

Boolean opt-in. Default `False`. When set:

- Asserts `TAVILY_API_KEY` is exported. If missing, raises
  `typer.BadParameter` with a message naming the env var. The
  source reads the key via `os.environ.get("TAVILY_API_KEY", "")`
  inside the module (matching `TavilyEnricher` and
  `_tools_impl._tavily_api_key()`); `slopmortem/config.py`'s
  `tavily_api_key: SecretStr` is not threaded through. Does not
  imply `--tavily-enrich` because every emitted entry already carries
  its body.
- Appends `TavilyNewsSource()` to the source list at the end. Insertion
  position relative to DefiLlama is irrelevant because both are
  opt-in; the existing list is `[Curated, HN, Crunchbase?,
  DefiLlama?, TavilyNews?]` where each `?`-marked source appears
  only when its enable-flag is set. If DefiLlama lands first the
  list grows; if it lands second, no merge conflict.

### `--only-source NAME`

Generic. Default unset. When set:

- Filters the constructed source list to only sources whose name
  matches `NAME`. The match is against an explicit registry table
  defined locally in `_ingest_cmd.py` (no Protocol-level changes to
  `Source` and no class attribute on every existing source), shape:

  ```python
  _SOURCE_REGISTRY: dict[str, _SourceSpec] = {
      "curated":        _SourceSpec(enable_flag=None,           gate=lambda **_: True),
      "hn_algolia":     _SourceSpec(enable_flag=None,           gate=lambda **_: True),
      "crunchbase_csv": _SourceSpec(enable_flag="crunchbase_csv", gate=lambda *, crunchbase_csv, **_: crunchbase_csv is not None),
      "defillama":      _SourceSpec(enable_flag="enable_defillama",   gate=lambda **_: True),
      "tavily_news":    _SourceSpec(enable_flag="enable_tavily_news", gate=lambda **_: True),
  }
  ```

  Names match `RawEntry.source` strings exactly.

- Auto-enable rules:
  - For sources gated by an `--enable-*` boolean flag (DefiLlama,
    Tavily news), `--only-source` flips the flag to `True` if not
    already set.
  - `crunchbase_csv` is gated by a path argument (`--crunchbase-csv
    PATH`), not a boolean flag. `--only-source crunchbase_csv`
    requires `--crunchbase-csv PATH` to be supplied separately;
    if absent, fail with a clear message ("--only-source
    crunchbase_csv requires --crunchbase-csv PATH"). Auto-enable
    only applies to flag-gated sources.
  - `curated` and `hn_algolia` have no enable flag — `--only-source
    curated` just filters the list.

- If `NAME` doesn't match any key in `_SOURCE_REGISTRY`, raises
  `typer.BadParameter` listing the valid names.

This is a small generalisation that pays off the moment any second
source needs isolated test runs. Future-proofs DefiLlama too: once
this lands, `slopmortem ingest --only-source defillama` Just Works.

### Per-source CLI overrides

| Flag | Override target |
|------|-----------------|
| `--tavily-news-start-year` | `year_range.start` |
| `--tavily-news-end-year` | `year_range.end` |
| `--tavily-news-max-emit` | `max_emit` |
| `--tavily-news-search-depth basic\|advanced` | `search_depth` |

Other knobs (queries, `min_score`) stay YAML-only — rare to tune
at runtime, no need to clutter `--help`. Quarter granularity is
fixed: there is no `--start-quarter` / `--end-quarter`; year
boundaries are the only user-tunable temporal axis.

## Error Handling

Per CLAUDE.md ("don't add bare `except Exception: pass`"):

- **Per-call failures** — HTTP 4xx/5xx, timeout, JSON parse error,
  payload not a dict, `results` field missing or wrong type. Logged
  at WARN with the query, year, and quarter, then dropped.
  `gather_resilient` swallows the exception so siblings keep going.
- **Per-result failures** — score not numeric, URL malformed, no
  `raw_content`. Logged at INFO once per affected row, then skipped.
- **Run-level failures** — only `TAVILY_API_KEY` missing at startup
  (`typer.BadParameter`). No mid-run abort even if every Tavily call
  fails — the source just yields nothing and logs a single WARN
  (`tavily_news: 0 entries after dedup`).

`gather_resilient`'s contract (one failed sibling doesn't abort
others) is the right primitive for this fan-out shape, exactly as
used in `slopmortem/stages/synthesize.py` and
`slopmortem/corpus/_reclassify.py`.

## Tracing

`fetch()` is wrapped with
`@observe(name="tavily_news.fetch", ignore_inputs=["api_key"], ignore_output=True)`
to match project convention. Per-call attributes (`query`, `year`,
`quarter`, `result_count`, `score_max`, `kept_after_filter`) emit as
custom span events via `slopmortem.tracing.events.SpanEvent`. The API
key never enters span attributes — it's loaded from `os.environ`
inside the module and excluded from the decorator's input capture.

## Testing

| Test | What it asserts |
|------|------------------|
| `test_canonicalize_url` | `utm_*`, `fbclid`, `gclid`, `ref`, `_ga`, `feature` stripped; host lowercased; fragment dropped; trailing slash normalised |
| `test_dedup_keeps_highest_score` | Same canonical URL appearing 3× with scores 0.4/0.6/0.5 → kept once at 0.6 |
| `test_mirror_domain_dropped` | Rows with hosts in `mirror_domains.yml` (and subdomains thereof) dropped before dedup; non-listed hosts pass through |
| `test_min_score_filter` | Result with `score=0.05` dropped at default `min_score=0.3` |
| `test_max_emit_cap` | 500 unique results in, `max_emit=50` → exactly 50 emitted, sorted by score |
| `test_score_zero_filler_dropped` | Replays the crypto-query-shaped response (15 score-0 hits) → 0 entries |
| `test_per_call_failure_isolated` | One of 20 mocked `safe_post` calls raises; the other 19 results still flow through |
| `test_yaml_loaded` | YAML defaults are picked up; constructor kwargs override |
| `test_no_api_key_fails_at_startup` | `--enable-tavily-news` without `TAVILY_API_KEY` → exit ≠ 0, output mentions the env var |
| `test_only_source_filter` | `--only-source tavily_news` runs the source in isolation; auto-enables; rejects unknown names |
| `test_only_source_unknown_name_lists_valid` | `--only-source nonexistent` → exit ≠ 0, output lists valid source names |
| `test_round_trip` (cassette, `pytest.mark.vcr`) | One real Tavily call recorded under `tests/sources/cassettes/test_tavily_news/test_round_trip.yaml` with `max_emit=5` and a single `(query, year, quarter)` tuple; replayed in CI. Skip-without-cassette pattern matches DefiLlama plan: `if not CASSETTE_FILE.exists() and not os.environ.get("RECORD"): pytest.skip(...)`. |
| `test_reliability_rank` | `(SOURCE_TAVILY_NEWS, 4)` |

`include_raw_content=true` produces moderate cassettes (~7 KB
raw_content per result on `search_depth=basic` empirically — Spirit
Airlines round-trip on 2026-05-06 was 6993 chars; longer outlets
reach 10–15 KB). At `max_results=5` × ~10 KB ≈ 50 KB per cassette,
small enough for git review. Record exactly one cassette with
`max_emit=5` and a single `(query, year, quarter)` tuple.

## Pros and Cons of Key Decisions

**Generic `--only-source` flag vs dedicated subcommand:**

- Pros of generic: one flag works for every source. DefiLlama and any
  future source inherit isolation-mode for free. Less CLI surface.
- Pros of subcommand: discoverability — `slopmortem ingest-tavily-news`
  shows up in `--help` cleanly.
- Cons of subcommand: every new source spawns another subcommand;
  shared CLI surface (`--limit`, `--dry-run`, `--post-mortems-root`)
  has to be repeated everywhere.
- **Pick generic.** Same composability story as the DefiLlama plan's
  `--enable-defillama`. Subcommands aren't free.

**Collect-then-rank vs streaming emission:**

- Pros of streaming: lower memory; first entry yields faster.
- Pros of collect-then-rank: deterministic emission order (parallel
  `gather_resilient` completion order is non-deterministic);
  highest-confidence entries always win the `max_emit` slot;
  cassettes stable across reruns.
- Cons of collect-then-rank: ~24 MB peak memory across all 120
  calls' raw content. Trivial.
- **Pick collect-then-rank.** Determinism + score-best-wins is worth
  ~4 MB. Streaming would silently drop legitimate hits because
  whichever calls happen to finish first take the cap.

**`min_score=0.3` default vs 0.1 or 0.2:**

- Pros of 0.3: empirically the boundary where named-event signal
  becomes dominant in the test data. Below 0.3 it's macro-trend
  pieces ("Startups had a tough year"), geopolitical noise (TikTok
  Canada, X Brazil) and outright off-topic (Al-Awda Hospital). The
  slop classifier would later drop these but at ~$0.0008 per Haiku
  call avoided.
- Pros of 0.1 or 0.2: tighter recall on edge cases — Fisker's
  query-2-copy-1 sat at 0.27 and would be dropped. But Fisker
  shows up in essentially every shutdown query, so the
  highest-scored copy from another query (≥ 0.5) survives via
  cross-query dedup.
- Cons of 0.3: marginal recall loss on borderline events that only
  appear once across all queries. Acceptable.
- **Pick 0.3.** YAML-tunable; drop to 0.2 if real hits start
  disappearing.

**`search_depth=basic` default vs `advanced`:**

- Pros of basic: 1 credit per call vs 2 for advanced — 2× headroom
  on the 1,000-credit free tier (8 vs 4 ingest runs/month at the
  120-call default).
- Pros of advanced: marginally higher top-of-list precision in
  testing — `score=1.00` for Lilium / Bench in the 2024-H2 query.
- Cons of advanced: cost; and the same query duplicated the Fisker
  article 11× in one response, so the precision uplift is partly
  illusory.
- **Pick basic.** YAML-tunable; CLI override exists for cost-aware
  one-offs.

**`year_range=2024..current` rolling window vs static 2020-2024:**

- Pros of rolling 2024-onward: matches Tavily's actual news-index
  coverage; eliminates ~80% of wasted calls that were hitting an empty
  pre-2024 index; spec stays accurate as time advances; once 2027
  rolls around, default end shifts and the source keeps working.
- Pros of static 2020-2024: deterministic; cassette-stable across
  calendar boundaries.
- Pros of "current year only" (1y × 4q × 10v = 40 calls): cheapest
  variant; matches the empirical productive zone exactly.
- Cons of rolling: end-of-year effects — January runs cover only a
  few days of the new year; year-boundary Q4↔Q1 events may
  fragment. Mitigated by the multi-year window keeping prior-year
  Q4 in scope.
- **Pick rolling 2024..current** (`current = datetime.now().year`).
  Three years × four quarters × ten queries = 120 calls / 120
  credits — 40% cheaper than the original 200-call quarterly sweep,
  with ~191 unique entries already validated at 2024-only saturation.
  Operators can override either bound via CLI for one-off historical
  sweeps.

**Quarterly buckets vs full-year buckets:**

- Pros of quarterly: 4× more surface area against Tavily's per-call
  20-result cap; spreads coverage across the year instead of
  saturating on whichever quarter generated the most news.
- Pros of yearly: 4× fewer credits.
- Cons of quarterly: a productive quarter (e.g. 2024-Q4) hits the
  20-cap regardless; only quarters with <20 distinct events benefit
  from the finer slicing. Empirically still net-positive given how
  bunched shutdown news is.
- **Pick quarterly.** Validated at 191 unique vs an extrapolated
  ~50 with yearly windows.

**`include_raw_content=true` vs separate `/extract` hop:**

- Pros of inline: zero additional Tavily credits (extract would be
  an extra 1 credit per 5 URLs at basic). One round trip per call.
  Body lands in `markdown_text` directly — no `TavilyEnricher`
  implication, no Wayback fallback.
- Pros of separate extract: smaller search response bytes; different
  rate-limit pool theoretically.
- Cons of separate extract: extra credit cost; extra failure mode
  per entry; slower.
- **Pick inline.** Tavily's news-search response already includes
  the body when `include_raw_content=true`; no reason to pay twice.

**YAML config vs Python constants for queries:**

- Pros of YAML: ops can tune queries without a deploy. Mirrors
  existing `platform_domains.yml` and `corporate_hierarchy_overrides.yml`
  patterns. Lives next to the source module so it's
  discoverable.
- Pros of Python constants: harder to drift; queries are part of the
  source's contract.
- Cons of YAML: untracked edits could degrade the corpus silently.
  Mitigation: file is checked into git; same review pressure as code.
- **Pick YAML.** Operator tunability matters here because queries are
  the only knob between "this source works" and "this source returns
  garbage." Worth the modest drift risk.

**Opt-in CLI flag vs always-on:**

- Pros of opt-in: existing `just ingest` output stays bit-identical;
  cassette/eval baselines don't shift; new source doesn't quietly
  add Tavily-credit cost; matches DefiLlama and the project's
  cassette-discipline rule from `CLAUDE.md`.
- Pros of always-on: zero ops friction, works out of the box.
- Cons of always-on: silently bumps Tavily credit consumption; a
  user who happens to have `TAVILY_API_KEY` set for the existing
  enricher gets news-search costs they didn't consent to.
- **Pick opt-in.** Same logic as `--enable-defillama`.

**Reliability rank `4` vs `2` vs `5`:**

- Pros of `4`: news is one step removed from primary data — a
  reporter writing about a shutdown is more reliable than aggregated
  analysis but less reliable than the company's SEC filing or the
  protocol's on-chain TVL.
- Pros of `2`: news is generally well-attributed.
- Pros of `5`: news cycles are noisy and prone to early-call errors.
- Cons of `4`: arbitrary; falls between the existing ranks.
- **Pick `4`.** Slots cleanly: Curated 0, HN 1, Crunchbase CSV 2,
  DefiLlama 3, Tavily News 4, dead-letter 9. **Coordination caveat:**
  if the DefiLlama plan lands at a different rank, or if Tavily
  News merges first, bump this rank so the order stays
  primary-source → derived-reporting.

---

## Out of Scope

- **Per-source budget caps.** Spending limits per adapter would
  require touching `slopmortem/budget.py`. Defer until a real
  overrun lands. `max_emit` is a yield-count proxy, not a true
  budget.
- **Domain include/exclude lists.** Tavily exposes
  `include_domains` / `exclude_domains` parameters. Empirically, the
  `min_score` threshold caught all observed filler; domain
  allowlists would be belt-and-braces complexity. Add later if
  filler reappears at scores above 0.3.
- **Sector-targeted queries.** Crypto-specific phrases returned
  pure filler in API testing (NASA pages, Kotaku screenshots, Forbes
  CD rates). Sector targeting collapses Tavily's news index to
  unrelated content. The 4 generic death verbs catch sector-diverse
  events without the failure mode.
- **Incremental ingest mode** (last-N-days windowing, content-hash
  skip). The default `year_range=2024..current` is a rolling window,
  so re-running annually keeps coverage current without code changes.
  A finer-grained "only fetch since last run" mode would save credits
  on frequent re-runs, but scheduled ingest doesn't exist yet, so the
  engineering surface isn't justified. Revisit if and when scheduled
  ingest lands.
- **Pitch enrichment** for the thin-pitch entries flagged in the
  recall-vs-depth caveat. News articles describe failure events, not
  the company's original pitch — fixing that requires data from
  outside the news topic. Empirical probing on 2026-05-06 validated:
  - **Wikipedia REST API** (`/api/rest_v1/page/summary/<title>` with
    a `User-Agent` header) recovered pitch-shaped summaries for
    ~50–70 % of long-tail shutdowns probed (Bowery Farming, Veev hit;
    Ghost Autonomy missed — no article).
  - **Tavily `/search` → `/extract` on the company homepage** works
    when the site is still live (Bench, First Mode) but fails when
    the domain is dead post-shutdown (Lilium).
  - **Wayback Machine `/available` API** was unvalidated — returned
    empty `archived_snapshots` for every probed URL. The CDX Server
    API (`https://web.archive.org/cdx/search/cdx`) likely needs to
    replace it; left as future investigation.

  Rather than bolt enrichment onto `TavilyNewsSource`, scope a
  separate `PitchEnricher` ingest stage between slop-classify and
  Qdrant write. The source's job stays "find shutdown events";
  pitch reconstruction is a separate axis with its own credit
  envelope and failure modes.

- **Updating `docs/architecture.md`.** Add a one-line note pointing
  to this spec once it lands. Full re-write isn't warranted.
- **Refactoring `TavilyEnricher` onto `safe_post`.** It uses
  `httpx` directly today. Out of scope; separate cleanup if anyone
  cares.
- **Re-recording eval cassettes.** The eval runner is gated by
  curated post-mortems plus the existing two sources; extra corpus
  from this source doesn't shift eval cassettes. Verify with
  `just eval` after Task 2 — divergence is a separate plan.
