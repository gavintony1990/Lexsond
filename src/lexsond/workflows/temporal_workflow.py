from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import common, exceptions, workflow
from temporalio.workflow import ActivityCancellationType

with workflow.unsafe.imports_passed_through():
    from lexsond.workflows.contracts import (
        ActivityInvocation,
        ActivityName,
        ActivityOutcomeStatus,
        CanaryWorkflowInput,
        FailureKind,
        WorkflowEvent,
        WorkflowEventType,
        WorkflowPhase,
    )
    from lexsond.workflows.state import (
        ACTIVITY_PHASE,
        WorkflowState,
        project_workflow_state,
    )
    from lexsond.workflows.temporal_activities import (
        APPEND_EVENT_ACTIVITY,
        EXECUTE_STEP_ACTIVITY,
        LOAD_HISTORY_ACTIVITY,
    )
    from lexsond.workflows.temporal_contracts import (
        TemporalAppendEventRequest,
        TemporalCanaryResult,
        TemporalHistoryRequest,
        TemporalStepRequest,
        TemporalStepResult,
    )


_JOURNAL_TIMEOUT = timedelta(seconds=30)
_JOURNAL_RETRY = common.RetryPolicy(
    initial_interval=timedelta(milliseconds=200),
    backoff_coefficient=2,
    maximum_interval=timedelta(seconds=3),
    maximum_attempts=5,
)
_NO_SDK_ACTIVITY_RETRY = common.RetryPolicy(maximum_attempts=1)


@workflow.defn(name="ProbeCanaryWorkflow")
class TemporalCanaryWorkflow:
    def __init__(self) -> None:
        self._workflow_input: CanaryWorkflowInput | None = None
        self._events: list[WorkflowEvent] = []
        self._state: WorkflowState | None = None

    @workflow.run
    async def run(self, workflow_input: CanaryWorkflowInput) -> TemporalCanaryResult:
        self._workflow_input = workflow_input
        history = await workflow.execute_activity(
            LOAD_HISTORY_ACTIVITY,
            TemporalHistoryRequest(workflow_input=workflow_input),
            result_type=list[dict[str, Any]],
            start_to_close_timeout=_JOURNAL_TIMEOUT,
            retry_policy=_JOURNAL_RETRY,
        )
        self._events = [WorkflowEvent.from_dict(value) for value in history]
        self._state = project_workflow_state(workflow_input, self._events)
        if self._state.is_terminal:
            return TemporalCanaryResult.from_state(self._state)

        try:
            if self._state.last_sequence == 0:
                await self._record(
                    event_type=WorkflowEventType.WORKFLOW_STARTED,
                    phase=WorkflowPhase.NONE,
                    workflow_input_sha256=workflow_input.content_hash(),
                )
            return await self._drive()
        except asyncio.CancelledError:
            if self._state is not None and not self._state.is_terminal:
                await self._record(
                    event_type=WorkflowEventType.WORKFLOW_CANCELLED,
                    phase=self._state.phase,
                    error_code="WORKFLOW_CANCEL_REQUESTED",
                )
            raise

    @workflow.query
    def current_state(self) -> TemporalCanaryResult | None:
        return (
            TemporalCanaryResult.from_state(self._state)
            if self._state is not None
            else None
        )

    async def _drive(self) -> TemporalCanaryResult:
        workflow_input = self._require_input()
        while True:
            state = self._require_state()
            if state.pending_error_code and not state.retry_scheduled:
                terminal_type = (
                    WorkflowEventType.WORKFLOW_REJECTED
                    if state.pending_failure_kind
                    in {FailureKind.CONFIGURATION, FailureKind.POLICY}
                    else WorkflowEventType.WORKFLOW_FAILED
                )
                await self._record(
                    event_type=terminal_type,
                    phase=state.phase,
                    error_code=state.pending_error_code,
                )
                return TemporalCanaryResult.from_state(self._require_state())

            if state.next_activity is None and state.current_activity is None:
                await self._record(
                    event_type=WorkflowEventType.WORKFLOW_SUCCEEDED,
                    phase=WorkflowPhase.COMPLETE,
                    result_ref=state.last_result_ref,
                )
                return TemporalCanaryResult.from_state(self._require_state())

            if state.current_activity is None:
                if state.retry_after_seconds:
                    await workflow.sleep(state.retry_after_seconds)
                activity_name = state.next_activity
                if activity_name is None:
                    continue
                attempt = state.attempts.get(activity_name, 0) + 1
                await self._record(
                    event_type=WorkflowEventType.ACTIVITY_STARTED,
                    phase=ACTIVITY_PHASE[activity_name],
                    activity_name=activity_name,
                    attempt=attempt,
                    idempotency_key=_idempotency_key(
                        workflow_input.run_id, activity_name
                    ),
                )
                state = self._require_state()

            activity_name = state.current_activity
            attempt = state.current_attempt
            if activity_name is None or attempt is None:
                raise RuntimeError("workflow projection lost current Activity")
            invocation = ActivityInvocation(
                run_id=workflow_input.run_id,
                activity_name=activity_name,
                attempt=attempt,
                idempotency_key=_idempotency_key(
                    workflow_input.run_id, activity_name
                ),
                input_ref=state.last_result_ref,
            )
            response = await self._execute_step(
                TemporalStepRequest(
                    workflow_input=workflow_input,
                    invocation=invocation,
                )
            )
            response.validate()
            if response.status == "BUSY":
                if response.retry_after_seconds is None:
                    raise RuntimeError("busy Activity response lost retry duration")
                await workflow.sleep(response.retry_after_seconds)
                continue
            if response.status == "FAILED":
                failure_kind = FailureKind(response.failure_kind)
                can_retry = bool(response.retryable) and (
                    attempt < workflow_input.retry_policy.max_attempts
                )
                delay = (
                    workflow_input.retry_policy.delay_after(attempt)
                    if can_retry
                    else None
                )
                await self._record(
                    event_type=WorkflowEventType.ACTIVITY_ATTEMPT_FAILED,
                    phase=ACTIVITY_PHASE[activity_name],
                    activity_name=activity_name,
                    attempt=attempt,
                    failure_kind=failure_kind,
                    error_code=response.error_code,
                    retryable=response.retryable,
                    retry_scheduled=can_retry,
                    retry_after_seconds=delay,
                )
                continue

            outcome_status = ActivityOutcomeStatus(response.status)
            if (
                outcome_status is ActivityOutcomeStatus.TARGET_FAILED
                and activity_name not in {ActivityName.PREFLIGHT, ActivityName.EXECUTE}
            ):
                await self._record(
                    event_type=WorkflowEventType.WORKFLOW_FAILED,
                    phase=state.phase,
                    error_code="INVALID_TARGET_FAILURE_OUTCOME",
                )
                return TemporalCanaryResult.from_state(self._require_state())
            await self._record(
                event_type=WorkflowEventType.ACTIVITY_COMPLETED,
                phase=ACTIVITY_PHASE[activity_name],
                activity_name=activity_name,
                attempt=attempt,
                outcome_status=outcome_status,
                result_ref=response.result_ref,
            )

    async def _execute_step(
        self, request: TemporalStepRequest
    ) -> TemporalStepResult:
        workflow_input = self._require_input()
        try:
            return await workflow.execute_activity(
                EXECUTE_STEP_ACTIVITY,
                request,
                result_type=TemporalStepResult,
                start_to_close_timeout=timedelta(
                    seconds=workflow_input.activity_timeout_seconds
                ),
                heartbeat_timeout=timedelta(
                    seconds=workflow_input.activity_heartbeat_seconds
                ),
                retry_policy=_NO_SDK_ACTIVITY_RETRY,
                cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                activity_id=(
                    f"{request.invocation.run_id}:"
                    f"{request.invocation.activity_name.value}:"
                    f"{request.invocation.attempt}"
                ),
            )
        except exceptions.ActivityError as error:
            if isinstance(error.cause, exceptions.CancelledError):
                raise asyncio.CancelledError from error
            return TemporalStepResult(
                status="FAILED",
                failure_kind=FailureKind.INFRASTRUCTURE.value,
                error_code="TEMPORAL_ACTIVITY_EXECUTION_FAILED",
                retryable=True,
            )

    async def _record(
        self,
        *,
        event_type: WorkflowEventType,
        phase: WorkflowPhase,
        **fields: object,
    ) -> None:
        state = self._require_state()
        workflow_input = self._require_input()
        event = WorkflowEvent.deterministic(
            run_id=workflow_input.run_id,
            sequence=state.last_sequence + 1,
            event_type=event_type,
            phase=phase,
            occurred_at=workflow.now().isoformat(),
            **fields,
        )
        await workflow.execute_activity(
            APPEND_EVENT_ACTIVITY,
            TemporalAppendEventRequest(
                event=event.to_dict(),
                expected_sequence=state.last_sequence,
            ),
            start_to_close_timeout=_JOURNAL_TIMEOUT,
            retry_policy=_JOURNAL_RETRY,
        )
        self._events.append(event)
        self._state = project_workflow_state(workflow_input, self._events)

    def _require_state(self) -> WorkflowState:
        if self._state is None:
            raise RuntimeError("Temporal workflow state is not initialized")
        return self._state

    def _require_input(self) -> CanaryWorkflowInput:
        if self._workflow_input is None:
            raise RuntimeError("Temporal workflow input is not initialized")
        return self._workflow_input


def _idempotency_key(run_id: str, activity_name: ActivityName) -> str:
    return f"canary:{run_id}:{activity_name.value}"
