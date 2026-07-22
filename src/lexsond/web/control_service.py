from __future__ import annotations

import hashlib
import threading
import time
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from copy import deepcopy
from concurrent.futures import Executor, ThreadPoolExecutor
from functools import wraps
from pathlib import Path
from queue import Queue
from typing import Any, Mapping, Protocol, Sequence
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import SecretStr

from ..agent.service import AgentCoordinator
from ..monitoring.scheduler import MonitorScheduler
from ..monitoring.challenge import ArithmeticChallenge, arithmetic_challenge
from ..probe import ProbeConfig, ProbeType, validate_api_key_value
from ..probe_components import (
    ComponentStepStatus,
    advance_component_run,
    begin_component_evidence,
    component_catalog,
    create_component_run,
    fail_component_run,
    finalize_component_run,
)
from ..providers import public_providers, resolve_provider_key
from ..storage.redaction import sanitized_result_for_persistence
from ..storage.runtime_contracts import canonical_json_bytes, validate_sanitized_result
from ..suite import ProbeSuite, compile_suite, load_suite_json, run_suite
from ..targets import fetch_model_catalog_entries
from ..workflows import WorkflowEvent, WorkflowEventType
from .api_models import (
    MonitorPolicyCreate,
    MonitorPolicyPatch,
    PartnerApplicationCreate,
    PartnerApplicationPatch,
    ProbeBatchCreate,
    RunCreate,
    SuiteCreate,
    SuitePatch,
    TargetCreate,
    TargetPatch,
)
from .control_contracts import (
    ControlPlaneConflict,
    ControlPlaneNotFound,
    _require_postgres_store_contract,
)
from .langchain_runtime import invoke_native_probe


class TemporalUnavailable(ControlPlaneConflict):
    pass


class TemporalLauncher(Protocol):
    @property
    def available(self) -> bool: ...

    @property
    def status(self) -> str: ...

    def start(
        self,
        *,
        run_id: str,
        target: Mapping[str, Any],
        model: str,
        suite_document: Mapping[str, Any],
        on_events: Any,
        on_terminal: Any,
    ) -> Any: ...

    def cancel(self, run_id: str) -> bool: ...

    def recover(
        self,
        run_id: str,
        *,
        on_events: Any,
        on_terminal: Any,
    ) -> bool: ...

    def close(self) -> bool: ...

    def wait_closed(self, timeout: float | None = None) -> bool: ...


class DisabledTemporalLauncher:
    available = False

    def __init__(self, status: str = "NOT_CONFIGURED") -> None:
        self.status = status

    def start(self, **_: Any) -> None:
        raise TemporalUnavailable("Temporal backend is not configured")

    def cancel(self, run_id: str) -> bool:
        del run_id
        raise TemporalUnavailable("Temporal backend is not configured")

    def recover(self, run_id: str, **_: Any) -> bool:
        del run_id
        return False

    def close(self) -> bool:
        return True

    def wait_closed(self, timeout: float | None = None) -> bool:
        del timeout
        return True


def _lifecycle_operation(method: Any) -> Any:
    @wraps(method)
    def guarded(self: "ControlPlaneService", *args: Any, **kwargs: Any) -> Any:
        self._begin_operation()
        try:
            return method(self, *args, **kwargs)
        finally:
            self._end_operation()

    return guarded


class ControlPlaneService:
    """Application service shared by FastAPI handlers and integration tests."""

    def __init__(
        self,
        *,
        store: Any,
        default_suite_path: str | Path,
        executor: Executor | None = None,
        temporal_launcher: TemporalLauncher | None = None,
        agent_model_factory: Any | None = None,
        monitor_scheduler: bool = True,
        monitor_sample_retention_days: int = 30,
        monitor_incident_retention_days: int = 365,
    ) -> None:
        _require_postgres_store_contract(store)
        self.store = store
        self.agent = AgentCoordinator(
            self.store,
            model_factory=agent_model_factory,
            credential_validator=self._validate_temporary_key_binding,
        )
        self.default_suite = load_suite_json(default_suite_path)
        self.executor = executor or ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="probe-control-run",
        )
        self.temporal = temporal_launcher or DisabledTemporalLauncher()
        self._cancel_signals: dict[str, threading.Event] = {}
        self._cancel_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._lifecycle_condition = threading.Condition(self._lifecycle_lock)
        self._operation_depth: ContextVar[int] = ContextVar(
            f"lexsond_operation_depth_{id(self)}", default=0
        )
        self._inflight_operations = 0
        self._background_futures: set[Any] = set()
        self._closing = threading.Event()
        self._closed = False
        self._close_in_progress = False
        self._shutdown_finalizer_started = False
        self._operation_drain_timeout_seconds = 2.0
        self._consumer_drain_timeout_seconds = 2.0
        try:
            self._recover_temporal_runs()
            self.monitor_scheduler = MonitorScheduler(
                self.store,
                self._dispatch_monitor_policy,
                enabled=monitor_scheduler,
                sample_retention_days=monitor_sample_retention_days,
                incident_retention_days=monitor_incident_retention_days,
            )
        except BaseException:
            self._closing.set()
            with suppress(Exception):
                self.temporal.close()
            self.executor.shutdown(wait=True, cancel_futures=True)
            raise

    def close(self) -> None:
        with self._lifecycle_condition:
            if self._closed or self._close_in_progress:
                return
            if self._operation_depth.get() > 0:
                raise RuntimeError("cannot close control plane from an active operation")
            self._close_in_progress = True
            self._closing.set()

        if not self._wait_for_operations(self._operation_drain_timeout_seconds):
            try:
                self._start_shutdown_finalizer()
            except BaseException:
                with self._lifecycle_lock:
                    self._close_in_progress = False
                raise
            raise RuntimeError(
                "active control operations did not drain; shutdown deferred"
            )

        errors, consumers_stopped = self._stop_consumers(
            timeout=self._consumer_drain_timeout_seconds
        )
        closed = False
        if consumers_stopped:
            try:
                self.store.close()
                closed = True
            except BaseException as exc:
                errors.append(exc)
        else:
            try:
                self._start_shutdown_finalizer()
            except BaseException as exc:
                errors.append(exc)
            if not errors:
                errors.append(
                    RuntimeError(
                        "control consumers did not stop; PostgreSQL store close deferred"
                    )
                )

        if closed:
            with self._lifecycle_lock:
                self._closed = True
                self._close_in_progress = False
        elif not self._shutdown_finalizer_started:
            with self._lifecycle_lock:
                self._close_in_progress = False
        if errors:
            raise errors[0]

    def _begin_operation(self) -> None:
        with self._lifecycle_condition:
            if self._closing.is_set():
                raise ControlPlaneConflict("control plane is closing")
            self._inflight_operations += 1
            self._operation_depth.set(self._operation_depth.get() + 1)

    def _end_operation(self) -> None:
        with self._lifecycle_condition:
            depth = self._operation_depth.get()
            if depth <= 0 or self._inflight_operations <= 0:
                raise RuntimeError("control plane operation lifecycle is corrupted")
            self._operation_depth.set(depth - 1)
            self._inflight_operations -= 1
            if self._inflight_operations == 0:
                self._lifecycle_condition.notify_all()

    @contextmanager
    def operation(self) -> Any:
        """Keep the PostgreSQL store alive for one complete external operation."""

        self._begin_operation()
        try:
            yield
        finally:
            self._end_operation()

    def _submit_background(self, function: Any, *args: Any) -> Any:
        with self._lifecycle_lock:
            admitted_operation = self._operation_depth.get() > 0
            if self._closing.is_set() and not admitted_operation:
                raise ControlPlaneConflict("control plane is closing")
            future = self.executor.submit(function, *args)
            self._background_futures.add(future)
        future.add_done_callback(self._background_finished)
        return future

    def _background_finished(self, future: Any) -> None:
        with self._lifecycle_condition:
            self._background_futures.discard(future)
            if not self._background_futures:
                self._lifecycle_condition.notify_all()

    def _wait_for_operations(self, timeout: float | None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lifecycle_condition:
            while self._inflight_operations:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._lifecycle_condition.wait(remaining)
            return True

    def _wait_for_background(self, timeout: float | None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lifecycle_condition:
            while self._background_futures:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._lifecycle_condition.wait(remaining)
            return True

    def _stop_consumers(
        self, *, timeout: float | None
    ) -> tuple[list[BaseException], bool]:
        errors: list[BaseException] = []
        for close in (self.monitor_scheduler.close, self.temporal.close):
            try:
                close()
            except BaseException as exc:
                errors.append(exc)
        try:
            self.executor.shutdown(wait=False, cancel_futures=False)
        except BaseException as exc:
            errors.append(exc)
        scheduler_stopped = self._wait_component_closed(
            self.monitor_scheduler, timeout=0
        )
        temporal_stopped = self._wait_component_closed(self.temporal, timeout=0)
        background_stopped = self._wait_for_background(timeout)
        return errors, scheduler_stopped and temporal_stopped and background_stopped

    @staticmethod
    def _wait_component_closed(component: Any, *, timeout: float | None) -> bool:
        try:
            return bool(component.wait_closed(timeout))
        except BaseException:
            return False

    def _start_shutdown_finalizer(self) -> None:
        with self._lifecycle_lock:
            if self._shutdown_finalizer_started:
                return
            self._shutdown_finalizer_started = True
        finalizer = threading.Thread(
            target=self._finish_deferred_shutdown,
            name="lexsond-postgres-close-finalizer",
            daemon=True,
        )
        try:
            finalizer.start()
        except BaseException:
            with self._lifecycle_lock:
                self._shutdown_finalizer_started = False
            raise

    def _finish_deferred_shutdown(self) -> None:
        closed = False
        try:
            self._wait_for_operations(None)
            self._stop_consumers(timeout=0)
            scheduler_stopped = self._wait_component_closed(
                self.monitor_scheduler, timeout=None
            )
            temporal_stopped = self._wait_component_closed(
                self.temporal, timeout=None
            )
            background_stopped = self._wait_for_background(None)
            if scheduler_stopped and temporal_stopped and background_stopped:
                self.store.close()
                closed = True
        finally:
            with self._lifecycle_lock:
                self._closed = closed
                self._close_in_progress = False
                self._shutdown_finalizer_started = False

    def _recover_temporal_runs(self) -> None:
        if not self.temporal.available:
            return
        runs = self.store.list_temporal_runs_for_recovery()
        for run in runs:
            if run["state"] != "RUNNING" or run["execution_backend"] != "temporal":
                continue
            with self._cancel_lock:
                self._cancel_signals.setdefault(run["run_id"], threading.Event())
            try:
                recovered = self.temporal.recover(
                    run["run_id"],
                    on_events=self._record_temporal_events,
                    on_terminal=self._complete_temporal_run,
                )
            except Exception:
                recovered = False
            if not recovered:
                self._mark_failed(run["run_id"], "TEMPORAL_DISPATCH_INCOMPLETE")
            elif run.get("cancel_requested_at") is not None:
                self._submit_background(self._retry_temporal_cancel, run["run_id"])

    def bootstrap(self, *, workspace_id: str | None = None) -> dict[str, Any]:
        store = self._workspace_store(workspace_id)
        runs = store.list_runs(limit=100)
        terminal = [run for run in runs if run["state"] != "RUNNING"]
        passed = [run for run in terminal if run["result_status"] == "PASS"]
        monitor_policies = store.list_monitor_policies()
        return {
            "product": {
                "name": "Lexsond",
                "english_name": "Lexsond · 码海测深",
                "version": "0.8.0",
            },
            "execution_backends": [
                {"id": "local", "available": True, "status": "READY"},
                {
                    "id": "temporal",
                    "available": self.temporal.available,
                    "status": self.temporal.status,
                    "supported_probe_types": ["chat"],
                    "supports_suites": True,
                },
            ],
            "defaults": {
                "target_kind": "local",
                "provider_id": "ollama",
                "base_url": "http://127.0.0.1:11434/v1",
                "probe_type": "chat",
                "execution_backend": "local",
                "stream": True,
                "timeout_seconds": 30,
            },
            "providers": public_providers(),
            "probe_components": component_catalog(),
            "stats": {
                "runs": len(runs),
                "running": sum(run["state"] == "RUNNING" for run in runs),
                "pass_rate": (
                    round(len(passed) / len(terminal) * 100, 1) if terminal else None
                ),
                "targets": len(store.list_targets()),
                "suites": len(store.list_suites()),
                "agent_sessions": len(store.list_agent_sessions()),
                "monitor_policies": len(monitor_policies),
            },
        }

    # Partner onboarding

    def create_partner_application(
        self,
        model: PartnerApplicationCreate,
        *,
        idempotency_key: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        try:
            normalized_idempotency = str(UUID(idempotency_key))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("Idempotency-Key must be a UUID") from exc
        value = model.model_dump(mode="json")
        value["idempotency_key"] = normalized_idempotency
        value["request_sha256"] = hashlib.sha256(
            canonical_json_bytes(model.model_dump(mode="json"))
        ).hexdigest()
        return self._workspace_store(workspace_id).create_partner_application(value)

    def update_partner_application(
        self,
        application_id: str,
        model: PartnerApplicationPatch,
        *,
        workspace_id: str,
    ) -> dict[str, Any]:
        value = model.model_dump(mode="json")
        version = int(value.pop("version"))
        return self._workspace_store(workspace_id).update_partner_application(
            application_id, value, expected_version=version
        )

    # Target management

    def _workspace_store(self, workspace_id: str | None) -> Any:
        if workspace_id is None:
            return self.store
        return self.store.for_workspace(workspace_id)

    def agent_for_workspace(self, workspace_id: str) -> AgentCoordinator:
        return AgentCoordinator(
            self.store.for_workspace(workspace_id),
            model_factory=self.agent.model_factory,
            credential_validator=self._validate_temporary_key_binding,
        )

    def create_target(
        self, model: TargetCreate, *, workspace_id: str | None = None
    ) -> dict[str, Any]:
        return self._workspace_store(workspace_id).create_target(model.model_dump())

    def update_target(
        self,
        target_id: str,
        patch: TargetPatch,
        *,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        store = self._workspace_store(workspace_id)
        current = store.get_target(target_id)
        fields = patch.model_fields_set - {"version"}
        changes = {field: getattr(patch, field) for field in fields}
        merged = {
            "name": changes.get("name", current["name"]),
            "target_kind": changes.get("target_kind", current["target_kind"]),
            "provider_id": changes.get("provider_id", current["provider_id"]),
            "base_url": changes.get("base_url", current["base_url"]),
            "default_model": changes.get("default_model", current["default_model"]),
            "credential_ref": changes.get("credential_ref", current["credential_ref"]),
        }
        validated = TargetCreate.model_validate(merged).model_dump()
        normalized_changes = {field: validated[field] for field in fields}
        return store.update_target(
            target_id,
            normalized_changes,
            expected_version=patch.version,
        )

    def target_catalog(
        self,
        target_id: str,
        api_key: str | None,
        *,
        workspace_id: str | None = None,
        credential_profile_id: str | None = None,
        credential_fingerprint: str | None = None,
        credential_version: int | None = None,
    ) -> dict[str, Any]:
        store = self._workspace_store(workspace_id)
        target = store.get_target(target_id)
        if target["target_kind"] == "cloud" and not api_key:
            raise ValueError("api_key is required for cloud targets")
        if api_key is not None:
            validate_api_key_value(api_key)
            self._validate_temporary_key_binding(target, api_key)
        entries = fetch_model_catalog_entries(
            target["base_url"],
            api_key=api_key,
            provider_id=target["provider_id"],
        )
        public_entries = [entry.to_public_dict() for entry in entries]
        snapshot = store.create_model_catalog_snapshot(
            {
                "target_id": target_id,
                "credential_profile_id": credential_profile_id,
                "credential_fingerprint": credential_fingerprint,
                "credential_version": credential_version,
                "target_version": target["version"],
                "target_base_url": target["base_url"],
                "target_kind": target["target_kind"],
                "protocol": "openai-compatible",
                "provider_id": target["provider_id"],
                "models": public_entries,
            }
        )
        return {
            "status": "CONNECTED",
            "target_id": target_id,
            "auth_mode": "bearer" if api_key else "none",
            "model_count": len(entries),
            "models": public_entries,
            "catalog_snapshot_id": snapshot["snapshot_id"],
            "catalog_expires_at": snapshot["expires_at"],
        }

    # Bounded multi-model batches

    @_lifecycle_operation
    def start_probe_batch(
        self,
        model: ProbeBatchCreate,
        *,
        workspace_id: str | None = None,
        idempotency_key: str,
        api_key_override: SecretStr | None = None,
    ) -> dict[str, Any]:
        normalized_idempotency = str(UUID(idempotency_key))
        store = self._workspace_store(workspace_id)
        target = store.get_target(str(model.target_id))
        snapshot = store.get_model_catalog_snapshot(str(model.catalog_snapshot_id))
        if snapshot["status"] != "FRESH":
            raise ControlPlaneConflict("model catalog snapshot is stale")
        if snapshot["target_id"] != str(model.target_id):
            raise ControlPlaneConflict("catalog snapshot belongs to another channel")
        if snapshot["target_version"] != target["version"]:
            raise ControlPlaneConflict("channel changed after model discovery")
        profile_id = (
            str(model.credential_profile_id)
            if model.credential_profile_id is not None
            else None
        )
        if snapshot["credential_profile_id"] != profile_id:
            if snapshot["credential_profile_id"] is not None or profile_id is not None:
                raise ControlPlaneConflict(
                    "batch credential does not match the catalog snapshot"
                )
        if model.mode != "catalog_only" and not model.confirm_unknown_cost:
            raise ValueError(
                "model pricing is unknown; confirm_unknown_cost is required"
            )
        if api_key_override is not None and model.api_key is not None:
            raise ValueError("execution credential override conflicts with api_key")
        execution_secret = api_key_override or model.api_key
        api_key = (
            execution_secret.get_secret_value()
            if execution_secret is not None
            else None
        )
        if api_key is not None:
            validate_api_key_value(api_key)
            self._validate_temporary_key_binding(target, api_key)
        if target["target_kind"] == "cloud" and model.mode != "catalog_only" and not api_key:
            raise ValueError("a credential is required for a cloud probe batch")
        effective_max_output_tokens = model.max_output_tokens
        effective_timeout_seconds = model.timeout_seconds
        if model.mode == "quality_suite":
            revision = store.get_suite_revision(str(model.suite_revision_id))
            suite = compile_suite(revision["document"])
            if suite.sampling.concurrency != 1:
                raise ValueError(
                    "quality_suite batch requires suite sampling concurrency 1"
                )
            effective_max_output_tokens = suite.request.max_output_tokens
            effective_timeout_seconds = suite.sampling.timeout_seconds
        durable = {
            "target_id": str(model.target_id),
            "credential_profile_id": profile_id,
            "catalog_snapshot_id": str(model.catalog_snapshot_id),
            "suite_revision_id": (
                str(model.suite_revision_id)
                if model.suite_revision_id is not None
                else None
            ),
            "mode": model.mode,
            "model_ids": model.model_ids,
            "max_concurrency": model.max_concurrency,
            "max_output_tokens": effective_max_output_tokens,
            "timeout_seconds": effective_timeout_seconds,
            "confirm_unknown_cost": model.confirm_unknown_cost,
        }
        request_sha256 = hashlib.sha256(canonical_json_bytes(durable)).hexdigest()
        replay = store.find_probe_batch_by_idempotency(
            normalized_idempotency, request_sha256
        )
        if replay is not None:
            return replay
        batch = store.create_probe_batch(
            {
                **durable,
                "batch_id": str(uuid4()),
                "idempotency_key": normalized_idempotency,
                "request_sha256": request_sha256,
            }
        )
        if model.mode == "catalog_only":
            return store.finalize_probe_batch(batch["batch_id"])
        else:
            self._submit_background(
                self._execute_probe_batch,
                batch["batch_id"],
                target["workspace_id"],
                api_key,
            )
        return batch

    def _execute_probe_batch(
        self,
        batch_id: str,
        workspace_id: str,
        api_key: str | None,
    ) -> None:
        store = self._workspace_store(workspace_id)
        active: dict[str, str] = {}
        while not self._closing.is_set():
            batch = store.get_probe_batch(batch_id)
            if batch["state"] != "RUNNING":
                return
            for item in batch["items"]:
                if item["state"] == "RUNNING" and item["run_id"]:
                    active[item["item_id"]] = item["run_id"]

            if batch["cancel_requested_at"] is not None:
                for item in batch["items"]:
                    if item["state"] == "PENDING":
                        store.finish_probe_batch_item(
                            batch_id,
                            item["item_id"],
                            state="CANCELLED",
                            failure_code="BATCH_CANCELLED",
                        )
                for run_id in tuple(active.values()):
                    try:
                        self.cancel_run(run_id, workspace_id=workspace_id)
                    except (ControlPlaneConflict, ControlPlaneNotFound):
                        pass
            else:
                pending = [
                    item for item in batch["items"] if item["state"] == "PENDING"
                ]
                while pending and len(active) < batch["max_concurrency"]:
                    item = pending.pop(0)
                    try:
                        child = RunCreate(
                            target_id=UUID(batch["target_id"]),
                            run_kind=(
                                "suite" if batch["mode"] == "quality_suite" else "component"
                            ),
                            probe_type=ProbeType.CHAT,
                            suite_revision_id=(
                                UUID(batch["suite_revision_id"])
                                if batch["suite_revision_id"] is not None
                                else None
                            ),
                            execution_backend="local",
                            model=item["model_id"],
                            stream=False,
                            timeout_seconds=batch["timeout_seconds"],
                            max_output_tokens=batch["max_output_tokens"],
                            credential_profile_id=(
                                UUID(batch["credential_profile_id"])
                                if batch["credential_profile_id"] is not None
                                else None
                            ),
                        )
                        child_idempotency = str(
                            uuid5(
                                NAMESPACE_URL,
                                f"lexsond:probe-batch:{batch_id}:{item['item_id']}",
                            )
                        )
                        run = self.start_run(
                            child,
                            workspace_id=workspace_id,
                            idempotency_key=child_idempotency,
                            api_key_override=(SecretStr(api_key) if api_key else None),
                        )
                        store.start_probe_batch_item(
                            batch_id, item["item_id"], run["run_id"]
                        )
                        active[item["item_id"]] = run["run_id"]
                    except Exception as exc:
                        store.finish_probe_batch_item(
                            batch_id,
                            item["item_id"],
                            state="FAILED",
                            failure_code=_safe_batch_dispatch_failure(exc),
                        )

            for item_id, run_id in tuple(active.items()):
                run = store.get_run(run_id, include_archived=True)
                if run["state"] == "RUNNING":
                    continue
                if run["state"] == "COMPLETED" and run["result_status"] != "FAIL":
                    state, failure_code = "COMPLETED", None
                elif run["state"] == "CANCELLED":
                    state, failure_code = "CANCELLED", "RUN_CANCELLED"
                else:
                    state = "FAILED"
                    failure_code = run.get("failure_code") or "TARGET_ASSERTION_FAILED"
                store.finish_probe_batch_item(
                    batch_id,
                    item_id,
                    state=state,
                    failure_code=failure_code,
                )
                active.pop(item_id, None)

            refreshed = store.get_probe_batch(batch_id)
            unfinished = any(
                item["state"] in {"PENDING", "RUNNING"}
                for item in refreshed["items"]
            )
            if not unfinished:
                store.finalize_probe_batch(batch_id)
                return
            time.sleep(0.05)

    @_lifecycle_operation
    def cancel_probe_batch(
        self, batch_id: str, *, workspace_id: str | None = None
    ) -> dict[str, Any]:
        return self._workspace_store(workspace_id).request_probe_batch_cancel(batch_id)

    # Suite management

    def create_suite(
        self, model: SuiteCreate, *, workspace_id: str | None = None
    ) -> dict[str, Any]:
        return self._workspace_store(workspace_id).create_suite(model.model_dump())

    def update_suite(
        self,
        suite_id: str,
        patch: SuitePatch,
        *,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        fields = patch.model_fields_set - {"version"}
        changes = {field: getattr(patch, field) for field in fields}
        return self._workspace_store(workspace_id).update_suite(
            suite_id,
            changes,
            expected_version=patch.version,
        )

    # Continuous monitoring

    def create_monitor_policy(
        self, model: MonitorPolicyCreate, *, workspace_id: str | None = None
    ) -> dict[str, Any]:
        store = self._workspace_store(workspace_id)
        value = self._validated_monitor_policy(model, store=store)
        policy = store.create_monitor_policy(value)
        self.monitor_scheduler.wake()
        return policy

    def update_monitor_policy(
        self,
        policy_id: str,
        patch: MonitorPolicyPatch,
        *,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        store = self._workspace_store(workspace_id)
        current = store.get_monitor_policy(policy_id)
        fields = patch.model_fields_set - {"version"}
        changes = {field: getattr(patch, field) for field in fields}
        merged = {
            "name": changes.get("name", current["name"]),
            "target_id": changes.get("target_id", current["target_id"]),
            "run_kind": changes.get("run_kind", current["run_kind"]),
            "probe_type": changes.get("probe_type", current["probe_type"]),
            "suite_revision_id": changes.get(
                "suite_revision_id", current["suite_revision_id"]
            ),
            "execution_backend": changes.get(
                "execution_backend", current["execution_backend"]
            ),
            "model": changes.get("model", current["model"]),
            "stream": changes.get("stream", current["stream"]),
            "timeout_seconds": changes.get(
                "timeout_seconds", current["timeout_seconds"]
            ),
            "interval_seconds": changes.get(
                "interval_seconds", current["interval_seconds"]
            ),
            "failure_threshold": changes.get(
                "failure_threshold", current["failure_threshold"]
            ),
            "recovery_threshold": changes.get(
                "recovery_threshold", current["recovery_threshold"]
            ),
            "enabled": changes.get("enabled", current["enabled"]),
        }
        validated = self._validated_monitor_policy(
            MonitorPolicyCreate.model_validate(merged), store=store
        )
        normalized = {field: validated[field] for field in fields}
        policy = store.update_monitor_policy(
            policy_id,
            normalized,
            expected_version=patch.version,
        )
        self.monitor_scheduler.wake()
        return policy

    def request_monitor_policy_run(
        self, policy_id: str, *, workspace_id: str | None = None
    ) -> dict[str, Any]:
        policy = self._workspace_store(workspace_id).request_monitor_policy_run(
            policy_id
        )
        self.monitor_scheduler.wake()
        return policy

    def _validated_monitor_policy(
        self, model: MonitorPolicyCreate, *, store: Any | None = None
    ) -> dict[str, Any]:
        repository = store or self.store
        target = repository.get_target(str(model.target_id))
        if model.execution_backend == "local" and target["target_kind"] == "cloud":
            raise ValueError(
                "recurring local execution cannot store a cloud API key; use Temporal credential_ref"
            )
        if model.execution_backend == "temporal":
            if not self.temporal.available:
                raise TemporalUnavailable("Temporal backend is not configured")
            if not target["credential_ref_configured"]:
                raise ValueError("Temporal monitor policy requires target credential_ref")
        suite = None
        if model.suite_revision_id is not None:
            revision = repository.get_suite_revision(str(model.suite_revision_id))
            suite = compile_suite(revision["document"])
        model_name = (model.model or target["default_model"]).strip()
        if not model_name:
            raise ValueError("model is required")
        value = model.model_dump(mode="json")
        value["target_id"] = target["id"]
        value["suite_revision_id"] = (
            str(model.suite_revision_id) if model.suite_revision_id is not None else None
        )
        value["probe_type"] = (model.probe_type or ProbeType.CHAT).value
        value["model"] = model_name
        if suite is not None:
            value["stream"] = suite.request.stream
            value["timeout_seconds"] = suite.sampling.timeout_seconds
        return value

    def _dispatch_monitor_policy(
        self, policy: Mapping[str, Any], idempotency_key: str
    ) -> str:
        model = RunCreate.model_validate(
            {
                "target_id": policy["target_id"],
                "run_kind": policy["run_kind"],
                "probe_type": policy["probe_type"],
                "suite_revision_id": policy["suite_revision_id"],
                "execution_backend": policy["execution_backend"],
                "model": policy["model"],
                "stream": policy["stream"],
                "timeout_seconds": policy["timeout_seconds"],
                "api_key": None,
            }
        )
        run = self.start_run(
            model,
            idempotency_key=idempotency_key,
            monitor_policy_id=policy["id"],
            workspace_id=policy["workspace_id"],
        )
        return run["run_id"]

    # Runs

    @_lifecycle_operation
    def start_run(
        self,
        model: RunCreate,
        *,
        workspace_id: str | None = None,
        idempotency_key: str | None = None,
        monitor_policy_id: str | None = None,
        api_key_override: SecretStr | None = None,
    ) -> dict[str, Any]:
        store = self._workspace_store(workspace_id)
        target = store.get_target(str(model.target_id))
        if api_key_override is not None and model.api_key is not None:
            raise ValueError("execution credential override conflicts with api_key")
        execution_secret = api_key_override or model.api_key
        api_key = (
            execution_secret.get_secret_value()
            if execution_secret is not None
            else None
        )
        if api_key is not None:
            validate_api_key_value(api_key)
            self._validate_temporary_key_binding(target, api_key)
        if target["target_kind"] == "cloud" and model.execution_backend == "local" and not api_key:
            raise ValueError("api_key is required for a local execution against a cloud target")
        if model.execution_backend == "temporal":
            if not self.temporal.available:
                raise TemporalUnavailable("Temporal backend is not configured")
            if not target["credential_ref_configured"]:
                raise ValueError("Temporal execution requires target credential_ref")

        probe_type = model.probe_type or ProbeType.CHAT
        suite: ProbeSuite | None = None
        suite_document: Mapping[str, Any] | None = None
        suite_revision_id: str | None = None
        if model.run_kind == "suite":
            revision = store.get_suite_revision(str(model.suite_revision_id))
            suite_revision_id = revision["id"]
            suite_document = revision["document"]
            suite = compile_suite(suite_document)
            probe_type = ProbeType.CHAT

        model_name = (model.model or target["default_model"]).strip()
        if not model_name:
            raise ValueError("model is required")
        _reject_secret_in_durable_run_fields(
            api_key,
            base_url=target["base_url"],
            model=model_name,
            provider_id=target.get("provider_id"),
        )
        if model.execution_backend == "temporal" or monitor_policy_id is not None:
            audio_voice, binding_source = None, "MANUAL_CONFIRMATION"
        else:
            audio_voice, binding_source = self._preflight_binding(
                target,
                api_key=api_key,
                model=model_name,
                probe_type=probe_type,
            )
        _reject_secret_in_durable_run_fields(api_key, audio_voice=audio_voice)
        run_id = str(uuid4())
        challenge = (
            arithmetic_challenge(idempotency_key or run_id)
            if monitor_policy_id is not None
            and model.run_kind == "component"
            and probe_type == ProbeType.CHAT
            else None
        )
        run_mode = "canary" if model.run_kind == "suite" else "single"
        workflow = (
            _create_temporal_workflow(occurred_at=_now())
            if model.execution_backend == "temporal"
            else create_component_run(
                probe_type.value,
                run_mode=run_mode,
                occurred_at=_now(),
                binding_source=binding_source,
            )
        )
        metadata = {
            "workspace_id": target["workspace_id"],
            "target_id": target["id"],
            "suite_revision_id": suite_revision_id,
            "monitor_policy_id": monitor_policy_id,
            "run_kind": model.run_kind,
            "execution_backend": model.execution_backend,
            "base_url": target["base_url"],
            "model": model_name,
            "target_kind": target["target_kind"],
            "provider_id": target["provider_id"],
            "run_mode": run_mode,
            "probe_type": probe_type.value,
            "stream": suite.request.stream if suite is not None else model.stream,
            "timeout_seconds": (
                suite.sampling.timeout_seconds if suite is not None else model.timeout_seconds
            ),
            "max_output_tokens": (
                suite.request.max_output_tokens
                if suite is not None
                else model.max_output_tokens
            ),
            "audio_voice": audio_voice,
            # A profile UUID is non-secret and binds idempotency to the selected
            # credential without persisting a locator or the resolved value.
            "credential_profile_id": (
                str(model.credential_profile_id)
                if model.credential_profile_id is not None
                else None
            ),
        }
        request_sha256 = hashlib.sha256(canonical_json_bytes(metadata)).hexdigest()
        created_run = store.create_run(
            run_id,
            metadata,
            workflow,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256 if idempotency_key is not None else None,
        )
        if created_run["run_id"] != run_id:
            return created_run
        signal = threading.Event()
        with self._cancel_lock:
            self._cancel_signals[run_id] = signal
        if model.execution_backend == "temporal":
            temporal_suite = suite_document or _single_chat_suite_document(
                model_name=model_name,
                stream=model.stream,
                timeout_seconds=model.timeout_seconds,
                challenge=challenge,
            )
            try:
                self.temporal.start(
                    run_id=run_id,
                    target=target,
                    model=model_name,
                    suite_document=temporal_suite,
                    on_events=self._record_temporal_events,
                    on_terminal=self._complete_temporal_run,
                )
            except Exception:
                self._mark_failed(run_id, "TEMPORAL_START_ERROR")
                raise
        else:
            try:
                self._submit_background(
                    self._execute_local,
                    run_id,
                    api_key,
                    metadata,
                    suite,
                    signal,
                    challenge,
                )
            except Exception:
                self._mark_failed(run_id, "LOCAL_DISPATCH_ERROR")
                raise
        return store.get_run(run_id)

    @_lifecycle_operation
    def cancel_run(
        self, run_id: str, *, workspace_id: str | None = None
    ) -> dict[str, Any]:
        store = self._workspace_store(workspace_id)
        run = store.get_run(run_id)
        with self._cancel_lock:
            signal = self._cancel_signals.get(run_id)
            if signal is not None:
                signal.set()
        if run["execution_backend"] != "temporal":
            cancelled = store.cancel_run(run_id)
            self._project_monitor_run(run_id)
            return cancelled
        store.request_cancel_run(run_id)
        try:
            dispatched = self.temporal.cancel(run_id)
        except Exception:
            dispatched = False
        if dispatched:
            return self._confirm_temporal_cancel(run_id)
        self._submit_background(self._retry_temporal_cancel, run_id)
        return store.get_run(run_id, include_archived=True)

    def _retry_temporal_cancel(self, run_id: str) -> None:
        delay = 0.25
        while not self._closing.is_set():
            try:
                run = self.store.get_run_system(run_id, include_archived=True)
            except Exception:
                return
            if run["state"] != "RUNNING" or run.get("cancel_requested_at") is None:
                return
            try:
                if self.temporal.cancel(run_id):
                    self._confirm_temporal_cancel(run_id)
                    return
            except Exception:
                pass
            if self._closing.wait(delay):
                return
            delay = min(delay * 2, 5.0)

    def _confirm_temporal_cancel(self, run_id: str) -> dict[str, Any]:
        try:
            cancelled = self.store.cancel_run_system(run_id)
            self._project_monitor_run(run_id)
        except ControlPlaneConflict:
            return self.store.get_run_system(run_id, include_archived=True)
        finally:
            with self._cancel_lock:
                self._cancel_signals.pop(run_id, None)
        return cancelled

    def _execute_local(
        self,
        run_id: str,
        api_key: str | None,
        metadata: Mapping[str, Any],
        suite: ProbeSuite | None,
        cancel_signal: threading.Event,
        challenge: ArithmeticChallenge | None = None,
    ) -> None:
        evidence_started = False
        try:
            if suite is not None:
                self._advance(run_id, "fixture_prepare", ComponentStepStatus.RUNNING)
                self._advance(run_id, "fixture_prepare", ComponentStepStatus.PASS)
                self._advance(run_id, "request_dispatch", ComponentStepStatus.RUNNING)
                result = run_suite(
                    suite,
                    base_url=metadata["base_url"],
                    api_key=api_key,
                    model=metadata["model"],
                    cancel_signal=cancel_signal,
                    probe_runner=lambda config: invoke_native_probe(config),
                )
                self._advance(run_id, "request_dispatch", ComponentStepStatus.PASS)
            else:
                recorder = _ControlProgressRecorder(self, run_id)
                try:
                    result = invoke_native_probe(
                        ProbeConfig(
                            base_url=metadata["base_url"],
                            api_key=api_key,
                            model=metadata["model"],
                            timeout_seconds=metadata["timeout_seconds"],
                            max_output_tokens=metadata["max_output_tokens"],
                            stream=metadata["stream"],
                            probe_type=ProbeType(metadata["probe_type"]),
                            provider_id=metadata["provider_id"],
                            audio_voice=metadata.get("audio_voice"),
                            prompt=(
                                challenge.prompt
                                if challenge is not None
                                else "Reply with exactly: PROBE_OK"
                            ),
                            expected_text=(
                                challenge.expected_text
                                if challenge is not None
                                else None
                            ),
                        ),
                        progress=recorder.emit,
                    )
                finally:
                    recorder.close()
            result.run_id = run_id
            if cancel_signal.is_set():
                return
            current = self.store.get_run_system(run_id)
            workflow = begin_component_evidence(
                current["workflow"],
                result=_component_result_view(result),
                occurred_at=_now(),
            )
            self.store.update_run_workflow(
                run_id,
                workflow,
                event_type="STEP_STARTED",
                phase="evidence_seal",
                status="RUNNING",
            )
            evidence_started = True
            sanitized = sanitized_result_for_persistence(
                result,
                sensitive_values=(api_key,) if api_key is not None else (),
            )
            final_workflow = finalize_component_run(
                workflow,
                result=_component_result_view(result),
                occurred_at=_now(),
            )
            self.store.complete_run(run_id, sanitized, final_workflow)
            self._project_monitor_run(run_id)
        except Exception:
            try:
                current = self.store.get_run_system(run_id, include_archived=True)
            except Exception:
                return
            if current["state"] == "CANCELLED":
                return
            self._mark_failed(
                run_id,
                "EVIDENCE_PERSISTENCE_ERROR" if evidence_started else "EXECUTION_ERROR",
            )
        finally:
            with self._cancel_lock:
                self._cancel_signals.pop(run_id, None)

    def _mark_failed(self, run_id: str, code: str) -> None:
        current = self.store.get_run_system(run_id, include_archived=True)
        if current["execution_backend"] == "temporal":
            workflow = _finalize_temporal_workflow(
                current["workflow"], status="FAIL", failure_code=code
            )
            self.store.fail_run(run_id, code, workflow)
            self._project_monitor_run(run_id)
            return
        workflow = fail_component_run(
            current["workflow"], failure_code=code, occurred_at=_now()
        )
        self.store.fail_run(run_id, code, workflow)
        self._project_monitor_run(run_id)

    def _record_temporal_events(
        self, run_id: str, events: Sequence[WorkflowEvent]
    ) -> None:
        for event in events:
            current = self.store.get_run_system(run_id, include_archived=True)
            if current["state"] != "RUNNING":
                return
            workflow = _apply_temporal_event(current["workflow"], event)
            status = _temporal_event_status(event)
            self.store.update_run_workflow(
                run_id,
                workflow,
                event_type=f"TEMPORAL_{event.event_type.value}",
                phase=event.phase.value.lower(),
                status=status,
                source_event_id=event.event_id,
            )

    def _complete_temporal_run(
        self,
        run_id: str,
        state: str,
        result: Mapping[str, Any] | None,
        failure_code: str | None,
    ) -> None:
        try:
            try:
                current = self.store.get_run_system(run_id, include_archived=True)
            except ControlPlaneNotFound:
                return
            if current["state"] != "RUNNING":
                return
            if state == "CANCELLED":
                self._confirm_temporal_cancel(run_id)
                return
            if state == "SUCCEEDED" and result is not None:
                try:
                    validate_sanitized_result(run_id, result)
                except (TypeError, ValueError):
                    self._mark_failed(run_id, "TEMPORAL_RESULT_VALIDATION_ERROR")
                    return
                workflow = _finalize_temporal_workflow(
                    current["workflow"], status=str(result.get("status", "UNKNOWN"))
                )
                self.store.complete_run(run_id, result, workflow)
                self._project_monitor_run(run_id)
                return
            self._mark_failed(run_id, failure_code or "TEMPORAL_WORKFLOW_FAILED")
        finally:
            with self._cancel_lock:
                self._cancel_signals.pop(run_id, None)

    def _project_monitor_run(self, run_id: str) -> None:
        try:
            self.store.record_monitor_run(run_id)
        except Exception:
            try:
                self.store.append_run_event(
                    run_id,
                    event_type="MONITOR_PROJECTION_FAILED",
                    phase="monitoring",
                    status="FAIL",
                )
            except Exception:
                pass

    def _advance(
        self, run_id: str, step_id: str, status: ComponentStepStatus
    ) -> None:
        current = self.store.get_run_system(run_id)
        workflow = advance_component_run(
            current["workflow"], step_id, status, occurred_at=_now()
        )
        self.store.update_run_workflow(
            run_id,
            workflow,
            event_type=("STEP_STARTED" if status is ComponentStepStatus.RUNNING else "STEP_COMPLETED"),
            phase=step_id,
            status=status.value,
        )

    @staticmethod
    def _preflight_binding(
        target: Mapping[str, Any],
        *,
        api_key: str | None,
        model: str,
        probe_type: ProbeType,
    ) -> tuple[str | None, str]:
        entries = fetch_model_catalog_entries(
            target["base_url"],
            api_key=api_key,
            provider_id=target["provider_id"],
        )
        selected = next((entry for entry in entries if entry.model_id == model), None)
        if (
            selected is not None
            and selected.capability_source == "PROVIDER_METADATA"
            and probe_type.value not in selected.probe_types
        ):
            raise ValueError("probe_type conflicts with declared model capabilities")
        binding_source = (
            "PROVIDER_METADATA"
            if selected is not None and selected.capability_source == "PROVIDER_METADATA"
            else "MANUAL_CONFIRMATION"
        )
        voice = None
        if probe_type is ProbeType.AUDIO_SPEECH:
            if selected is not None and selected.supported_voices:
                voice = selected.supported_voices[0]
            if target["provider_id"] == "openrouter" and voice is None:
                raise ValueError("OpenRouter speech probe requires a declared supported voice")
        return voice, binding_source

    @staticmethod
    def detect_provider(api_key: str, provider_id: str | None) -> dict[str, Any]:
        return resolve_provider_key(api_key, provider_id).to_dict()

    @staticmethod
    def _validate_temporary_key_binding(
        target: Mapping[str, Any], api_key: str
    ) -> None:
        if target.get("target_kind") != "cloud":
            return
        provider_id = target.get("provider_id")
        if not provider_id:
            return
        detection = resolve_provider_key(api_key, provider_id)
        if detection.status == "MISMATCH":
            raise ValueError("api_key does not match the selected provider")


class _ControlProgressRecorder:
    def __init__(self, service: ControlPlaneService, run_id: str) -> None:
        self._service = service
        self._run_id = run_id
        self._queue: Queue[tuple[str, ComponentStepStatus] | None] = Queue()
        self._closed = False
        self._thread = threading.Thread(
            target=self._drain,
            name=f"probe-control-progress-{run_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def emit(self, step_id: str, status: ComponentStepStatus) -> None:
        if not self._closed:
            self._queue.put((step_id, status))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        self._thread.join()

    def _drain(self) -> None:
        failed = False
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                if not failed:
                    self._service._advance(self._run_id, item[0], item[1])
            except Exception:
                failed = True
            finally:
                self._queue.task_done()


def _component_result_view(result: Any) -> dict[str, Any]:
    return {
        "status": result.status.value,
        "reason_codes": list(result.reason_codes),
        "dimension_scores": [
            {"dimension": score.dimension.value, "status": score.status.value}
            for score in result.dimension_scores
        ],
        "measurements": [
            {
                "error_class": (
                    measurement.error_class.value
                    if measurement.error_class is not None
                    else None
                )
            }
            for measurement in result.measurements
        ],
    }


def _single_chat_suite_document(
    *,
    model_name: str,
    stream: bool,
    timeout_seconds: float,
    challenge: ArithmeticChallenge | None = None,
) -> dict[str, Any]:
    del model_name
    return {
        "apiVersion": "probe.ai/v1alpha1",
        "kind": "ProbeSuite",
        "metadata": {"name": "system-single-chat", "version": "1"},
        "spec": {
            "layer": "L1",
            "protocol": "openai-chat",
            "request": {
                "prompt": (
                    challenge.prompt
                    if challenge is not None
                    else "Reply with exactly: PROBE_OK"
                ),
                "stream": stream,
                "max_output_tokens": 64,
            },
            "sampling": {
                "warmup": 0,
                "requests": 1,
                "concurrency": 1,
                "timeout_seconds": timeout_seconds,
                "max_cost_usd": 0.1,
            },
            "assertions": [
                {"type": "http_status", "equals": 200},
                {"type": "output_nonempty"},
                *(
                    [{"type": "exact_text", "equals": challenge.expected_text}]
                    if challenge is not None
                    else []
                ),
            ],
        },
    }


_TEMPORAL_STEPS = (
    ("validate_config", "VALIDATE", "校验不可变配置"),
    ("preflight_endpoint", "PREFLIGHT", "预检目标与凭据引用"),
    ("execute_native_probe", "EXECUTE", "执行原生计时探针"),
    ("normalize_measurements", "NORMALIZE", "标准化测量结果"),
    ("compute_dimension_scores", "SCORE", "计算质量维度"),
    ("persist_result", "PERSIST", "持久化脱敏结果"),
    ("compare_slo", "COMPARE", "对比质量阈值"),
    ("notify_state_change", "NOTIFY", "发布状态变化"),
)


def _create_temporal_workflow(*, occurred_at: str) -> dict[str, Any]:
    return {
        "schema_version": "probe.ai/temporal-run/v1alpha1",
        "component_id": "chat",
        "component_label": "Temporal 聊天金丝雀",
        "icon": "TMP",
        "scenario": "PostgreSQL 快照 → Temporal 八活动工作流",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "run_mode": "canary",
        "binding_source": "WORKER_PREFLIGHT",
        "status": "RUNNING",
        "current_step_id": None,
        "started_at": occurred_at,
        "finished_at": None,
        "failure_code": None,
        "steps": [
            {
                "id": step_id,
                "stage": stage,
                "label": label,
                "description": "Temporal Activity；事件来自持久化工作流日志。",
                "status": "PENDING",
                "started_at": None,
                "finished_at": None,
                "facts": [],
            }
            for step_id, stage, label in _TEMPORAL_STEPS
        ],
    }


def _apply_temporal_event(
    workflow: Mapping[str, Any], event: WorkflowEvent
) -> dict[str, Any]:
    value = deepcopy(dict(workflow))
    activity_id = event.activity_name.value if event.activity_name is not None else None
    step = next(
        (candidate for candidate in value.get("steps", ()) if candidate["id"] == activity_id),
        None,
    )
    if event.event_type is WorkflowEventType.ACTIVITY_STARTED and step is not None:
        step["status"] = "RUNNING"
        step["started_at"] = event.occurred_at
        step["facts"] = [f"ATTEMPT {event.attempt}"]
        value["current_step_id"] = activity_id
    elif event.event_type is WorkflowEventType.ACTIVITY_COMPLETED and step is not None:
        step["status"] = "PASS"
        step["finished_at"] = event.occurred_at
        step["facts"] = [event.outcome_status.value] if event.outcome_status else []
        value["current_step_id"] = None
    elif event.event_type is WorkflowEventType.ACTIVITY_ATTEMPT_FAILED and step is not None:
        step["status"] = "PENDING" if event.retry_scheduled else "FAIL"
        step["finished_at"] = event.occurred_at
        step["facts"] = [event.error_code] if event.error_code else []
        value["current_step_id"] = None
    return value


def _finalize_temporal_workflow(
    workflow: Mapping[str, Any], *, status: str, failure_code: str | None = None
) -> dict[str, Any]:
    value = deepcopy(dict(workflow))
    successful = status in {"PASS", "WARN", "SUCCEEDED"}
    value["status"] = status if successful else "FAIL"
    value["current_step_id"] = None
    value["finished_at"] = _now()
    value["failure_code"] = failure_code
    for step in value.get("steps", ()):
        if step["status"] == "RUNNING" and not successful:
            step["status"] = "FAIL"
            step["finished_at"] = value["finished_at"]
        elif step["status"] == "PENDING":
            step["status"] = "PASS" if successful else "SKIPPED"
            if successful:
                step["started_at"] = value["finished_at"]
                step["finished_at"] = value["finished_at"]
    return value


def _temporal_event_status(event: WorkflowEvent) -> str:
    if event.event_type is WorkflowEventType.ACTIVITY_STARTED:
        return "RUNNING"
    if event.event_type is WorkflowEventType.ACTIVITY_COMPLETED:
        return "PASS"
    if event.event_type is WorkflowEventType.ACTIVITY_ATTEMPT_FAILED:
        return "RETRYING" if event.retry_scheduled else "FAIL"
    if event.event_type is WorkflowEventType.WORKFLOW_SUCCEEDED:
        return "PASS"
    if event.event_type is WorkflowEventType.WORKFLOW_CANCELLED:
        return "CANCELLED"
    if event.event_type in {
        WorkflowEventType.WORKFLOW_FAILED,
        WorkflowEventType.WORKFLOW_REJECTED,
    }:
        return "FAIL"
    return "RUNNING"


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _reject_secret_in_durable_run_fields(
    api_key: str | None,
    **fields: object,
) -> None:
    """Reject a transient credential before any request-derived value is stored."""

    if api_key is None:
        return
    for value in fields.values():
        if isinstance(value, str) and api_key in value:
            raise ValueError("api_key must not appear in a persisted run field")


def _safe_batch_dispatch_failure(exc: BaseException) -> str:
    if isinstance(exc, ValueError):
        return "BATCH_CONFIGURATION_REJECTED"
    if isinstance(exc, ControlPlaneConflict):
        return "BATCH_STATE_CONFLICT"
    return "BATCH_DISPATCH_ERROR"
