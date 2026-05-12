# Skip-Already-Processed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or `/team-feature` to implement this plan task-by-task, per the Execution Strategy below. Steps use checkbox (`- [ ]`) syntax — these are **persistent durable state**, not visual decoration. The executor edits the plan file in place: `- [ ]` → `- [x]` the instant a step verifies, before moving on. On resume (new session, crash, takeover), the executor scans existing `- [x]` marks and skips them — these steps are NOT redone. TodoWrite mirrors this state in-session; the plan file is the source of truth across sessions.

**Goal:** Stop re-paying Haiku + Tavily on entries the journal already considers done. Re-runs of `slopmortem ingest` should skip any (source, source_id) whose merge_journal row is `complete` or whose quarantine_journal row exists, before the enricher chain runs.

**Architecture:** Add `MergeJournal.is_terminal(source, source_id) -> bool` that returns True when a `merge_state="complete"` row exists OR a quarantine row exists for that key. Wire a pre-enrich gate at the top of `_classify_phase._one` (`slopmortem/ingest/_ingest.py:139`) that calls `is_terminal` and short-circuits the entry as a `skipped` count before any enricher runs. Thread the existing `force` flag from `ingest()` into `_classify_phase` so `--force` bypasses the gate (matches the existing semantics of `force` in the write-phase `skip_key` check at `_journal_writes.py:128-132`).

**Tech Stack:** Python 3.13, anyio, sqlite3 via `anyio.to_thread.run_sync`, pytest with `asyncio_mode="auto"`.

## Execution Strategy

**Subagents** — default; no spec override. Tasks 1 and 2 must run sequentially because Task 2's gate calls the method introduced in Task 1.

## Task Dependency Graph

- Task 1 [AFK]: `MergeJournal.is_terminal` → depends on `none` → batch 1
- Task 2 [AFK]: pre-enrich gate in `_classify_phase` → depends on `Task 1` → batch 2
- Polish: post-implementation-polish → depends on `Task 2` → batch 3

## Agent Assignments

- Task 1: `MergeJournal.is_terminal` → python-development:python-pro
- Task 2: pre-enrich gate + force threading → python-development:python-pro
- Polish: post-implementation-polish → general-purpose

---

## File Structure

**New:** none.

**Modified:**
- `slopmortem/corpus/_merge.py` — add `is_terminal` async method on `MergeJournal` plus its `_is_terminal_sync` helper. Mirror the `_lookup_reverse_sync` shape: one SQL query against `merge_journal` (filter `merge_state='complete'`), one against `quarantine_journal`, return True on the first hit.
- `slopmortem/ingest/_ingest.py:113-242` — add `force: bool` parameter to `_classify_phase`; thread it from `ingest()` (`:402-412`); insert a pre-enrich gate at the top of `_one` (`:140`) that returns `None` after `result.skipped += 1` when `force` is False and `is_terminal(source, source_id)` is True.
- `tests/corpus/test_merge_journal.py` — append three tests covering complete/quarantine/neither.
- `tests/ingest/test_orchestration.py` — append one test asserting the gate skips at classify time and the enricher chain is never called.

**Decisions:**

- **Coarse `(source, source_id)` match vs precise `skip_key`.** The write-phase dedup (`_journal_writes.py:128-132`) already gates on a precise `skip_key` (content_hash + prompt SHAs + model IDs). The pre-enrich gate cannot use `skip_key` because the body and the prompt SHAs both arrive *after* enrichment runs. Coarse is the only option at this seam. Trade-off: bumping `model_pitch_filler` or editing `pitch_filler.j2` will not invalidate already-processed entries; users must `--force` after such a bump. Mirrors the existing CLAUDE.md guidance ("Don't bump pinned models without re-recording cassettes"), so the manual escape hatch is acceptable.

- **Terminal states: `complete` and quarantine only.** `pending`, `alias_blocked`, `resolver_flipped` left out on purpose. `pending` rows come from a crashed previous run — the existing write-phase logic re-processes them when `merge_state != "complete"`, and the pre-enrich gate matches that. `alias_blocked` and `resolver_flipped` are entity-resolution decisions that occur after enrichment was already paid for; treating them as terminal would silently lock in stale resolutions when the alias graph is edited. Bias toward re-running these rare states; the user controls cost via `--force` and `--limit`. Auto-selected — no downsides compared to alternatives.

- **Reuse `result.skipped` vs new `result.already_processed` counter.** Lump into `skipped`. The counter currently covers "title pre-filter rejected" and "empty body" — both "we saw it but didn't process it". Adding a third reason fits the existing meaning. A separate counter would touch `IngestResult` (`_ports.py:116`), every progress renderer, and run summary logs for marginal observability gain. The classify-phase log line already prints reason text, so blame is recoverable from the log without a new counter.

- **One SQLite query or two.** Two SELECTs (one per table) over a UNION'd query. Two SELECTs are clearer to read and to update independently; the cost is two cursor round-trips per entry against a local SQLite file (microseconds). UNION SQL is hard to read and harder to evolve when the schemas drift. Auto-selected — no downsides compared to alternatives.

---

### Task 1: `MergeJournal.is_terminal`

**Files:**
- Modify: `slopmortem/corpus/_merge.py` — add method directly after `lookup_canonical_for_source` (around `:291-310`) so terminal-state queries cluster together.
- Test: `tests/corpus/test_merge_journal.py` (append).

- [x] **Step 1: Write the failing tests**

Append to `tests/corpus/test_merge_journal.py`:

```python
async def test_is_terminal_returns_false_when_unseen(journal):
    assert await journal.is_terminal("hn", "999") is False


async def test_is_terminal_true_for_complete_row(journal):
    await journal.upsert_pending(canonical_id="acme.com", source="hn", source_id="1")
    # pending alone must NOT count as terminal — a crashed prior run should re-run.
    assert await journal.is_terminal("hn", "1") is False
    await journal.mark_complete(
        canonical_id="acme.com",
        source="hn",
        source_id="1",
        skip_key="k1",
        merged_at="2026-05-08T00:00:00Z",
    )
    assert await journal.is_terminal("hn", "1") is True


async def test_is_terminal_true_for_quarantined_row(journal):
    await journal.write_quarantine(
        content_sha256="0" * 64,
        source="hn",
        source_id="2",
        reason="slop",
        slop_score=0.92,
    )
    assert await journal.is_terminal("hn", "2") is True


async def test_is_terminal_false_for_alias_blocked(journal):
    # alias_blocked / resolver_flipped are mid-flight entity-resolution decisions,
    # not terminal — re-run is expected when the alias graph is edited.
    from slopmortem.models import AliasEdge

    edge = AliasEdge(
        canonical_id="a.com",
        alias_kind="rebranded_to",
        target_canonical_id="b.com",
        evidence_source_id="hn:3",
        confidence=0.9,
    )
    await journal.upsert_alias_blocked(
        canonical_id="a.com", source="hn", source_id="3", alias_edge=edge
    )
    assert await journal.is_terminal("hn", "3") is False
```

- [x] **Step 2: Run tests to verify they fail**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/corpus/test_merge_journal.py -v -k is_terminal`
Expected: FAIL — `AttributeError: 'MergeJournal' object has no attribute 'is_terminal'`.

- [x] **Step 3: Add the method**

In `slopmortem/corpus/_merge.py`, insert directly after the `_lookup_reverse_sync` method (around `:294-310`):

```python
    async def is_terminal(self, source: str, source_id: str) -> bool:
        """Return True when (source, source_id) has reached a terminal state.

        Terminal = a ``merge_state='complete'`` row in ``merge_journal``, or any
        row in ``quarantine_journal``. Mid-flight states (``pending``,
        ``alias_blocked``, ``resolver_flipped``) deliberately count as
        non-terminal so a crashed prior run, an edited alias graph, or a
        re-resolved entity can re-run end-to-end. Used by the ingest classify
        phase to short-circuit before the enricher chain pays for an entry
        we already processed.
        """
        return await to_thread.run_sync(self._is_terminal_sync, source, source_id)

    def _is_terminal_sync(self, source: str, source_id: str) -> bool:
        with connect(self._db) as conn:
            cur = conn.execute(
                """
                SELECT 1 FROM merge_journal
                 WHERE source = ? AND source_id = ? AND merge_state = 'complete'
                 LIMIT 1
                """,
                (source, source_id),
            )
            if cur.fetchone() is not None:
                return True
            cur = conn.execute(
                """
                SELECT 1 FROM quarantine_journal
                 WHERE source = ? AND source_id = ?
                 LIMIT 1
                """,
                (source, source_id),
            )
            return cur.fetchone() is not None
```

- [x] **Step 4: Run tests to verify they pass**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/corpus/test_merge_journal.py -v`
Expected: all PASS, including the four new `is_terminal` tests and all existing ones.

- [x] **Step 5: Lint + typecheck**

Run: `just lint && just typecheck`
Expected: clean.

- [x] **Step 6: Commit**

`git add slopmortem/corpus/_merge.py tests/corpus/test_merge_journal.py && git commit -m "journal: is_terminal lookup for complete + quarantine"`

---

### Task 2: Pre-enrich gate in `_classify_phase`

**Files:**
- Modify: `slopmortem/ingest/_ingest.py:113-242` (the `_classify_phase` function and its inner `_one`) and `:402-412` (the `_classify_phase` call site inside `ingest()`).
- Test: `tests/ingest/test_orchestration.py` (append).

- [x] **Step 1: Write the failing test**

Read `tests/ingest/test_orchestration.py` first to see the existing fake-classifier / fake-llm patterns and import them. Then append:

```python
_HAIKU = "anthropic/claude-haiku-4.5"


async def test_already_processed_entry_skipped_before_enrichers_run(tmp_path):
    """Pre-enrich gate: a complete-in-journal entry must not invoke any enricher.

    ``source="hn"`` (not in ``_PRE_VETTED_SOURCES``) so the classifier_calls
    assertion is meaningful — without the gate, ``classify_one`` would invoke
    ``slop_classifier.score`` for non-pre-vetted entries.
    """
    from datetime import UTC, datetime

    from slopmortem.budget import Budget
    from slopmortem.config import Config
    from slopmortem.corpus import MergeJournal
    from slopmortem.ingest import InMemoryCorpus, ingest
    from slopmortem.llm import FakeEmbeddingClient, FakeLLMClient
    from slopmortem.models import RawEntry

    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()
    # Pre-seed the journal as if a prior run already completed this entry.
    await journal.upsert_pending(canonical_id="acme.com", source="hn", source_id="acme")
    await journal.mark_complete(
        canonical_id="acme.com",
        source="hn",
        source_id="acme",
        skip_key="prior",
        merged_at="2026-05-07T00:00:00Z",
    )

    class _OneShotSource:
        async def fetch(self):
            yield RawEntry(
                source="hn",
                source_id="acme",
                url="https://acme.com",
                raw_html=None,
                markdown_text="prior body",
                fetched_at=datetime(2026, 5, 8, tzinfo=UTC),
            )

    enricher_calls: list[str] = []

    class _RecordingEnricher:
        async def enrich(self, entry):
            enricher_calls.append(entry.source_id)
            return entry

    classifier_calls: list[str] = []

    class _RecordingClassifier:
        async def score(self, text: str) -> float:  # noqa: ARG002 - protocol body
            classifier_calls.append(text[:16])
            return 0.0

    config = Config()
    result = await ingest(
        sources=[_OneShotSource()],
        enrichers=[_RecordingEnricher()],
        journal=journal,
        corpus=InMemoryCorpus(),
        llm=FakeLLMClient(canned={}, default_model=_HAIKU),
        embed_client=FakeEmbeddingClient(model=config.embed_model_id),
        budget=Budget(cap_usd=1.0),
        slop_classifier=_RecordingClassifier(),
        config=config,
        post_mortems_root=tmp_path / "pm",
        sparse_encoder=lambda _t: {0: 1.0},
    )
    assert result.skipped == 1
    assert result.processed == 0
    assert enricher_calls == [], "pre-enrich gate must short-circuit BEFORE the enricher runs"
    assert classifier_calls == [], "pre-enrich gate must short-circuit BEFORE the classifier runs"


async def test_force_bypasses_already_processed_skip(tmp_path):
    """--force re-runs an already-complete entry through the enricher chain.

    ``FakeSlopClassifier(default_score=0.9)`` quarantines the entry post-enrich
    (0.9 > default ``slop_threshold=0.7``), so ``keepers`` stays empty and
    ``ingest`` returns before any LLM-backed stage runs.
    """
    from datetime import UTC, datetime

    from slopmortem.budget import Budget
    from slopmortem.config import Config
    from slopmortem.corpus import MergeJournal
    from slopmortem.ingest import FakeSlopClassifier, InMemoryCorpus, ingest
    from slopmortem.llm import FakeEmbeddingClient, FakeLLMClient
    from slopmortem.models import RawEntry

    journal = MergeJournal(tmp_path / "j.sqlite")
    await journal.init()
    await journal.upsert_pending(canonical_id="acme.com", source="hn", source_id="acme")
    await journal.mark_complete(
        canonical_id="acme.com",
        source="hn",
        source_id="acme",
        skip_key="prior",
        merged_at="2026-05-07T00:00:00Z",
    )

    class _OneShotSource:
        async def fetch(self):
            yield RawEntry(
                source="hn",
                source_id="acme",
                url="https://acme.com",
                raw_html=None,
                markdown_text="prior body",
                fetched_at=datetime(2026, 5, 8, tzinfo=UTC),
            )

    enricher_calls: list[str] = []

    class _RecordingEnricher:
        async def enrich(self, entry):
            enricher_calls.append(entry.source_id)
            return entry

    config = Config()
    result = await ingest(
        sources=[_OneShotSource()],
        enrichers=[_RecordingEnricher()],
        journal=journal,
        corpus=InMemoryCorpus(),
        llm=FakeLLMClient(canned={}, default_model=_HAIKU),
        embed_client=FakeEmbeddingClient(model=config.embed_model_id),
        budget=Budget(cap_usd=1.0),
        slop_classifier=FakeSlopClassifier(default_score=0.9),
        config=config,
        post_mortems_root=tmp_path / "pm",
        force=True,
        sparse_encoder=lambda _t: {0: 1.0},
    )
    assert enricher_calls == ["acme"], "--force must bypass the pre-enrich skip"
    assert result.quarantined == 1, "non-curated source with score=0.9 should quarantine post-enrich"
```

(The first test asserts the gate fires; the second asserts `--force` bypasses it. Both tests deliberately use a non-pre-vetted source so the slop classifier is exercised, and both leave `keepers` empty so no LLM-backed stage runs — the bare `FakeLLMClient(canned={})` is intentional.)

- [x] **Step 2: Run tests to verify they fail**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/ingest/test_orchestration.py -v -k "already_processed or force_bypasses"`
Expected: FAIL — the recording enricher IS called because the gate doesn't exist yet (the first test fails on `enricher_calls == []`).

- [x] **Step 3: Thread `force` into `_classify_phase`**

In `slopmortem/ingest/_ingest.py`, add `force: bool` to the `_classify_phase` signature (currently `:113-124`):

```python
async def _classify_phase(  # noqa: PLR0913 - one phase, every dep at this seam
    entries: Sequence[RawEntry],
    *,
    enrichers: Sequence[Enricher],
    slop_classifier: SlopClassifier,
    journal: MergeJournal,
    config: Config,
    post_mortems_root: Path,
    dry_run: bool,
    force: bool,
    progress: IngestProgress,
    result: IngestResult,
) -> list[tuple[RawEntry, str]]:
```

…and pass it from the `ingest()` call site at `:402-412`:

```python
    keepers = await _classify_phase(
        entries,
        enrichers=enrichers,
        slop_classifier=slop_classifier,
        journal=journal,
        config=config,
        post_mortems_root=post_mortems_root,
        dry_run=dry_run,
        force=force,
        progress=progress,
        result=result,
    )
```

- [x] **Step 4: Insert the pre-enrich gate**

In `_one` (currently `:139-154`), insert this block as the very first action inside the `async with limiter:` body, before `result.seen += 1` is incremented — the gate's "we saw it but skipped" semantics match `skipped`, not `seen`:

```python
    async def _one(entry: RawEntry) -> tuple[RawEntry, str] | None:
        async with limiter:
            if not force and await journal.is_terminal(entry.source, entry.source_id):
                logger.info(
                    "ingest: skipped %s:%s — already processed (use --force to re-run)",
                    entry.source,
                    entry.source_id,
                )
                result.skipped += 1
                progress.advance_phase(IngestPhase.CLASSIFY)
                return None
            result.seen += 1
            try:
                enriched = await _enrich_pipeline(entry, enrichers)
            ...
```

The `result.seen` increment stays after the gate so "seen" continues to mean "entries the pipeline actually inspected" rather than "entries the source emitted".

- [x] **Step 5: Run the orchestration tests**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/ingest/test_orchestration.py -v`
Expected: all PASS, including the two new tests and existing ones.

- [x] **Step 6: Run the full ingest test suite**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/ingest/ -v`
Expected: all PASS. The existing `tests/ingest/test_idempotency.py` already tests the write-phase `skip_key` dedup; it should continue to pass because the write-phase gate still fires for entries the pre-enrich gate misses (e.g. crashed prior run with only `pending` rows).

- [x] **Step 7: Lint + typecheck**

Run: `just lint && just typecheck`
Expected: clean.

- [ ] **Step 8: Smoke test (manual, optional)**

If you have a journal with completed entries from a prior run:

```
UV_CACHE_DIR=/tmp/uv-cache uv run slopmortem ingest --only-source hn_algolia --limit 20
```

…and re-run the same command. The second run's log should show `ingest: skipped <src>:<id> — already processed` lines and zero `pitch filler tavily_search` calls for previously-completed entries. Add `--force` to confirm the bypass.

- [x] **Step 9: Commit**

`git add slopmortem/ingest/_ingest.py tests/ingest/test_orchestration.py && git commit -m "ingest: skip already-processed entries before enrichers"`

---

### Polish

- [x] **Step 1: Run post-implementation polish**

Dispatch the `post-implementation-polish` skill on the diff produced by Tasks 1–2.

- [x] **Step 2: Address findings, recommit if needed**

One commit per polish-driven fix so blame stays useful.

- [x] **Step 3: Final lint/typecheck/test sweep**

Run: `just lint && just typecheck && just test`
Expected: clean.
