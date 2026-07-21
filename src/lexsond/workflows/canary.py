from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Callable, Protocol

from .contracts import (
    ActivityFailure,
    ActivityInvocation,
    ActivityLeaseBusy,
    ActivityName,
    ActivityOutcome,
    ActivityOutcomeStatus,
    CanaryWorkflowInput,
    FailureKind,
    WorkflowEvent,
    WorkflowEventType,
    WorkflowPhase,
)
from .state import ACTIVITY_PHASE, WorkflowState, project_workflow_state


class ConcurrentWorkflowUpdate(RuntimeError):
    pass


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


class WorkflowJournal(Protocol):
    def load(self, run_id: str) -> tuple[WorkflowEvent, ...]: ...

    def append(self, event: WorkflowEvent, *, expected_sequence: int) -> None: ...


class WorkflowRunInitializer(Protocol):
    def prepare_run(self, workflow_input: CanaryWorkflowInput) -> None: ...


class CanaryActivities(Protocol):
    def invoke(
        self,
        workflow_input: CanaryWorkflowInput,
        invocation: ActivityInvocation,
        cancel_signal: CancellationSignal | None,
    ) -> ActivityOutcome: ...


class RetryWaiter(Protocol):
    def wait(
        self, seconds: float, cancel_signal: CancellationSignal | None
    ) -> bool: ...


class InMemoryWorkflowJournal:
    """Atomic in-memory journal used by tests and local development."""

    def __init__(self) -> None:
        self._events: dict[str, list[WorkflowEvent]] = {}
        self._lock = threading.Lock()

    def load(self, run_id: str) -> tuple[WorkflowEvent, ...]:
        with self._lock:
            return tuple(self._events.get(run_id, ()))

    def append(self, event: WorkflowEvent, *, expected_sequence: int) -> None:
        with self._lock:
            events = self._events.setdefault(event.run_id, [])
            if len(events) != expected_sequence:
                if (
                    1 <= event.sequence <= len(events)
                    and events[event.sequence - 1] == event
                ):
                    return
                raise ConcurrentWorkflowUpdate(
                    f"expected sequence {expected_sequence}, found {len(events)}"
                )
            if event.sequence != expected_sequence + 1:
                raise ConcurrentWorkflowUpdate("event sequence does not follow expected sequence")
            events.append(event)


class CanaryWorkflow:
    """Replayable CanaryWorkflow core, independent of a workflow SDK.

    Temporal activities can implement ``CanaryActivities`` while this class
    remains the authoritative transition, retry, and target-failure policy.
    """

    def __init__(
        self,
        journal: WorkflowJournal,
        *,
        clock: Callable[[], datetime] | None = None,
        retry_waiter: RetryWaiter | None = None,
    ) -> None:
        self._journal = journal
        self._clock = clock or (lambda: datetime.now(UTC))
        self._retry_waiter = retry_waiter or _PollingRetryWaiter()

    def run(
        self,
        workflow_input: CanaryWorkflowInput,
        activities: CanaryActivities,
        *,
        cancel_signal: CancellationSignal | None = None,
    ) -> WorkflowState:
        events = self._journal.load(workflow_input.run_id)
        state = project_workflow_state(workflow_input, events)
        if state.is_terminal:
            return state

        if state.last_sequence == 0:
            state = self._record(
                workflow_input,
                state,
                event_type=WorkflowEventType.WORKFLOW_STARTED,
                phase=WorkflowPhase.NONE,
                workflow_input_sha256=workflow_input.content_hash(),
            )

        while not state.is_terminal:
            if state.pending_error_code and not state.retry_scheduled:
                terminal_type = (
                    WorkflowEventType.WORKFLOW_REJECTED
                    if state.pending_failure_kind
                    in {FailureKind.CONFIGURATION, FailureKind.POLICY}
                    else WorkflowEventType.WORKFLOW_FAILED
                )
                return self._terminate(
                    workflow_input,
                    state,
                    terminal_type,
                    state.pending_error_code,
                )
            if _is_cancelled(cancel_signal):
                return self._terminate(
                    workflow_input,
                    state,
                    WorkflowEventType.WORKFLOW_CANCELLED,
                    "WORKFLOW_CANCEL_REQUESTED",
                )

            if state.next_activity is None and state.current_activity is None:
                return self._record(
                    workflow_input,
                    state,
                    event_type=WorkflowEventType.WORKFLOW_SUCCEEDED,
                    phase=WorkflowPhase.COMPLETE,
                    result_ref=state.last_result_ref,
                )

            if state.current_activity is None:
                if state.retry_after_seconds:
                    completed_wait = self._retry_waiter.wait(
                        state.retry_after_seconds, cancel_signal
                    )
                    if not completed_wait or _is_cancelled(cancel_signal):
                        return self._terminate(
                            workflow_input,
                            state,
                            WorkflowEventType.WORKFLOW_CANCELLED,
                            "WORKFLOW_CANCELLED_DURING_RETRY",
                        )
                activity_name = state.next_activity
                if activity_name is None:
                    continue
                attempt = state.attempts.get(activity_name, 0) + 1
                state = self._record(
                    workflow_input,
                    state,
                    event_type=WorkflowEventType.ACTIVITY_STARTED,
                    phase=ACTIVITY_PHASE[activity_name],
                    activity_name=activity_name,
                    attempt=attempt,
                    idempotency_key=_idempotency_key(
                        workflow_input.run_id, activity_name
                    ),
                )

            activity_name = state.current_activity
            attempt = state.current_attempt
            if activity_name is None or attempt is None:
                raise RuntimeError("workflow projection lost the current activity")
            invocation = ActivityInvocation(
                run_id=workflow_input.run_id,
                activity_name=activity_name,
                attempt=attempt,
                idempotency_key=_idempotency_key(
                    workflow_input.run_id, activity_name
                ),
                input_ref=state.last_result_ref,
            )

            try:
                outcome = activities.invoke(workflow_input, invocation, cancel_signal)
            except ActivityLeaseBusy as busy:
                completed_wait = self._retry_waiter.wait(
                    busy.retry_after_seconds,
                    cancel_signal,
                )
                if not completed_wait or _is_cancelled(cancel_signal):
                    return self._terminate(
                        workflow_input,
                        state,
                        WorkflowEventType.WORKFLOW_CANCELLED,
                        "WORKFLOW_CANCELLED_DURING_RETRY",
                    )
                continue
            except ActivityFailure as failure:
                can_retry = (
                    failure.retryable
                    and attempt < workflow_input.retry_policy.max_attempts
                )
                delay = workflow_input.retry_policy.delay_after(attempt) if can_retry else None
                state = self._record(
                    workflow_input,
                    state,
                    event_type=WorkflowEventType.ACTIVITY_ATTEMPT_FAILED,
                    phase=ACTIVITY_PHASE[activity_name],
                    activity_name=activity_name,
                    attempt=attempt,
                    failure_kind=failure.kind,
                    error_code=failure.error_code,
                    retryable=failure.retryable,
                    retry_scheduled=can_retry,
                    retry_after_seconds=delay,
                )
                continue
            except Exception:
                return self._terminate(
                    workflow_input,
                    state,
                    WorkflowEventType.WORKFLOW_FAILED,
                    "UNEXPECTED_ACTIVITY_EXCEPTION",
                )

            if (
                outcome.status is ActivityOutcomeStatus.TARGET_FAILED
                and activity_name not in {ActivityName.PREFLIGHT, ActivityName.EXECUTE}
            ):
                return self._terminate(
                    workflow_input,
                    state,
                    WorkflowEventType.WORKFLOW_FAILED,
                    "INVALID_TARGET_FAILURE_OUTCOME",
                )
            state = self._record(
                workflow_input,
                state,
                event_type=WorkflowEventType.ACTIVITY_COMPLETED,
                phase=ACTIVITY_PHASE[activity_name],
                activity_name=activity_name,
                attempt=attempt,
                outcome_status=outcome.status,
                result_ref=outcome.result_ref,
            )
        return state

    def _terminate(
        self,
        workflow_input: CanaryWorkflowInput,
        state: WorkflowState,
        event_type: WorkflowEventType,
        error_code: str,
    ) -> WorkflowState:
        return self._record(
            workflow_input,
            state,
            event_type=event_type,
            phase=state.phase,
            error_code=error_code,
        )

    def _record(
        self,
        workflow_input: CanaryWorkflowInput,
        state: WorkflowState,
        *,
        event_type: WorkflowEventType,
        phase: WorkflowPhase,
        **fields: object,
    ) -> WorkflowState:
        event = WorkflowEvent.deterministic(
            run_id=workflow_input.run_id,
            sequence=state.last_sequence + 1,
            event_type=event_type,
            phase=phase,
            occurred_at=self._timestamp(),
            **fields,
        )
        self._journal.append(event, expected_sequence=state.last_sequence)
        return project_workflow_state(
            workflow_input, self._journal.load(workflow_input.run_id)
        )

    def _timestamp(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("workflow clock must return a timezone-aware datetime")
        return value.isoformat()


class _PollingRetryWaiter:
    def wait(
        self, seconds: float, cancel_signal: CancellationSignal | None
    ) -> bool:
        deadline = time.monotonic() + seconds
        while True:
            if _is_cancelled(cancel_signal):
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(remaining, 0.1))


def _idempotency_key(run_id: str, activity_name: ActivityName) -> str:
    return f"canary:{run_id}:{activity_name.value}"


def _is_cancelled(cancel_signal: CancellationSignal | None) -> bool:
    return cancel_signal is not None and cancel_signal.is_set()
