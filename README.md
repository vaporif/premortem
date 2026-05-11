# slopmortem

[![ci](https://github.com/vaporif/slopmortem/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/vaporif/slopmortem/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/vaporif/slopmortem/branch/main/graph/badge.svg)](https://codecov.io/gh/vaporif/slopmortem)

[![asciicast](https://asciinema.org/a/wexQWJ8nMrPPC5l1.svg)](https://asciinema.org/a/wexQWJ8nMrPPC5l1?speed=5)

You give it a pitch, it finds dead startups that tried something similar.

`slopmortem` runs locally. LLM calls go through OpenRouter (Sonnet + Haiku by default). Qdrant runs where you want (there's a `docker-compose.yml` in the repo if you'd rather not run your own).

Pipeline diagram, query/ingest flow, and source layout live in [`docs/architecture.md`](docs/architecture.md).

<p align="center">
  <img src="docs/architecture-flow.svg" alt="ingest and query data flow" width="100%">
</p>

Reports lead with a "Top risks across all comparables" section: pure-Python clustering of the per-candidate `lessons_for_input` lists by token-set similarity, sorted by how many comparables raised each one. Then the per-candidate post-mortems, then a cost/latency/trace footer.

## Running it

Dev shell is a Nix flake (reproducible, pinned toolchain). With direnv: `direnv allow` and the shell loads on `cd`. Without: `nix develop`.

The shellHook calls `uv venv` + `uv sync --frozen`, so Python is ready by the time the prompt returns. Then `just` for the rest.

Secrets go in `.env` (gitignored); `just init-env` walks the prompts and is re-runnable. Knobs live in `slopmortem.toml` with comments.

First-run sequence:

```
direnv allow                         # or: nix develop
just init-env                        # interactive — fill OPENROUTER_API_KEY, skip the rest (Tavily/OpenAI/Laminar are feature-gated)
docker compose up -d qdrant          # Qdrant on :16333 (host port; container still 6333)
slopmortem embed-prefetch            # one-time ~550 MB ONNX download
just ingest                          # ~$0.75 for 50 entries with all enrichers; or `just ingest-all`
just query "your pitch here"         # ~$0.10 warm / ~$0.30 cold cache; or `just query-debug` to skip rerank+synth
```

Ingest pulls from curated + HN by default. Useful flags:

- `--crunchbase-csv PATH` — turn on the Crunchbase adapter for this run (see below)
- `--enable-tavily-news` — add a Tavily news source for shutdown-event coverage; needs `TAVILY_API_KEY`. Window with `--tavily-news-start-year/--tavily-news-end-year`, cap with `--tavily-news-max-emit`, depth with `--tavily-news-search-depth basic|advanced` (1 vs 2 Tavily credits per query).
- `--only-source curated|hn_algolia|crunchbase_csv|tavily_news` — run a single source; its `--enable-*` flag fires for you
- `--limit N` — first N entries across sources, in order curated → HN → Crunchbase → TavilyNews. Cheap live smoke test.
- `--no-pitch-filler` / `--no-title-pre-filter` — disable the HN pitch-filler (Tavily-backed body synth) and the Haiku title gate. Both auto-enable when HN is in the source set; you only need these to opt out.
- `--dry-run` — count without writing; `--force` bypasses the per-source skip key
- `--post-mortems-root PATH` — override the `./post_mortems/` tree

<details>
<summary><b>Crunchbase setup</b></summary>

The repo ships the 2015 `notpeter/crunchbase-data` mirror as a git submodule under `external/crunchbase-data/`. Run `git submodule update --init` once to fetch it, then `just crunchbase` to produce a closed-only slice (~6.2K rows at `data/crunchbase/companies-closed.csv`, tracked in this repo) and point `--crunchbase-csv` at it.

</details>

<details>
<summary><b>Maintenance corners</b></summary>

`slopmortem ingest --reconcile` patches drift between the journal, the markdown tree, and Qdrant. `slopmortem ingest --reclassify` re-runs the slop classifier against the quarantine tree and routes survivors back through entity resolution. `slopmortem ingest --list-review` prints the entity-resolution review queue (tier-2 ambiguous pairs that landed in the calibration band).

Storage defaults to `./post_mortems/{raw,canonical,quarantine}/` with the merge journal at `./journal.sqlite` next to the root. Override with `--post-mortems-root` or the `POST_MORTEMS_ROOT` / `MERGE_JOURNAL_PATH` env vars. The fastembed model lands wherever fastembed defaults unless you point `embed_cache_dir` somewhere in `slopmortem.toml`.

</details>

<details>
<summary><b>Configuration</b></summary>

`slopmortem.toml` (tracked) holds the documented defaults; every field has a comment. Don't edit it for personal tweaks. Drop a `slopmortem.local.toml` next to it with only the keys you want to override — the loader reads both from the current working directory and `.local.toml` wins over `slopmortem.toml`. `.local.toml` is gitignored. Env vars (and `.env`) override both TOML files (standard 12-factor), so use `.local.toml` for your durable personal config and export an env var when you want a one-off override on top.

</details>

<details>
<summary><b>Embedding provider</b></summary>

fastembed is the default because it runs offline, costs nothing, and means CI doesn't need an OpenAI key. The model is `nomic-ai/nomic-embed-text-v1.5`, 768d. Switch to OpenAI in `slopmortem.toml`:

```toml
embedding_provider = "openai"
embed_model_id = "text-embedding-3-small"   # or text-embedding-3-large
```

Bringing a different model? Add a row to `EMBED_DIMS` in `slopmortem/llm/openai_embeddings.py`. Qdrant reads it to size the collection.

</details>

<details>
<summary><b>Testing, evals &amp; cassettes</b></summary>

Every LLM and HTTP call made during tests or evals replays from `tests/fixtures/cassettes/` (pytest-recording, vcrpy underneath). `FakeLLMClient` + `FakeEmbeddingClient` cover the rest, so `just test` and `just eval` are free and offline.

`just eval` runs the seed dataset through the pipeline against recorded cassettes; deterministic, asserted against `tests/evals/baseline.json`. `just eval-record` re-records against live OpenRouter + local fastembed under a `--max-cost-usd 2.0` ceiling. `just eval-record-corpus` regenerates the seed corpus fixture from `tests/fixtures/corpus_fixture_inputs.yml` (~$0.30–$1 with fastembed). Both record commands cost real money, so trigger them by hand. Don't wire either into CI.

`just smoke-live` hits live OpenRouter on a manual trigger, roughly weekly, to catch silent SDK/model/routing shifts. `slopmortem replay <dataset>` re-runs a saved JSONL through current code without re-burning the LLM bill — useful when iterating on prompts.

</details>

## Known limitations

- Chunk-to-parent over-fetch assumes ~4 chunks/doc — long post-mortems can under-fill the parent set ([#25](https://github.com/vaporif/premortem/issues/25)).
- LLM rerank cost is linear in `K_retrieve` — fine at K=30, revisit if K grows ([#27](https://github.com/vaporif/premortem/issues/27)).

## Possible enhancements

**Failure-mode-first retrieval.** The pipeline is similarity-first all the way down. Facets narrow retrieval, hybrid RRF picks neighbors, and `consolidate_risks` is the only stage that generalizes across candidates. It only runs after we've already committed to whichever startups happened to land near the pitch in vector space. That works when the corpus has a close neighbor and fails quietly when it doesn't: a pitch in a domain the corpus barely covers gets cited against generic neighbors that miss the actual risk surface, and the top-risks section ends up reflecting what's retrievable, not what's predictive.

Reframe worth spiking behind a feature flag: predict failure modes from the pitch first (closed taxonomy seeded from current `consolidate_risks` outputs, plus an `other` bucket so novel modes don't get dropped), retrieve 2–3 evidence-strong candidates per predicted mode, then synthesize per mode; the candidate becomes a supporting citation, not the unit of analysis. Cost stays roughly flat — one added Sonnet call for the prediction, balanced by a lighter per-mode rerank rubric. The obvious risk is one more place to hallucinate, so don't swap; layer. Run both paths and let the synthesizer pick whichever evidence is stronger per mode. An A/B on the eval seed dataset will tell you whether it helps on novel pitches without regressing the cases where the corpus already has a near-perfect analog.

**More shapes in the rerank rubric.** The rubric scores each candidate on four perspectives today — `business_model`, `market`, `gtm`, `stage_scale`. They capture what a startup *looks like* well but miss the structural shapes that predict how things die. The one I'd add first is `platform_dependency`: built on someone else's rails — Foursquare's API, the Twitter dev platform, an App Store policy shift, one SaaS vendor's pricing whim. It's a recurring cause of death and it's orthogonal to the four existing perspectives. The other candidate is `moat_shape` (network effects, brand, data, nothing); two startups dying because incumbents copied them in 18 months share a shape `business_model` doesn't see. The case for `moat_shape` is weaker, though, so cap the additions at one for now: Sonnet scoring seven perspectives at 0–10 in one JSON loses calibration well before it gains signal. Ship `platform_dependency`, watch the eval baseline, decide whether `moat_shape` earns its tokens after that.

## Examples

Sample runs with pitch, rendered report, and Laminar trace live under [`docs/examples/`](docs/examples/).

## Design notes

Full spec is in [`docs/specs/2026-04-27-slopmortem-design.md`](docs/specs/2026-04-27-slopmortem-design.md). The pre-implementation punch list of contract bugs to close before code is in [`docs/specs/2026-04-28-design-spec-blockers.md`](docs/specs/2026-04-28-design-spec-blockers.md).
