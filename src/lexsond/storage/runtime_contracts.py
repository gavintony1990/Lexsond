from __future__ import annotations

import math
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Protocol

from ..workflows.contracts import (
    ActivityInvocation,
    ActivityOutcome,
    FailureKind,
)


class CanaryRuntimeStoreIntegrityError(RuntimeError):
    """Raised when durable idempotency state contradicts an invocation."""


class ActivityClaimDisposition(StrEnum):
    ACQUIRED = "ACQUIRED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BUSY = "BUSY"


@dataclass(frozen=True, slots=True)
class ActivityFailureRecord:
    error_code: str
    kind: FailureKind
    retryable: bool

    def __post_init__(self) -> None:
        # Reuse the public failure contract as the single validation source.
        from ..workflows.contracts import ActivityFailure

        ActivityFailure(
            self.error_code,
            kind=self.kind,
            retryable=self.retryable,
        )


@dataclass(frozen=True, slots=True)
class ActivityClaim:
    disposition: ActivityClaimDisposition
    lease_token: str | None = None
    outcome: ActivityOutcome | None = None
    failure: ActivityFailureRecord | None = None
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        expected = {
            ActivityClaimDisposition.ACQUIRED: (True, False, False, False),
            ActivityClaimDisposition.COMPLETED: (False, True, False, False),
            ActivityClaimDisposition.FAILED: (False, False, True, False),
            ActivityClaimDisposition.BUSY: (False, False, False, True),
        }[self.disposition]
        actual = (
            self.lease_token is not None,
            self.outcome is not None,
            self.failure is not None,
            self.retry_after_seconds is not None,
        )
        if actual != expected:
            raise ValueError("Activity claim fields do not match its disposition")
        if self.retry_after_seconds is not None:
            validate_lease_seconds(self.retry_after_seconds)


class CanaryRuntimeStore(Protocol):
    def claim(
        self,
        invocation: ActivityInvocation,
        *,
        lease_seconds: float,
    ) -> ActivityClaim: ...

    def complete(
        self,
        invocation: ActivityInvocation,
        *,
        lease_token: str,
        outcome: ActivityOutcome,
    ) -> None: ...

    def renew(
        self,
        invocation: ActivityInvocation,
        *,
        lease_token: str,
        lease_seconds: float,
    ) -> None: ...

    def fail(
        self,
        invocation: ActivityInvocation,
        *,
        lease_token: str,
        failure: ActivityFailureRecord,
    ) -> None: ...

    def persist_result(
        self,
        *,
        run_id: str,
        result_ref: str,
        result: Mapping[str, Any],
    ) -> str: ...

    def load_result(self, run_id: str) -> dict[str, Any] | None: ...


def validate_lease_seconds(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= 1_800
    ):
        raise ValueError("lease_seconds must be positive and at most 1800")
    return float(value)


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    if not isinstance(value, Mapping):
        raise ValueError("JSON value must be an object")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def validate_sanitized_result(run_id: str, value: Mapping[str, Any]) -> None:
    if value.get("run_id") != run_id:
        raise ValueError("result run_id does not match workflow run")
    if value.get("schema_version") != "probe.ai/result/v1alpha1":
        raise ValueError("unsupported normalized result schema_version")
    measurements = value.get("measurements")
    if not isinstance(measurements, list):
        raise ValueError("normalized result measurements must be an array")
    serialized = canonical_json_bytes(value).decode("utf-8").lower()
    for forbidden in (
        '"api_key"',
        '"authorization"',
        '"credential_handle"',
        '"credential_ref"',
        '"access_token"',
        '"refresh_token"',
        '"secret"',
    ):
        if forbidden in serialized:
            raise ValueError("normalized result contains a forbidden secret field")
    for measurement in measurements:
        if not isinstance(measurement, Mapping):
            raise ValueError("normalized result measurement must be an object")
        if (
            measurement.get("output_text") != ""
            or measurement.get("error_message") is not None
        ):
            raise ValueError("normalized result contains durable raw response text")
