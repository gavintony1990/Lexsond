from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import uuid4

from lexsond.storage import (
    ActivityClaim,
    ActivityClaimDisposition,
    ActivityFailureRecord,
    CanaryRuntimeStoreIntegrityError,
)
from lexsond.storage.runtime_contracts import (
    canonical_json_bytes,
    validate_lease_seconds,
    validate_sanitized_result,
)
from lexsond.workflows import ActivityInvocation, ActivityOutcome


@dataclass
class _Execution:
    run_id: str
    activity_name: object
    input_ref: str | None
    attempt: int
    status: str
    lease_token: str | None
    lease_expires_at: float | None
    outcome: ActivityOutcome | None = None
    failure: ActivityFailureRecord | None = None


class InMemoryCanaryRuntimeStore:
    """Process-local test double for the PostgreSQL runtime-store contract."""

    def __init__(self) -> None:
        self._executions: dict[str, _Execution] = {}
        self._results: dict[str, tuple[str, bytes]] = {}
        self._lock = threading.Lock()

    def claim(
        self, invocation: ActivityInvocation, *, lease_seconds: float
    ) -> ActivityClaim:
        lease_seconds = validate_lease_seconds(lease_seconds)
        now = time.monotonic()
        with self._lock:
            entry = self._executions.get(invocation.idempotency_key)
            if entry is None:
                return self._acquire(invocation, now, lease_seconds)
            self._validate_identity(invocation, entry)
            if invocation.attempt < entry.attempt:
                raise CanaryRuntimeStoreIntegrityError(
                    "Activity attempt moved backwards"
                )
            if entry.status == "SUCCEEDED":
                return ActivityClaim(
                    ActivityClaimDisposition.COMPLETED, outcome=entry.outcome
                )
            if entry.status == "FAILED" and invocation.attempt == entry.attempt:
                return ActivityClaim(
                    ActivityClaimDisposition.FAILED, failure=entry.failure
                )
            if (
                entry.status == "LEASED"
                and entry.lease_expires_at is not None
                and entry.lease_expires_at > now
            ):
                return ActivityClaim(
                    ActivityClaimDisposition.BUSY,
                    retry_after_seconds=max(entry.lease_expires_at - now, 0.001),
                )
            return self._acquire(invocation, now, lease_seconds)

    def renew(
        self,
        invocation: ActivityInvocation,
        *,
        lease_token: str,
        lease_seconds: float,
    ) -> None:
        lease_seconds = validate_lease_seconds(lease_seconds)
        with self._lock:
            entry = self._leased_entry(invocation, lease_token)
            entry.lease_expires_at = time.monotonic() + lease_seconds

    def complete(
        self,
        invocation: ActivityInvocation,
        *,
        lease_token: str,
        outcome: ActivityOutcome,
    ) -> None:
        with self._lock:
            entry = self._leased_entry(invocation, lease_token)
            entry.status = "SUCCEEDED"
            entry.lease_token = None
            entry.lease_expires_at = None
            entry.outcome = outcome

    def fail(
        self,
        invocation: ActivityInvocation,
        *,
        lease_token: str,
        failure: ActivityFailureRecord,
    ) -> None:
        with self._lock:
            entry = self._leased_entry(invocation, lease_token)
            entry.status = "FAILED"
            entry.lease_token = None
            entry.lease_expires_at = None
            entry.failure = failure

    def persist_result(
        self,
        *,
        run_id: str,
        result_ref: str,
        result: Mapping[str, Any],
    ) -> str:
        validate_sanitized_result(run_id, result)
        serialized = canonical_json_bytes(result)
        with self._lock:
            existing = self._results.get(run_id)
            candidate = (result_ref, serialized)
            if existing is not None and existing != candidate:
                raise CanaryRuntimeStoreIntegrityError(
                    "probe result is immutable for a workflow run"
                )
            self._results[run_id] = candidate
        return result_ref

    def load_result(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            stored = self._results.get(run_id)
        return None if stored is None else json.loads(stored[1])

    def _acquire(
        self,
        invocation: ActivityInvocation,
        now: float,
        lease_seconds: float,
    ) -> ActivityClaim:
        token = str(uuid4())
        self._executions[invocation.idempotency_key] = _Execution(
            run_id=invocation.run_id,
            activity_name=invocation.activity_name,
            input_ref=invocation.input_ref,
            attempt=invocation.attempt,
            status="LEASED",
            lease_token=token,
            lease_expires_at=now + lease_seconds,
        )
        return ActivityClaim(ActivityClaimDisposition.ACQUIRED, lease_token=token)

    def _leased_entry(
        self, invocation: ActivityInvocation, lease_token: str
    ) -> _Execution:
        entry = self._executions.get(invocation.idempotency_key)
        if entry is None:
            raise CanaryRuntimeStoreIntegrityError(
                "Activity execution does not exist"
            )
        self._validate_identity(invocation, entry)
        if (
            entry.status != "LEASED"
            or entry.attempt != invocation.attempt
            or entry.lease_token != lease_token
        ):
            raise CanaryRuntimeStoreIntegrityError(
                "Activity lease no longer belongs to this execution"
            )
        return entry

    @staticmethod
    def _validate_identity(
        invocation: ActivityInvocation, entry: _Execution
    ) -> None:
        if (
            entry.run_id != invocation.run_id
            or entry.activity_name != invocation.activity_name
            or entry.input_ref != invocation.input_ref
        ):
            raise CanaryRuntimeStoreIntegrityError(
                "idempotency key belongs to a different Activity invocation"
            )
