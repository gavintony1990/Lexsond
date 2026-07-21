from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from ..workflows.canary import ConcurrentWorkflowUpdate
from ..workflows.contracts import WorkflowEvent


class WorkflowJournalCorruption(RuntimeError):
    pass


class WorkflowJournalIntegrityError(RuntimeError):
    pass


class SqliteWorkflowJournal:
    """Durable local WorkflowJournal with atomic compare-and-append.

    SQLite is the executable local-development adapter. Production uses the
    equivalent PostgreSQL contract from ``migrations/0001_core.sql``.
    """

    def __init__(self, database_path: Path, *, busy_timeout_ms: int = 5000) -> None:
        if not isinstance(database_path, Path):
            raise ValueError("database_path must be a pathlib.Path")
        if database_path.exists() and database_path.is_dir():
            raise ValueError("database_path must not be a directory")
        if not database_path.parent.is_dir():
            raise ValueError("database_path parent directory must exist")
        if not 1 <= busy_timeout_ms <= 60_000:
            raise ValueError("busy_timeout_ms must be between 1 and 60000")
        self._database_path = database_path
        self._busy_timeout_ms = busy_timeout_ms
        self._initialize()

    def load(self, run_id: str) -> tuple[WorkflowEvent, ...]:
        with closing(self._connect()) as connection:
            head = connection.execute(
                "SELECT last_sequence FROM workflow_journal_heads WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT sequence, event_id, event_json
                FROM workflow_events
                WHERE run_id = ?
                ORDER BY sequence ASC
                """,
                (run_id,),
            ).fetchall()
        if head is None:
            if rows:
                raise WorkflowJournalCorruption("events exist without a journal head")
            return ()
        if head[0] != len(rows):
            raise WorkflowJournalCorruption(
                "journal head does not match the number of stored events"
            )

        events: list[WorkflowEvent] = []
        for expected_sequence, row in enumerate(rows, start=1):
            sequence, event_id, raw_json = row
            if sequence != expected_sequence:
                raise WorkflowJournalCorruption("stored event sequence is not contiguous")
            try:
                value: Any = json.loads(raw_json)
                event = WorkflowEvent.from_dict(value)
            except (json.JSONDecodeError, ValueError) as exc:
                raise WorkflowJournalCorruption(
                    f"stored workflow event {sequence} is invalid"
                ) from exc
            if event.run_id != run_id or event.sequence != sequence or event.event_id != event_id:
                raise WorkflowJournalCorruption(
                    "stored event columns do not match the event JSON"
                )
            events.append(event)
        return tuple(events)

    def append(self, event: WorkflowEvent, *, expected_sequence: int) -> None:
        if isinstance(expected_sequence, bool) or not isinstance(expected_sequence, int):
            raise ValueError("expected_sequence must be an integer")
        if expected_sequence < 0:
            raise ValueError("expected_sequence must be non-negative")
        if event.sequence != expected_sequence + 1:
            raise ConcurrentWorkflowUpdate(
                "event sequence does not follow the expected sequence"
            )
        event_json = json.dumps(
            event.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT last_sequence FROM workflow_journal_heads WHERE run_id = ?",
                (event.run_id,),
            ).fetchone()
            actual_sequence = row[0] if row is not None else 0
            if actual_sequence != expected_sequence:
                existing = connection.execute(
                    """
                    SELECT event_id, event_json
                    FROM workflow_events
                    WHERE run_id = ? AND sequence = ?
                    """,
                    (event.run_id, event.sequence),
                ).fetchone()
                if existing is not None and existing == (event.event_id, event_json):
                    connection.commit()
                    return
                raise ConcurrentWorkflowUpdate(
                    f"expected sequence {expected_sequence}, found {actual_sequence}"
                )
            if row is None:
                connection.execute(
                    """
                    INSERT INTO workflow_journal_heads (run_id, last_sequence)
                    VALUES (?, 0)
                    """,
                    (event.run_id,),
                )
            connection.execute(
                """
                INSERT INTO workflow_events
                    (run_id, sequence, event_id, occurred_at, event_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.run_id,
                    event.sequence,
                    event.event_id,
                    event.occurred_at,
                    event_json,
                ),
            )
            cursor = connection.execute(
                """
                UPDATE workflow_journal_heads
                SET last_sequence = ?
                WHERE run_id = ? AND last_sequence = ?
                """,
                (event.sequence, event.run_id, expected_sequence),
            )
            if cursor.rowcount != 1:
                raise ConcurrentWorkflowUpdate("journal head changed during append")
            connection.commit()
        except ConcurrentWorkflowUpdate:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise WorkflowJournalIntegrityError(
                "workflow event violated a journal integrity constraint"
            ) from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflow_journal_heads (
                    run_id TEXT PRIMARY KEY NOT NULL,
                    last_sequence INTEGER NOT NULL DEFAULT 0
                        CHECK (last_sequence >= 0)
                );

                CREATE TABLE IF NOT EXISTS workflow_events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK (sequence >= 1),
                    event_id TEXT NOT NULL UNIQUE,
                    occurred_at TEXT NOT NULL,
                    event_json TEXT NOT NULL CHECK (json_valid(event_json)),
                    PRIMARY KEY (run_id, sequence),
                    FOREIGN KEY (run_id)
                        REFERENCES workflow_journal_heads(run_id)
                        ON DELETE RESTRICT
                );

                CREATE INDEX IF NOT EXISTS idx_workflow_events_occurred_at
                    ON workflow_events (occurred_at);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=self._busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection
