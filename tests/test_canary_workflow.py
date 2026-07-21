from __future__ import annotations

import json
import threading
import unittest
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from lexsond.workflows import (
    ActivityFailure,
    ActivityInvocation,
    ActivityLeaseBusy,
    ActivityName,
    ActivityOutcome,
    ActivityOutcomeStatus,
    CanaryWorkflow,
    CanaryWorkflowInput,
    FailureKind,
    InMemoryWorkflowJournal,
    RetryPolicy,
    WorkflowEvent,
    WorkflowStatus,
)


class ScriptedActivities:
    def __init__(self) -> None:
        self.scripts: dict[ActivityName, list[object]] = defaultdict(list)
        self.calls: list[ActivityInvocation] = []

    def invoke(
        self,
        workflow_input: CanaryWorkflowInput,
        invocation: ActivityInvocation,
        cancel_signal: object,
    ) -> ActivityOutcome:
        self.calls.append(invocation)
        script = self.scripts[invocation.activity_name]
        if script:
            item = script.pop(0)
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, ActivityOutcome):
                return item
            raise AssertionError(f"unsupported scripted result: {item!r}")
        return ActivityOutcome(
            ActivityOutcomeStatus.SUCCEEDED,
            f"evidence:{invocation.activity_name.value}:{invocation.attempt}",
        )


class RecordingWaiter:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def wait(self, seconds: float, cancel_signal: object) -> bool:
        self.delays.append(seconds)
        return True


class CrashOnceJournal(InMemoryWorkflowJournal):
    def __init__(self, crash_sequence: int) -> None:
        super().__init__()
        self.crash_sequence = crash_sequence
        self.crashed = False

    def append(self, event: WorkflowEvent, *, expected_sequence: int) -> None:
        if not self.crashed and event.sequence == self.crash_sequence:
            self.crashed = True
            raise KeyboardInterrupt()
        super().append(event, expected_sequence=expected_sequence)


def workflow_input() -> CanaryWorkflowInput:
    return CanaryWorkflowInput(
        run_id=str(uuid4()),
        endpoint_snapshot_id="endpoint-snapshot-v1",
        suite_name="openai-compatible-canary",
        suite_version="2026.07.19",
        suite_uri="s3://probe-suites/openai-compatible.json",
        suite_sha256="a" * 64,
        region="cn-east-1",
    )


class CanaryWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.input = workflow_input()
        self.journal = InMemoryWorkflowJournal()
        self.clock = lambda: datetime(2026, 7, 19, 8, 0, tzinfo=UTC)

    def test_happy_path_is_replayable_and_passes_result_refs(self) -> None:
        activities = ScriptedActivities()
        workflow = CanaryWorkflow(self.journal, clock=self.clock)
        state = workflow.run(self.input, activities)

        self.assertEqual(state.status, WorkflowStatus.SUCCEEDED)
        self.assertFalse(state.target_failed_seen)
        self.assertEqual([call.activity_name for call in activities.calls], list(ActivityName))
        self.assertIsNone(activities.calls[0].input_ref)
        self.assertEqual(
            activities.calls[1].input_ref,
            "evidence:validate_config:1",
        )
        self.assertEqual(len(self.journal.load(self.input.run_id)), 18)

        replay_activities = ScriptedActivities()
        replayed = workflow.run(self.input, replay_activities)
        self.assertEqual(replayed.status, WorkflowStatus.SUCCEEDED)
        self.assertEqual(replay_activities.calls, [])

    def test_target_failure_is_measurement_and_preflight_skips_execute(self) -> None:
        activities = ScriptedActivities()
        activities.scripts[ActivityName.PREFLIGHT] = [
            ActivityOutcome(
                ActivityOutcomeStatus.TARGET_FAILED,
                "evidence:preflight:target-down",
            )
        ]
        state = CanaryWorkflow(self.journal, clock=self.clock).run(
            self.input, activities
        )

        self.assertEqual(state.status, WorkflowStatus.SUCCEEDED)
        self.assertTrue(state.target_failed_seen)
        names = [call.activity_name for call in activities.calls]
        self.assertNotIn(ActivityName.EXECUTE, names)
        self.assertIn(ActivityName.NORMALIZE, names)
        self.assertIn(ActivityName.PERSIST, names)
        self.assertIn(ActivityName.NOTIFY, names)

    def test_retry_uses_stable_idempotency_key_and_bounded_backoff(self) -> None:
        activities = ScriptedActivities()
        activities.scripts[ActivityName.EXECUTE] = [
            ActivityFailure(
                "UPSTREAM_CONNECTION_RESET",
                kind=FailureKind.INFRASTRUCTURE,
                retryable=True,
            ),
            ActivityOutcome(ActivityOutcomeStatus.SUCCEEDED, "evidence:execute:ok"),
        ]
        waiter = RecordingWaiter()
        workflow = CanaryWorkflow(
            self.journal,
            clock=self.clock,
            retry_waiter=waiter,
        )
        retry_input = replace(
            self.input,
            retry_policy=RetryPolicy(
                max_attempts=2,
                initial_backoff_seconds=0.25,
                max_backoff_seconds=1,
            ),
        )
        state = workflow.run(retry_input, activities)

        self.assertEqual(state.status, WorkflowStatus.SUCCEEDED)
        execute_calls = [
            call for call in activities.calls if call.activity_name is ActivityName.EXECUTE
        ]
        self.assertEqual([call.attempt for call in execute_calls], [1, 2])
        self.assertEqual(
            {call.idempotency_key for call in execute_calls},
            {f"canary:{retry_input.run_id}:execute_native_probe"},
        )
        self.assertEqual(waiter.delays, [0.25])

    def test_busy_lease_waits_without_consuming_domain_attempt(self) -> None:
        activities = ScriptedActivities()
        activities.scripts[ActivityName.VALIDATE] = [
            ActivityLeaseBusy(0.25),
            ActivityOutcome(
                ActivityOutcomeStatus.SUCCEEDED,
                "evidence:validate:1",
            ),
        ]
        waiter = RecordingWaiter()

        state = CanaryWorkflow(
            self.journal,
            clock=self.clock,
            retry_waiter=waiter,
        ).run(self.input, activities)

        self.assertEqual(state.status, WorkflowStatus.SUCCEEDED)
        validate_calls = [
            call
            for call in activities.calls
            if call.activity_name is ActivityName.VALIDATE
        ]
        self.assertEqual([call.attempt for call in validate_calls], [1, 1])
        self.assertEqual(waiter.delays, [0.25])
        failed_events = [
            event
            for event in self.journal.load(self.input.run_id)
            if event.event_type.value == "ACTIVITY_ATTEMPT_FAILED"
        ]
        self.assertEqual(failed_events, [])

    def test_policy_failure_is_rejected_without_retry(self) -> None:
        activities = ScriptedActivities()
        activities.scripts[ActivityName.VALIDATE] = [
            ActivityFailure(
                "PROBE_BUDGET_EXCEEDED",
                kind=FailureKind.POLICY,
                retryable=False,
            )
        ]
        state = CanaryWorkflow(self.journal, clock=self.clock).run(
            self.input, activities
        )

        self.assertEqual(state.status, WorkflowStatus.REJECTED)
        self.assertEqual(state.terminal_error_code, "PROBE_BUDGET_EXCEEDED")
        self.assertEqual([call.activity_name for call in activities.calls], [ActivityName.VALIDATE])

    def test_cancellation_is_terminal_and_does_not_invoke_activity(self) -> None:
        cancelled = threading.Event()
        cancelled.set()
        activities = ScriptedActivities()
        state = CanaryWorkflow(self.journal, clock=self.clock).run(
            self.input, activities, cancel_signal=cancelled
        )

        self.assertEqual(state.status, WorkflowStatus.CANCELLED)
        self.assertEqual(state.terminal_error_code, "WORKFLOW_CANCEL_REQUESTED")
        self.assertEqual(activities.calls, [])

    def test_incomplete_attempt_resumes_without_duplicate_start(self) -> None:
        crashing = ScriptedActivities()
        crashing.scripts[ActivityName.VALIDATE] = [KeyboardInterrupt()]
        workflow = CanaryWorkflow(self.journal, clock=self.clock)
        with self.assertRaises(KeyboardInterrupt):
            workflow.run(self.input, crashing)

        before = self.journal.load(self.input.run_id)
        self.assertEqual(len(before), 2)
        self.assertEqual(before[-1].activity_name, ActivityName.VALIDATE)
        self.assertEqual(before[-1].attempt, 1)

        resumed_activities = ScriptedActivities()
        state = workflow.run(self.input, resumed_activities)
        self.assertEqual(state.status, WorkflowStatus.SUCCEEDED)
        self.assertEqual(resumed_activities.calls[0].activity_name, ActivityName.VALIDATE)
        self.assertEqual(resumed_activities.calls[0].attempt, 1)
        starts = [
            event
            for event in self.journal.load(self.input.run_id)
            if event.activity_name is ActivityName.VALIDATE
            and event.event_type.value == "ACTIVITY_STARTED"
        ]
        self.assertEqual(len(starts), 1)

    def test_input_rejects_presigned_or_credential_bearing_suite_uri(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not contain credentials"):
            CanaryWorkflowInput(
                run_id=str(uuid4()),
                endpoint_snapshot_id="endpoint-v1",
                suite_name="canary",
                suite_version="1",
                suite_uri="https://user:secret@example.test/suite.json?token=secret",
                suite_sha256="a" * 64,
                region="test",
            )

    def test_same_run_id_cannot_replay_with_a_different_snapshot(self) -> None:
        workflow = CanaryWorkflow(self.journal, clock=self.clock)
        workflow.run(self.input, ScriptedActivities())
        changed = replace(self.input, endpoint_snapshot_id="different-endpoint")
        with self.assertRaisesRegex(ValueError, "input does not match"):
            workflow.run(changed, ScriptedActivities())

    def test_exhausted_retry_resumes_to_failure_without_extra_api_call(self) -> None:
        journal = CrashOnceJournal(crash_sequence=4)
        activities = ScriptedActivities()
        activities.scripts[ActivityName.VALIDATE] = [
            ActivityFailure(
                "VALIDATOR_TEMPORARILY_UNAVAILABLE",
                kind=FailureKind.INFRASTRUCTURE,
                retryable=True,
            )
        ]
        one_attempt = replace(
            self.input,
            retry_policy=RetryPolicy(max_attempts=1),
        )
        workflow = CanaryWorkflow(journal, clock=self.clock)
        with self.assertRaises(KeyboardInterrupt):
            workflow.run(one_attempt, activities)

        failure_event = journal.load(one_attempt.run_id)[-1]
        self.assertTrue(failure_event.retryable)
        self.assertFalse(failure_event.retry_scheduled)
        resumed_activities = ScriptedActivities()
        state = workflow.run(one_attempt, resumed_activities)
        self.assertEqual(state.status, WorkflowStatus.FAILED)
        self.assertEqual(
            state.terminal_error_code,
            "VALIDATOR_TEMPORARILY_UNAVAILABLE",
        )
        self.assertEqual(resumed_activities.calls, [])

    def test_serialized_contracts_match_schema_and_contain_no_secret_fields(self) -> None:
        schemas = Path(__file__).parents[1] / "schemas"
        input_schema = json.loads(
            (schemas / "canary-workflow-input.schema.json").read_text(encoding="utf-8")
        )
        serialized_input = self.input.to_dict()
        self.assertEqual(set(serialized_input), set(input_schema["required"]))

        CanaryWorkflow(self.journal, clock=self.clock).run(
            self.input, ScriptedActivities()
        )
        event_schema = json.loads(
            (schemas / "workflow-event.schema.json").read_text(encoding="utf-8")
        )
        serialized_event = self.journal.load(self.input.run_id)[0].to_dict()
        self.assertEqual(set(serialized_event), set(event_schema["required"]))
        serialized = json.dumps(
            {"input": serialized_input, "event": serialized_event}
        ).lower()
        for forbidden in ("api_key", "authorization", "credential_handle"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
