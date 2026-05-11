# sqlite3.Cursor.fetchall/fetchone and sqlite3.Row.__getitem__ are typed Any in stdlib stubs.
# pyright: reportAny=false
"""SQLite-backed merge journal, quarantine table, and alias graph.

All sqlite calls go through ``anyio.to_thread.run_sync``; WAL +
``busy_timeout=5000``. Terminal-state writers each run in one
``BEGIN; ... COMMIT;``, so a crash commits everything or nothing.
``mark_complete`` is the only ``pending`` → ``complete`` path and runs
after qdrant and disk writes succeed.

Quarantine rows live in a separate table keyed on
``(content_sha256, source, source_id)`` and don't appear in the main journal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from anyio import to_thread

from slopmortem._time import utcnow_iso
from slopmortem.corpus._db import connect
from slopmortem.models import AliasEdge, PendingReviewRow

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable
    from pathlib import Path


_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS merge_journal (
        canonical_id TEXT NOT NULL,
        source       TEXT NOT NULL,
        source_id    TEXT NOT NULL,
        merge_state  TEXT NOT NULL,
        skip_key     TEXT,
        content_hash TEXT,
        merged_at    TEXT,
        PRIMARY KEY (canonical_id, source, source_id)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS merge_reverse_idx
      ON merge_journal(source, source_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS quarantine_journal (
        content_sha256  TEXT NOT NULL,
        source          TEXT NOT NULL,
        source_id       TEXT NOT NULL,
        reason          TEXT NOT NULL,
        slop_score      REAL,
        quarantined_at  TEXT NOT NULL,
        PRIMARY KEY (content_sha256, source, source_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS aliases (
        canonical_id        TEXT NOT NULL,
        alias_kind          TEXT NOT NULL,
        target_canonical_id TEXT NOT NULL,
        evidence_source_id  TEXT NOT NULL,
        confidence          REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pending_review (
        pair_key          TEXT PRIMARY KEY,
        similarity_score  REAL,
        haiku_decision    TEXT,
        haiku_rationale   TEXT,
        raw_section_heads TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS founding_year_cache (
        registrable_domain TEXT NOT NULL,
        content_sha256     TEXT NOT NULL,
        founding_year      INTEGER,
        PRIMARY KEY (registrable_domain, content_sha256)
    )
    """,
)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
    return {k: row[k] for k in row.keys()}  # noqa: SIM118 — sqlite3.Row needs .keys()


class MergeJournal:
    """Async wrapper over a SQLite merge journal. See module docstring."""

    def __init__(self, db_path: Path) -> None:
        self._db = db_path

    async def init(self) -> None:
        await to_thread.run_sync(self._init_sync)

    def _init_sync(self) -> None:
        self._db.parent.mkdir(parents=True, exist_ok=True)
        with connect(self._db) as conn:
            for stmt in _SCHEMA:
                conn.execute(stmt)

    async def upsert_pending(
        self,
        *,
        canonical_id: str,
        source: str,
        source_id: str,
    ) -> None:
        await to_thread.run_sync(self._upsert_state, canonical_id, source, source_id, "pending")

    async def upsert_resolver_flipped(
        self,
        *,
        canonical_id: str,
        source: str,
        source_id: str,
    ) -> None:
        await to_thread.run_sync(
            self._upsert_state, canonical_id, source, source_id, "resolver_flipped"
        )

    async def upsert_alias_blocked(
        self,
        *,
        canonical_id: str,
        source: str,
        source_id: str,
        alias_edge: AliasEdge,
    ) -> None:
        """Write the alias_blocked journal row and alias graph edge in one transaction."""
        await to_thread.run_sync(
            self._upsert_alias_blocked_sync,
            canonical_id,
            source,
            source_id,
            alias_edge,
        )

    def _upsert_state(self, canonical_id: str, source: str, source_id: str, state: str) -> None:
        with connect(self._db) as conn:
            conn.execute("BEGIN")
            try:
                # Resolver flip rebinds (source, source_id) to a new canonical;
                # drop the prior row first since the UNIQUE reverse index would
                # block the insert. Prior chunks/raw/canonical files stay on
                # disk — reconcile drift class (f) handles repair.
                if state == "resolver_flipped":
                    conn.execute(
                        """
                        DELETE FROM merge_journal
                         WHERE source = ? AND source_id = ?
                           AND canonical_id <> ?
                        """,
                        (source, source_id, canonical_id),
                    )
                conn.execute(
                    """
                    INSERT INTO merge_journal
                      (canonical_id, source, source_id, merge_state)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(canonical_id, source, source_id) DO UPDATE SET
                      merge_state = excluded.merge_state
                    """,
                    (canonical_id, source, source_id, state),
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def _upsert_alias_blocked_sync(
        self,
        canonical_id: str,
        source: str,
        source_id: str,
        alias_edge: AliasEdge,
    ) -> None:
        with connect(self._db) as conn:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    """
                    INSERT INTO merge_journal
                      (canonical_id, source, source_id, merge_state)
                    VALUES (?, ?, ?, 'alias_blocked')
                    ON CONFLICT(canonical_id, source, source_id) DO UPDATE SET
                      merge_state = 'alias_blocked'
                    """,
                    (canonical_id, source, source_id),
                )
                conn.execute(
                    """
                    INSERT INTO aliases
                      (canonical_id, alias_kind, target_canonical_id,
                       evidence_source_id, confidence)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        alias_edge.canonical_id,
                        alias_edge.alias_kind,
                        alias_edge.target_canonical_id,
                        alias_edge.evidence_source_id,
                        alias_edge.confidence,
                    ),
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    async def mark_complete(  # noqa: PLR0913 — keyword-only journal write contract
        self,
        *,
        canonical_id: str,
        source: str,
        source_id: str,
        skip_key: str,
        merged_at: str,
        content_hash: str | None = None,
    ) -> None:
        """Promote pending → complete; runs after qdrant + disk writes succeed."""
        await to_thread.run_sync(
            self._mark_complete_sync,
            canonical_id,
            source,
            source_id,
            skip_key,
            merged_at,
            content_hash,
        )

    def _mark_complete_sync(  # noqa: PLR0913 — signature mirrors public method
        self,
        canonical_id: str,
        source: str,
        source_id: str,
        skip_key: str,
        merged_at: str,
        content_hash: str | None,
    ) -> None:
        with connect(self._db) as conn:
            conn.execute(
                """
                UPDATE merge_journal
                   SET merge_state = 'complete',
                       skip_key    = ?,
                       merged_at   = ?,
                       content_hash = COALESCE(?, content_hash)
                 WHERE canonical_id = ? AND source = ? AND source_id = ?
                """,
                (skip_key, merged_at, content_hash, canonical_id, source, source_id),
            )

    async def fetch_pending(self) -> list[dict[str, Any]]:  # pyright: ignore[reportExplicitAny]
        return await to_thread.run_sync(self._fetch_pending_sync)

    def _fetch_pending_sync(self) -> list[dict[str, Any]]:  # pyright: ignore[reportExplicitAny]
        with connect(self._db) as conn:
            cur = conn.execute("SELECT * FROM merge_journal WHERE merge_state = 'pending'")
            return [_row_to_dict(r) for r in cur.fetchall()]

    async def fetch_by_key(
        self, canonical_id: str, source: str, source_id: str
    ) -> list[dict[str, Any]]:  # pyright: ignore[reportExplicitAny]
        """Return rows matching the full primary key (0 or 1 entries)."""
        return await to_thread.run_sync(self._fetch_by_key_sync, canonical_id, source, source_id)

    def _fetch_by_key_sync(
        self, canonical_id: str, source: str, source_id: str
    ) -> list[dict[str, Any]]:  # pyright: ignore[reportExplicitAny]
        with connect(self._db) as conn:
            cur = conn.execute(
                """
                SELECT * FROM merge_journal
                 WHERE canonical_id = ? AND source = ? AND source_id = ?
                """,
                (canonical_id, source, source_id),
            )
            return [_row_to_dict(r) for r in cur.fetchall()]

    async def lookup_canonical_for_source(self, source: str, source_id: str) -> str | None:
        return await to_thread.run_sync(self._lookup_reverse_sync, source, source_id)

    def _lookup_reverse_sync(self, source: str, source_id: str) -> str | None:
        with connect(self._db) as conn:
            cur = conn.execute(
                """
                SELECT canonical_id FROM merge_journal
                 WHERE source = ? AND source_id = ?
                 ORDER BY CASE merge_state
                            WHEN 'complete' THEN 0
                            WHEN 'pending' THEN 1
                            ELSE 2
                          END
                 LIMIT 1
                """,
                (source, source_id),
            )
            row = cur.fetchone()
            return None if row is None else str(row["canonical_id"])

    async def is_terminal(self, source: str, source_id: str) -> bool:
        """True when (source, source_id) has a terminal row.

        Terminal = ``merge_state='complete'`` or any ``quarantine_journal``
        row. Mid-flight states (``pending``, ``alias_blocked``,
        ``resolver_flipped``) stay non-terminal so crashed runs, alias-graph
        edits, and re-resolutions re-run end-to-end.
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

    async def fetch_aliases(self, canonical_id: str) -> list[AliasEdge]:
        rows = await to_thread.run_sync(self._fetch_aliases_sync, canonical_id)
        return [AliasEdge.model_validate(r) for r in rows]

    def _fetch_aliases_sync(self, canonical_id: str) -> list[dict[str, Any]]:  # pyright: ignore[reportExplicitAny]
        with connect(self._db) as conn:
            cur = conn.execute(
                """
                SELECT canonical_id, alias_kind, target_canonical_id,
                       evidence_source_id, confidence
                  FROM aliases
                 WHERE canonical_id = ?
                """,
                (canonical_id,),
            )
            return [_row_to_dict(r) for r in cur.fetchall()]

    async def fetch_all(self) -> list[dict[str, Any]]:  # pyright: ignore[reportExplicitAny]
        return await to_thread.run_sync(self._fetch_all_sync)

    def _fetch_all_sync(self) -> list[dict[str, Any]]:  # pyright: ignore[reportExplicitAny]
        with connect(self._db) as conn:
            cur = conn.execute("SELECT * FROM merge_journal")
            return [_row_to_dict(r) for r in cur.fetchall()]

    async def write_quarantine(
        self,
        *,
        content_sha256: str,
        source: str,
        source_id: str,
        reason: str,
        slop_score: float | None,
    ) -> None:
        """Record a slop-classified quarantine row; no canonical_id is assigned."""
        await to_thread.run_sync(
            self._write_quarantine_sync,
            content_sha256,
            source,
            source_id,
            reason,
            slop_score,
        )

    def _write_quarantine_sync(
        self,
        content_sha256: str,
        source: str,
        source_id: str,
        reason: str,
        slop_score: float | None,
    ) -> None:
        with connect(self._db) as conn:
            conn.execute(
                """
                INSERT INTO quarantine_journal
                  (content_sha256, source, source_id, reason, slop_score, quarantined_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(content_sha256, source, source_id) DO UPDATE SET
                  reason = excluded.reason,
                  slop_score = excluded.slop_score
                """,
                (content_sha256, source, source_id, reason, slop_score, utcnow_iso()),
            )

    async def fetch_quarantined(self) -> list[dict[str, Any]]:  # pyright: ignore[reportExplicitAny]
        return await to_thread.run_sync(self._fetch_quarantined_sync)

    def _fetch_quarantined_sync(self) -> list[dict[str, Any]]:  # pyright: ignore[reportExplicitAny]
        with connect(self._db) as conn:
            cur = conn.execute("SELECT * FROM quarantine_journal")
            return [_row_to_dict(r) for r in cur.fetchall()]

    async def drop_quarantine_row(
        self,
        *,
        content_sha256: str,
        source: str,
        source_id: str,
    ) -> None:
        """Used by ``--reclassify`` after a survivor's markdown is moved out of quarantine."""
        await to_thread.run_sync(self._drop_quarantine_row_sync, content_sha256, source, source_id)

    def _drop_quarantine_row_sync(self, content_sha256: str, source: str, source_id: str) -> None:
        with connect(self._db) as conn:
            conn.execute(
                """
                DELETE FROM quarantine_journal
                 WHERE content_sha256 = ? AND source = ? AND source_id = ?
                """,
                (content_sha256, source, source_id),
            )

    async def list_pending_review(self) -> list[PendingReviewRow]:
        """Returns rows in INSERT order — ``--list-review`` is exploratory, callers can sort."""
        return await to_thread.run_sync(self._list_pending_review_sync)

    def _list_pending_review_sync(self) -> list[PendingReviewRow]:
        with connect(self._db) as conn:
            cur = conn.execute("SELECT * FROM pending_review")
            return [
                PendingReviewRow(
                    pair_key=row["pair_key"],
                    similarity_score=row["similarity_score"],
                    haiku_decision=row["haiku_decision"],
                    haiku_rationale=row["haiku_rationale"],
                    raw_section_heads=row["raw_section_heads"],
                )
                for row in cur.fetchall()
            ]


def aliases_iterable(edges: Iterable[AliasEdge]) -> list[dict[str, Any]]:  # pyright: ignore[reportExplicitAny]
    return [e.model_dump() for e in edges]
