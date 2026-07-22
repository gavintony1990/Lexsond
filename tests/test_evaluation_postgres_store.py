from __future__ import annotations

import unittest
from contextlib import contextmanager
from types import SimpleNamespace

import psycopg

from lexsond.evaluations.coordinator import EvaluationItemOutcome
from lexsond.web.postgres_evaluation_store import PostgresEvaluationStore
from lexsond.web.control_contracts import ControlPlaneConflict, ControlPlaneNotFound


class EvaluationPostgresStoreTests(unittest.TestCase):
    def test_replayed_item_insert_does_not_double_count_model_totals(self) -> None:
        connection = _Connection(first_rowcount=0)
        store = PostgresEvaluationStore(_Pool(connection))  # type: ignore[arg-type]

        store.record_item(
            "run-id", _outcome(), workspace_id="workspace-id", lease_id="lease-id"
        )

        self.assertEqual(len(connection.calls), 3)
        self.assertIn("ON CONFLICT", connection.calls[1][0])

    def test_new_item_insert_updates_totals_once(self) -> None:
        connection = _Connection(first_rowcount=1)
        store = PostgresEvaluationStore(_Pool(connection))  # type: ignore[arg-type]

        store.record_item(
            "run-id", _outcome(), workspace_id="workspace-id", lease_id="lease-id"
        )

        self.assertEqual(len(connection.calls), 3)
        self.assertIn("completed_items = completed_items + 1", connection.calls[2][0])

    def test_conflicting_item_replay_is_rejected(self) -> None:
        connection = _Connection(first_rowcount=0, conflicting_replay=True)
        store = PostgresEvaluationStore(_Pool(connection))  # type: ignore[arg-type]

        with self.assertRaises(ControlPlaneConflict):
            store.record_item(
                "run-id", _outcome(), workspace_id="workspace-id", lease_id="lease-id"
            )

    def test_purge_maps_not_found_and_retention_conflicts_without_sql_text(self) -> None:
        for error_type, expected in (
            (psycopg.errors.NoDataFound, ControlPlaneNotFound),
            (psycopg.errors.ObjectNotInPrerequisiteState, ControlPlaneConflict),
            (psycopg.errors.ForeignKeyViolation, ControlPlaneConflict),
        ):
            error = error_type("sensitive database detail")
            store = PostgresEvaluationStore(_Pool(_FailingConnection(error)))  # type: ignore[arg-type]
            with self.assertRaises(expected) as raised:
                store.purge_dataset("dataset-id", workspace_id="workspace-id")
            self.assertNotIn("sensitive", str(raised.exception))


class _Pool:
    def __init__(self, connection: "_Connection") -> None:
        self._connection = connection

    @contextmanager
    def connection(self):
        yield self._connection


class _Connection:
    def __init__(self, *, first_rowcount: int, conflicting_replay: bool = False) -> None:
        self._first_rowcount = first_rowcount
        self._conflicting_replay = conflicting_replay
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql: str, parameters: object = None):
        self.calls.append((sql, parameters))
        if "SET lease_expires_at" in sql:
            return _Cursor(1, {"state": "RUNNING", "cancel_requested_at": None})
        if "SELECT item_id, category" in sql:
            value = _outcome()
            return _Cursor(1, {
                "item_id": "different" if self._conflicting_replay else value.item_id,
                "category": value.category,
                "state": value.state,
                "score": value.score,
                "status": value.status,
                "reason_code": value.reason_code,
                "latency_json": dict(value.latency),
                "usage_json": dict(value.usage),
                "output_sha256": value.output_sha256,
                "safe_facts_json": dict(value.safe_facts),
            })
        non_lease_calls = sum("SET lease_expires_at" not in call[0] for call in self.calls)
        rowcount = self._first_rowcount if non_lease_calls == 1 else 1
        return _Cursor(rowcount)


class _FailingConnection:
    def __init__(self, error: psycopg.Error) -> None:
        self._error = error

    def execute(self, sql: str, parameters: object = None):
        raise self._error


class _Cursor(SimpleNamespace):
    def __init__(self, rowcount: int, row=None) -> None:
        super().__init__(rowcount=rowcount)
        self._row = row

    def fetchone(self):
        return self._row


def _outcome() -> EvaluationItemOutcome:
    return EvaluationItemOutcome(
        model_id="model-a",
        item_id="arithmetic-001",
        category="arithmetic",
        sequence=1,
        state="COMPLETED",
        score=1.0,
        status="PASS",
        reason_code="NORMALIZED_MATCH",
        latency={"e2e_ms": 12.0},
        usage={"total_tokens": 7},
        output_sha256="a" * 64,
        safe_facts={"normalized_length": 2},
        cost_usd=None,
    )


if __name__ == "__main__":
    unittest.main()
