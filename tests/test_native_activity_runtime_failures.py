from __future__ import annotations

import unittest
import threading
from uuid import uuid4

from lexsond.storage import (
    ActivityClaim,
    ActivityClaimDisposition,
)
from lexsond.workflows import (
    ActivityFailure,
    ActivityInvocation,
    ActivityName,
    CanaryWorkflowInput,
    FailureKind,
)
from lexsond.workflows.native_activities import NativeCanaryActivities


class _UnavailableClaimStore:
    def claim(self, invocation, *, lease_seconds):
        raise OSError("database unavailable")


class _UnavailableFailureStore:
    def claim(self, invocation, *, lease_seconds):
        return ActivityClaim(
            ActivityClaimDisposition.ACQUIRED,
            lease_token="00000000-0000-0000-0000-000000000001",
        )

    def fail(self, invocation, *, lease_token, failure):
        raise OSError("database unavailable")


class _MissingEndpointResolver:
    def resolve(self, endpoint_snapshot_id):
        raise LookupError("missing")


class _RenewingStore:
    def __init__(self, *, renewal_fails: bool = False) -> None:
        self.renewal_fails = renewal_fails
        self.renewed = threading.Event()
        self.recorded_failure = None

    def claim(self, invocation, *, lease_seconds):
        return ActivityClaim(
            ActivityClaimDisposition.ACQUIRED,
            lease_token="00000000-0000-0000-0000-000000000002",
        )

    def renew(self, invocation, *, lease_token, lease_seconds):
        self.renewed.set()
        if self.renewal_fails:
            raise OSError("lease database unavailable")

    def fail(self, invocation, *, lease_token, failure):
        self.recorded_failure = failure


class _WaitForRenewalThenMissingEndpoint:
    def __init__(self, store: _RenewingStore) -> None:
        self._store = store

    def resolve(self, endpoint_snapshot_id):
        if not self._store.renewed.wait(timeout=2):
            raise AssertionError("lease was not renewed while Activity was running")
        raise LookupError("missing")


class _Unused:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected call: {name}")


class NativeActivityRuntimeFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow_input = CanaryWorkflowInput(
            run_id=str(uuid4()),
            endpoint_snapshot_id="endpoint-v1",
            suite_name="canary",
            suite_version="1",
            suite_uri="s3://probe-suites/canary.json",
            suite_sha256="a" * 64,
            region="test",
        )
        self.invocation = ActivityInvocation(
            run_id=self.workflow_input.run_id,
            activity_name=ActivityName.VALIDATE,
            attempt=1,
            idempotency_key=f"canary:{self.workflow_input.run_id}:validate_config",
            input_ref=None,
        )

    def test_claim_outage_is_retryable_infrastructure_failure(self) -> None:
        delegate = NativeCanaryActivities(
            endpoint_resolver=_Unused(),
            suite_resolver=_Unused(),
            secret_resolver=_Unused(),
            evidence_store=_Unused(),
            runtime_store=_UnavailableClaimStore(),
        )

        with self.assertRaises(ActivityFailure) as raised:
            delegate.invoke(self.workflow_input, self.invocation, None)

        self.assertEqual(raised.exception.error_code, "ACTIVITY_STATE_UNAVAILABLE")
        self.assertEqual(raised.exception.kind, FailureKind.INFRASTRUCTURE)
        self.assertTrue(raised.exception.retryable)

    def test_running_activity_renews_lease(self) -> None:
        store = _RenewingStore()
        delegate = NativeCanaryActivities(
            endpoint_resolver=_WaitForRenewalThenMissingEndpoint(store),
            suite_resolver=_Unused(),
            secret_resolver=_Unused(),
            evidence_store=_Unused(),
            runtime_store=store,
        )
        workflow_input = CanaryWorkflowInput(
            **{
                **self.workflow_input.to_dict(),
                "retry_policy": self.workflow_input.retry_policy,
                "activity_timeout_seconds": 0.3,
                "activity_heartbeat_seconds": 0.1,
            }
        )

        with self.assertRaises(ActivityFailure) as raised:
            delegate.invoke(workflow_input, self.invocation, None)

        self.assertEqual(raised.exception.error_code, "ENDPOINT_SNAPSHOT_NOT_FOUND")
        self.assertTrue(store.renewed.is_set())
        self.assertEqual(
            store.recorded_failure.error_code,
            "ENDPOINT_SNAPSHOT_NOT_FOUND",
        )

    def test_renewal_failure_cancels_and_retries_activity(self) -> None:
        store = _RenewingStore(renewal_fails=True)
        delegate = NativeCanaryActivities(
            endpoint_resolver=_WaitForRenewalThenMissingEndpoint(store),
            suite_resolver=_Unused(),
            secret_resolver=_Unused(),
            evidence_store=_Unused(),
            runtime_store=store,
        )
        workflow_input = CanaryWorkflowInput(
            **{
                **self.workflow_input.to_dict(),
                "retry_policy": self.workflow_input.retry_policy,
                "activity_timeout_seconds": 0.3,
                "activity_heartbeat_seconds": 0.1,
            }
        )

        with self.assertRaises(ActivityFailure) as raised:
            delegate.invoke(workflow_input, self.invocation, None)

        self.assertEqual(raised.exception.error_code, "ACTIVITY_LEASE_LOST")
        self.assertTrue(raised.exception.retryable)
        self.assertIsNone(store.recorded_failure)

    def test_failure_record_outage_replaces_configuration_rejection_with_retry(self) -> None:
        delegate = NativeCanaryActivities(
            endpoint_resolver=_MissingEndpointResolver(),
            suite_resolver=_Unused(),
            secret_resolver=_Unused(),
            evidence_store=_Unused(),
            runtime_store=_UnavailableFailureStore(),
        )

        with self.assertRaises(ActivityFailure) as raised:
            delegate.invoke(self.workflow_input, self.invocation, None)

        self.assertEqual(
            raised.exception.error_code,
            "ACTIVITY_STATE_PERSISTENCE_FAILED",
        )
        self.assertEqual(raised.exception.kind, FailureKind.INFRASTRUCTURE)
        self.assertTrue(raised.exception.retryable)


if __name__ == "__main__":
    unittest.main()
