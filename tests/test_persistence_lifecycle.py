from __future__ import annotations

import importlib
import sys
import threading
import time
import types
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).parents[1]


class PersistenceLifecycleTests(unittest.TestCase):
    def test_temporal_cancel_terminal_projects_postgres_run_as_cancelled(self) -> None:
        class Store(_LifecycleStore):
            def __init__(self) -> None:
                self.run = {
                    "run_id": "run-cancelled",
                    "state": "RUNNING",
                    "execution_backend": "temporal",
                    "workflow": {"status": "RUNNING"},
                }
                self.failed = False
                self.get_calls = 0

            def get_run(self, _run_id, **_kwargs):
                self.get_calls += 1
                if self.get_calls == 1:
                    raise RuntimeError("PostgreSQL temporarily unavailable")
                return dict(self.run)

            def cancel_run(self, _run_id):
                self.run["state"] = "CANCELLED"
                return dict(self.run)

            def fail_run(self, *_args, **_kwargs):
                self.failed = True

        store = Store()
        module = _import_control_service_without_optional_dependencies()
        service = module.ControlPlaneService(
            store=store,
            default_suite_path=PROJECT_ROOT
            / "suites/canary/openai-compatible.json",
            monitor_scheduler=False,
        )

        with self.assertRaisesRegex(RuntimeError, "temporarily unavailable"):
            service._complete_temporal_run(
                "run-cancelled", "CANCELLED", None, None
            )
        service._complete_temporal_run("run-cancelled", "CANCELLED", None, None)

        self.assertEqual(store.run["state"], "CANCELLED")
        self.assertFalse(store.failed)
        service.close()

    def test_close_is_bounded_when_admitted_operation_does_not_drain(self) -> None:
        operation_entered = threading.Event()
        release_operation = threading.Event()
        store_closed = threading.Event()

        class Store(_LifecycleStore):
            def close(self) -> None:
                store_closed.set()

        module = _import_control_service_without_optional_dependencies()
        service = module.ControlPlaneService(
            store=Store(),
            default_suite_path=PROJECT_ROOT
            / "suites/canary/openai-compatible.json",
            monitor_scheduler=False,
        )
        service._operation_drain_timeout_seconds = 0.01

        def blocked_operation() -> None:
            service._begin_operation()
            try:
                operation_entered.set()
                release_operation.wait(timeout=1)
            finally:
                service._end_operation()

        operation = threading.Thread(target=blocked_operation)
        operation.start()
        self.assertTrue(operation_entered.wait(timeout=1))

        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "shutdown deferred"):
            service.close()

        self.assertLess(time.monotonic() - started, 0.2)
        self.assertFalse(store_closed.is_set())
        release_operation.set()
        operation.join(timeout=1)
        self.assertTrue(store_closed.wait(timeout=1))

    def test_close_waits_for_admitted_operation_and_allows_its_dispatch(self) -> None:
        operation_entered = threading.Event()
        dispatch_submitted = threading.Event()
        background_ran = threading.Event()
        store_closed = threading.Event()

        class Store(_LifecycleStore):
            def close(self) -> None:
                store_closed.set()

        module = _import_control_service_without_optional_dependencies()
        service = module.ControlPlaneService(
            store=Store(),
            default_suite_path=PROJECT_ROOT
            / "suites/canary/openai-compatible.json",
            monitor_scheduler=False,
        )

        def admitted_operation() -> None:
            service._begin_operation()
            try:
                operation_entered.set()
                self.assertTrue(service._closing.wait(timeout=1))
                service._submit_background(background_ran.set)
                dispatch_submitted.set()
            finally:
                service._end_operation()

        operation = threading.Thread(target=admitted_operation)
        closer = threading.Thread(target=service.close)
        operation.start()
        self.assertTrue(operation_entered.wait(timeout=1))
        closer.start()

        self.assertTrue(dispatch_submitted.wait(timeout=1))
        operation.join(timeout=1)
        closer.join(timeout=1)
        self.assertFalse(operation.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertTrue(background_ran.is_set())
        self.assertTrue(store_closed.is_set())

    def test_close_defers_store_release_until_scheduler_really_stops(self) -> None:
        scheduler_waiting = threading.Event()
        scheduler_stopped = threading.Event()
        store_closed = threading.Event()

        class Store(_LifecycleStore):
            def close(self) -> None:
                store_closed.set()

        class DelayedScheduler:
            def close(self) -> bool:
                return False

            def wait_closed(self, timeout=None) -> bool:
                scheduler_waiting.set()
                return scheduler_stopped.wait(timeout)

        module = _import_control_service_without_optional_dependencies()
        service = module.ControlPlaneService(
            store=Store(),
            default_suite_path=PROJECT_ROOT
            / "suites/canary/openai-compatible.json",
            monitor_scheduler=False,
        )
        service.monitor_scheduler = DelayedScheduler()

        with self.assertRaisesRegex(RuntimeError, "close deferred"):
            service.close()

        self.assertTrue(scheduler_waiting.wait(timeout=1))
        self.assertFalse(store_closed.is_set())
        scheduler_stopped.set()
        self.assertTrue(store_closed.wait(timeout=1))

    def test_close_defers_store_release_until_temporal_callbacks_stop(self) -> None:
        temporal_stopped = threading.Event()
        store_closed = threading.Event()

        class Store(_LifecycleStore):
            def close(self) -> None:
                store_closed.set()

        class DelayedTemporal:
            available = True
            status = "READY"

            def recover(self, *_args, **_kwargs):
                return False

            def close(self) -> bool:
                return False

            def wait_closed(self, timeout=None) -> bool:
                return temporal_stopped.wait(timeout)

        module = _import_control_service_without_optional_dependencies()
        service = module.ControlPlaneService(
            store=Store(),
            default_suite_path=PROJECT_ROOT
            / "suites/canary/openai-compatible.json",
            temporal_launcher=DelayedTemporal(),
            monitor_scheduler=False,
        )

        with self.assertRaisesRegex(RuntimeError, "close deferred"):
            service.close()

        self.assertFalse(store_closed.is_set())
        temporal_stopped.set()
        self.assertTrue(store_closed.wait(timeout=1))

    def test_close_attempts_every_cleanup_stage_after_temporal_error(self) -> None:
        close_order = []

        class Store(_LifecycleStore):
            def close(self) -> None:
                close_order.append("store")

        class BrokenTemporal:
            available = True
            status = "READY"

            def recover(self, *_args, **_kwargs):
                return False

            def close(self) -> bool:
                close_order.append("temporal")
                raise RuntimeError("temporal close failed")

            def wait_closed(self, timeout=None) -> bool:
                return True

        class RecordingExecutor:
            def submit(self, function, *args):
                future = Future()
                try:
                    future.set_result(function(*args))
                except BaseException as exc:
                    future.set_exception(exc)
                return future

            def shutdown(self, **_kwargs) -> None:
                close_order.append("executor")

        module = _import_control_service_without_optional_dependencies()
        service = module.ControlPlaneService(
            store=Store(),
            default_suite_path=PROJECT_ROOT
            / "suites/canary/openai-compatible.json",
            executor=RecordingExecutor(),
            temporal_launcher=BrokenTemporal(),
            monitor_scheduler=False,
        )

        with self.assertRaisesRegex(RuntimeError, "temporal close failed"):
            service.close()

        self.assertEqual(close_order, ["temporal", "executor", "store"])

    def test_control_close_stops_cancel_retry_before_releasing_store(self) -> None:
        entered_retry = threading.Event()
        close_order = []

        class Store:
            def close(self) -> None:
                close_order.append("store")

            def list_temporal_runs_for_recovery(self):
                return []

            def list_monitor_policies(self):
                return []

            def claim_due_monitor_policies(self, **_kwargs):
                return []

            def complete_monitor_policy_dispatch(self, *_args, **_kwargs):
                return None

            def fail_monitor_policy_dispatch(self, *_args, **_kwargs):
                return None

            def prune_monitoring_data(self, **_kwargs):
                return {"samples": 0, "incidents": 0}

            def record_monitor_run(self, _run_id):
                return None

            def get_run(self, _run_id, **_kwargs):
                entered_retry.set()
                return {
                    "state": "RUNNING",
                    "cancel_requested_at": "2026-07-22T00:00:00+00:00",
                }

        class Temporal:
            available = True
            status = "READY"

            def recover(self, *_args, **_kwargs):
                return False

            def cancel(self, _run_id):
                return False

            def close(self) -> bool:
                close_order.append("temporal")
                return True

            def wait_closed(self, timeout=None) -> bool:
                return True

        module = _import_control_service_without_optional_dependencies()
        service = module.ControlPlaneService(
            store=Store(),
            default_suite_path=PROJECT_ROOT
            / "suites/canary/openai-compatible.json",
            temporal_launcher=Temporal(),
            monitor_scheduler=False,
        )
        service._submit_background(service._retry_temporal_cancel, "run-1")
        self.assertTrue(entered_retry.wait(timeout=1))

        started = time.monotonic()
        service.close()

        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(close_order, ["temporal", "store"])

    def test_postgres_pool_closes_when_initial_open_fails(self) -> None:
        pools = []

        class ConnectionPool:
            check_connection = object()

            def __init__(self, **_kwargs) -> None:
                self.closed = False
                pools.append(self)

            def open(self, **_kwargs) -> None:
                raise RuntimeError("database unavailable")

            def close(self) -> None:
                self.closed = True

        postgres_module = types.ModuleType("psycopg")
        postgres_module.Error = type("Error", (Exception,), {})
        postgres_module.errors = types.SimpleNamespace(
            SerializationFailure=type("SerializationFailure", (Exception,), {})
        )
        rows_module = types.ModuleType("psycopg.rows")
        rows_module.dict_row = object()
        types_module = types.ModuleType("psycopg.types")
        json_module = types.ModuleType("psycopg.types.json")
        json_module.Jsonb = lambda value: value
        pool_module = types.ModuleType("psycopg_pool")
        pool_module.ConnectionPool = ConnectionPool
        stubs = {
            "psycopg": postgres_module,
            "psycopg.rows": rows_module,
            "psycopg.types": types_module,
            "psycopg.types.json": json_module,
            "psycopg_pool": pool_module,
        }
        with patch.dict(sys.modules, stubs):
            module = _fresh_import("lexsond.storage.postgres")
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                module.PostgresPool("postgresql://db.invalid/probe")

        self.assertEqual(len(pools), 1)
        self.assertTrue(pools[0].closed)


class _LifecycleStore:
    def close(self) -> None:
        return None

    def list_temporal_runs_for_recovery(self):
        return []

    def list_monitor_policies(self):
        return []

    def claim_due_monitor_policies(self, **_kwargs):
        return []

    def complete_monitor_policy_dispatch(self, *_args, **_kwargs):
        return None

    def fail_monitor_policy_dispatch(self, *_args, **_kwargs):
        return None

    def prune_monitoring_data(self, **_kwargs):
        return {"samples": 0, "incidents": 0}

    def record_monitor_run(self, _run_id):
        return None


def _import_control_service_without_optional_dependencies():
    agent = types.ModuleType("lexsond.agent.service")

    class AgentCoordinator:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

    agent.AgentCoordinator = AgentCoordinator
    api_models = types.ModuleType("lexsond.web.api_models")
    for name in (
        "MonitorPolicyCreate",
        "MonitorPolicyPatch",
        "RunCreate",
        "SuiteCreate",
        "SuitePatch",
        "TargetCreate",
        "TargetPatch",
    ):
        setattr(api_models, name, type(name, (), {}))
    langchain_runtime = types.ModuleType("lexsond.web.langchain_runtime")
    langchain_runtime.invoke_native_probe = lambda *_args, **_kwargs: None
    with patch.dict(
        sys.modules,
        {
            "lexsond.agent.service": agent,
            "lexsond.web.api_models": api_models,
            "lexsond.web.langchain_runtime": langchain_runtime,
        },
    ):
        return _fresh_import("lexsond.web.control_service")


def _fresh_import(name: str):
    previous = sys.modules.pop(name, None)
    try:
        return importlib.import_module(name)
    finally:
        sys.modules.pop(name, None)
        if previous is not None:
            sys.modules[name] = previous


if __name__ == "__main__":
    unittest.main()
