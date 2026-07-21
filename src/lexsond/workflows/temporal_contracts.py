from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contracts import (
    ActivityFailure,
    ActivityInvocation,
    ActivityLeaseBusy,
    ActivityOutcome,
    ActivityOutcomeStatus,
    CanaryWorkflowInput,
    FailureKind,
)
from .state import WorkflowState


@dataclass(frozen=True, slots=True)
class TemporalHistoryRequest:
    workflow_input: CanaryWorkflowInput


@dataclass(frozen=True, slots=True)
class TemporalAppendEventRequest:
    event: dict[str, Any]
    expected_sequence: int


@dataclass(frozen=True, slots=True)
class TemporalStepRequest:
    workflow_input: CanaryWorkflowInput
    invocation: ActivityInvocation


@dataclass(frozen=True, slots=True)
class TemporalStepResult:
    status: str
    result_ref: str | None = None
    failure_kind: str | None = None
    error_code: str | None = None
    retryable: bool | None = None
    retry_after_seconds: float | None = None

    @classmethod
    def from_outcome(cls, outcome: ActivityOutcome) -> TemporalStepResult:
        return cls(status=outcome.status.value, result_ref=outcome.result_ref)

    @classmethod
    def from_failure(cls, failure: ActivityFailure) -> TemporalStepResult:
        return cls(
            status="FAILED",
            failure_kind=failure.kind.value,
            error_code=failure.error_code,
            retryable=failure.retryable,
        )

    @classmethod
    def from_busy(cls, busy: ActivityLeaseBusy) -> TemporalStepResult:
        return cls(
            status="BUSY",
            retry_after_seconds=busy.retry_after_seconds,
        )

    def validate(self) -> None:
        if self.status in {
            ActivityOutcomeStatus.SUCCEEDED.value,
            ActivityOutcomeStatus.TARGET_FAILED.value,
        }:
            if not self.result_ref:
                raise ValueError("successful Temporal step requires result_ref")
            if any(
                value is not None
                for value in (
                    self.failure_kind,
                    self.error_code,
                    self.retryable,
                    self.retry_after_seconds,
                )
            ):
                raise ValueError("successful Temporal step cannot contain failure fields")
            return
        if self.status == "BUSY":
            try:
                ActivityLeaseBusy(self.retry_after_seconds)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "busy Temporal step requires retry_after_seconds"
                ) from exc
            if any(
                value is not None
                for value in (
                    self.result_ref,
                    self.failure_kind,
                    self.error_code,
                    self.retryable,
                )
            ):
                raise ValueError("busy Temporal step cannot contain outcome fields")
            return
        if self.status != "FAILED":
            raise ValueError("Temporal step has an unknown status")
        if self.failure_kind not in {kind.value for kind in FailureKind}:
            raise ValueError("failed Temporal step requires failure_kind")
        if not self.error_code or not isinstance(self.retryable, bool):
            raise ValueError("failed Temporal step requires error_code and retryable")
        if self.result_ref is not None:
            raise ValueError("failed Temporal step cannot contain result_ref")
        if self.retry_after_seconds is not None:
            raise ValueError("failed Temporal step cannot contain retry_after_seconds")


@dataclass(frozen=True, slots=True)
class TemporalCanaryResult:
    run_id: str
    status: str
    phase: str
    target_failed_seen: bool
    last_result_ref: str | None
    terminal_error_code: str | None
    last_sequence: int

    @classmethod
    def from_state(cls, state: WorkflowState) -> TemporalCanaryResult:
        return cls(
            run_id=state.run_id,
            status=state.status.value,
            phase=state.phase.value,
            target_failed_seen=state.target_failed_seen,
            last_result_ref=state.last_result_ref,
            terminal_error_code=state.terminal_error_code,
            last_sequence=state.last_sequence,
        )
