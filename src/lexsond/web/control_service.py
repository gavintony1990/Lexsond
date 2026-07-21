from __future__ import annotations

import hashlib
import threading
import time
from copy import deepcopy
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from typing import Any, Mapping, Protocol, Sequence
from uuid import uuid4

from ..agent.service import AgentCoordinator
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
from .api_models import RunCreate, SuiteCreate, SuitePatch, TargetCreate, TargetPatch
from .control_store import ControlPlaneConflict, ControlPlaneStore
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

    def close(self) -> None: ...


class DisabledTemporalLauncher:
    available = False

    def __init__(self, status: str = "NOT_CONFIGURED") -> None:
        self.status = status

    def start(self, **_: Any) -> None:
        raise TemporalUnavailable("Temporal backend is not configured")

    def cancel(self, run_id: str) -> bool:
        del run_id
        raise TemporalUnavailable("Temporal backend is not configured")

    def close(self) -> None:
        return None


class ControlPlaneService:
    """Application service shared by FastAPI handlers and integration tests."""

    def __init__(
        self,
        *,
        database_path: str | Path | None,
        default_suite_path: str | Path,
        executor: Executor | None = None,
        temporal_launcher: TemporalLauncher | None = None,
        store: Any | None = None,
        agent_model_factory: Any | None = None,
    ) -> None:
        if store is None and database_path is None:
            raise ValueError("database_path is required when store is not provided")
        self.store = store or ControlPlaneStore(database_path)
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
        self._recover_temporal_runs()

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)
        self.temporal.close()
        close_store = getattr(self.store, "close", None)
        if close_store is not None:
            close_store()

    def _recover_temporal_runs(self) -> None:
        recover = getattr(self.temporal, "recover", None)
        if not self.temporal.available or not callable(recover):
            return
        recovery_query = getattr(self.store, "list_temporal_runs_for_recovery", None)
        runs = recovery_query() if callable(recovery_query) else self.store.list_runs(limit=100)
        for run in runs:
            if run["state"] != "RUNNING" or run["execution_backend"] != "temporal":
                continue
            with self._cancel_lock:
                self._cancel_signals.setdefault(run["run_id"], threading.Event())
            try:
                recovered = recover(
                    run["run_id"],
                    on_events=self._record_temporal_events,
                    on_terminal=self._complete_temporal_run,
                )
            except Exception:
                recovered = False
            if not recovered:
                self._mark_failed(run["run_id"], "TEMPORAL_DISPATCH_INCOMPLETE")
            elif run.get("cancel_requested_at") is not None:
                self.executor.submit(self._retry_temporal_cancel, run["run_id"])

    def bootstrap(self) -> dict[str, Any]:
        runs = self.store.list_runs(limit=100)
        terminal = [run for run in runs if run["state"] != "RUNNING"]
        passed = [run for run in terminal if run["result_status"] == "PASS"]
        return {
            "product": {
                "name": "Lexsond",
                "english_name": "Lexsond · 码海测深",
                "version": "0.7.0",
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
                "targets": len(self.store.list_targets()),
                "suites": len(self.store.list_suites()),
                "agent_sessions": len(self.store.list_agent_sessions()),
            },
        }

    # Target management

    def create_target(self, model: TargetCreate) -> dict[str, Any]:
        return self.store.create_target(model.model_dump())

    def update_target(self, target_id: str, patch: TargetPatch) -> dict[str, Any]:
        current = self.store.get_target(target_id)
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
        return self.store.update_target(
            target_id,
            normalized_changes,
            expected_version=patch.version,
        )

    def target_catalog(self, target_id: str, api_key: str | None) -> dict[str, Any]:
        target = self.store.get_target(target_id)
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
        return {
            "status": "CONNECTED",
            "target_id": target_id,
            "auth_mode": "bearer" if api_key else "none",
            "model_count": len(entries),
            "models": [entry.to_public_dict() for entry in entries],
        }

    # Suite management

    def create_suite(self, model: SuiteCreate) -> dict[str, Any]:
        return self.store.create_suite(model.model_dump())

    def update_suite(self, suite_id: str, patch: SuitePatch) -> dict[str, Any]:
        fields = patch.model_fields_set - {"version"}
        changes = {field: getattr(patch, field) for field in fields}
        return self.store.update_suite(
            suite_id,
            changes,
            expected_version=patch.version,
        )

    # Runs

    def start_run(
        self,
        model: RunCreate,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        target = self.store.get_target(str(model.target_id))
        api_key = model.api_key.get_secret_value() if model.api_key is not None else None
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
            revision = self.store.get_suite_revision(str(model.suite_revision_id))
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
        if model.execution_backend == "temporal":
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
            "target_id": target["id"],
            "suite_revision_id": suite_revision_id,
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
            "audio_voice": audio_voice,
        }
        request_sha256 = hashlib.sha256(canonical_json_bytes(metadata)).hexdigest()
        created_run = self.store.create_run(
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
            self.executor.submit(
                self._execute_local,
                run_id,
                api_key,
                metadata,
                suite,
                signal,
            )
        return self.store.get_run(run_id)

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        with self._cancel_lock:
            signal = self._cancel_signals.get(run_id)
            if signal is not None:
                signal.set()
        if run["execution_backend"] != "temporal":
            return self.store.cancel_run(run_id)
        intent = self.store.request_cancel_run(run_id)
        try:
            dispatched = self.temporal.cancel(run_id)
        except Exception:
            dispatched = False
        if dispatched:
            return self._confirm_temporal_cancel(run_id)
        self.executor.submit(self._retry_temporal_cancel, run_id)
        return self.store.get_run(run_id, include_archived=True)

    def _retry_temporal_cancel(self, run_id: str) -> None:
        delay = 0.25
        while True:
            try:
                run = self.store.get_run(run_id, include_archived=True)
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
            time.sleep(delay)
            delay = min(delay * 2, 5.0)

    def _confirm_temporal_cancel(self, run_id: str) -> dict[str, Any]:
        try:
            cancelled = self.store.cancel_run(run_id)
        except ControlPlaneConflict:
            return self.store.get_run(run_id, include_archived=True)
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
                            stream=metadata["stream"],
                            probe_type=ProbeType(metadata["probe_type"]),
                            provider_id=metadata["provider_id"],
                            audio_voice=metadata.get("audio_voice"),
                        ),
                        progress=recorder.emit,
                    )
                finally:
                    recorder.close()
            if cancel_signal.is_set():
                return
            current = self.store.get_run(run_id)
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
        except Exception:
            try:
                current = self.store.get_run(run_id, include_archived=True)
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
        current = self.store.get_run(run_id, include_archived=True)
        if current["execution_backend"] == "temporal":
            workflow = _finalize_temporal_workflow(
                current["workflow"], status="FAIL", failure_code=code
            )
            self.store.fail_run(run_id, code, workflow)
            return
        workflow = fail_component_run(
            current["workflow"], failure_code=code, occurred_at=_now()
        )
        self.store.fail_run(run_id, code, workflow)

    def _record_temporal_events(
        self, run_id: str, events: Sequence[WorkflowEvent]
    ) -> None:
        for event in events:
            current = self.store.get_run(run_id, include_archived=True)
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
                current = self.store.get_run(run_id, include_archived=True)
            except Exception:
                return
            if current["state"] != "RUNNING":
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
                return
            self._mark_failed(run_id, failure_code or "TEMPORAL_WORKFLOW_FAILED")
        finally:
            with self._cancel_lock:
                self._cancel_signals.pop(run_id, None)

    def _advance(
        self, run_id: str, step_id: str, status: ComponentStepStatus
    ) -> None:
        current = self.store.get_run(run_id)
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
    *, model_name: str, stream: bool, timeout_seconds: float
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
                "prompt": "Reply with exactly: PROBE_OK",
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
