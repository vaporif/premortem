# slopmortem — agent guide

Project-specific conventions for AI coding agents. Read this before touching code. User-facing setup lives in `README.md`; architecture in `docs/architecture.md`; query-pipeline behaviors in `docs/specs/`.

## What this project is

Local CLI that takes a startup pitch and returns a "slopmortem" — dead/struggling startups that tried something similar, plus consolidated risks. Pipeline: `ingest` (sources → slop classify → entity-resolve → Qdrant) and `query` (facet → retrieve → rerank → synthesize → consolidate_risks). Python 3.13+, async via `anyio`, Pydantic v2.

## Commands

Use `uv` for everything. Don't invoke `pip`, `python -m venv`, or `poetry`.

| Task | Command |
|------|---------|
| Install deps | `just install` (= `uv sync`) |
| Run tests | `just test` (`pytest -n auto`) |
| Lint | `just lint` (ruff check + format check) |
| Auto-fix lint | `just format` |
| Type check | `just typecheck` (`basedpyright`, strict) |
| Coverage | `just coverage` |
| Eval (offline, cassettes) | `just eval` |
| Eval (record live, costs ~$2) | `just eval-record` — **don't run unprompted** |
| Ingest first 50 entries | `just ingest` |
| Run a query | `just query "pitch text"` |
| Retrieve-only (cheap, no LLM rerank/synth) | `just query-debug "pitch"` |
| Wipe all ingested state | `just nuke` (interactive, prompts) |

`just --list` shows the rest. Don't add new top-level scripts; extend `justfile`.

## Configuration

Precedence (highest wins): env vars → `.env` → `slopmortem.local.toml` → `slopmortem.toml` (tracked defaults). Loader: `slopmortem/config.py`. Pydantic-settings with `extra="forbid"` — unknown keys are an error, not a warning. Env always wins; personal overrides go in `slopmortem.local.toml`, but if you also export the same key in env, env beats it.

- **Don't edit `slopmortem.toml` for personal tweaks.** That file is the documented default surface. Personal overrides go in `slopmortem.local.toml` (gitignored).
- Secrets go in `.env` (gitignored). `OPENROUTER_API_KEY` is the only required key; Tavily, OpenAI, Laminar are gated by feature flags.
- API keys are `SecretStr` — don't log them, don't include them in span attributes.

## Code conventions

- **Type hints:** strict `basedpyright`, `reportAny="error"`. No `Any` leaks. Use PEP 695 type aliases (`type X = ...`) and PEP 604 unions (`X | None`, not `Optional[X]`).
- **Pydantic v2 only.** `BaseModel` for schemas (`slopmortem/models.py`), `BaseSettings` for config. `model_validator(mode="after")` for cross-field checks. Closed sets are `StrEnum` or `Literal[*_TAXONOMY_VALUES]` with a `TYPE_CHECKING` fallback.
- **Async with `anyio`, not bare `asyncio`.** Bounded concurrency via `anyio.CapacityLimiter`. SQLite calls go through `anyio.to_thread.run_sync`. Use `slopmortem.concurrency.gather_resilient` at fan-out points so one failed sibling doesn't abort the whole stage.
- **Error handling:** per-entry/per-candidate failures log and continue; budget exceeded short-circuits with `budget_exceeded=True` on the report. Don't add bare `except Exception: pass`. Don't introduce a global retry decorator — retry policy lives in the LLM client (`slopmortem/llm/openrouter.py`).
- **Tracing:** Laminar via `@observe(name=...)` decorators. Use `ignore_inputs=[...]` / `ignore_output=True` to redact prompt/response bodies. Custom span events use `slopmortem.tracing.events.SpanEvent`.
- **Logging:** stdlib `logging`. Don't add `print()` to library code (CLI uses Rich, that's fine).
- **Comments:** explain *why*, not *what*. Don't restate code. Don't leave AI-generated breadcrumbs ("This function does X by doing Y").

## Testing

- `pytest` with `asyncio_mode="auto"` (no `@pytest.mark.asyncio` needed) and `pytest-xdist` (parallel). Tests must be parallel-safe — no shared filesystem state outside `tmp_path`.
- **Fakes over mocks.** `slopmortem/llm/fake.py` (`FakeLLMClient`), `slopmortem/llm/fake_embeddings.py`, `InMemoryCorpus` in `ingest.py`, `FakeSlopClassifier`. Tests inject them; prod code never touches `unittest.mock`.
- **Cassettes** for eval and integration: `tests/fixtures/cassettes/...`, recorded by `just eval-record`. If a test raises `NoCannedResponseError` / `NoCannedEmbeddingError`, a prompt or model changed — re-record the affected scope, don't widen the matcher. See `docs/cassettes.md`.
- **Markers:** `requires_qdrant` (live `localhost:6333`), `slow` (downloads ~550 MB ONNX). CI runs both with a Qdrant service container; lazy ONNX stub keeps `just test` offline by default.
- Tests live in `tests/` mirroring `slopmortem/` layout. New module → new test file.

## Load-bearing things to not break

- **Merge journal (`journal.sqlite`)** — not a log; it's the source of truth for entity resolution and quarantine state. Terminal-state writes happen in one transaction; `mark_complete` only fires *after* both Qdrant and disk writes succeed (`slopmortem/ingest.py` header comment). Don't add a write path that bypasses the journal.
- **Three-tier entity resolution** (`slopmortem/corpus/entity_resolution.py`): registrable domain → normalized name+sector → dense similarity + Haiku tiebreaker inside the calibration band only. Tier-3 cache key is lex-sorted to collapse `(A,B)` and `(B,A)`. Don't change tier ordering or the cache key shape without updating the journal migration.
- **Slop classifier** quarantines docs above `slop_threshold` to `post_mortems/quarantine/` — they get *no* Qdrant point and *no* journal row. `--reclassify` is the only way back.
- **Lazy ONNX loading** — fastembed model loads on first call. Tests pass a no-op stub so `just test` doesn't trigger the ~550 MB download. Don't eager-load at import time.
- **Prompt cache warm pattern** (`slopmortem/ingest.py`): first entry runs alone, then the rest fan out. Emits `CACHE_READ_RATIO_LOW` if the ratio drops under 0.80 across the first 5 responses. Preserve this when refactoring ingest.
- **Injection marker** (`slopmortem/stages/synthesize.py`): when the synthesis LLM emits `where_diverged == "prompt_injection_attempted"` (compared against `_INJECTION_MARKER` defined at `synthesize.py:72`, compared at `synthesize.py:122`), the stage flips `injection_detected=True` and `consolidate_risks` short-circuits to an empty risk list. Don't normalize the marker string away in prompts or post-processing.

## Forbidden / discouraged

- **Don't import `slopmortem.evals` from `slopmortem.llm` or other prod modules.** Direction is one-way: evals → llm. Test infra must not bleed into prod.
- **Don't abort ingest on per-entry failures** (source fetch, slop classifier, enricher) — log and continue. Run-level failures (budget exceeded) are the exception.
- **Don't hardcode embedding dimensions or model IDs** in stage code. They live in config / the embedding client.
- **Don't add `# type: ignore`** to silence basedpyright — fix the type. If a third-party stub is missing, narrow with `cast` and a one-line comment explaining why.
- **Don't commit `.live.yaml` cassettes, `.env`, `slopmortem.local.toml`, `journal.sqlite`, or `post_mortems/`.** Already gitignored — don't `git add -f`.
- **Don't bump pinned models** (`model_facet`, `model_synthesize`, etc.) without re-recording cassettes. The eval baseline will drift silently otherwise.

## Layout

```
slopmortem/
  cli.py              # typer entrypoints (ingest, query, replay, reclassify, …)
  pipeline.py         # pure orchestration, deps injected
  ingest.py           # source → slop → facet → summarize → qdrant fan-out
  config.py           # pydantic-settings, TOML + env precedence
  models.py           # Pydantic schemas, taxonomies
  budget.py           # cost ceilings, anyio.Lock-guarded bookkeeping
  concurrency.py      # gather_resilient + capacity limiters
  errors.py           # typed errors (RerankLengthError, …); BudgetExceededError lives in budget.py, NoCanned* in llm/
  http.py             # safe HTTP client (SSRF guard)
  render.py           # report → markdown
  corpus/             # qdrant_store, merge journal, entity_resolution, sources/, alias_graph
  stages/             # facet_extract, retrieve, llm_rerank, synthesize, consolidate_risks
  llm/                # openrouter, embeddings (fastembed/openai), fakes, prompts
  evals/              # runner, cassette recording, assertions  (NEVER imported by prod)
  tracing/            # Laminar wiring + span events
tests/                # mirrors slopmortem/ layout
docs/                 # architecture.md, cassettes.md, specs/, plans/
data/                 # crunchbase CSVs, qdrant volume (gitignored)
external/             # crunchbase-data submodule
```

## Commit style

Terse subjects: `fix`, `upd`, `docs`, `cleanup`, `wiring`, `idiomatic pass`. Multi-feature work gets a PR number suffix (e.g. `consolidate risks (#29)`). Don't add `Co-Authored-By` trailers. One concern per commit when feasible.

## Plans

In-progress and historical implementation plans live under `docs/plans/` (dated, e.g. `2026-04-30-consolidate-risks.md`). Read the relevant plan before changing the area it covers; if you're starting non-trivial work without a plan, write one there first.
