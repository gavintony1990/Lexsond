from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import threading
import time
import unittest
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4, uuid5

from lexsond.mock_relay import create_server
from lexsond.storage import (
    FileEvidenceStore,
    SqliteCanaryRuntimeStore,
    SqliteWorkflowJournal,
)
from lexsond.workflows import (
    ActivityFailure,
    ActivityInvocation,
    ActivityName,
    ActivityOutcome,
    ActivityOutcomeStatus,
    CanaryWorkflowInput,
    FailureKind,
    RetryPolicy,
    WorkflowEventType,
    WorkflowStatus,
    project_workflow_state,
)


TEMPORAL_AVAILABLE = importlib.util.find_spec("temporalio") is not None
RUN_TEMPORAL_TESTS = os.environ.get("RUN_TEMPORAL_TESTS") == "1"

if TEMPORAL_AVAILABLE:
    from temporalio.client import WorkflowFailureError
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Replayer, Worker

    from lexsond.workflows.temporal_activities import (
        TemporalCanaryStepActivity,
        TemporalJournalActivities,
    )
    from lexsond.workflows.temporal_contracts import TemporalCanaryResult
    from lexsond.workflows.native_activities import (
        EndpointSnapshot,
        FileSuiteDocumentResolver,
        MappingEndpointSnapshotResolver,
        MappingSecretResolver,
        NativeCanaryActivities,
    )
    from lexsond.workflows.temporal_workflow import TemporalCanaryWorkflow


def workflow_input() -> CanaryWorkflowInput:
    return CanaryWorkflowInput(
        run_id=str(uuid4()),
        endpoint_snapshot_id="endpoint-snapshot-v1",
        suite_name="openai-compatible-canary",
        suite_version="2026.07.19",
        suite_uri="s3://probe-suites/openai-compatible.json",
        suite_sha256="a" * 64,
        region="cn-east-1",
        activity_timeout_seconds=10,
        activity_heartbeat_seconds=0.5,
        retry_policy=RetryPolicy(
            max_attempts=2,
            initial_backoff_seconds=0.01,
            max_backoff_seconds=0.01,
        ),
    )


class ScriptedTemporalActivities:
    def __init__(self) -> None:
        self.scripts: dict[ActivityName, list[object]] = defaultdict(list)
        self.calls: list[ActivityInvocation] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.block_activity: ActivityName | None = None
        self._lock = threading.Lock()

    def invoke(
        self,
        workflow_input: CanaryWorkflowInput,
        invocation: ActivityInvocation,
        cancel_signal: object,
    ) -> ActivityOutcome:
        with self._lock:
            self.calls.append(invocation)
            script = self.scripts[invocation.activity_name]
            item = script.pop(0) if script else None

        if invocation.activity_name is self.block_activity:
            self.started.set()
            while not self.release.wait(0.02):
                if cancel_signal.is_set():
                    raise ActivityFailure(
                        "ACTIVITY_CANCELLED",
                        kind=FailureKind.INFRASTRUCTURE,
                        retryable=False,
                    )

        if isinstance(item, BaseException):
            raise item
        if isinstance(item, ActivityOutcome):
            return item
        if item is not None:
            raise AssertionError(f"unsupported scripted result: {item!r}")
        return ActivityOutcome(
            ActivityOutcomeStatus.SUCCEEDED,
            f"evidence:{invocation.activity_name.value}:{invocation.attempt}",
        )


@unittest.skipUnless(
    TEMPORAL_AVAILABLE and RUN_TEMPORAL_TESTS,
    "set RUN_TEMPORAL_TESTS=1 and install the temporal extra",
)
class TemporalWorkflowIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_retry_query_and_cancellation_are_durable(self) -> None:
        async with await WorkflowEnvironment.start_local() as environment:
            with ThreadPoolExecutor(max_workers=8) as executor:
                await self._assert_success_and_manual_retry(environment, executor)
                await self._assert_target_failure_continues(environment, executor)
                await self._assert_query_while_activity_runs(environment, executor)
                await self._assert_cancellation_is_journaled(environment, executor)
                await self._assert_native_probe_end_to_end(environment, executor)

    async def _assert_success_and_manual_retry(
        self, environment: object, executor: ThreadPoolExecutor
    ) -> None:
        value = workflow_input()
        activities = ScriptedTemporalActivities()
        activities.scripts[ActivityName.EXECUTE] = [
            ActivityFailure(
                "UPSTREAM_CONNECTION_RESET",
                kind=FailureKind.INFRASTRUCTURE,
                retryable=True,
            )
        ]
        with TemporaryDirectory() as directory:
            journal = SqliteWorkflowJournal(Path(directory) / "journal.sqlite3")
            result = await self._execute(
                environment,
                executor,
                value,
                journal,
                activities,
            )

            self.assertEqual(result.status, WorkflowStatus.SUCCEEDED.value)
            execute_calls = [
                call
                for call in activities.calls
                if call.activity_name is ActivityName.EXECUTE
            ]
            self.assertEqual([call.attempt for call in execute_calls], [1, 2])
            self.assertEqual(
                {call.idempotency_key for call in execute_calls},
                {f"canary:{value.run_id}:execute_native_probe"},
            )
            events = journal.load(value.run_id)
            self.assertEqual(events[-1].event_type, WorkflowEventType.WORKFLOW_SUCCEEDED)
            self.assertEqual(
                [event.sequence for event in events], list(range(1, len(events) + 1))
            )
            for event in events:
                expected = uuid5(
                    UUID(value.run_id),
                    f"probe-workflow-event:{event.sequence}",
                )
                self.assertEqual(event.event_id, str(expected))

    async def _assert_target_failure_continues(
        self, environment: object, executor: ThreadPoolExecutor
    ) -> None:
        value = workflow_input()
        activities = ScriptedTemporalActivities()
        activities.scripts[ActivityName.PREFLIGHT] = [
            ActivityOutcome(
                ActivityOutcomeStatus.TARGET_FAILED,
                "evidence:preflight:target-down",
            )
        ]
        with TemporaryDirectory() as directory:
            journal = SqliteWorkflowJournal(Path(directory) / "journal.sqlite3")
            result = await self._execute(
                environment,
                executor,
                value,
                journal,
                activities,
            )

            self.assertEqual(result.status, WorkflowStatus.SUCCEEDED.value)
            self.assertTrue(result.target_failed_seen)
            names = [call.activity_name for call in activities.calls]
            self.assertNotIn(ActivityName.EXECUTE, names)
            self.assertIn(ActivityName.NORMALIZE, names)
            self.assertIn(ActivityName.PERSIST, names)

    async def _assert_query_while_activity_runs(
        self, environment: object, executor: ThreadPoolExecutor
    ) -> None:
        value = workflow_input()
        activities = ScriptedTemporalActivities()
        activities.block_activity = ActivityName.VALIDATE
        with TemporaryDirectory() as directory:
            journal = SqliteWorkflowJournal(Path(directory) / "journal.sqlite3")
            task_queue = f"probe-query-{uuid4()}"
            async with self._worker(
                environment,
                executor,
                task_queue,
                journal,
                activities,
            ):
                handle = await environment.client.start_workflow(
                    TemporalCanaryWorkflow.run,
                    value,
                    id=f"probe-{value.run_id}",
                    task_queue=task_queue,
                )
                await asyncio.wait_for(
                    asyncio.to_thread(activities.started.wait), timeout=5
                )
                state = await handle.query(TemporalCanaryWorkflow.current_state)
                self.assertIsNotNone(state)
                self.assertEqual(state.status, WorkflowStatus.RUNNING.value)
                self.assertEqual(state.phase, "VALIDATE")
                activities.release.set()
                result = await handle.result()
                self.assertEqual(result.status, WorkflowStatus.SUCCEEDED.value)

    async def _assert_cancellation_is_journaled(
        self, environment: object, executor: ThreadPoolExecutor
    ) -> None:
        value = replace(
            workflow_input(),
            activity_timeout_seconds=20,
            activity_heartbeat_seconds=0.25,
        )
        activities = ScriptedTemporalActivities()
        activities.block_activity = ActivityName.VALIDATE
        with TemporaryDirectory() as directory:
            journal = SqliteWorkflowJournal(Path(directory) / "journal.sqlite3")
            task_queue = f"probe-cancel-{uuid4()}"
            async with self._worker(
                environment,
                executor,
                task_queue,
                journal,
                activities,
            ):
                handle = await environment.client.start_workflow(
                    TemporalCanaryWorkflow.run,
                    value,
                    id=f"probe-{value.run_id}",
                    task_queue=task_queue,
                )
                await asyncio.wait_for(
                    asyncio.to_thread(activities.started.wait), timeout=5
                )
                await handle.cancel()
                with self.assertRaises(WorkflowFailureError):
                    await handle.result()

            events = journal.load(value.run_id)
            state = project_workflow_state(value, events)
            self.assertEqual(state.status, WorkflowStatus.CANCELLED)
            self.assertEqual(state.terminal_error_code, "WORKFLOW_CANCEL_REQUESTED")
            self.assertEqual(events[-1].event_type, WorkflowEventType.WORKFLOW_CANCELLED)

    async def _assert_native_probe_end_to_end(
        self, environment: object, executor: ThreadPoolExecutor
    ) -> None:
        server = create_server()
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        host, port = server.server_address
        try:
            with TemporaryDirectory() as directory:
                root = Path(directory)
                suite_root = root / "suites"
                suite_root.mkdir()
                evidence_root = root / "evidence"
                evidence_root.mkdir()
                source = (
                    Path(__file__).parents[1]
                    / "suites"
                    / "canary"
                    / "openai-compatible.json"
                )
                document = json.loads(source.read_text(encoding="utf-8"))
                document["spec"]["sampling"].update(
                    {"warmup": 0, "requests": 2}
                )
                suite_bytes = json.dumps(
                    document, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                suite_path = suite_root / "canary.json"
                suite_path.write_bytes(suite_bytes)
                endpoint_id = "native-endpoint-v1"
                value = CanaryWorkflowInput(
                    run_id=str(uuid4()),
                    endpoint_snapshot_id=endpoint_id,
                    suite_name="openai-compatible-canary",
                    suite_version="0.1.0",
                    suite_uri=suite_path.as_uri(),
                    suite_sha256=hashlib.sha256(suite_bytes).hexdigest(),
                    region="temporal-local-test",
                    activity_timeout_seconds=20,
                    activity_heartbeat_seconds=0.5,
                )
                database = root / "probe.sqlite3"
                journal = SqliteWorkflowJournal(database)
                runtime_store = SqliteCanaryRuntimeStore(database)
                activities = NativeCanaryActivities(
                    endpoint_resolver=MappingEndpointSnapshotResolver(
                        {
                            endpoint_id: EndpointSnapshot(
                                endpoint_snapshot_id=endpoint_id,
                                protocol="openai-chat",
                                base_url=f"http://{host}:{port}/v1",
                                model="mock-model",
                                credential_handle="secret://mock/test",
                            )
                        }
                    ),
                    suite_resolver=FileSuiteDocumentResolver(suite_root),
                    secret_resolver=MappingSecretResolver(
                        {"secret://mock/test": "test-key"}
                    ),
                    evidence_store=FileEvidenceStore(evidence_root),
                    runtime_store=runtime_store,
                )

                result = await self._execute(
                    environment,
                    executor,
                    value,
                    journal,
                    activities,
                )

                self.assertEqual(result.status, WorkflowStatus.SUCCEEDED.value)
                persisted = runtime_store.load_result(value.run_id)
                self.assertIsNotNone(persisted)
                self.assertEqual(persisted["status"], "PASS")
                serialized = json.dumps(persisted).lower()
                self.assertNotIn("test-key", serialized)
                self.assertNotIn("probe_ok", serialized)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    async def _execute(
        self,
        environment: object,
        executor: ThreadPoolExecutor,
        value: CanaryWorkflowInput,
        journal: SqliteWorkflowJournal,
        activities: object,
    ) -> TemporalCanaryResult:
        task_queue = f"probe-run-{uuid4()}"
        async with self._worker(
            environment,
            executor,
            task_queue,
            journal,
            activities,
        ):
            handle = await environment.client.start_workflow(
                TemporalCanaryWorkflow.run,
                value,
                id=f"probe-{value.run_id}",
                task_queue=task_queue,
            )
            result = await handle.result()
            history = await handle.fetch_history()
            replay = await Replayer(
                workflows=[TemporalCanaryWorkflow]
            ).replay_workflow(history)
            self.assertIsNone(replay.replay_failure)
            return result

    def _worker(
        self,
        environment: object,
        executor: ThreadPoolExecutor,
        task_queue: str,
        journal: SqliteWorkflowJournal,
        activities: object,
    ) -> Worker:
        journal_activities = TemporalJournalActivities(journal)
        step_activity = TemporalCanaryStepActivity(activities)
        return Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[TemporalCanaryWorkflow],
            activities=[
                journal_activities.load_history,
                journal_activities.append_event,
                step_activity.execute_step,
            ],
            activity_executor=executor,
            max_heartbeat_throttle_interval=timedelta(milliseconds=100),
            default_heartbeat_throttle_interval=timedelta(milliseconds=100),
        )


if __name__ == "__main__":
    unittest.main()
