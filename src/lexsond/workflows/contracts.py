from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import UUID, uuid4, uuid5


WORKFLOW_API_VERSION = "probe.ai/workflow/v1alpha1"
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")


class WorkflowStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class WorkflowPhase(StrEnum):
    NONE = "NONE"
    VALIDATE = "VALIDATE"
    PREFLIGHT = "PREFLIGHT"
    EXECUTE = "EXECUTE"
    NORMALIZE = "NORMALIZE"
    SCORE = "SCORE"
    PERSIST = "PERSIST"
    COMPARE = "COMPARE"
    NOTIFY = "NOTIFY"
    COMPLETE = "COMPLETE"


class ActivityName(StrEnum):
    VALIDATE = "validate_config"
    PREFLIGHT = "preflight_endpoint"
    EXECUTE = "execute_native_probe"
    NORMALIZE = "normalize_measurements"
    SCORE = "compute_dimension_scores"
    PERSIST = "persist_result"
    COMPARE = "compare_slo"
    NOTIFY = "notify_state_change"


class ActivityOutcomeStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    TARGET_FAILED = "TARGET_FAILED"


class FailureKind(StrEnum):
    CONFIGURATION = "CONFIGURATION"
    POLICY = "POLICY"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    RUNNER = "RUNNER"


class WorkflowEventType(StrEnum):
    WORKFLOW_STARTED = "WORKFLOW_STARTED"
    ACTIVITY_STARTED = "ACTIVITY_STARTED"
    ACTIVITY_ATTEMPT_FAILED = "ACTIVITY_ATTEMPT_FAILED"
    ACTIVITY_COMPLETED = "ACTIVITY_COMPLETED"
    WORKFLOW_SUCCEEDED = "WORKFLOW_SUCCEEDED"
    WORKFLOW_FAILED = "WORKFLOW_FAILED"
    WORKFLOW_REJECTED = "WORKFLOW_REJECTED"
    WORKFLOW_CANCELLED = "WORKFLOW_CANCELLED"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_backoff_seconds: float = 1.0
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 30.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 10
        ):
            raise ValueError("max_attempts must be between 1 and 10")
        values = (
            self.initial_backoff_seconds,
            self.backoff_multiplier,
            self.max_backoff_seconds,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in values
        ):
            raise ValueError("retry timing values must be finite and positive")
        if self.backoff_multiplier < 1:
            raise ValueError("backoff_multiplier must be at least 1")
        # Temporal's typed JSON converter decodes these fields as floats. Keep
        # their canonical JSON (and therefore the workflow input hash) stable
        # when a caller supplies an equivalent integer such as ``1``.
        object.__setattr__(
            self, "initial_backoff_seconds", float(self.initial_backoff_seconds)
        )
        object.__setattr__(self, "backoff_multiplier", float(self.backoff_multiplier))
        object.__setattr__(
            self, "max_backoff_seconds", float(self.max_backoff_seconds)
        )

    def delay_after(self, failed_attempt: int) -> float:
        return min(
            self.max_backoff_seconds,
            self.initial_backoff_seconds
            * self.backoff_multiplier ** max(0, failed_attempt - 1),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RetryPolicy:
        if not isinstance(value, Mapping):
            raise ValueError("retry_policy must be an object")
        expected = {
            "max_attempts",
            "initial_backoff_seconds",
            "backoff_multiplier",
            "max_backoff_seconds",
        }
        if set(value) != expected:
            raise ValueError("retry_policy fields differ from contract")
        try:
            return cls(
                max_attempts=value["max_attempts"],
                initial_backoff_seconds=value["initial_backoff_seconds"],
                backoff_multiplier=value["backoff_multiplier"],
                max_backoff_seconds=value["max_backoff_seconds"],
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid retry_policy: {exc}") from exc


@dataclass(frozen=True, slots=True)
class CanaryWorkflowInput:
    run_id: str
    endpoint_snapshot_id: str
    suite_name: str
    suite_version: str
    suite_uri: str
    suite_sha256: str
    region: str
    activity_timeout_seconds: float = 120.0
    activity_heartbeat_seconds: float = 15.0
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    api_version: str = WORKFLOW_API_VERSION
    kind: str = "CanaryWorkflowInput"

    def __post_init__(self) -> None:
        _uuid(self.run_id, "run_id")
        for field_name in (
            "endpoint_snapshot_id",
            "suite_name",
            "suite_version",
            "region",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-empty")
        if not isinstance(self.retry_policy, RetryPolicy):
            raise ValueError("retry_policy must be a RetryPolicy")
        if self.api_version != WORKFLOW_API_VERSION:
            raise ValueError(f"api_version must be {WORKFLOW_API_VERSION}")
        if self.kind != "CanaryWorkflowInput":
            raise ValueError("kind must be CanaryWorkflowInput")
        for value, field_name in (
            (self.activity_timeout_seconds, "activity_timeout_seconds"),
            (self.activity_heartbeat_seconds, "activity_heartbeat_seconds"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{field_name} must be finite and positive")
        if self.activity_timeout_seconds > 900:
            raise ValueError("activity_timeout_seconds must not exceed 900")
        if self.activity_heartbeat_seconds >= self.activity_timeout_seconds:
            raise ValueError(
                "activity_heartbeat_seconds must be less than activity_timeout_seconds"
            )
        object.__setattr__(
            self, "activity_timeout_seconds", float(self.activity_timeout_seconds)
        )
        object.__setattr__(
            self, "activity_heartbeat_seconds", float(self.activity_heartbeat_seconds)
        )
        if not isinstance(self.suite_sha256, str) or len(self.suite_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.suite_sha256.lower()
        ):
            raise ValueError("suite_sha256 must be a 64-character hexadecimal digest")
        object.__setattr__(self, "suite_sha256", self.suite_sha256.lower())
        if not isinstance(self.suite_uri, str):
            raise ValueError("suite_uri must be a string")
        parsed = urlsplit(self.suite_uri)
        if parsed.scheme not in {"s3", "https"}:
            raise ValueError("suite_uri must use s3 or https")
        if not parsed.hostname:
            raise ValueError("suite_uri must include a host or bucket")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("suite_uri must not contain credentials, query, or fragment")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def content_hash(self) -> str:
        canonical = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CanaryWorkflowInput:
        if not isinstance(value, Mapping):
            raise ValueError("workflow input must be an object")
        expected = {
            "run_id",
            "endpoint_snapshot_id",
            "suite_name",
            "suite_version",
            "suite_uri",
            "suite_sha256",
            "region",
            "activity_timeout_seconds",
            "activity_heartbeat_seconds",
            "retry_policy",
            "api_version",
            "kind",
        }
        if set(value) != expected:
            raise ValueError("workflow input fields differ from contract")
        try:
            return cls(
                run_id=value["run_id"],
                endpoint_snapshot_id=value["endpoint_snapshot_id"],
                suite_name=value["suite_name"],
                suite_version=value["suite_version"],
                suite_uri=value["suite_uri"],
                suite_sha256=value["suite_sha256"],
                region=value["region"],
                activity_timeout_seconds=value["activity_timeout_seconds"],
                activity_heartbeat_seconds=value["activity_heartbeat_seconds"],
                retry_policy=RetryPolicy.from_dict(value["retry_policy"]),
                api_version=value["api_version"],
                kind=value["kind"],
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid workflow input: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ActivityInvocation:
    run_id: str
    activity_name: ActivityName
    attempt: int
    idempotency_key: str
    input_ref: str | None

    def __post_init__(self) -> None:
        _uuid(self.run_id, "run_id")
        if not isinstance(self.activity_name, ActivityName):
            raise ValueError("activity_name must be an ActivityName")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 1:
            raise ValueError("activity attempt must be positive")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key:
            raise ValueError("idempotency_key must be non-empty")
        if self.input_ref is not None and not isinstance(self.input_ref, str):
            raise ValueError("input_ref must be a string or null")


@dataclass(frozen=True, slots=True)
class ActivityOutcome:
    status: ActivityOutcomeStatus
    result_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, ActivityOutcomeStatus):
            raise ValueError("activity status must be an ActivityOutcomeStatus")
        if not isinstance(self.result_ref, str) or not self.result_ref.strip():
            raise ValueError("activity result_ref must be non-empty")


class ActivityFailure(Exception):
    def __init__(self, error_code: str, *, kind: FailureKind, retryable: bool) -> None:
        if not isinstance(error_code, str) or not _ERROR_CODE.fullmatch(error_code):
            raise ValueError("activity error_code must be a stable uppercase code")
        if not isinstance(kind, FailureKind):
            raise ValueError("activity failure kind must be a FailureKind")
        if not isinstance(retryable, bool):
            raise ValueError("activity failure retryable must be boolean")
        super().__init__(error_code)
        self.error_code = error_code
        self.kind = kind
        self.retryable = retryable


class ActivityLeaseBusy(Exception):
    def __init__(self, retry_after_seconds: float) -> None:
        if (
            isinstance(retry_after_seconds, bool)
            or not isinstance(retry_after_seconds, (int, float))
            or not math.isfinite(retry_after_seconds)
            or retry_after_seconds <= 0
            or retry_after_seconds > 1_800
        ):
            raise ValueError(
                "lease retry_after_seconds must be positive and at most 1800"
            )
        super().__init__("ACTIVITY_LEASE_BUSY")
        self.retry_after_seconds = float(retry_after_seconds)


@dataclass(frozen=True, slots=True)
class WorkflowEvent:
    run_id: str
    sequence: int
    event_type: WorkflowEventType
    phase: WorkflowPhase
    occurred_at: str
    event_id: str
    workflow_input_sha256: str | None = None
    activity_name: ActivityName | None = None
    attempt: int | None = None
    idempotency_key: str | None = None
    outcome_status: ActivityOutcomeStatus | None = None
    result_ref: str | None = None
    failure_kind: FailureKind | None = None
    error_code: str | None = None
    retryable: bool | None = None
    retry_scheduled: bool | None = None
    retry_after_seconds: float | None = None
    api_version: str = WORKFLOW_API_VERSION

    def __post_init__(self) -> None:
        for value, enum_type, field_name in (
            (self.event_type, WorkflowEventType, "event_type"),
            (self.phase, WorkflowPhase, "phase"),
        ):
            if not isinstance(value, enum_type):
                raise ValueError(f"{field_name} has an invalid enum value")
        for value, enum_type, field_name in (
            (self.activity_name, ActivityName, "activity_name"),
            (self.outcome_status, ActivityOutcomeStatus, "outcome_status"),
            (self.failure_kind, FailureKind, "failure_kind"),
        ):
            if value is not None and not isinstance(value, enum_type):
                raise ValueError(f"{field_name} has an invalid enum value")
        _uuid(self.run_id, "run_id")
        _uuid(self.event_id, "event_id")
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 1
        ):
            raise ValueError("workflow event sequence must be positive")
        if self.api_version != WORKFLOW_API_VERSION:
            raise ValueError(f"api_version must be {WORKFLOW_API_VERSION}")
        if not isinstance(self.occurred_at, str):
            raise ValueError("occurred_at must be ISO-8601")
        try:
            timestamp = datetime.fromisoformat(self.occurred_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("occurred_at must be ISO-8601") from exc
        if timestamp.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        if self.attempt is not None and (
            isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or self.attempt < 1
        ):
            raise ValueError("workflow event attempt must be positive")
        for value, field_name in (
            (self.idempotency_key, "idempotency_key"),
            (self.result_ref, "result_ref"),
        ):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{field_name} must be a string or null")
        for value, field_name in (
            (self.retryable, "retryable"),
            (self.retry_scheduled, "retry_scheduled"),
        ):
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{field_name} must be boolean or null")
        if self.error_code is not None and (
            not isinstance(self.error_code, str)
            or not _ERROR_CODE.fullmatch(self.error_code)
        ):
            raise ValueError("workflow error_code must be a stable uppercase code")
        if self.retry_after_seconds is not None and (
            isinstance(self.retry_after_seconds, bool)
            or not isinstance(self.retry_after_seconds, (int, float))
            or not math.isfinite(self.retry_after_seconds)
            or self.retry_after_seconds < 0
        ):
            raise ValueError("retry_after_seconds must be finite and non-negative")
        if self.workflow_input_sha256 is not None and (
            not isinstance(self.workflow_input_sha256, str)
            or len(self.workflow_input_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.workflow_input_sha256.lower()
            )
        ):
            raise ValueError("workflow_input_sha256 must be a SHA-256 digest")

    @classmethod
    def new(
        cls,
        *,
        run_id: str,
        sequence: int,
        event_type: WorkflowEventType,
        phase: WorkflowPhase,
        occurred_at: str,
        **fields: Any,
    ) -> WorkflowEvent:
        return cls(
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            phase=phase,
            occurred_at=occurred_at,
            event_id=str(uuid4()),
            **fields,
        )

    @classmethod
    def deterministic(
        cls,
        *,
        run_id: str,
        sequence: int,
        event_type: WorkflowEventType,
        phase: WorkflowPhase,
        occurred_at: str,
        **fields: Any,
    ) -> WorkflowEvent:
        """Create a replay-safe event ID from the immutable run and sequence."""

        namespace = UUID(run_id)
        event_id = str(uuid5(namespace, f"probe-workflow-event:{sequence}"))
        return cls(
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            phase=phase,
            occurred_at=occurred_at,
            event_id=event_id,
            **fields,
        )

    def to_dict(self) -> dict[str, Any]:
        return _enum_values(asdict(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorkflowEvent:
        if not isinstance(value, Mapping):
            raise ValueError("workflow event must be an object")
        expected = {
            "run_id",
            "sequence",
            "event_type",
            "phase",
            "occurred_at",
            "event_id",
            "workflow_input_sha256",
            "activity_name",
            "attempt",
            "idempotency_key",
            "outcome_status",
            "result_ref",
            "failure_kind",
            "error_code",
            "retryable",
            "retry_scheduled",
            "retry_after_seconds",
            "api_version",
        }
        unknown = set(value) - expected
        missing = expected - set(value)
        if unknown or missing:
            raise ValueError(
                f"workflow event fields differ from contract; missing={sorted(missing)}, "
                f"unknown={sorted(unknown)}"
            )
        try:
            return cls(
                run_id=value["run_id"],
                sequence=value["sequence"],
                event_type=WorkflowEventType(value["event_type"]),
                phase=WorkflowPhase(value["phase"]),
                occurred_at=value["occurred_at"],
                event_id=value["event_id"],
                workflow_input_sha256=value["workflow_input_sha256"],
                activity_name=_optional_enum(ActivityName, value["activity_name"]),
                attempt=value["attempt"],
                idempotency_key=value["idempotency_key"],
                outcome_status=_optional_enum(
                    ActivityOutcomeStatus, value["outcome_status"]
                ),
                result_ref=value["result_ref"],
                failure_kind=_optional_enum(FailureKind, value["failure_kind"]),
                error_code=value["error_code"],
                retryable=value["retryable"],
                retry_scheduled=value["retry_scheduled"],
                retry_after_seconds=value["retry_after_seconds"],
                api_version=value["api_version"],
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid workflow event: {exc}") from exc


def _uuid(value: str, field: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _enum_values(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {key: _enum_values(item) for key, item in value.items()}
    return value


def _optional_enum(enum_type: type[StrEnum], value: Any) -> StrEnum | None:
    return None if value is None else enum_type(value)
