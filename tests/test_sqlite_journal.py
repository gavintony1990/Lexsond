from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from lexsond.storage import SqliteWorkflowJournal, WorkflowJournalCorruption
from lexsond.workflows import (
    ActivityInvocation,
    ActivityName,
    ActivityOutcome,
    ActivityOutcomeStatus,
    CanaryWorkflow,
    CanaryWorkflowInput,
    ConcurrentWorkflowUpdate,
    WorkflowEvent,
    WorkflowEventType,
    WorkflowPhase,
    WorkflowStatus,
)


class SuccessfulActivities:
    def __init__(self, *, crash_once: bool = False) -> None:
        self.crash_once = crash_once
        self.calls: list[ActivityInvocation] = []

    def invoke(
        self,
        workflow_input: CanaryWorkflowInput,
        invocation: ActivityInvocation,
        cancel_signal: object,
    ) -> ActivityOutcome:
        self.calls.append(invocation)
        if self.crash_once:
            self.crash_once = False
            raise KeyboardInterrupt()
        return ActivityOutcome(
            ActivityOutcomeStatus.SUCCEEDED,
            f"evidence:{invocation.activity_name.value}",
        )


def workflow_input() -> CanaryWorkflowInput:
    return CanaryWorkflowInput(
        run_id=str(uuid4()),
        endpoint_snapshot_id="endpoint-v1",
        suite_name="canary",
        suite_version="1",
        suite_uri="s3://probe/suite.json",
        suite_sha256="a" * 64,
        region="local",
    )


class SqliteWorkflowJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "journal.sqlite3"
        self.clock = lambda: datetime(2026, 7, 19, 9, 0, tzinfo=UTC)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_event_round_trip_is_strict(self) -> None:
        event = WorkflowEvent.new(
            run_id=str(uuid4()),
            sequence=1,
            event_type=WorkflowEventType.WORKFLOW_STARTED,
            phase=WorkflowPhase.NONE,
            occurred_at=self.clock().isoformat(),
            workflow_input_sha256="a" * 64,
        )
        self.assertEqual(WorkflowEvent.from_dict(event.to_dict()), event)
        malformed = event.to_dict()
        malformed["unknown_field"] = True
        with self.assertRaisesRegex(ValueError, "unknown"):
            WorkflowEvent.from_dict(malformed)

    def test_workflow_resumes_after_process_restart(self) -> None:
        item = workflow_input()
        first_journal = SqliteWorkflowJournal(self.database_path)
        crashing = SuccessfulActivities(crash_once=True)
        with self.assertRaises(KeyboardInterrupt):
            CanaryWorkflow(first_journal, clock=self.clock).run(item, crashing)

        second_journal = SqliteWorkflowJournal(self.database_path)
        resumed = SuccessfulActivities()
        state = CanaryWorkflow(second_journal, clock=self.clock).run(item, resumed)
        self.assertEqual(state.status, WorkflowStatus.SUCCEEDED)
        self.assertEqual(resumed.calls[0].activity_name, ActivityName.VALIDATE)
        self.assertEqual(resumed.calls[0].attempt, 1)
        self.assertEqual(len(second_journal.load(item.run_id)), 18)

    def test_stale_expected_sequence_is_rejected_atomically(self) -> None:
        item = workflow_input()
        journal = SqliteWorkflowJournal(self.database_path)
        crashing = SuccessfulActivities(crash_once=True)
        with self.assertRaises(KeyboardInterrupt):
            CanaryWorkflow(journal, clock=self.clock).run(item, crashing)
        last_event = journal.load(item.run_id)[-1]
        journal.append(last_event, expected_sequence=1)
        conflicting_event = replace(last_event, event_id=str(uuid4()))
        with self.assertRaises(ConcurrentWorkflowUpdate):
            journal.append(conflicting_event, expected_sequence=1)
        self.assertEqual(len(journal.load(item.run_id)), 2)

    def test_corrupted_event_json_is_not_replayed(self) -> None:
        item = workflow_input()
        journal = SqliteWorkflowJournal(self.database_path)
        activities = SuccessfulActivities(crash_once=True)
        with self.assertRaises(KeyboardInterrupt):
            CanaryWorkflow(journal, clock=self.clock).run(item, activities)

        connection = sqlite3.connect(self.database_path)
        raw = connection.execute(
            "SELECT event_json FROM workflow_events WHERE run_id = ? AND sequence = 2",
            (item.run_id,),
        ).fetchone()[0]
        value = json.loads(raw)
        value["unknown_field"] = "must fail closed"
        connection.execute(
            "UPDATE workflow_events SET event_json = ? WHERE run_id = ? AND sequence = 2",
            (json.dumps(value), item.run_id),
        )
        connection.commit()
        connection.close()

        with self.assertRaises(WorkflowJournalCorruption):
            journal.load(item.run_id)


if __name__ == "__main__":
    unittest.main()
