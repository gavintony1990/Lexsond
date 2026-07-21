from __future__ import annotations

import unittest
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from lexsond.models import NormalizedRunResult
from lexsond.storage import (
    ActivityClaimDisposition,
    ActivityFailureRecord,
    CanaryRuntimeStoreIntegrityError,
    SqliteCanaryRuntimeStore,
    sanitized_result_for_persistence,
)
from lexsond.workflows import (
    ActivityInvocation,
    ActivityName,
    ActivityOutcome,
    ActivityOutcomeStatus,
    FailureKind,
)


class SqliteCanaryRuntimeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.now = 1_700_000_000.0
        self.store = SqliteCanaryRuntimeStore(
            Path(self.temporary.name) / "runtime.sqlite3",
            clock=lambda: self.now,
        )
        self.run_id = str(uuid4())
        self.invocation = ActivityInvocation(
            run_id=self.run_id,
            activity_name=ActivityName.EXECUTE,
            attempt=1,
            idempotency_key=f"canary:{self.run_id}:execute_native_probe",
            input_ref="file:///evidence/preflight",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_completed_outcome_is_exactly_idempotent(self) -> None:
        outcome = ActivityOutcome(
            ActivityOutcomeStatus.SUCCEEDED,
            "file:///evidence/result",
        )

        claim = self.store.claim(self.invocation, lease_seconds=10)
        self.assertEqual(claim.disposition, ActivityClaimDisposition.ACQUIRED)
        self.store.complete(
            self.invocation,
            lease_token=claim.lease_token,
            outcome=outcome,
        )
        self.store.complete(
            self.invocation,
            lease_token=claim.lease_token,
            outcome=outcome,
        )

        replay = self.store.claim(self.invocation, lease_seconds=10)
        self.assertEqual(replay.disposition, ActivityClaimDisposition.COMPLETED)
        self.assertEqual(replay.outcome, outcome)

    def test_conflicting_outcome_or_invocation_is_rejected(self) -> None:
        claim = self.store.claim(self.invocation, lease_seconds=10)
        outcome = ActivityOutcome(
            ActivityOutcomeStatus.SUCCEEDED,
            "file:///evidence/result",
        )
        self.store.complete(
            self.invocation,
            lease_token=claim.lease_token,
            outcome=outcome,
        )

        with self.assertRaises(CanaryRuntimeStoreIntegrityError):
            self.store.complete(
                self.invocation,
                lease_token=claim.lease_token,
                outcome=ActivityOutcome(
                    ActivityOutcomeStatus.TARGET_FAILED,
                    "file:///evidence/different",
                ),
            )
        conflicting_invocation = ActivityInvocation(
            run_id=self.run_id,
            activity_name=ActivityName.PREFLIGHT,
            attempt=1,
            idempotency_key=self.invocation.idempotency_key,
            input_ref=None,
        )
        with self.assertRaises(CanaryRuntimeStoreIntegrityError):
            self.store.claim(conflicting_invocation, lease_seconds=10)

    def test_active_lease_is_busy_and_expired_lease_can_be_taken_over(self) -> None:
        first = self.store.claim(self.invocation, lease_seconds=10)
        self.assertEqual(first.disposition, ActivityClaimDisposition.ACQUIRED)

        busy = self.store.claim(self.invocation, lease_seconds=10)
        self.assertEqual(busy.disposition, ActivityClaimDisposition.BUSY)
        self.assertGreater(busy.retry_after_seconds, 0)

        self.now += 11
        replacement = self.store.claim(self.invocation, lease_seconds=10)
        self.assertEqual(replacement.disposition, ActivityClaimDisposition.ACQUIRED)
        self.assertNotEqual(replacement.lease_token, first.lease_token)
        with self.assertRaises(CanaryRuntimeStoreIntegrityError):
            self.store.complete(
                self.invocation,
                lease_token=first.lease_token,
                outcome=ActivityOutcome(
                    ActivityOutcomeStatus.SUCCEEDED,
                    "file:///evidence/stale",
                ),
            )

    def test_current_lease_can_be_renewed(self) -> None:
        claim = self.store.claim(self.invocation, lease_seconds=10)
        self.now += 9

        self.store.renew(
            self.invocation,
            lease_token=claim.lease_token,
            lease_seconds=10,
        )
        self.now += 2

        busy = self.store.claim(self.invocation, lease_seconds=10)
        self.assertEqual(busy.disposition, ActivityClaimDisposition.BUSY)
        self.assertGreaterEqual(busy.retry_after_seconds, 7)

    def test_failed_attempt_replays_and_next_attempt_can_acquire(self) -> None:
        claim = self.store.claim(self.invocation, lease_seconds=10)
        failure = ActivityFailureRecord(
            "UPSTREAM_TIMEOUT",
            kind=FailureKind.INFRASTRUCTURE,
            retryable=True,
        )
        self.store.fail(
            self.invocation,
            lease_token=claim.lease_token,
            failure=failure,
        )

        replay = self.store.claim(self.invocation, lease_seconds=10)
        self.assertEqual(replay.disposition, ActivityClaimDisposition.FAILED)
        self.assertEqual(replay.failure, failure)
        next_attempt = ActivityInvocation(
            run_id=self.invocation.run_id,
            activity_name=self.invocation.activity_name,
            attempt=2,
            idempotency_key=self.invocation.idempotency_key,
            input_ref=self.invocation.input_ref,
        )
        acquired = self.store.claim(next_attempt, lease_seconds=10)
        self.assertEqual(acquired.disposition, ActivityClaimDisposition.ACQUIRED)

    def test_final_result_is_sanitized_and_immutable(self) -> None:
        result = NormalizedRunResult(run_id=self.run_id)
        sanitized = sanitized_result_for_persistence(result)

        ref = self.store.persist_result(
            run_id=self.run_id,
            result_ref="file:///evidence/result",
            result=sanitized,
        )
        self.assertEqual(ref, "file:///evidence/result")
        self.assertEqual(self.store.load_result(self.run_id), sanitized)
        self.store.persist_result(
            run_id=self.run_id,
            result_ref="file:///evidence/result",
            result=sanitized,
        )
        with self.assertRaises(CanaryRuntimeStoreIntegrityError):
            self.store.persist_result(
                run_id=self.run_id,
                result_ref="file:///evidence/changed",
                result=sanitized,
            )

    def test_raw_response_text_is_rejected(self) -> None:
        result = NormalizedRunResult(run_id=self.run_id).to_dict()
        result["measurements"] = [
            {"output_text": "raw model answer", "error_message": None}
        ]

        with self.assertRaisesRegex(ValueError, "raw response"):
            self.store.persist_result(
                run_id=self.run_id,
                result_ref="file:///evidence/result",
                result=result,
            )

    def test_legacy_success_cache_is_rejected_instead_of_silently_ignored(self) -> None:
        path = Path(self.temporary.name) / "legacy.sqlite3"
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "CREATE TABLE canary_activity_outcomes (idempotency_key TEXT)"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(
            CanaryRuntimeStoreIntegrityError,
            "no safe lease migration",
        ):
            SqliteCanaryRuntimeStore(path)


if __name__ == "__main__":
    unittest.main()
