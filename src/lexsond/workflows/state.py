from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .contracts import (
    ActivityName,
    ActivityOutcomeStatus,
    CanaryWorkflowInput,
    FailureKind,
    WorkflowEvent,
    WorkflowEventType,
    WorkflowPhase,
    WorkflowStatus,
)


ACTIVITY_ORDER = (
    ActivityName.VALIDATE,
    ActivityName.PREFLIGHT,
    ActivityName.EXECUTE,
    ActivityName.NORMALIZE,
    ActivityName.SCORE,
    ActivityName.PERSIST,
    ActivityName.COMPARE,
    ActivityName.NOTIFY,
)

ACTIVITY_PHASE = {
    ActivityName.VALIDATE: WorkflowPhase.VALIDATE,
    ActivityName.PREFLIGHT: WorkflowPhase.PREFLIGHT,
    ActivityName.EXECUTE: WorkflowPhase.EXECUTE,
    ActivityName.NORMALIZE: WorkflowPhase.NORMALIZE,
    ActivityName.SCORE: WorkflowPhase.SCORE,
    ActivityName.PERSIST: WorkflowPhase.PERSIST,
    ActivityName.COMPARE: WorkflowPhase.COMPARE,
    ActivityName.NOTIFY: WorkflowPhase.NOTIFY,
}


class WorkflowHistoryError(ValueError):
    pass


@dataclass(slots=True)
class WorkflowState:
    run_id: str
    workflow_input_sha256: str
    status: WorkflowStatus = WorkflowStatus.NOT_STARTED
    phase: WorkflowPhase = WorkflowPhase.NONE
    last_sequence: int = 0
    next_activity_index: int = 0
    current_activity: ActivityName | None = None
    current_attempt: int | None = None
    attempts: dict[ActivityName, int] = field(default_factory=dict)
    completed_activities: list[ActivityName] = field(default_factory=list)
    last_result_ref: str | None = None
    target_failed_seen: bool = False
    retry_after_seconds: float | None = None
    pending_failure_kind: FailureKind | None = None
    pending_error_code: str | None = None
    retry_scheduled: bool = False
    terminal_error_code: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            WorkflowStatus.SUCCEEDED,
            WorkflowStatus.FAILED,
            WorkflowStatus.REJECTED,
            WorkflowStatus.CANCELLED,
        }

    @property
    def next_activity(self) -> ActivityName | None:
        if self.next_activity_index >= len(ACTIVITY_ORDER):
            return None
        return ACTIVITY_ORDER[self.next_activity_index]


def project_workflow_state(
    workflow_input: CanaryWorkflowInput, events: Iterable[WorkflowEvent]
) -> WorkflowState:
    state = WorkflowState(
        run_id=workflow_input.run_id,
        workflow_input_sha256=workflow_input.content_hash(),
    )
    for event in events:
        _apply_event(state, event)
    return state


def _apply_event(state: WorkflowState, event: WorkflowEvent) -> None:
    if event.run_id != state.run_id:
        raise WorkflowHistoryError("workflow event run_id does not match input")
    if event.sequence != state.last_sequence + 1:
        raise WorkflowHistoryError("workflow event sequence is not contiguous")
    if state.is_terminal:
        raise WorkflowHistoryError("workflow history contains events after terminal state")

    if event.event_type is WorkflowEventType.WORKFLOW_STARTED:
        if state.status is not WorkflowStatus.NOT_STARTED or event.sequence != 1:
            raise WorkflowHistoryError("WORKFLOW_STARTED must be the first event")
        if event.phase is not WorkflowPhase.NONE:
            raise WorkflowHistoryError("WORKFLOW_STARTED must use phase NONE")
        if event.workflow_input_sha256 != state.workflow_input_sha256:
            raise WorkflowHistoryError("workflow input does not match recorded snapshot")
        state.status = WorkflowStatus.RUNNING

    elif event.event_type is WorkflowEventType.ACTIVITY_STARTED:
        _require_running(state)
        if state.current_activity is not None:
            raise WorkflowHistoryError("cannot start an activity while another is active")
        if event.activity_name is None or event.activity_name is not state.next_activity:
            raise WorkflowHistoryError("activity does not match the expected workflow step")
        expected_attempt = state.attempts.get(event.activity_name, 0) + 1
        if event.attempt != expected_attempt:
            raise WorkflowHistoryError("activity attempt is not contiguous")
        if event.phase is not ACTIVITY_PHASE[event.activity_name]:
            raise WorkflowHistoryError("activity event phase is inconsistent")
        if not event.idempotency_key:
            raise WorkflowHistoryError("activity start requires idempotency_key")
        state.current_activity = event.activity_name
        state.current_attempt = event.attempt
        state.attempts[event.activity_name] = event.attempt
        state.phase = event.phase
        state.retry_after_seconds = None
        state.pending_failure_kind = None
        state.pending_error_code = None
        state.retry_scheduled = False

    elif event.event_type is WorkflowEventType.ACTIVITY_ATTEMPT_FAILED:
        _require_current_activity(state, event)
        if (
            not event.error_code
            or event.failure_kind is None
            or event.retryable is None
            or event.retry_scheduled is None
        ):
            raise WorkflowHistoryError("failed attempt requires structured failure fields")
        if event.outcome_status is not None or event.result_ref is not None:
            raise WorkflowHistoryError("failed attempt cannot contain an activity outcome")
        state.current_activity = None
        state.current_attempt = None
        state.retry_after_seconds = event.retry_after_seconds
        state.pending_failure_kind = event.failure_kind
        state.pending_error_code = event.error_code
        state.retry_scheduled = event.retry_scheduled
        if state.retry_scheduled != (state.retry_after_seconds is not None):
            raise WorkflowHistoryError(
                "retry_scheduled must match the presence of retry_after_seconds"
            )

    elif event.event_type is WorkflowEventType.ACTIVITY_COMPLETED:
        _require_current_activity(state, event)
        if event.outcome_status is None or not event.result_ref:
            raise WorkflowHistoryError("completed activity requires outcome and result_ref")
        if event.outcome_status is ActivityOutcomeStatus.TARGET_FAILED and event.activity_name not in {
            ActivityName.PREFLIGHT,
            ActivityName.EXECUTE,
        }:
            raise WorkflowHistoryError("TARGET_FAILED is invalid for this activity")
        state.completed_activities.append(event.activity_name)
        state.last_result_ref = event.result_ref
        state.current_activity = None
        state.current_attempt = None
        state.retry_after_seconds = None
        state.pending_failure_kind = None
        state.pending_error_code = None
        state.retry_scheduled = False
        if event.outcome_status is ActivityOutcomeStatus.TARGET_FAILED:
            state.target_failed_seen = True
        if (
            event.activity_name is ActivityName.PREFLIGHT
            and event.outcome_status is ActivityOutcomeStatus.TARGET_FAILED
        ):
            state.next_activity_index = ACTIVITY_ORDER.index(ActivityName.NORMALIZE)
        else:
            state.next_activity_index += 1

    elif event.event_type in {
        WorkflowEventType.WORKFLOW_FAILED,
        WorkflowEventType.WORKFLOW_REJECTED,
        WorkflowEventType.WORKFLOW_CANCELLED,
    }:
        _require_running(state)
        if not event.error_code:
            raise WorkflowHistoryError("terminal failure event requires error_code")
        state.status = {
            WorkflowEventType.WORKFLOW_FAILED: WorkflowStatus.FAILED,
            WorkflowEventType.WORKFLOW_REJECTED: WorkflowStatus.REJECTED,
            WorkflowEventType.WORKFLOW_CANCELLED: WorkflowStatus.CANCELLED,
        }[event.event_type]
        state.phase = WorkflowPhase.COMPLETE
        state.terminal_error_code = event.error_code
        state.current_activity = None
        state.current_attempt = None
        state.retry_after_seconds = None
        state.pending_failure_kind = None
        state.pending_error_code = None
        state.retry_scheduled = False

    elif event.event_type is WorkflowEventType.WORKFLOW_SUCCEEDED:
        _require_running(state)
        if state.next_activity is not None or state.current_activity is not None:
            raise WorkflowHistoryError("workflow cannot succeed before all activities complete")
        if event.error_code is not None:
            raise WorkflowHistoryError("successful workflow cannot contain error_code")
        state.status = WorkflowStatus.SUCCEEDED
        state.phase = WorkflowPhase.COMPLETE

    else:
        raise WorkflowHistoryError(f"unsupported workflow event: {event.event_type}")

    state.last_sequence = event.sequence


def _require_running(state: WorkflowState) -> None:
    if state.status is not WorkflowStatus.RUNNING:
        raise WorkflowHistoryError("workflow must be running for this event")


def _require_current_activity(state: WorkflowState, event: WorkflowEvent) -> None:
    _require_running(state)
    if event.activity_name is None or event.activity_name is not state.current_activity:
        raise WorkflowHistoryError("activity event does not match current activity")
    if event.attempt != state.current_attempt:
        raise WorkflowHistoryError("activity event attempt does not match current attempt")
    if event.phase is not state.phase:
        raise WorkflowHistoryError("activity event phase does not match current phase")
