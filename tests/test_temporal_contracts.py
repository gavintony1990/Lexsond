from __future__ import annotations

import unittest
from uuid import uuid4

from lexsond.workflows import (
    ActivityFailure,
    ActivityInvocation,
    ActivityLeaseBusy,
    ActivityName,
    ActivityOutcome,
    ActivityOutcomeStatus,
    CanaryWorkflowInput,
    FailureKind,
    RetryPolicy,
)
from lexsond.workflows.temporal_contracts import (
    TemporalHistoryRequest,
    TemporalStepRequest,
    TemporalStepResult,
)


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


class TemporalContractTests(unittest.TestCase):
    def test_workflow_input_strict_round_trip_and_history_initialization_payload(
        self,
    ) -> None:
        value = workflow_input()

        restored = CanaryWorkflowInput.from_dict(value.to_dict())
        request = TemporalHistoryRequest(workflow_input=restored)

        self.assertEqual(restored, value)
        self.assertEqual(request.workflow_input.content_hash(), value.content_hash())
        malformed = value.to_dict()
        malformed["credential_handle"] = "secret://must-not-enter-history"
        with self.assertRaisesRegex(ValueError, "fields differ"):
            CanaryWorkflowInput.from_dict(malformed)

    def test_activity_timeouts_are_frozen_in_workflow_input(self) -> None:
        value = CanaryWorkflowInput(
            run_id=str(uuid4()),
            endpoint_snapshot_id="endpoint-snapshot-v1",
            suite_name="canary",
            suite_version="1",
            suite_uri="s3://probe-suites/canary.json",
            suite_sha256="a" * 64,
            region="test",
            activity_timeout_seconds=120,
            activity_heartbeat_seconds=15,
            retry_policy=RetryPolicy(
                initial_backoff_seconds=1,
                backoff_multiplier=2,
                max_backoff_seconds=30,
            ),
        )

        self.assertEqual(value.activity_timeout_seconds, 120.0)
        self.assertEqual(value.activity_heartbeat_seconds, 15.0)
        self.assertIsInstance(value.activity_timeout_seconds, float)
        self.assertIsInstance(value.retry_policy.initial_backoff_seconds, float)
        with self.assertRaisesRegex(ValueError, "must be less than"):
            CanaryWorkflowInput(
                run_id=str(uuid4()),
                endpoint_snapshot_id="endpoint-snapshot-v1",
                suite_name="canary",
                suite_version="1",
                suite_uri="s3://probe-suites/canary.json",
                suite_sha256="a" * 64,
                region="test",
                activity_timeout_seconds=5,
                activity_heartbeat_seconds=5,
            )

    def test_step_request_is_a_single_typed_activity_argument(self) -> None:
        value = workflow_input()
        request = TemporalStepRequest(
            workflow_input=value,
            invocation=ActivityInvocation(
                run_id=value.run_id,
                activity_name=ActivityName.EXECUTE,
                attempt=1,
                idempotency_key=f"canary:{value.run_id}:execute_native_probe",
                input_ref="evidence:preflight:1",
            ),
        )

        self.assertEqual(request.workflow_input, value)
        self.assertEqual(request.invocation.activity_name, ActivityName.EXECUTE)

    def test_success_and_target_failure_results_are_valid(self) -> None:
        for status in (
            ActivityOutcomeStatus.SUCCEEDED,
            ActivityOutcomeStatus.TARGET_FAILED,
        ):
            with self.subTest(status=status):
                result = TemporalStepResult.from_outcome(
                    ActivityOutcome(status, f"evidence:{status.value.lower()}")
                )
                result.validate()
                self.assertEqual(result.status, status.value)

    def test_structured_activity_failure_is_valid(self) -> None:
        result = TemporalStepResult.from_failure(
            ActivityFailure(
                "UPSTREAM_CONNECTION_RESET",
                kind=FailureKind.INFRASTRUCTURE,
                retryable=True,
            )
        )

        result.validate()
        self.assertEqual(result.failure_kind, FailureKind.INFRASTRUCTURE.value)
        self.assertTrue(result.retryable)

    def test_busy_step_preserves_wait_without_becoming_failure(self) -> None:
        result = TemporalStepResult.from_busy(ActivityLeaseBusy(2.5))

        result.validate()
        self.assertEqual(result.status, "BUSY")
        self.assertEqual(result.retry_after_seconds, 2.5)

    def test_malformed_results_are_rejected(self) -> None:
        malformed = (
            TemporalStepResult(status="SUCCEEDED"),
            TemporalStepResult(
                status="FAILED",
                failure_kind=FailureKind.RUNNER.value,
                error_code="RUNNER_CRASHED",
            ),
            TemporalStepResult(status="UNKNOWN", result_ref="evidence:x"),
            TemporalStepResult(status="BUSY"),
            TemporalStepResult(
                status="BUSY",
                retry_after_seconds=1,
                error_code="ACTIVITY_LEASE_BUSY",
            ),
        )

        for result in malformed:
            with self.subTest(result=result), self.assertRaises(ValueError):
                result.validate()


if __name__ == "__main__":
    unittest.main()
