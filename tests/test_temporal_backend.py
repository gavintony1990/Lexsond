from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

from lexsond.web.temporal_backend import (
    ConfiguredTemporalLauncher,
    TemporalDispatchUncertain,
    TemporalDispatchUnavailable,
    build_temporal_launch_artifacts,
    temporal_launcher_from_environment,
)


class TemporalBackendTests(unittest.TestCase):
    def test_close_does_not_close_pool_while_monitor_thread_is_live(self) -> None:
        class Pool:
            closed = False

            def close(self) -> None:
                self.closed = True

        class LiveThread:
            joined = False

            def join(self, timeout=None) -> None:
                self.joined = timeout == 2

        launcher = ConfiguredTemporalLauncher.__new__(ConfiguredTemporalLauncher)
        launcher._lock = threading.Lock()
        launcher._closing = False
        launcher._closing_event = threading.Event()
        launcher._pool_closed = False
        launcher._pool_close_in_progress = False
        launcher._stopped = threading.Event()
        launcher._inflight_starts = 0
        launcher._inflight_dispatches = 0
        launcher._pool = Pool()
        launcher._active = {}
        live_thread = LiveThread()
        launcher._threads = {"run": live_thread}

        self.assertFalse(launcher.close())

        self.assertTrue(live_thread.joined)
        self.assertFalse(launcher._pool.closed)
        launcher._threads = {}
        launcher._close_pool_when_idle()
        self.assertTrue(launcher._pool.closed)
        self.assertTrue(launcher.wait_closed(timeout=0))

    def test_close_keeps_pool_open_until_inflight_start_finishes(self) -> None:
        class Pool:
            closed = False

            def close(self) -> None:
                self.closed = True

        launcher = ConfiguredTemporalLauncher.__new__(ConfiguredTemporalLauncher)
        launcher._lock = threading.Lock()
        launcher._closing = False
        launcher._closing_event = threading.Event()
        launcher._pool_closed = False
        launcher._pool_close_in_progress = False
        launcher._stopped = threading.Event()
        launcher._inflight_starts = 0
        launcher._inflight_dispatches = 0
        launcher._pool = Pool()
        launcher._active = {}
        launcher._threads = {}

        launcher._begin_start()
        self.assertFalse(launcher.close())

        self.assertFalse(launcher._pool.closed)
        launcher._end_start()
        self.assertTrue(launcher._pool.closed)
        self.assertTrue(launcher.wait_closed(timeout=0))

    def test_close_keeps_pool_open_until_admitted_dispatch_finishes(self) -> None:
        class Pool:
            closed = False

            def close(self) -> None:
                self.closed = True

        launcher = ConfiguredTemporalLauncher.__new__(ConfiguredTemporalLauncher)
        launcher._lock = threading.Lock()
        launcher._closing = False
        launcher._closing_event = threading.Event()
        launcher._pool_closed = False
        launcher._pool_close_in_progress = False
        launcher._stopped = threading.Event()
        launcher._inflight_starts = 0
        launcher._inflight_dispatches = 0
        launcher._pool = Pool()
        launcher._active = {}
        launcher._threads = {}

        self.assertTrue(launcher._begin_dispatch())
        self.assertFalse(launcher.close())

        self.assertFalse(launcher._pool.closed)
        launcher._end_dispatch()
        self.assertTrue(launcher._pool.closed)
        self.assertTrue(launcher.wait_closed(timeout=0))

    def test_pool_close_failure_is_not_reported_as_stopped_and_can_retry(self) -> None:
        class Pool:
            calls = 0

            def close(self) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("pool close failed")

        launcher = ConfiguredTemporalLauncher.__new__(ConfiguredTemporalLauncher)
        launcher._lock = threading.Lock()
        launcher._closing = False
        launcher._closing_event = threading.Event()
        launcher._pool_closed = False
        launcher._pool_close_in_progress = False
        launcher._inflight_starts = 0
        launcher._inflight_dispatches = 0
        launcher._pool = Pool()
        launcher._active = {}
        launcher._threads = {}
        launcher._stopped = threading.Event()

        with self.assertRaisesRegex(RuntimeError, "pool close failed"):
            launcher.close()

        self.assertTrue(launcher.wait_closed(timeout=0.2))
        self.assertEqual(launcher._pool.calls, 2)

    def test_waiter_retries_async_final_pool_close_failure(self) -> None:
        class Pool:
            calls = 0

            def close(self) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("async pool close failed")

        launcher = ConfiguredTemporalLauncher.__new__(ConfiguredTemporalLauncher)
        launcher._lock = threading.Lock()
        launcher._closing = True
        launcher._closing_event = threading.Event()
        launcher._closing_event.set()
        launcher._pool_closed = False
        launcher._pool_close_in_progress = False
        launcher._inflight_starts = 0
        launcher._inflight_dispatches = 0
        launcher._pool = Pool()
        launcher._active = {}
        launcher._threads = {"run": object()}
        launcher._stopped = threading.Event()
        waiter_started = threading.Event()
        waiter_result = []

        def wait_for_close() -> None:
            waiter_started.set()
            waiter_result.append(launcher.wait_closed(timeout=0.5))

        waiter = threading.Thread(target=wait_for_close)
        waiter.start()
        self.assertTrue(waiter_started.wait(timeout=1))
        with launcher._lock:
            launcher._threads.clear()
        with self.assertRaisesRegex(RuntimeError, "async pool close failed"):
            launcher._close_pool_when_idle()

        waiter.join(timeout=1)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(waiter_result, [True])
        self.assertEqual(launcher._pool.calls, 2)

    def test_monitor_start_failure_removes_thread_registration(self) -> None:
        class BrokenThread:
            def __init__(self, **_kwargs) -> None:
                pass

            def start(self) -> None:
                raise RuntimeError("thread unavailable")

        launcher = ConfiguredTemporalLauncher.__new__(ConfiguredTemporalLauncher)
        launcher._lock = threading.Lock()
        launcher._closing = False
        launcher._inflight_dispatches = 0
        launcher._threads = {}
        workflow_input = types.SimpleNamespace(run_id="run-1")

        with patch(
            "lexsond.web.temporal_backend.threading.Thread",
            BrokenThread,
        ):
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                launcher._start_monitor(workflow_input, lambda *_: None, lambda *_: None)

        self.assertEqual(launcher._threads, {})

    def test_uncertain_start_retries_in_the_same_monitor_thread(self) -> None:
        launcher = ConfiguredTemporalLauncher.__new__(ConfiguredTemporalLauncher)
        launcher._lock = threading.Lock()
        launcher._closing = False
        launcher._closing_event = threading.Event()
        launcher._dispatch_retry_initial_seconds = 0.001
        launcher._active = {}
        launcher._cancel_requested = set()
        launcher._threads = {"run-recoverable": object()}
        workflow_input = types.SimpleNamespace(run_id="run-recoverable")
        attempts = []
        terminal = []

        async def execute(workflow, _on_events, on_terminal):
            attempts.append(workflow.run_id)
            if len(attempts) == 1:
                raise TemporalDispatchUncertain("uncertain")
            if len(attempts) == 2:
                raise TemporalDispatchUnavailable("connect unavailable")
            on_terminal(workflow.run_id, "SUCCEEDED", {}, None)

        launcher._execute = execute
        launcher._close_pool_when_idle = lambda: None
        launcher._thread_main(
            workflow_input,
            lambda *_: None,
            lambda *values: terminal.append(values),
        )

        self.assertEqual(
            attempts,
            ["run-recoverable", "run-recoverable", "run-recoverable"],
        )
        self.assertEqual(terminal[0][0:2], ("run-recoverable", "SUCCEEDED"))
        self.assertEqual(launcher._threads, {})

    def test_uncertain_start_preserves_cancel_intent_through_reconnect(self) -> None:
        launcher = ConfiguredTemporalLauncher.__new__(ConfiguredTemporalLauncher)
        launcher._lock = threading.Lock()
        launcher._closing = False
        launcher._closing_event = threading.Event()
        launcher._dispatch_retry_initial_seconds = 0.001
        launcher._active = {}
        launcher._cancel_requested = {"run-cancel"}
        launcher._threads = {"run-cancel": object()}
        workflow_input = types.SimpleNamespace(run_id="run-cancel")
        attempts = []
        terminal = []

        async def execute(workflow, _on_events, on_terminal):
            attempts.append(workflow.run_id)
            if len(attempts) == 1:
                raise TemporalDispatchUncertain("uncertain")
            if len(attempts) == 2:
                raise TemporalDispatchUnavailable("connect unavailable")
            self.assertIn(workflow.run_id, launcher._cancel_requested)
            on_terminal(workflow.run_id, "CANCELLED", None, None)

        launcher._execute = execute
        launcher._close_pool_when_idle = lambda: None
        launcher._thread_main(
            workflow_input,
            lambda *_: None,
            lambda *values: terminal.append(values),
        )

        self.assertEqual(len(attempts), 3)
        self.assertEqual(terminal[0][0:2], ("run-cancel", "CANCELLED"))

    def test_workflow_failure_retries_failed_terminal_projection(self) -> None:
        launcher = ConfiguredTemporalLauncher.__new__(ConfiguredTemporalLauncher)
        launcher._lock = threading.Lock()
        launcher._closing = False
        launcher._closing_event = threading.Event()
        launcher._dispatch_retry_initial_seconds = 0.001
        launcher._active = {}
        launcher._cancel_requested = set()
        launcher._threads = {"run-workflow-failed": object()}
        workflow_input = types.SimpleNamespace(run_id="run-workflow-failed")
        terminal_attempts = []

        async def execute(*_args):
            raise RuntimeError("authoritative workflow failure")

        def project_terminal(*values):
            terminal_attempts.append(values)
            if len(terminal_attempts) == 1:
                raise RuntimeError("PostgreSQL temporarily unavailable")

        launcher._execute = execute
        launcher._close_pool_when_idle = lambda: None
        launcher._thread_main(
            workflow_input,
            lambda *_: None,
            project_terminal,
        )

        self.assertEqual(
            terminal_attempts,
            [
                (
                    "run-workflow-failed",
                    "FAILED",
                    None,
                    "TEMPORAL_EXECUTION_ERROR",
                ),
                (
                    "run-workflow-failed",
                    "FAILED",
                    None,
                    "TEMPORAL_EXECUTION_ERROR",
                ),
            ],
        )

    def test_deterministic_dispatch_rejection_retries_failed_projection(self) -> None:
        launcher = ConfiguredTemporalLauncher.__new__(ConfiguredTemporalLauncher)
        launcher._lock = threading.Lock()
        launcher._closing = False
        launcher._closing_event = threading.Event()
        launcher._dispatch_retry_initial_seconds = 0.001
        launcher._inflight_dispatches = 0
        launcher._temporal_target = "temporal.invalid:7233"
        launcher._namespace = "default"
        launcher._task_queue = "test"
        launcher._connect_timeout_seconds = 1.0
        launcher._start_timeout_seconds = 1.0
        launcher._active = {}
        launcher._cancel_requested = set()
        launcher._threads = {"run-rejected": object()}
        launcher._close_pool_when_idle = lambda: None
        workflow_input = types.SimpleNamespace(run_id="run-rejected")
        terminal_attempts = []

        temporal_modules = _fake_temporal_modules(None)
        rpc_error = temporal_modules["temporalio.service"].RPCError
        invalid_argument = temporal_modules[
            "temporalio.service"
        ].RPCStatusCode.INVALID_ARGUMENT

        class ClientInstance:
            async def start_workflow(self, *_args, **_kwargs):
                raise rpc_error("invalid workflow input", status=invalid_argument)

        class Client:
            @staticmethod
            async def connect(*_args, **_kwargs):
                return ClientInstance()

        def project_terminal(*values):
            terminal_attempts.append(values)
            if len(terminal_attempts) == 1:
                raise RuntimeError("PostgreSQL temporarily unavailable")

        temporal_modules["temporalio.client"].Client = Client
        with patch.dict(sys.modules, temporal_modules):
            launcher._thread_main(
                workflow_input,
                lambda *_: None,
                project_terminal,
            )

        self.assertEqual(len(terminal_attempts), 2)
        self.assertTrue(
            all(values[1] == "FAILED" for values in terminal_attempts)
        )

    def test_shutdown_during_connect_does_not_start_workflow(self) -> None:
        launcher = ConfiguredTemporalLauncher.__new__(ConfiguredTemporalLauncher)
        launcher._lock = threading.Lock()
        launcher._closing = False
        launcher._temporal_target = "temporal.invalid:7233"
        launcher._namespace = "default"
        launcher._task_queue = "test"
        launcher._connect_timeout_seconds = 1.0

        class ClientInstance:
            starts = 0

            async def start_workflow(self, *_args, **_kwargs):
                self.starts += 1
                raise AssertionError("workflow started after shutdown")

        client = ClientInstance()

        class Client:
            @staticmethod
            async def connect(*_args, **_kwargs):
                launcher._closing = True
                return client

        temporal_modules = _fake_temporal_modules(Client)
        with patch.dict(sys.modules, temporal_modules):
            asyncio.run(
                launcher._execute(
                    types.SimpleNamespace(run_id="run-1"),
                    lambda *_: None,
                    lambda *_: None,
                )
            )

        self.assertEqual(client.starts, 0)

    def test_temporal_connection_attempt_is_bounded(self) -> None:
        launcher = ConfiguredTemporalLauncher.__new__(ConfiguredTemporalLauncher)
        launcher._lock = threading.Lock()
        launcher._closing = False
        launcher._temporal_target = "temporal.invalid:7233"
        launcher._namespace = "default"
        launcher._task_queue = "test"
        launcher._connect_timeout_seconds = 0.01

        class Client:
            @staticmethod
            async def connect(*_args, **_kwargs):
                await asyncio.Event().wait()

        started = time.monotonic()
        with patch.dict(sys.modules, _fake_temporal_modules(Client)):
            with self.assertRaises(TemporalDispatchUnavailable):
                asyncio.run(
                    launcher._execute(
                        types.SimpleNamespace(run_id="run-1"),
                        lambda *_: None,
                        lambda *_: None,
                    )
                )
        self.assertLess(time.monotonic() - started, 0.2)

    def test_temporal_workflow_start_attempt_is_bounded_and_recoverable(self) -> None:
        launcher = ConfiguredTemporalLauncher.__new__(ConfiguredTemporalLauncher)
        launcher._lock = threading.Lock()
        launcher._closing = False
        launcher._inflight_dispatches = 0
        launcher._temporal_target = "temporal.invalid:7233"
        launcher._namespace = "default"
        launcher._task_queue = "test"
        launcher._connect_timeout_seconds = 1.0
        launcher._start_timeout_seconds = 0.01

        class ClientInstance:
            async def start_workflow(self, *_args, **_kwargs):
                await asyncio.Event().wait()

        client = ClientInstance()

        class Client:
            @staticmethod
            async def connect(*_args, **_kwargs):
                return client

        started = time.monotonic()
        with patch.dict(sys.modules, _fake_temporal_modules(Client)):
            with self.assertRaises(TemporalDispatchUncertain):
                asyncio.run(
                    launcher._execute(
                        types.SimpleNamespace(run_id="run-recoverable"),
                        lambda *_: None,
                        lambda *_: None,
                    )
                )

        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(launcher._inflight_dispatches, 0)

    def test_temporal_workflow_start_transport_error_is_uncertain(self) -> None:
        launcher = ConfiguredTemporalLauncher.__new__(ConfiguredTemporalLauncher)
        launcher._lock = threading.Lock()
        launcher._closing = False
        launcher._inflight_dispatches = 0
        launcher._temporal_target = "temporal.invalid:7233"
        launcher._namespace = "default"
        launcher._task_queue = "test"
        launcher._connect_timeout_seconds = 1.0
        launcher._start_timeout_seconds = 1.0

        class ClientInstance:
            async def start_workflow(self, *_args, **_kwargs):
                raise ConnectionError("transport reset after send")

        client = ClientInstance()

        class Client:
            @staticmethod
            async def connect(*_args, **_kwargs):
                return client

        with patch.dict(sys.modules, _fake_temporal_modules(Client)):
            with self.assertRaises(TemporalDispatchUncertain):
                asyncio.run(
                    launcher._execute(
                        types.SimpleNamespace(run_id="run-uncertain"),
                        lambda *_: None,
                        lambda *_: None,
                    )
                )

        self.assertEqual(launcher._inflight_dispatches, 0)

    def test_result_transport_error_reattaches_and_reaches_terminal_state(self) -> None:
        launcher = ConfiguredTemporalLauncher.__new__(ConfiguredTemporalLauncher)
        launcher._lock = threading.Lock()
        launcher._closing = False
        launcher._closing_event = threading.Event()
        launcher._dispatch_retry_initial_seconds = 0.001
        launcher._inflight_dispatches = 0
        launcher._temporal_target = "temporal.invalid:7233"
        launcher._namespace = "default"
        launcher._task_queue = "test"
        launcher._connect_timeout_seconds = 1.0
        launcher._start_timeout_seconds = 1.0
        launcher._poll_seconds = 0.001
        launcher._active = {}
        launcher._cancel_requested = set()
        launcher._threads = {"run-result": object()}
        launcher._journal = types.SimpleNamespace(load=lambda _run_id: [])
        launcher._runtime = types.SimpleNamespace(
            load_result=lambda _run_id: {"status": "PASS"}
        )
        launcher._close_pool_when_idle = lambda: None
        workflow_input = types.SimpleNamespace(run_id="run-result")
        starts = []
        connections = []
        terminal = []

        class Handle:
            def __init__(self, fail: bool) -> None:
                self.fail = fail

            async def result(self):
                if self.fail:
                    raise ConnectionError("result long-poll disconnected")
                return types.SimpleNamespace(
                    status="SUCCEEDED",
                    terminal_error_code=None,
                )

            async def cancel(self):
                return None

        class ClientInstance:
            async def start_workflow(self, *_args, **kwargs):
                starts.append(kwargs["id"])
                return Handle(fail=len(starts) == 1)

        client = ClientInstance()

        class Client:
            @staticmethod
            async def connect(*_args, **_kwargs):
                connections.append(len(connections) + 1)
                if len(connections) == 2:
                    raise ConnectionError("Temporal temporarily unavailable")
                return client

        with patch.dict(sys.modules, _fake_temporal_modules(Client)):
            launcher._thread_main(
                workflow_input,
                lambda *_: None,
                lambda *values: terminal.append(values),
            )

        self.assertEqual(starts, ["probe-run-result", "probe-run-result"])
        self.assertEqual(connections, [1, 2, 3])
        self.assertEqual(terminal[0][0:2], ("run-result", "SUCCEEDED"))

    def test_cancel_before_monitor_attach_reports_authoritative_terminal(self) -> None:
        launcher = ConfiguredTemporalLauncher.__new__(ConfiguredTemporalLauncher)
        launcher._lock = threading.Lock()
        launcher._closing = False
        launcher._closing_event = threading.Event()
        launcher._dispatch_retry_initial_seconds = 0.001
        launcher._inflight_dispatches = 0
        launcher._temporal_target = "temporal.invalid:7233"
        launcher._namespace = "default"
        launcher._task_queue = "test"
        launcher._connect_timeout_seconds = 1.0
        launcher._start_timeout_seconds = 1.0
        launcher._poll_seconds = 0.001
        launcher._active = {}
        launcher._cancel_requested = {"run-cancel-before-attach"}
        launcher._threads = {"run-cancel-before-attach": object()}
        launcher._journal = types.SimpleNamespace(load=lambda _run_id: [])
        launcher._close_pool_when_idle = lambda: None
        workflow_input = types.SimpleNamespace(run_id="run-cancel-before-attach")
        cancel_calls = []
        terminal_attempts = []

        temporal_modules = _fake_temporal_modules(None)
        workflow_failure = temporal_modules[
            "temporalio.client"
        ].WorkflowFailureError
        cancelled_error = temporal_modules["temporalio.exceptions"].CancelledError

        class Handle:
            async def cancel(self):
                cancel_calls.append("cancel")
                return None

            async def result(self):
                raise workflow_failure(cause=cancelled_error("cancelled"))

        class ClientInstance:
            async def start_workflow(self, *_args, **_kwargs):
                return Handle()

        class Client:
            @staticmethod
            async def connect(*_args, **_kwargs):
                return ClientInstance()

        temporal_modules["temporalio.client"].Client = Client

        def project_terminal(*values):
            terminal_attempts.append(values)
            if len(terminal_attempts) == 1:
                raise RuntimeError("PostgreSQL temporarily unavailable")

        with patch.dict(sys.modules, temporal_modules):
            launcher._thread_main(
                workflow_input,
                lambda *_: None,
                project_terminal,
            )

        self.assertEqual(
            terminal_attempts,
            [
                ("run-cancel-before-attach", "CANCELLED", None, None),
                ("run-cancel-before-attach", "CANCELLED", None, None),
            ],
        )
        self.assertEqual(cancel_calls, ["cancel"])
        self.assertNotIn("run-cancel-before-attach", launcher._threads)
        self.assertNotIn("run-cancel-before-attach", launcher._cancel_requested)

    def test_launcher_factory_closes_pool_when_configuration_is_invalid(self) -> None:
        pools = []

        class Pool:
            def __init__(self, *_args, **_kwargs) -> None:
                self.closed = False
                pools.append(self)

            def close(self) -> None:
                self.closed = True

        postgres = types.ModuleType("lexsond.storage.postgres")
        postgres.PostgresPool = Pool
        environment = {
            "LEXSOND_POSTGRES_DSN": "postgresql://test.invalid/probe",
            "LEXSOND_TEMPORAL_TARGET": "temporal.invalid:7233",
            "LEXSOND_REGION": "",
        }
        with (
            patch.dict(os.environ, environment, clear=False),
            patch.dict(sys.modules, {"lexsond.storage.postgres": postgres}),
        ):
            with self.assertRaisesRegex(ValueError, "non-empty"):
                temporal_launcher_from_environment()

        self.assertEqual(len(pools), 1)
        self.assertTrue(pools[0].closed)

    def test_launcher_factory_uses_explicit_control_plane_dsn(self) -> None:
        pools = []

        class Pool:
            def __init__(self, conninfo, **_kwargs) -> None:
                self.conninfo = conninfo
                self.closed = False
                pools.append(self)

            def close(self) -> None:
                self.closed = True

        postgres = types.ModuleType("lexsond.storage.postgres")
        postgres.PostgresPool = Pool
        configured_launcher = object()
        environment = {
            "LEXSOND_POSTGRES_DSN": "postgresql://wrong.invalid/probe",
            "LEXSOND_TEMPORAL_TARGET": "temporal.invalid:7233",
            "LEXSOND_REGION": "test-region",
        }
        with (
            patch.dict(os.environ, environment, clear=False),
            patch.dict(sys.modules, {"lexsond.storage.postgres": postgres}),
            patch(
                "lexsond.web.temporal_backend.ConfiguredTemporalLauncher",
                return_value=configured_launcher,
            ),
        ):
            launcher = temporal_launcher_from_environment(
                postgres_dsn="postgresql://control.invalid/probe"
            )

        self.assertIs(launcher, configured_launcher)
        self.assertEqual(len(pools), 1)
        self.assertEqual(
            pools[0].conninfo,
            "postgresql://control.invalid/probe",
        )
        self.assertFalse(pools[0].closed)

    def test_launch_input_has_one_attempt_and_no_credential_reference(self) -> None:
        suite = {
            "apiVersion": "probe.ai/v1alpha1",
            "kind": "ProbeSuite",
            "metadata": {"name": "single-chat", "version": "1"},
            "spec": {
                "layer": "L1",
                "protocol": "openai-chat",
                "request": {
                    "prompt": "Reply with exactly: PROBE_OK",
                    "stream": True,
                    "max_output_tokens": 32,
                },
                "sampling": {
                    "warmup": 0,
                    "requests": 1,
                    "concurrency": 1,
                    "timeout_seconds": 10,
                    "max_cost_usd": 0.1,
                },
                "assertions": [
                    {"type": "http_status", "equals": 200},
                    {"type": "output_nonempty"},
                ],
            },
        }
        target = {
            "id": "00000000-0000-4000-8000-000000000010",
            "provider_id": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "credential_ref": "vault://ai/deepseek",
        }

        launch = build_temporal_launch_artifacts(
            run_id="00000000-0000-4000-8000-000000000020",
            target=target,
            model="deepseek-chat",
            suite_document=suite,
            region="cn-east",
        )

        self.assertEqual(launch.workflow_input.retry_policy.max_attempts, 1)
        encoded_input = json.dumps(launch.workflow_input.to_dict())
        encoded_configuration = json.dumps(launch.endpoint_configuration)
        for forbidden in (
            "credential_ref",
            "vault://",
            "authorization",
            "PROBE_OK",
        ):
            self.assertNotIn(forbidden, encoded_input)
            self.assertNotIn(forbidden, encoded_configuration)
        self.assertTrue(launch.workflow_input.suite_uri.startswith("https://"))

        rotated = build_temporal_launch_artifacts(
            run_id="00000000-0000-4000-8000-000000000021",
            target={**target, "credential_ref": "vault://ai/deepseek-rotated"},
            model="deepseek-chat",
            suite_document=suite,
            region="cn-east",
        )
        self.assertNotEqual(
            launch.workflow_input.endpoint_snapshot_id,
            rotated.workflow_input.endpoint_snapshot_id,
        )
        self.assertNotIn("vault://", rotated.workflow_input.endpoint_snapshot_id)


def _fake_temporal_modules(client_type):
    common = type(
        "Common",
        (),
        {
            "WorkflowIDReusePolicy": type(
                "Reuse", (), {"REJECT_DUPLICATE": object()}
            ),
            "WorkflowIDConflictPolicy": type("Conflict", (), {"FAIL": object()}),
        },
    )
    temporalio = types.ModuleType("temporalio")
    temporalio.common = common
    client = types.ModuleType("temporalio.client")
    client.Client = client_type

    class WorkflowFailureError(Exception):
        def __init__(self, *, cause) -> None:
            super().__init__("workflow failed")
            self.cause = cause

    client.WorkflowFailureError = WorkflowFailureError
    exceptions = types.ModuleType("temporalio.exceptions")
    exceptions.CancelledError = type("CancelledError", (Exception,), {})
    exceptions.WorkflowAlreadyStartedError = type(
        "WorkflowAlreadyStartedError", (Exception,), {}
    )
    service = types.ModuleType("temporalio.service")
    class RPCError(Exception):
        def __init__(self, message="RPC failed", *, status=None) -> None:
            super().__init__(message)
            self.status = status

    service.RPCError = RPCError
    service.RPCStatusCode = type(
        "RPCStatusCode",
        (),
        {
            name: object()
            for name in (
                "INVALID_ARGUMENT",
                "NOT_FOUND",
                "PERMISSION_DENIED",
                "FAILED_PRECONDITION",
                "UNIMPLEMENTED",
                "UNAUTHENTICATED",
            )
        },
    )
    workflow = types.ModuleType("lexsond.workflows.temporal_workflow")
    workflow.TemporalCanaryWorkflow = type(
        "TemporalCanaryWorkflow", (), {"run": object()}
    )
    return {
        "temporalio": temporalio,
        "temporalio.client": client,
        "temporalio.exceptions": exceptions,
        "temporalio.service": service,
        "lexsond.workflows.temporal_workflow": workflow,
    }


if __name__ == "__main__":
    unittest.main()
