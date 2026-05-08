# Strict Sector Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or `/team-feature` to implement this plan task-by-task, per the Execution Strategy below. Steps use checkbox (`- [ ]`) syntax — these are **persistent durable state**, not visual decoration. The executor edits the plan file in place: `- [ ]` → `- [x]` the instant a step verifies, before moving on. On resume (new session, crash, takeover), the executor scans existing `- [x]` marks and skips them — these steps are NOT redone. TodoWrite mirrors this state in-session; the plan file is the source of truth across sessions.

**Goal:** Add an opt-in hard filter on `facets.sector` at retrieve time, to complement (not replace) the existing soft sector boost. With the flag off, behaviour is unchanged. With the flag on, `Corpus.query` filters candidates to `MatchAny([pitch.sector, "other"])` so wrong-vertical entries cannot reach the rerank — the canonical example: a Hacken-style Web3-security pitch (sector `crypto_web3`) surfacing general cyber-threat-intel deaths (Norse Corp, Carbon Black) because their non-sector facet boosts compensate for the sector mismatch. A second tighter flag drops the `"other"` safety valve for operators who have audited that bucket.

**Relationship to the LLM recall fallback plan** (`docs/plans/2026-05-08-llm-recall-fallback.md`): the recall plan's Task 1 originally bundled this same filter. This plan extracts it into a standalone PR. **Whichever lands first owns the filter; the other plan drops its corresponding scope.** Behavioural defaults are off, so default-call behaviour is unchanged either way — that's the only "independence" claim. The file-level overlap (config keys, Protocol kwargs, `_qdrant_store.py` filter logic, `retrieve.py` plumbing, `pipeline.py` wiring) is total, so coordination is required, not optional.

**Architecture:**

The current `QdrantCorpus.query` (`slopmortem/corpus/_qdrant_store.py:111-180`) applies `sector` only as a **soft boost** through the `FormulaQuery` path (line 162-166), alongside four other facet boosts. The only **hard** payload filter today is `_build_recency_filter` (line 176-179, definition at line 342-382), AND-combined into `query_filter`.

This plan adds a sibling helper `_build_sector_filter` and AND-combines it with the recency filter into one `query_filter`:

```
query_filter = AND(
    _build_recency_filter(cutoff_iso, strict_deaths),     # existing
    _build_sector_filter(facets.sector, strict, exclude), # NEW
)
```

`_build_sector_filter` returns:

| `strict_sector_filter` | `strict_sector_filter_excludes_other` | `pitch.sector` | filter returned |
|---|---|---|---|
| `False` | * | * | `None` (no-op — current behaviour) |
| `True` | `False` | `"other"` | `None` (uninformative — pitch sector is unknown) |
| `True` | `False` | `"crypto_web3"` | `MatchAny(["crypto_web3", "other"])` — keep `"other"` reachable |
| `True` | `True` | `"other"` | `None` (same reason as above) |
| `True` | `True` | `"crypto_web3"` | `MatchValue("crypto_web3")` — strict, drop `"other"` |

The Protocol surface change is two new keyword arguments on `Corpus.query`. With a default of `False` on both, every existing callsite stays correct without edits. The 3 fake `Corpus` impls (`tests/test_pipeline_e2e.py:247`, `tests/test_observe_redaction.py:219`, `tests/test_synthesis_tools.py:43`) absorb the kwargs as no-ops; `QdrantCorpus.query` is the only impl that acts on them.

**Tech Stack:** Python 3.13, anyio, qdrant-client (existing), pydantic-settings, pytest with `asyncio_mode="auto"`. Live qdrant integration test gated by the existing `requires_qdrant` marker.

## Execution Strategy

**Subagents** — default. Three tasks, sequential because each task's tests depend on the previous task's surface area.

## Task Dependency Graph

- Task 1 [AFK]: `_build_sector_filter` helper + Protocol kwarg widening + 3 fake updates → depends on `none` → batch 1
- Task 2 [AFK]: config keys + `retrieve()` + `pipeline.py` plumbing → depends on `Task 1` → batch 2
- Task 3 [AFK]: live `requires_qdrant` integration test for the filter → depends on `Task 2` → batch 3
- Polish: post-implementation-polish + lint + typecheck on the diff → depends on `Task 3` → batch 4

## Agent Assignments

- Task 1: filter helper + Protocol → python-development:python-pro
- Task 2: config + plumbing → python-development:python-pro
- Task 3: qdrant integration test → python-development:python-pro
- Polish: post-implementation-polish → general-purpose

---

## File Structure

**New:** none.

**Modified:**

- `slopmortem/corpus/_qdrant_store.py` — add `_build_sector_filter(*, sector: str, strict: bool, exclude_other: bool) -> Filter | None` adjacent to `_build_recency_filter` (around `:342`); call it from `QdrantCorpus.query` (`:176`) and AND-combine its result with the recency filter into `query_filter`. Sector value comes from `facets.sector`. Widen the `Corpus.query` signature with two new keyword arguments — `strict_sector_filter: bool = False`, `strict_sector_filter_excludes_other: bool = False` — propagating defaults through to keep callers correct.
- `slopmortem/corpus/_store.py` — widen the `Corpus` Protocol `query` signature in lockstep (`:15-24`). Defaults match `QdrantCorpus`.
- `tests/test_pipeline_e2e.py` (line `:247`) — `_FakeCorpus.query` accepts the two new kwargs (records them in `self.queries` for assertion symmetry; no behavioural change).
- `tests/test_observe_redaction.py` (line `:219`) — same widening.
- `tests/test_synthesis_tools.py` (line `:43`) — same widening.
- `slopmortem/config.py` — add `strict_sector_filter: bool = False` and `strict_sector_filter_excludes_other: bool = False`. Group with the existing `min_similarity_score` retrieval knobs.
- `slopmortem.toml` — add commented-out defaults so the surface is documented.
- `slopmortem/stages/retrieve.py` — thread `strict_sector_filter` and `strict_sector_filter_excludes_other` from caller through to `corpus.query()`. Defaults `False` so existing callers don't need to change immediately.
- `slopmortem/pipeline.py` (`:156-165`) — pass `strict_sector_filter=config.strict_sector_filter`, `strict_sector_filter_excludes_other=config.strict_sector_filter_excludes_other` into `retrieve()`.
- `tests/corpus/test_qdrant_store.py` — append a `requires_qdrant` integration test that seeds two points with different sectors, queries with the flag on, and asserts only the matching-sector point comes back.

**Decisions:**

- **Hard filter, not just lifting the boost weight.** The current sector boost is multiplicative (`× facet_boost=0.01`) on top of `$score`; raising the boost would just push wrong-sector candidates further down the list, not eliminate them. A score-ordered top-K pull always risks them re-surfacing if their dense+sparse RRF score is high enough. Hard filter is the only mechanism that gives the operator a categorical "this pitch never sees wrong-sector candidates" guarantee. Auto-selected — alternatives don't hit the goal.
- **`MatchAny([sector, "other"])` default over pure `MatchValue`.** The `"other"` bucket holds ~2% of the corpus. The historical motivator was 7 telecom-shaped misclassifications under the pre-`9d24034` taxonomy; commit `9d24034` added `telecom` as a taxonomy value, but the 7 already-ingested entries remain `other`-tagged until a backfill reclassifies them (deferred per the recall plan's Task 8). Permanently hiding `"other"` would leak those misclassified-but-relevant entries; surfacing them with a soft tighter flag (`strict_sector_filter_excludes_other`) lets operators who've audited the bucket opt into pure `MatchValue`. Trade-off: default-on `"other"` admits some noise on clean-sector queries. Acceptable — wrong-sector noise is what the strict filter targets, not `"other"`-tagged noise.
- **Two flags, not one tri-state enum.** `strict_sector_filter` (off / on) plus `strict_sector_filter_excludes_other` (independent narrowing) reads cleaner in `slopmortem.toml` and lets `STRICT_SECTOR_FILTER=true` env-var override the primary switch without forcing operators to remember an enum string. Trade-off: a third `True/True` × `pitch.sector="other"` row exists in the truth table and must be tested. Acceptable — the table is small enough.
- **Skip the filter when `pitch.sector == "other"`.** Pitch sector `"other"` means the facet extractor couldn't classify; filtering on `"other"` would either match all `"other"`-tagged corpus entries (noise dominated by misclassifications) or, with `excludes_other=True`, return nothing. Returning `None` from the helper short-circuits the AND-combine and matches the existing "uninformative facets skip the boost" pattern at `_qdrant_store.py:164`. Auto-selected — alternatives are worse.
- **No CLI flag.** This is an operator-level retrieval dial, not a per-query knob. TOML + env var is the right surface. A `--strict-sector-filter` flag could be added later if a use case appears, but speculative CLI surface is out of scope.
- **Sequenced with — not independent of — the LLM recall plan.** The recall plan's Task 1 covers identical scope (config keys, Protocol kwarg, `_qdrant_store.py` filter, `retrieve.py` plumbing, `pipeline.py` wiring). Behavioural defaults are off in both plans, so default-call behaviour doesn't conflict, but file edits do. Whichever plan lands first owns the filter; the other drops its corresponding task with a one-line "superseded by ..." pointer. Ship-order doesn't matter for correctness; coordination is non-optional.
- **AND-combine via Filter nesting, not by merging `must` clauses.** Wrapping both filters under `Filter(must=[recency, sector])` works for every clause shape — including the non-strict-deaths recency branch, which is `Filter(should=[…])` and so has `must=None`. Clause-list merging (`must=[*recency.must, *sector.must]`) crashes on `None` *and* silently drops the `should` branches when recency is should-shaped. Nesting is one extra wrapper level in the wire format that Qdrant evaluates as a sub-clause inside `must`.

---

### Task 1: `_build_sector_filter` helper + `Corpus.query` Protocol widening

**Files:**

- Modify: `slopmortem/corpus/_qdrant_store.py` (`:111-180` for the call; `:342` for the helper insertion point).
- Modify: `slopmortem/corpus/_store.py` (`:15-24`).
- Modify: `tests/test_pipeline_e2e.py:247`, `tests/test_observe_redaction.py:219`, `tests/test_synthesis_tools.py:43` (fake `query` widening — three sites total).
- Test: `tests/corpus/test_qdrant_filter.py` (NEW) for the pure-fn truth table.

- [x] **Step 1: Write the failing pure-fn tests**

Create `tests/corpus/test_qdrant_filter.py` with a table-driven test of `_build_sector_filter`. Cover all 5 truth-table rows from the architecture section. Import the helper directly:

```python
from slopmortem.corpus._qdrant_store import _build_sector_filter

def test_build_sector_filter_disabled_returns_none():
    assert _build_sector_filter(sector="crypto_web3", strict=False, exclude_other=False) is None
    assert _build_sector_filter(sector="crypto_web3", strict=False, exclude_other=True) is None

def test_build_sector_filter_pitch_other_returns_none():
    # Pitch sector "other" is uninformative — filter must not narrow.
    assert _build_sector_filter(sector="other", strict=True, exclude_other=False) is None
    assert _build_sector_filter(sector="other", strict=True, exclude_other=True) is None

def test_build_sector_filter_strict_keeps_other():
    f = _build_sector_filter(sector="crypto_web3", strict=True, exclude_other=False)
    # Filter must include both "crypto_web3" and "other".
    [cond] = f.must
    assert cond.key == "facets.sector"
    assert sorted(cond.match.any) == ["crypto_web3", "other"]

def test_build_sector_filter_strict_excludes_other():
    f = _build_sector_filter(sector="crypto_web3", strict=True, exclude_other=True)
    [cond] = f.must
    assert cond.key == "facets.sector"
    assert cond.match.value == "crypto_web3"
```

- [x] **Step 2: Run tests to verify they fail**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/corpus/test_qdrant_filter.py -v`
Expected: FAIL — `ImportError: cannot import name '_build_sector_filter'`.

- [x] **Step 3: Add the helper**

Insert directly above `_build_recency_filter` in `slopmortem/corpus/_qdrant_store.py` (around `:342`):

`Filter`, `FieldCondition`, and `MatchValue` are already imported at the top of `_qdrant_store.py:16-26`; only `MatchAny` needs adding to that import block. Annotating the return as `Filter | None` (instead of `Any | None`) avoids the `# pyright: ignore[reportExplicitAny]` escape hatch that the helper's neighbour `_build_recency_filter` uses — the project rule is "fix the type, don't ignore" (CLAUDE.md).

```python
def _build_sector_filter(
    *, sector: str, strict: bool, exclude_other: bool
) -> Filter | None:
    """Hard payload filter on ``facets.sector``. ``None`` = no narrowing.

    The current behaviour (no filter; sector participates only as a soft boost)
    is the ``strict=False`` branch. ``strict=True`` enforces the pitch's sector
    at retrieve time. ``exclude_other=True`` further drops the ``"other"``
    safety valve — only set if the corpus's ``"other"`` bucket has been
    audited and intentional misclassifications have been reclassified.

    Returns ``None`` when ``pitch.sector == "other"`` regardless of the flags:
    the pitch sector is uninformative, and filtering on ``"other"`` would
    either match misclassification noise or (with ``exclude_other=True``)
    return nothing.
    """
    if not strict or sector == "other":
        return None

    if exclude_other:
        cond = FieldCondition(key="facets.sector", match=MatchValue(value=sector))
    else:
        cond = FieldCondition(
            key="facets.sector", match=MatchAny(any=[sector, "other"])
        )
    return Filter(must=[cond])
```

- [x] **Step 4: Run pure-fn tests to verify they pass**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/corpus/test_qdrant_filter.py -v`
Expected: all 4 PASS.

- [x] **Step 5: Widen the `Corpus` Protocol**

Edit `slopmortem/corpus/_store.py:15-24`:

```python
async def query(  # noqa: PLR0913 — Protocol method signature is the public contract
    self,
    *,
    dense: list[float],
    sparse: dict[int, float],
    facets: Facets,
    cutoff_iso: str | None,
    strict_deaths: bool,
    k_retrieve: int,
    strict_sector_filter: bool = False,
    strict_sector_filter_excludes_other: bool = False,
) -> list[Candidate]: ...
```

- [x] **Step 6: Wire `_build_sector_filter` into `QdrantCorpus.query`**

Two edits to `slopmortem/corpus/_qdrant_store.py`:

**(a) Widen the `query` signature (`:111-120`)** with the two new kwargs (defaults `False` so existing callers stay correct):

```python
async def query(  # noqa: PLR0913 — Protocol method signature is the public contract
    self,
    *,
    dense: list[float],
    sparse: dict[int, float],
    facets: Facets,
    cutoff_iso: str | None,
    strict_deaths: bool,
    k_retrieve: int,
    strict_sector_filter: bool = False,
    strict_sector_filter_excludes_other: bool = False,
) -> list[Candidate]:
```

**(b) AND-combine the sector filter** immediately after the existing `query_filter = _build_recency_filter(...)` call (`:176-179`). Qdrant evaluates a nested `Filter` as a sub-clause inside `must`:

```python
sector_filter = _build_sector_filter(
    sector=facets.sector,
    strict=strict_sector_filter,
    exclude_other=strict_sector_filter_excludes_other,
)
if sector_filter is not None:
    if query_filter is None:
        query_filter = sector_filter
    else:
        # Nest, don't merge clauses: _build_recency_filter returns
        # Filter(should=[…]) when strict_deaths=False, so query_filter.must
        # is None and merging clauses would crash and/or silently drop the
        # should branches. Wrapping both filters under an outer must=[…]
        # AND-combines them regardless of which clause shape each carries.
        query_filter = Filter(must=[query_filter, sector_filter])
```

- [x] **Step 7: Update the 3 fake `Corpus` impls**

Each fake just needs to accept and (for assertion symmetry) record the two new kwargs. No behavioural change.

In `tests/test_pipeline_e2e.py:247`, append the kwargs to `_FakeCorpus.query`:

```python
async def query(  # noqa: PLR0913 - Protocol contract dictates the signature
    self,
    *,
    dense: list[float],
    sparse: dict[int, float],
    facets: Facets,
    cutoff_iso: str | None,
    strict_deaths: bool,
    k_retrieve: int,
    strict_sector_filter: bool = False,
    strict_sector_filter_excludes_other: bool = False,
) -> list[Candidate]:
    self.queries.append(
        {
            "dense_dim": len(dense),
            "sparse_keys": list(sparse.keys()),
            "facets": facets.model_dump(),
            "cutoff_iso": cutoff_iso,
            "strict_deaths": strict_deaths,
            "k_retrieve": k_retrieve,
            "strict_sector_filter": strict_sector_filter,
            "strict_sector_filter_excludes_other": strict_sector_filter_excludes_other,
        }
    )
    return list(self.candidates[:k_retrieve])
```

Apply the same kwarg widening to:

- `tests/test_observe_redaction.py:219` — its fake also records args via `self.queries.append({...})` (see `:229-234`), so add the same two recorded keys for symmetry.
- `tests/test_synthesis_tools.py:43` — this fake discards args (`_ = (dense, sparse, …)` at `:53`); add the kwargs to the signature and to the discard tuple, no recording dict to update.

- [x] **Step 8: Run the full unit test suite**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -n auto -m "not requires_qdrant and not slow"`
Expected: all PASS. The fake `Corpus` widening is the smoke test that the Protocol contract is satisfied.

- [x] **Step 9: Lint + typecheck**

Run: `just lint && just typecheck`
Expected: clean. basedpyright will catch any fake `Corpus` impl that didn't get its signature widened in lockstep.

- [x] **Step 10: Commit**

```
git add slopmortem/corpus/_qdrant_store.py slopmortem/corpus/_store.py \
        tests/corpus/test_qdrant_filter.py \
        tests/test_pipeline_e2e.py tests/test_observe_redaction.py \
        tests/test_synthesis_tools.py && \
git commit -m "retrieve: hard sector filter (corpus protocol + qdrant impl)"
```

---

### Task 2: Config keys + `retrieve()` and `pipeline.py` plumbing

**Files:**

- Modify: `slopmortem/config.py` — add the two new keys.
- Modify: `slopmortem.toml` — add commented documentation.
- Modify: `slopmortem/stages/retrieve.py` — thread the kwargs from caller to `corpus.query()`.
- Modify: `slopmortem/pipeline.py` (`:156-165`) — pass `config.strict_sector_filter`, `config.strict_sector_filter_excludes_other` into `retrieve()`.
- Test: `tests/test_pipeline_e2e.py` (append) — assert the flag flows from config to `_FakeCorpus.query`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_pipeline_e2e.py`:

```python
async def test_strict_sector_filter_flows_from_config_to_corpus(...):
    """Config flag `strict_sector_filter=True` must reach `Corpus.query`."""
    # Build the same fixture the existing pipeline E2E test uses, but with
    # config.strict_sector_filter = True. Run the pipeline. Assert that
    # `_FakeCorpus.queries[0]["strict_sector_filter"] is True`.
```

(Mirror the existing happy-path E2E fixture; no need to invent a new corpus.)

- [ ] **Step 2: Run test to verify it fails**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_pipeline_e2e.py -v -k strict_sector`
Expected: FAIL — config keys don't exist yet, OR the kwarg never reaches the fake's recorded `queries` dict.

- [ ] **Step 3: Add the config keys**

Edit `slopmortem/config.py` — add near the existing retrieval knobs:

```python
strict_sector_filter: bool = False
strict_sector_filter_excludes_other: bool = False
```

- [ ] **Step 4: Document in `slopmortem.toml`**

Append commented-out defaults in the retrieval section:

```toml
# Hard filter on `facets.sector` at retrieve time. Off by default — the
# soft sector boost is unchanged. Turn on to prevent wrong-vertical
# entries from reaching the rerank stage. The "other" bucket stays
# reachable unless `strict_sector_filter_excludes_other` is also set.
# strict_sector_filter = false
# strict_sector_filter_excludes_other = false
```

- [ ] **Step 5: Thread through `retrieve.py`**

Edit `slopmortem/stages/retrieve.py`:

1. Add the two kwargs to the `retrieve` function signature with defaults `False`.
2. Pass them through to `corpus.query(…)`.

- [ ] **Step 6: Wire `pipeline.py` call site**

Edit `slopmortem/pipeline.py:156-165`:

```python
retrieved = await retrieve(
    description=input_ctx.description,
    facets=facets,
    corpus=corpus,
    embedding_client=embedding_client,
    cutoff_iso=cutoff,
    strict_deaths=config.strict_deaths,
    k_retrieve=config.K_retrieve,
    sparse_encoder=sparse_encoder,
    strict_sector_filter=config.strict_sector_filter,
    strict_sector_filter_excludes_other=config.strict_sector_filter_excludes_other,
)
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_pipeline_e2e.py -v -k strict_sector`
Expected: PASS.

- [ ] **Step 8: Run the full suite**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -n auto -m "not requires_qdrant and not slow"`
Expected: all PASS.

- [ ] **Step 9: Lint + typecheck**

Run: `just lint && just typecheck`
Expected: clean.

- [ ] **Step 10: Commit**

```
git add slopmortem/config.py slopmortem.toml \
        slopmortem/stages/retrieve.py slopmortem/pipeline.py \
        tests/test_pipeline_e2e.py && \
git commit -m "retrieve: thread strict_sector_filter from config through pipeline"
```

---

### Task 3: Live qdrant integration test

**Files:**

- Modify: `tests/corpus/test_qdrant_store.py` — append two `requires_qdrant` tests covering: (a) `MatchAny([sector, "other"])` default returns sector + other, drops mismatched; (b) `excludes_other=True` returns sector only.

- [ ] **Step 1: Write the failing tests**

Append to `tests/corpus/test_qdrant_store.py`:

```python
@pytest.mark.requires_qdrant
async def test_strict_sector_filter_default_keeps_sector_and_other(
    qdrant_client: AsyncQdrantClient, tmp_path: Path
) -> None:
    """With strict=True, exclude_other=False: keep crypto_web3 + other, drop fintech."""
    name = "test_strict_sector_default"
    corpus = await _build_corpus(qdrant_client, tmp_path, name)
    try:
        # Three points with three different sectors.
        await corpus.upsert_chunk(_make_chunk_with_sector("c:web3", 0, "crypto_web3"))
        await corpus.upsert_chunk(_make_chunk_with_sector("c:other", 1, "other"))
        await corpus.upsert_chunk(_make_chunk_with_sector("c:fin", 2, "fintech"))

        candidates = await corpus.query(
            dense=[0.001] * _DIM, sparse={0: 1.0},
            facets=_facets_with_sector("crypto_web3"),
            cutoff_iso=None, strict_deaths=False, k_retrieve=10,
            strict_sector_filter=True, strict_sector_filter_excludes_other=False,
        )
        sectors = {c.payload.facets.sector for c in candidates}
        assert sectors == {"crypto_web3", "other"}
    finally:
        await qdrant_client.delete_collection(name)


@pytest.mark.requires_qdrant
async def test_strict_sector_filter_excludes_other_returns_only_sector(
    qdrant_client: AsyncQdrantClient, tmp_path: Path
) -> None:
    """With strict=True, exclude_other=True: keep crypto_web3 only."""
    name = "test_strict_sector_exclude_other"
    corpus = await _build_corpus(qdrant_client, tmp_path, name)
    try:
        await corpus.upsert_chunk(_make_chunk_with_sector("c:web3", 0, "crypto_web3"))
        await corpus.upsert_chunk(_make_chunk_with_sector("c:other", 1, "other"))
        await corpus.upsert_chunk(_make_chunk_with_sector("c:fin", 2, "fintech"))

        candidates = await corpus.query(
            dense=[0.001] * _DIM, sparse={0: 1.0},
            facets=_facets_with_sector("crypto_web3"),
            cutoff_iso=None, strict_deaths=False, k_retrieve=10,
            strict_sector_filter=True, strict_sector_filter_excludes_other=True,
        )
        sectors = {c.payload.facets.sector for c in candidates}
        assert sectors == {"crypto_web3"}
    finally:
        await qdrant_client.delete_collection(name)
```

The query vector is `dense=[0.001] * _DIM` (matches the existing `_make_chunk` convention at `test_qdrant_store.py:25`); a true zero vector triggers undefined behaviour under cosine distance. The sparse half is `{0: 1.0}` rather than `{}` because the inner `Prefetch` builds a `SparseVector(indices=[], values=[])` from an empty dict, which Qdrant may reject in the hybrid path.

Add two local helpers at the top of the file. `_make_chunk_with_sector` builds a full `CandidatePayload`-shaped payload (the read path runs payload through `_payload_dict_to_candidate_payload`, which validates every required field — minimal `{canonical_id, chunk_idx}` payloads pass `delete_chunks_for_canonical` tests but not `query`). `_facets_with_sector` builds a `Facets` with the closed-set taxonomy values:

```python
def _make_chunk_with_sector(canonical_id: str, idx: int, sector: str) -> _Point:
    """Like ``_make_chunk`` but with a full CandidatePayload — ``query`` validates payloads."""
    dense = [float((idx + 1) * 0.001)] * _DIM
    sparse: dict[int, float] = {idx: 1.0}
    payload = {
        "canonical_id": canonical_id,
        "chunk_idx": idx,
        "name": f"chunk-{canonical_id}",
        "summary": "fixture",
        "body": "fixture body",
        "facets": _facets_with_sector(sector).model_dump(),
        "founding_date": None,
        "failure_date": None,
        "founding_date_unknown": True,
        "failure_date_unknown": True,
        "provenance": "curated_real",
        "slop_score": 0.0,
        "sources": [],
        "provenance_id": "",
        "text_id": canonical_id.replace(":", "_"),
    }
    return _Point(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"{canonical_id}:{idx}").hex,
        vector={"dense": dense, "sparse": sparse},
        payload=payload,
    )


def _facets_with_sector(sector: str) -> Facets:
    """Pick any valid taxonomy values for the non-sector fields.

    Values below are taken from ``slopmortem/corpus/taxonomy.yml`` — bump the
    test if the taxonomy renames any of these enums.
    """
    return Facets(
        sector=sector,  # pyright: ignore[reportArgumentType]  # validated at runtime
        business_model="b2b_saas",
        customer_type="enterprise",
        geography="us",
        monetization="subscription_recurring",
    )
```

- [ ] **Step 2: Run tests against live qdrant**

Run: `docker compose up -d qdrant && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/corpus/test_qdrant_store.py -v -k strict_sector -m requires_qdrant`
Expected: both PASS — Task 1's `_build_sector_filter` and Task 2's plumbing are already in place, so this is a confidence test against live qdrant rather than a TDD gate. If the tests fail, the most likely cause is a payload-shape mismatch in `_make_chunk_with_sector` or a misseeded collection — diagnose before assuming a filter regression.

- [ ] **Step 3: Lint + typecheck**

Run: `just lint && just typecheck`
Expected: clean.

- [ ] **Step 4: Commit**

```
git add tests/corpus/test_qdrant_store.py && \
git commit -m "retrieve: live qdrant test for strict sector filter"
```

---

### Polish

- [ ] **Step 1: Run `post-implementation-polish` over the diff**

Use the `post-implementation-polish` skill on the touched files:

```
slopmortem/corpus/_qdrant_store.py
slopmortem/corpus/_store.py
slopmortem/config.py
slopmortem/stages/retrieve.py
slopmortem/pipeline.py
slopmortem.toml
tests/corpus/test_qdrant_filter.py
tests/corpus/test_qdrant_store.py
tests/test_pipeline_e2e.py
tests/test_observe_redaction.py
tests/test_synthesis_tools.py
```

Apply review-round fixes, then idiomatic pass, then `/cleanup` over the diff, then strip AI comments.

- [ ] **Step 2: Full lint + typecheck + test sweep**

Run: `just lint && just typecheck && just test`
Expected: clean.

- [ ] **Step 3: Manual smoke**

```
echo 'strict_sector_filter = true' >> slopmortem.local.toml
just query "Web3-native smart contract audit firm with on-chain monitoring"
```

Expected: rendered report shows only `crypto_web3` (or `"other"`) sector entries; no fintech-tagged or unrelated-vertical entries leak in.

Roll back the `slopmortem.local.toml` edit before committing.

- [ ] **Step 4: Final commit (if any polish edits)**

Only if polish produced edits. Otherwise skip.

```
git add -- <touched files> && git commit -m "retrieve: polish + smoke for strict sector filter"
```
