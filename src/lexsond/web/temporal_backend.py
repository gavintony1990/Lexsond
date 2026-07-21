from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from ..storage.runtime_contracts import canonical_json_bytes
from ..suite import compile_suite
from ..workflows import CanaryWorkflowInput, RetryPolicy, WorkflowEvent


TemporalEventSink = Callable[[str, Sequence[WorkflowEvent]], None]
TemporalTerminalSink = Callable[
    [str, str, Mapping[str, Any] | None, str | None], None
]


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
    ) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (temporal_target, namespace, task_queue, region)
        ):
            raise ValueError("Temporal connection settings must be non-empty")
        if not 0.05 <= poll_seconds <= 10:
            raise ValueError("poll_seconds must be between 0.05 and 10")
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
        self._lock = threading.Lock()
        self._active: dict[str, tuple[asyncio.AbstractEventLoop, Any]] = {}
        self._cancel_requested: set[str] = set()
        self._threads: dict[str, threading.Thread] = {}
        self._closing = False

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

    def recover(
        self,
        run_id: str,
        *,
        on_events: TemporalEventSink,
        on_terminal: TemporalTerminalSink,
    ) -> bool:
        workflow_input = self._journal.load_input(run_id)
        if workflow_input is None:
            return False
        self._start_monitor(workflow_input, on_events, on_terminal)
        return True

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
            if run_id in self._threads:
                raise ValueError("Temporal run is already active")
            self._threads[run_id] = thread
        thread.start()

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            self._cancel_requested.add(run_id)
            active = self._active.get(run_id)
        if active is None:
            return False
        loop, handle = active
        future = asyncio.run_coroutine_threadsafe(handle.cancel(), loop)
        future.result(timeout=10)
        return True

    def close(self) -> None:
        with self._lock:
            self._closing = True
            threads = tuple(self._threads.values())
        for thread in threads:
            thread.join(timeout=2)
        self._pool.close()

    def _thread_main(
        self,
        workflow_input: CanaryWorkflowInput,
        on_events: TemporalEventSink,
        on_terminal: TemporalTerminalSink,
    ) -> None:
        try:
            asyncio.run(self._execute(workflow_input, on_events, on_terminal))
        except Exception:
            with self._lock:
                cancelled = workflow_input.run_id in self._cancel_requested
                closing = self._closing
            if cancelled or closing:
                return
            try:
                on_terminal(
                    workflow_input.run_id,
                    "FAILED",
                    None,
                    "TEMPORAL_EXECUTION_ERROR",
                )
            except Exception:
                return
        finally:
            with self._lock:
                self._active.pop(workflow_input.run_id, None)
                self._threads.pop(workflow_input.run_id, None)
                self._cancel_requested.discard(workflow_input.run_id)

    async def _execute(
        self,
        workflow_input: CanaryWorkflowInput,
        on_events: TemporalEventSink,
        on_terminal: TemporalTerminalSink,
    ) -> None:
        from temporalio import common
        from temporalio.client import Client
        from temporalio.exceptions import WorkflowAlreadyStartedError

        from ..workflows.temporal_workflow import TemporalCanaryWorkflow

        client = await Client.connect(
            self._temporal_target,
            namespace=self._namespace,
        )
        workflow_id = f"probe-{workflow_input.run_id}"
        try:
            handle = await client.start_workflow(
                TemporalCanaryWorkflow.run,
                workflow_input,
                id=workflow_id,
                task_queue=self._task_queue,
                id_reuse_policy=common.WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=common.WorkflowIDConflictPolicy.FAIL,
            )
        except WorkflowAlreadyStartedError:
            handle = client.get_workflow_handle(workflow_id)
        loop = asyncio.get_running_loop()
        with self._lock:
            self._active[workflow_input.run_id] = (loop, handle)
            cancel_requested = workflow_input.run_id in self._cancel_requested
        if cancel_requested:
            await handle.cancel()

        result_task = asyncio.create_task(handle.result())
        last_sequence = 0
        while not result_task.done():
            last_sequence = self._publish_new_events_safely(
                workflow_input.run_id,
                last_sequence,
                on_events,
            )
            await asyncio.wait((result_task,), timeout=self._poll_seconds)
        temporal_result = await result_task
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
                await asyncio.sleep(projection_delay)
                projection_delay = min(projection_delay * 2, 5.0)

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


def temporal_launcher_from_environment() -> Any:
    """Construct a production launcher only when all explicit env vars exist."""

    import os

    dsn = os.environ.get("LEXSOND_POSTGRES_DSN")
    target = os.environ.get("LEXSOND_TEMPORAL_TARGET")
    if not dsn or not target:
        return None
    from ..storage.postgres import PostgresPool

    pool = PostgresPool(
        dsn,
        application_name="lexsond-control",
    )
    return ConfiguredTemporalLauncher(
        pool=pool,
        temporal_target=target,
        namespace=os.environ.get("LEXSOND_TEMPORAL_NAMESPACE", "default"),
        task_queue=os.environ.get(
            "LEXSOND_TEMPORAL_TASK_QUEUE", "lexsond-canary-local"
        ),
        region=os.environ.get("LEXSOND_REGION", "local"),
    )
