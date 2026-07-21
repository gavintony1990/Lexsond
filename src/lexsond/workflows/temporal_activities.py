from __future__ import annotations

import asyncio
import threading

from temporalio import activity, exceptions

from .canary import CanaryActivities, WorkflowJournal, WorkflowRunInitializer
from .contracts import ActivityFailure, ActivityLeaseBusy, WorkflowEvent
from .temporal_contracts import (
    TemporalAppendEventRequest,
    TemporalHistoryRequest,
    TemporalStepRequest,
    TemporalStepResult,
)


LOAD_HISTORY_ACTIVITY = "probe.load_canary_history"
APPEND_EVENT_ACTIVITY = "probe.append_canary_event"
EXECUTE_STEP_ACTIVITY = "probe.execute_canary_step"


class TemporalJournalActivities:
    def __init__(
        self,
        journal: WorkflowJournal,
        *,
        run_initializer: WorkflowRunInitializer | None = None,
    ) -> None:
        self._journal = journal
        self._run_initializer = run_initializer

    @activity.defn(name=LOAD_HISTORY_ACTIVITY)
    def load_history(self, request: TemporalHistoryRequest) -> list[dict[str, object]]:
        if self._run_initializer is not None:
            self._run_initializer.prepare_run(request.workflow_input)
        return [
            event.to_dict()
            for event in self._journal.load(request.workflow_input.run_id)
        ]

    @activity.defn(name=APPEND_EVENT_ACTIVITY)
    def append_event(self, request: TemporalAppendEventRequest) -> None:
        event = WorkflowEvent.from_dict(request.event)
        self._journal.append(event, expected_sequence=request.expected_sequence)


class TemporalCanaryStepActivity:
    def __init__(self, delegate: CanaryActivities) -> None:
        self._delegate = delegate

    @activity.defn(name=EXECUTE_STEP_ACTIVITY)
    async def execute_step(self, request: TemporalStepRequest) -> TemporalStepResult:
        heartbeat_details = {
            "run_id": request.invocation.run_id,
            "activity_name": request.invocation.activity_name.value,
            "attempt": request.invocation.attempt,
        }
        activity.heartbeat(heartbeat_details)
        cancel_signal = threading.Event()
        delegate_task = asyncio.create_task(
            asyncio.to_thread(
                self._delegate.invoke,
                request.workflow_input,
                request.invocation,
                cancel_signal,
            )
        )
        heartbeat_interval = min(
            5.0,
            max(0.05, request.workflow_input.activity_heartbeat_seconds / 3),
        )
        try:
            while not delegate_task.done():
                completed, _ = await asyncio.wait(
                    {delegate_task},
                    timeout=heartbeat_interval,
                )
                if not completed:
                    activity.heartbeat(heartbeat_details)
            outcome = delegate_task.result()
        except (asyncio.CancelledError, exceptions.CancelledError):
            cancel_signal.set()
            while not delegate_task.done():
                _clear_current_task_cancellation()
                try:
                    # asyncio.wait never propagates cancellation into the
                    # to_thread Task and does not create a shield Future whose
                    # exception logger can outlive this Activity.
                    await asyncio.wait({delegate_task}, timeout=0.1)
                except (asyncio.CancelledError, exceptions.CancelledError):
                    continue
            if delegate_task.done():
                try:
                    delegate_task.result()
                except BaseException:
                    # Cancellation wins over a concurrent delegate failure. The
                    # Workflow records the cancellation as the terminal fact.
                    pass
            raise
        except ActivityLeaseBusy as busy:
            return TemporalStepResult.from_busy(busy)
        except ActivityFailure as failure:
            return TemporalStepResult.from_failure(failure)
        return TemporalStepResult.from_outcome(outcome)


def _clear_current_task_cancellation() -> None:
    current_task = asyncio.current_task()
    if current_task is not None:
        while current_task.cancelling():
            current_task.uncancel()
