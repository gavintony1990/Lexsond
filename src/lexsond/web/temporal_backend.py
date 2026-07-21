from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from ..storage.runtime_contracts import canonical_json_bytes
from ..suite import compile_suite
from ..workflows import CanaryWorkflowInput, RetryPolicy, WorkflowEvent


TemporalEventSink = Callable[[str, Sequence[WorkflowEvent]], None]
TemporalTerminalSink = Callable[
    [str, str, Mapping[str, Any] | None, str | None], None
]


class TemporalDispatchUncertain(RuntimeError):
    """The deterministic workflow start may have reached Temporal."""


class TemporalDispatchUnavailable(RuntimeError):
    """Temporal could not confirm or attach to a deterministic dispatch."""


class _TemporalMonitorRetryable(RuntimeError):
    """An attached monitor must reconnect without dropping durable intent."""


@dataclass(frozen=True, slots=True)
class TemporalLaunchArtifacts:
    workflow_input: CanaryWorkflowInput
    endpoint_configuration: Mapping[str, Any]
    suite_document: Mapping[str, Any]


def build_temporal_launch_artifacts(
    *,
    run_id: str,
    target: Mapping[str, Any],
    model: str,
    suite_document: Mapping[str, Any],
    region: str,
    activity_timeout_seconds: float = 120.0,
    activity_heartbeat_seconds: float = 15.0,
) -> TemporalLaunchArtifacts:
    """Build the entire safe Temporal input without a secret or credential ref."""

    credential_ref = target.get("credential_ref")
    if not isinstance(credential_ref, str) or not credential_ref:
        raise ValueError("Temporal target requires a credential_ref")
    base_url = target.get("base_url")
    if not isinstance(base_url, str) or not base_url.startswith("https://"):
        raise ValueError("Temporal endpoint snapshots require an HTTPS base_url")
    suite = compile_suite(suite_document)
    suite_value = dict(suite_document)
    suite_sha256 = hashlib.sha256(canonical_json_bytes(suite_value)).hexdigest()
    configuration: dict[str, Any] = {
        "target_id": target["id"],
        "provider_id": target.get("provider_id") or "custom",
        "protocol": "openai-chat",
        "base_url": base_url,
        "model": model,
    }
    configuration_sha256 = hashlib.sha256(
        canonical_json_bytes(configuration)
    ).hexdigest()
    # Credential references stay out of Temporal history, but their digest is
    # part of the immutable snapshot identity so reference rotation creates a
    # new snapshot instead of conflicting with an older row.
    credential_identity = hashlib.sha256(credential_ref.encode("utf-8")).hexdigest()
    endpoint_snapshot_id = f"web-{configuration_sha256}-{credential_identity[:16]}"
    suite_uri = f"https://control.lexsond.invalid/suites/{suite_sha256}"
    workflow_input = CanaryWorkflowInput(
        run_id=run_id,
        endpoint_snapshot_id=endpoint_snapshot_id,
        suite_name=suite.name,
        suite_version=suite.version,
        suite_uri=suite_uri,
        suite_sha256=suite_sha256,
        region=region,
        activity_timeout_seconds=activity_timeout_seconds,
        activity_heartbeat_seconds=activity_heartbeat_seconds,
        # The execute-native-probe activity can be billable. The workflow owns
        # retry semantics and a Web launch never hides a duplicate request.
        retry_policy=RetryPolicy(max_attempts=1),
    )
    return TemporalLaunchArtifacts(workflow_input, configuration, suite_value)


class ConfiguredTemporalLauncher:
    """PostgreSQL-backed Temporal launcher with event and result projection."""

    available = True
    status = "READY"

    def __init__(
        self,
        *,
        pool: Any,
        temporal_target: str,
        namespace: str,
        task_queue: str,
        region: str,
        poll_seconds: float = 0.25,
        connect_timeout_seconds: float = 10.0,
        start_timeout_seconds: float = 10.0,
    ) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (temporal_target, namespace, task_queue, region)
        ):
            raise ValueError("Temporal connection settings must be non-empty")
        if not 0.05 <= poll_seconds <= 10:
            raise ValueError("poll_seconds must be between 0.05 and 10")
        if not 0.1 <= connect_timeout_seconds <= 120:
            raise ValueError("connect_timeout_seconds must be between 0.1 and 120")
        if not 0.1 <= start_timeout_seconds <= 120:
            raise ValueError("start_timeout_seconds must be between 0.1 and 120")
        from ..storage.postgres import (
            PostgresCanaryRuntimeStore,
            PostgresSnapshotWriter,
            PostgresWorkflowJournal,
        )

        self._pool = pool
        self._writer = PostgresSnapshotWriter(pool)
        self._journal = PostgresWorkflowJournal(pool)
        self._runtime = PostgresCanaryRuntimeStore(pool)
        self._temporal_target = temporal_target
        self._namespace = namespace
        self._task_queue = task_queue
        self._region = region
        self._poll_seconds = poll_seconds
        self._connect_timeout_seconds = connect_timeout_seconds
        self._start_timeout_seconds = start_timeout_seconds
        self._lock = threading.Lock()
        self._active: dict[
            str, tuple[asyncio.AbstractEventLoop, Any, asyncio.Event]
        ] = {}
        self._cancel_requested: set[str] = set()
        self._threads: dict[str, threading.Thread] = {}
        self._inflight_starts = 0
        self._inflight_dispatches = 0
        self._closing = False
        self._closing_event = threading.Event()
        self._dispatch_retry_initial_seconds = 0.25
        self._pool_closed = False
        self._pool_close_in_progress = False
        self._stopped = threading.Event()

    def start(
        self,
        *,
        run_id: str,
        target: Mapping[str, Any],
        model: str,
        suite_document: Mapping[str, Any],
        on_events: TemporalEventSink,
        on_terminal: TemporalTerminalSink,
    ) -> CanaryWorkflowInput:
        artifacts = build_temporal_launch_artifacts(
            run_id=run_id,
            target=target,
            model=model,
            suite_document=suite_document,
            region=self._region,
        )
        workflow_input = artifacts.workflow_input
        self._begin_start()
        try:
            configuration_sha256, suite_sha256 = self._writer.persist(
                endpoint_snapshot_id=workflow_input.endpoint_snapshot_id,
                provider_id=str(target.get("provider_id") or "custom"),
                protocol="openai-chat",
                base_url=str(target["base_url"]),
                model=model,
                credential_ref=str(target["credential_ref"]),
                configuration=artifacts.endpoint_configuration,
                suite_uri=workflow_input.suite_uri,
                suite_document=artifacts.suite_document,
            )
            if configuration_sha256 not in workflow_input.endpoint_snapshot_id:
                raise RuntimeError("endpoint snapshot digest does not match launch input")
            if suite_sha256 != workflow_input.suite_sha256:
                raise RuntimeError("suite snapshot digest does not match launch input")
            # Persist the safe workflow input before the asynchronous dispatch. It
            # acts as a durable outbox and lets a restarted control process attach
            # to the same deterministic Temporal workflow ID.
            self._journal.prepare_run(workflow_input)

            self._start_monitor(workflow_input, on_events, on_terminal)
            return workflow_input
        finally:
            self._end_start()

    def recover(
        self,
        run_id: str,
        *,
        on_events: TemporalEventSink,
        on_terminal: TemporalTerminalSink,
    ) -> bool:
        self._begin_start()
        try:
            workflow_input = self._journal.load_input(run_id)
            if workflow_input is None:
                return False
            self._start_monitor(workflow_input, on_events, on_terminal)
            return True
        finally:
            self._end_start()

    def _start_monitor(
        self,
        workflow_input: CanaryWorkflowInput,
        on_events: TemporalEventSink,
        on_terminal: TemporalTerminalSink,
    ) -> None:
        run_id = workflow_input.run_id

        thread = threading.Thread(
            target=self._thread_main,
            args=(workflow_input, on_events, on_terminal),
            name=f"probe-temporal-{run_id[:8]}",
            daemon=True,
        )
        with self._lock:
            if self._closing:
                raise RuntimeError("Temporal launcher is closing")
            if run_id in self._threads:
                raise ValueError("Temporal run is already active")
            self._threads[run_id] = thread
            try:
                thread.start()
            except BaseException:
                self._threads.pop(run_id, None)
                raise

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            self._cancel_requested.add(run_id)
            active = self._active.get(run_id)
        if active is None:
            return False
        loop, handle, _ = active
        future = asyncio.run_coroutine_threadsafe(handle.cancel(), loop)
        future.result(timeout=10)
        return True

    def close(self) -> bool:
        with self._lock:
            self._closing = True
            self._closing_event.set()
            threads = tuple(self._threads.values())
            wakeups = tuple(
                (loop, shutdown_event)
                for loop, _, shutdown_event in self._active.values()
            )
        for loop, shutdown_event in wakeups:
            try:
                loop.call_soon_threadsafe(shutdown_event.set)
            except RuntimeError:
                # The monitor's finally block will remove the closed loop and
                # close the pool once no live thread can use it.
                continue
        for thread in threads:
            thread.join(timeout=2)
        self._close_pool_when_idle()
        return self._stopped.is_set()

    def wait_closed(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        close_failures = 0
        retry_delay = 0.05
        while True:
            if self._stopped.is_set():
                return True
            try:
                self._close_pool_when_idle()
            except BaseException as exc:
                close_failures += 1
                if close_failures >= 3:
                    raise RuntimeError("Temporal PostgreSQL pool did not close") from exc
            if self._stopped.is_set():
                return True
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            wait_seconds = retry_delay if remaining is None else min(retry_delay, remaining)
            if self._stopped.wait(wait_seconds):
                return True
            retry_delay = min(retry_delay * 2, 0.5)

    def _thread_main(
        self,
        workflow_input: CanaryWorkflowInput,
        on_events: TemporalEventSink,
        on_terminal: TemporalTerminalSink,
    ) -> None:
        dispatch_retry_seconds = self._dispatch_retry_initial_seconds
        dispatch_outcome_uncertain = False
        try:
            while True:
                try:
                    asyncio.run(self._execute(workflow_input, on_events, on_terminal))
                    break
                except TemporalDispatchUncertain:
                    dispatch_outcome_uncertain = True
                except TemporalDispatchUnavailable:
                    if not dispatch_outcome_uncertain:
                        raise
                except _TemporalMonitorRetryable:
                    # A monitor retry can only occur after start/attach returned a
                    # handle, so a later connection outage must not turn the
                    # durable execution into a synthetic terminal failure.
                    dispatch_outcome_uncertain = True
                # Retrying the deterministic Workflow ID either creates the one
                # execution or attaches after an uncertain acknowledgement.
                if self._closing_event.wait(dispatch_retry_seconds):
                    return
                dispatch_retry_seconds = min(dispatch_retry_seconds * 2, 5.0)
        except Exception:
            with self._lock:
                closing = self._closing
            if closing:
                return
            self._project_failed_terminal_until_closed(
                workflow_input.run_id,
                on_terminal,
            )
        finally:
            with self._lock:
                self._active.pop(workflow_input.run_id, None)
                self._threads.pop(workflow_input.run_id, None)
                self._cancel_requested.discard(workflow_input.run_id)
            try:
                self._close_pool_when_idle()
            except Exception:
                # A concurrent wait_closed() owns bounded retry and error state.
                pass

    def _project_failed_terminal_until_closed(
        self,
        run_id: str,
        on_terminal: TemporalTerminalSink,
    ) -> None:
        projection_delay = self._dispatch_retry_initial_seconds
        while not self._closing_event.is_set():
            try:
                on_terminal(
                    run_id,
                    "FAILED",
                    None,
                    "TEMPORAL_EXECUTION_ERROR",
                )
                return
            except Exception:
                if self._closing_event.wait(projection_delay):
                    return
                projection_delay = min(projection_delay * 2, 5.0)

    async def _execute(
        self,
        workflow_input: CanaryWorkflowInput,
        on_events: TemporalEventSink,
        on_terminal: TemporalTerminalSink,
    ) -> None:
        from temporalio import common
        from temporalio.client import Client, WorkflowFailureError
        from temporalio.exceptions import CancelledError, WorkflowAlreadyStartedError
        from temporalio.service import RPCError, RPCStatusCode

        from ..workflows.temporal_workflow import TemporalCanaryWorkflow

        if self._is_closing():
            return
        try:
            client = await asyncio.wait_for(
                Client.connect(
                    self._temporal_target,
                    namespace=self._namespace,
                ),
                timeout=self._connect_timeout_seconds,
            )
        except Exception as exc:
            raise TemporalDispatchUnavailable(
                "Temporal connection is unavailable"
            ) from exc
        if not self._begin_dispatch():
            return
        workflow_id = f"probe-{workflow_input.run_id}"
        try:
            try:
                handle = await asyncio.wait_for(
                    client.start_workflow(
                        TemporalCanaryWorkflow.run,
                        workflow_input,
                        id=workflow_id,
                        task_queue=self._task_queue,
                        id_reuse_policy=common.WorkflowIDReusePolicy.REJECT_DUPLICATE,
                        id_conflict_policy=common.WorkflowIDConflictPolicy.FAIL,
                    ),
                    timeout=self._start_timeout_seconds,
                )
            except WorkflowAlreadyStartedError:
                try:
                    handle = client.get_workflow_handle(workflow_id)
                except Exception as exc:
                    raise TemporalDispatchUnavailable(
                        "Temporal workflow handle is unavailable"
                    ) from exc
            except TimeoutError as exc:
                raise TemporalDispatchUncertain(
                    "Temporal workflow start outcome is uncertain"
                ) from exc
            except RPCError as exc:
                deterministic_rejections = {
                    RPCStatusCode.INVALID_ARGUMENT,
                    RPCStatusCode.NOT_FOUND,
                    RPCStatusCode.PERMISSION_DENIED,
                    RPCStatusCode.FAILED_PRECONDITION,
                    RPCStatusCode.UNIMPLEMENTED,
                    RPCStatusCode.UNAUTHENTICATED,
                }
                if getattr(exc, "status", None) in deterministic_rejections:
                    raise RuntimeError("Temporal workflow dispatch was rejected") from exc
                raise TemporalDispatchUncertain(
                    "Temporal workflow start outcome is uncertain"
                ) from exc
            except Exception as exc:
                raise TemporalDispatchUncertain(
                    "Temporal workflow start outcome is uncertain"
                ) from exc
        finally:
            self._end_dispatch()
        loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()
        with self._lock:
            self._active[workflow_input.run_id] = (loop, handle, shutdown_event)
            cancel_requested = workflow_input.run_id in self._cancel_requested
            if self._closing:
                shutdown_event.set()
        if cancel_requested:
            try:
                await handle.cancel()
            except Exception as exc:
                raise _TemporalMonitorRetryable(
                    "Temporal cancellation acknowledgement is unavailable"
                ) from exc

        result_task = asyncio.create_task(handle.result())
        shutdown_task = asyncio.create_task(shutdown_event.wait())
        last_sequence = 0
        while not result_task.done():
            if shutdown_event.is_set():
                result_task.cancel()
                await asyncio.gather(result_task, return_exceptions=True)
                return
            last_sequence = self._publish_new_events_safely(
                workflow_input.run_id,
                last_sequence,
                on_events,
            )
            await asyncio.wait(
                (result_task, shutdown_task),
                timeout=self._poll_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        shutdown_task.cancel()
        await asyncio.gather(shutdown_task, return_exceptions=True)
        try:
            temporal_result = await result_task
        except WorkflowFailureError as exc:
            if isinstance(exc.cause, CancelledError):
                projection_delay = self._poll_seconds
                while not self._is_closing() and not shutdown_event.is_set():
                    try:
                        on_terminal(
                            workflow_input.run_id, "CANCELLED", None, None
                        )
                        return
                    except Exception:
                        # Cancellation is already authoritative. Retry only the
                        # PostgreSQL projection; never re-cancel a closed run.
                        if await self._wait_for_shutdown(
                            shutdown_event, projection_delay
                        ):
                            return
                        projection_delay = min(projection_delay * 2, 5.0)
                return
            # This is Temporal's authoritative non-cancellation Workflow failure.
            raise
        except Exception as exc:
            raise _TemporalMonitorRetryable(
                "Temporal result polling is unavailable"
            ) from exc
        if self._is_closing():
            return
        self._publish_new_events_safely(
            workflow_input.run_id,
            last_sequence,
            on_events,
        )
        projection_delay = self._poll_seconds
        while True:
            with self._lock:
                if self._closing:
                    return
            try:
                normalized_result = self._runtime.load_result(workflow_input.run_id)
                if str(temporal_result.status) == "SUCCEEDED" and normalized_result is None:
                    raise RuntimeError("Temporal result is not visible yet")
                on_terminal(
                    workflow_input.run_id,
                    str(temporal_result.status),
                    normalized_result,
                    temporal_result.terminal_error_code,
                )
                return
            except Exception:
                # Both the Temporal outcome and normalized result are durable.
                # Keep retrying projection in this process until it succeeds or
                # application shutdown hands recovery to the next process.
                if await self._wait_for_shutdown(shutdown_event, projection_delay):
                    return
                projection_delay = min(projection_delay * 2, 5.0)

    @staticmethod
    async def _wait_for_shutdown(
        shutdown_event: asyncio.Event, timeout_seconds: float
    ) -> bool:
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return False
        return True

    def _is_closing(self) -> bool:
        with self._lock:
            return self._closing

    def _begin_start(self) -> None:
        with self._lock:
            if self._closing:
                raise RuntimeError("Temporal launcher is closing")
            self._inflight_starts += 1

    def _end_start(self) -> None:
        with self._lock:
            self._inflight_starts -= 1
            if self._inflight_starts < 0:
                raise RuntimeError("Temporal launcher start lifecycle is corrupted")
        self._close_pool_when_idle()

    def _begin_dispatch(self) -> bool:
        with self._lock:
            if self._closing:
                return False
            self._inflight_dispatches += 1
            return True

    def _end_dispatch(self) -> None:
        with self._lock:
            self._inflight_dispatches -= 1
            if self._inflight_dispatches < 0:
                raise RuntimeError("Temporal dispatch lifecycle is corrupted")
        self._close_pool_when_idle()

    def _close_pool_when_idle(self) -> None:
        with self._lock:
            quiescent = (
                self._closing
                and not self._threads
                and self._inflight_starts == 0
                and self._inflight_dispatches == 0
            )
            should_close = (
                quiescent
                and not self._pool_closed
                and not self._pool_close_in_progress
            )
            if not should_close:
                return
            self._pool_close_in_progress = True
        try:
            self._pool.close()
        except BaseException:
            with self._lock:
                self._pool_close_in_progress = False
            raise
        else:
            with self._lock:
                self._pool_closed = True
                self._pool_close_in_progress = False
                self._stopped.set()

    def _publish_new_events_safely(
        self,
        run_id: str,
        last_sequence: int,
        on_events: TemporalEventSink,
    ) -> int:
        try:
            return self._publish_new_events(run_id, last_sequence, on_events)
        except Exception:
            return last_sequence

    def _publish_new_events(
        self,
        run_id: str,
        last_sequence: int,
        on_events: TemporalEventSink,
    ) -> int:
        events = self._journal.load(run_id)
        new_events = events[last_sequence:]
        if new_events:
            on_events(run_id, new_events)
        return len(events)


def temporal_launcher_from_environment(
    *, postgres_dsn: str | None = None
) -> ConfiguredTemporalLauncher | None:
    """Construct a production launcher only when all explicit env vars exist."""

    import os

    dsn = postgres_dsn or os.environ.get("LEXSOND_POSTGRES_DSN")
    target = os.environ.get("LEXSOND_TEMPORAL_TARGET")
    if not dsn or not target:
        return None
    from ..storage.postgres import PostgresPool

    pool = PostgresPool(
        dsn,
        application_name="lexsond-control",
    )
    try:
        return ConfiguredTemporalLauncher(
            pool=pool,
            temporal_target=target,
            namespace=os.environ.get("LEXSOND_TEMPORAL_NAMESPACE", "default"),
            task_queue=os.environ.get(
                "LEXSOND_TEMPORAL_TASK_QUEUE", "lexsond-canary-local"
            ),
            region=os.environ.get("LEXSOND_REGION", "local"),
        )
    except Exception:
        pool.close()
        raise
