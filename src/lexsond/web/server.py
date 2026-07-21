from __future__ import annotations

import argparse
import ipaddress
import json
import sqlite3
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

from ..probe import (
    ProbeConfig,
    ProbeType,
    run_openai_probe,
    validate_api_key_value,
    validate_base_url_transport,
)
from ..probe_components import (
    ComponentStepStatus,
    advance_component_run,
    begin_component_evidence,
    component_catalog,
    create_component_run,
    fail_component_run,
    finalize_component_run,
)
from ..providers import (
    detect_provider_key,
    get_provider,
    public_providers,
    resolve_provider_key,
)
from ..storage.redaction import sanitized_result_for_persistence
from ..suite import ProbeSuite, load_suite_json, run_suite
from ..targets import TargetConnectionError, fetch_model_catalog_entries


MAX_REQUEST_BYTES = 64 * 1024
MAX_HISTORY_ITEMS = 100


class RunHistoryStore:
    """Small local history index that stores only sanitized probe results."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create(
        self,
        run_id: str,
        metadata: Mapping[str, Any],
        workflow: Mapping[str, Any],
    ) -> None:
        workflow_payload = self._encode_workflow(workflow)
        with self._session() as connection:
            connection.execute(
                """
                INSERT INTO web_probe_runs (
                    run_id, state, result_status, created_at, finished_at,
                    base_url, model, target_kind, provider_id, run_mode,
                    probe_type, streaming, timeout_seconds, result_json, failure_code,
                    workflow_json
                ) VALUES (?, 'RUNNING', NULL, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    run_id,
                    _now(),
                    metadata["base_url"],
                    metadata["model"],
                    metadata["target_kind"],
                    metadata["provider_id"],
                    metadata["run_mode"],
                    metadata["probe_type"],
                    1 if metadata["stream"] else 0,
                    metadata["timeout_seconds"],
                    workflow_payload,
                ),
            )

    def advance(
        self,
        run_id: str,
        step_id: str,
        status: ComponentStepStatus,
    ) -> None:
        occurred_at = _now()
        with self._session() as connection:
            row = connection.execute(
                "SELECT state, workflow_json FROM web_probe_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None or row["state"] != "RUNNING" or row["workflow_json"] is None:
                raise RuntimeError("run workflow cannot be advanced")
            workflow = advance_component_run(
                json.loads(row["workflow_json"]),
                step_id,
                status,
                occurred_at=occurred_at,
            )
            cursor = connection.execute(
                """
                UPDATE web_probe_runs SET workflow_json = ?
                WHERE run_id = ? AND state = 'RUNNING'
                """,
                (self._encode_workflow(workflow), run_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("run workflow update lost its state boundary")

    def complete(self, run_id: str, result: Mapping[str, Any]) -> None:
        payload = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        occurred_at = _now()
        with self._session() as connection:
            row = connection.execute(
                "SELECT state, workflow_json FROM web_probe_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None or row["state"] != "RUNNING" or row["workflow_json"] is None:
                raise RuntimeError("run cannot be completed from its current state")
            workflow = finalize_component_run(
                json.loads(row["workflow_json"]),
                result=result,
                occurred_at=occurred_at,
            )
            cursor = connection.execute(
                """
                UPDATE web_probe_runs
                SET state = 'COMPLETED', result_status = ?, finished_at = ?,
                    result_json = ?, failure_code = NULL, workflow_json = ?
                WHERE run_id = ? AND state = 'RUNNING'
                """,
                (
                    result["status"],
                    occurred_at,
                    payload,
                    self._encode_workflow(workflow),
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("run cannot be completed from its current state")

    def begin_evidence(self, run_id: str, result: Mapping[str, Any]) -> None:
        occurred_at = _now()
        with self._session() as connection:
            row = connection.execute(
                "SELECT state, workflow_json FROM web_probe_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None or row["state"] != "RUNNING" or row["workflow_json"] is None:
                raise RuntimeError("run evidence cannot be started")
            workflow = begin_component_evidence(
                json.loads(row["workflow_json"]),
                result=result,
                occurred_at=occurred_at,
            )
            cursor = connection.execute(
                """
                UPDATE web_probe_runs SET workflow_json = ?
                WHERE run_id = ? AND state = 'RUNNING'
                """,
                (self._encode_workflow(workflow), run_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("run evidence update lost its state boundary")

    def fail(self, run_id: str, code: str = "EXECUTION_ERROR") -> None:
        occurred_at = _now()
        with self._session() as connection:
            row = connection.execute(
                "SELECT state, workflow_json FROM web_probe_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None or row["state"] != "RUNNING" or row["workflow_json"] is None:
                raise RuntimeError("run cannot be failed from its current state")
            workflow = fail_component_run(
                json.loads(row["workflow_json"]),
                failure_code=code,
                occurred_at=occurred_at,
            )
            cursor = connection.execute(
                """
                UPDATE web_probe_runs
                SET state = 'FAILED', result_status = 'FAIL', finished_at = ?,
                    failure_code = ?, result_json = NULL, workflow_json = ?
                WHERE run_id = ? AND state = 'RUNNING'
                """,
                (occurred_at, code, self._encode_workflow(workflow), run_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("run cannot be failed from its current state")

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM web_probe_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return None if row is None else self._decode(row, include_result=True)

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        bounded_limit = min(max(int(limit), 1), MAX_HISTORY_ITEMS)
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT * FROM web_probe_runs
                ORDER BY created_at DESC, run_id DESC
                LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
        return [self._decode(row, include_result=False) for row in rows]

    def _decode(self, row: sqlite3.Row, *, include_result: bool) -> dict[str, Any]:
        value: dict[str, Any] = {
            "run_id": row["run_id"],
            "state": row["state"],
            "result_status": row["result_status"],
            "created_at": row["created_at"],
            "finished_at": row["finished_at"],
            "config": {
                "base_url": row["base_url"],
                "model": row["model"],
                "target_kind": row["target_kind"],
                "provider_id": row["provider_id"],
                "run_mode": row["run_mode"],
                "probe_type": row["probe_type"],
                "stream": bool(row["streaming"]),
                "timeout_seconds": row["timeout_seconds"],
            },
            "failure_code": row["failure_code"],
            "workflow": (
                json.loads(row["workflow_json"])
                if row["workflow_json"] is not None
                else None
            ),
        }
        if include_result:
            value["result"] = (
                json.loads(row["result_json"])
                if row["result_json"] is not None
                else None
            )
        return value

    def _initialize(self) -> None:
        with self._session() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA synchronous = FULL;
                CREATE TABLE IF NOT EXISTS web_probe_runs (
                    run_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL CHECK (state IN ('RUNNING', 'COMPLETED', 'FAILED')),
                    result_status TEXT,
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    base_url TEXT NOT NULL,
                    model TEXT NOT NULL,
                    target_kind TEXT NOT NULL DEFAULT 'cloud',
                    provider_id TEXT,
                    run_mode TEXT NOT NULL CHECK (run_mode IN ('single', 'canary')),
                    probe_type TEXT NOT NULL DEFAULT 'chat',
                    streaming INTEGER NOT NULL CHECK (streaming IN (0, 1)),
                    timeout_seconds REAL NOT NULL,
                    result_json TEXT,
                    failure_code TEXT,
                    workflow_json TEXT,
                    CHECK (result_json IS NULL OR state = 'COMPLETED'),
                    CHECK (failure_code IS NULL OR state = 'FAILED')
                );
                CREATE INDEX IF NOT EXISTS idx_web_probe_runs_created
                ON web_probe_runs(created_at DESC);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(web_probe_runs)")
            }
            if "target_kind" not in columns:
                connection.execute(
                    "ALTER TABLE web_probe_runs ADD COLUMN "
                    "target_kind TEXT NOT NULL DEFAULT 'cloud'"
                )
            if "provider_id" not in columns:
                connection.execute(
                    "ALTER TABLE web_probe_runs ADD COLUMN provider_id TEXT"
                )
            if "probe_type" not in columns:
                connection.execute(
                    "ALTER TABLE web_probe_runs ADD COLUMN "
                    "probe_type TEXT NOT NULL DEFAULT 'chat'"
                )
            if "workflow_json" not in columns:
                connection.execute(
                    "ALTER TABLE web_probe_runs ADD COLUMN workflow_json TEXT"
                )
            self._migrate_workflow_binding_sources(connection)

    @staticmethod
    def _migrate_workflow_binding_sources(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT run_id, workflow_json FROM web_probe_runs WHERE workflow_json IS NOT NULL"
        ).fetchall()
        for row in rows:
            try:
                workflow = json.loads(row["workflow_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(workflow, dict):
                continue
            changed = False
            if workflow.get("schema_version") == "probe.ai/component-run/v1alpha1":
                workflow["schema_version"] = "probe.ai/component-run/v1alpha2"
                workflow["binding_source"] = "LEGACY_UNSPECIFIED"
                steps = workflow.get("steps")
                if isinstance(steps, list) and steps and isinstance(steps[0], dict):
                    steps[0]["facts"] = ["LEGACY BINDING SOURCE UNKNOWN"]
                changed = True
            if "binding_source" in workflow:
                if changed:
                    connection.execute(
                        "UPDATE web_probe_runs SET workflow_json = ? WHERE run_id = ?",
                        (RunHistoryStore._encode_workflow(workflow), row["run_id"]),
                    )
                continue
            steps = workflow.get("steps")
            if not isinstance(steps, list) or not steps or not isinstance(steps[0], dict):
                continue
            workflow["binding_source"] = "LEGACY_UNSPECIFIED"
            steps[0]["facts"] = ["LEGACY BINDING SOURCE UNKNOWN"]
            connection.execute(
                "UPDATE web_probe_runs SET workflow_json = ? WHERE run_id = ?",
                (RunHistoryStore._encode_workflow(workflow), row["run_id"]),
            )

    @staticmethod
    def _encode_workflow(workflow: Mapping[str, Any]) -> str:
        return json.dumps(
            workflow,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _session(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()


class ProbeWebService:
    """Application boundary shared by the HTTP handler and tests."""

    def __init__(
        self,
        *,
        database_path: str | Path,
        suite_path: str | Path,
        executor: Executor | None = None,
    ) -> None:
        self.store = RunHistoryStore(database_path)
        self.suite_path = Path(suite_path)
        self.suite = load_suite_json(self.suite_path)
        self.executor = executor or ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="probe-web-run",
        )

    def bootstrap(self) -> dict[str, Any]:
        return {
            "product": {
                "name": "Lexsond",
                "english_name": "Lexsond · 码海测深",
                "version": "0.5.0",
            },
            "suite": {
                "name": self.suite.name,
                "version": self.suite.version,
                "layer": self.suite.layer.value,
                "requests": self.suite.sampling.requests,
                "warmup": self.suite.sampling.warmup,
                "timeout_seconds": self.suite.sampling.timeout_seconds,
            },
            "defaults": {
                "target_kind": "local",
                "provider_id": "ollama",
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "",
                "run_mode": "single",
                "probe_type": "chat",
                "stream": True,
                "timeout_seconds": 30,
            },
            "providers": public_providers(),
            "probe_components": component_catalog(),
        }

    def detect_provider(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"api_key", "provider_id"}
        if set(payload) - allowed:
            raise ValueError("request contains unknown fields")
        if "api_key" not in payload:
            raise ValueError("request is missing required fields")
        provider_id = payload.get("provider_id")
        if provider_id is not None and (
            not isinstance(provider_id, str) or not provider_id
        ):
            raise ValueError("provider_id must be a non-empty string or null")
        return resolve_provider_key(payload["api_key"], provider_id).to_dict()

    def inspect_target(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = _validate_target_request(payload)
        entries = fetch_model_catalog_entries(
            request["base_url"],
            api_key=request["api_key"],
            provider_id=request["provider_id"],
        )
        return {
            "status": "CONNECTED",
            "base_url": request["base_url"],
            "target_kind": request["target_kind"],
            "auth_mode": "bearer" if request["api_key"] is not None else "none",
            "models": [entry.model_id for entry in entries],
            "model_catalog": [entry.to_public_dict() for entry in entries],
            "model_count": len(entries),
        }

    def start_run(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = _validate_run_request(payload)
        audio_voice, binding_source = self._validate_declared_probe_binding(request)
        if audio_voice is not None:
            request["audio_voice"] = audio_voice
        api_key = request.pop("api_key")
        run_id = str(uuid4())
        workflow = create_component_run(
            request["probe_type"],
            run_mode=request["run_mode"],
            occurred_at=_now(),
            binding_source=binding_source,
        )
        self.store.create(run_id, request, workflow)
        self.executor.submit(self._execute, run_id, api_key, request)
        run = self.store.get(run_id)
        if run is None:  # defensive: create and read share the same durable store
            raise RuntimeError("run disappeared after creation")
        return run

    def _validate_declared_probe_binding(
        self,
        request: Mapping[str, Any],
    ) -> tuple[str | None, str]:
        """Stop an incompatible billable POST before it leaves the process."""

        entries = fetch_model_catalog_entries(
            request["base_url"],
            api_key=request["api_key"],
            provider_id=request["provider_id"],
        )
        selected = next(
            (entry for entry in entries if entry.model_id == request["model"]),
            None,
        )
        if (
            selected is not None
            and selected.capability_source == "PROVIDER_METADATA"
            and request["probe_type"] not in selected.probe_types
        ):
            raise ValueError("probe_type conflicts with declared model capabilities")
        binding_source = (
            "PROVIDER_METADATA"
            if selected is not None
            and selected.capability_source == "PROVIDER_METADATA"
            else "MANUAL_CONFIRMATION"
        )
        audio_voice = None
        if request["probe_type"] == ProbeType.AUDIO_SPEECH.value:
            if selected is not None and selected.supported_voices:
                audio_voice = selected.supported_voices[0]
            if request["provider_id"] == "openrouter":
                if audio_voice is None:
                    raise ValueError("OpenRouter speech probe requires a declared supported voice")
        return audio_voice, binding_source

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.list(limit)

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)

    def _execute(
        self,
        run_id: str,
        api_key: str | None,
        request: Mapping[str, Any],
    ) -> None:
        evidence_started = False
        try:
            if request["run_mode"] == "canary":
                self.store.advance(
                    run_id,
                    "fixture_prepare",
                    ComponentStepStatus.RUNNING,
                )
                self.store.advance(
                    run_id,
                    "fixture_prepare",
                    ComponentStepStatus.PASS,
                )
                self.store.advance(
                    run_id,
                    "request_dispatch",
                    ComponentStepStatus.RUNNING,
                )
                result = run_suite(
                    self.suite,
                    base_url=request["base_url"],
                    api_key=api_key,
                    model=request["model"],
                )
                self.store.advance(
                    run_id,
                    "request_dispatch",
                    ComponentStepStatus.PASS,
                )
            else:
                recorder = _OrderedProgressRecorder(self.store, run_id)
                try:
                    result = run_openai_probe(
                        ProbeConfig(
                            base_url=request["base_url"],
                            api_key=api_key,
                            model=request["model"],
                            timeout_seconds=request["timeout_seconds"],
                            stream=request["stream"],
                            probe_type=ProbeType(request["probe_type"]),
                            provider_id=request["provider_id"],
                            audio_voice=request.get("audio_voice"),
                        ),
                        progress=recorder.emit,
                    )
                finally:
                    recorder.close()
            self.store.begin_evidence(run_id, _component_result_view(result))
            evidence_started = True
            self.store.complete(
                run_id,
                sanitized_result_for_persistence(
                    result,
                    sensitive_values=(api_key,) if api_key is not None else (),
                ),
            )
        except Exception:
            # Do not persist or reflect exception text: provider errors can contain
            # sensitive request context. Normal target failures are already results.
            self.store.fail(
                run_id,
                (
                    "EVIDENCE_PERSISTENCE_ERROR"
                    if evidence_started
                    else "EXECUTION_ERROR"
                ),
            )


class _OrderedProgressRecorder:
    """Persist ordered UI progress without entering the timed provider path."""

    def __init__(self, store: RunHistoryStore, run_id: str) -> None:
        self._store = store
        self._run_id = run_id
        self._queue: Queue[tuple[str, ComponentStepStatus] | None] = Queue()
        self._error: Exception | None = None
        self._closed = False
        self._thread = Thread(
            target=self._drain,
            name=f"probe-progress-{run_id[:8]}",
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
        # Live progress is best-effort observability. The provider result remains
        # authoritative and begin_evidence() safely settles any missing steps.
        # A real database outage will still fail at the final evidence boundary.

    def _drain(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                if self._error is None:
                    step_id, status = item
                    self._store.advance(self._run_id, step_id, status)
            except Exception as exc:
                self._error = exc
            finally:
                self._queue.task_done()


def _component_result_view(result: Any) -> dict[str, Any]:
    """Return only fields needed to place a workflow failure, never model output."""

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


class ProbeWebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        service: ProbeWebService,
    ) -> None:
        self.service = service
        super().__init__(server_address, ProbeWebHandler)


class ProbeWebHandler(BaseHTTPRequestHandler):
    server: ProbeWebServer

    def do_GET(self) -> None:  # noqa: N802 - stdlib HTTP handler contract
        path = urlsplit(self.path).path
        if path == "/api/health":
            self._json(HTTPStatus.OK, {"data": {"status": "ok"}})
            return
        if path == "/api/bootstrap":
            self._json(HTTPStatus.OK, {"data": self.server.service.bootstrap()})
            return
        if path == "/api/providers":
            self._json(HTTPStatus.OK, {"data": public_providers()})
            return
        if path == "/api/runs":
            self._json(HTTPStatus.OK, {"data": self.server.service.list_runs()})
            return
        if path.startswith("/api/runs/"):
            run_id = path.removeprefix("/api/runs/")
            if not run_id or "/" in run_id:
                self._api_error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Run not found")
                return
            try:
                run = self.server.service.get_run(run_id)
            except KeyError:
                self._api_error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Run not found")
                return
            self._json(HTTPStatus.OK, {"data": run})
            return
        if path.startswith("/api/"):
            self._api_error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "API route not found")
            return
        self._static(path)

    def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler contract
        path = urlsplit(self.path).path
        if path not in {
            "/api/runs",
            "/api/providers/detect",
            "/api/targets/models",
        }:
            self._api_error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "API route not found")
            return
        try:
            payload = self._read_json()
            if path == "/api/providers/detect":
                detection = self.server.service.detect_provider(payload)
            elif path == "/api/targets/models":
                target = self.server.service.inspect_target(payload)
            else:
                run = self.server.service.start_run(payload)
        except TargetConnectionError as exc:
            self._api_error(
                HTTPStatus.BAD_GATEWAY,
                "TARGET_CONNECTION_ERROR",
                str(exc),
            )
            return
        except ValueError as exc:
            self._api_error(
                HTTPStatus.BAD_REQUEST,
                "VALIDATION_ERROR",
                str(exc),
            )
            return
        if path == "/api/providers/detect":
            self._json(HTTPStatus.OK, {"data": detection})
            return
        if path == "/api/targets/models":
            self._json(HTTPStatus.OK, {"data": target})
            return
        self._json(HTTPStatus.ACCEPTED, {"data": run})

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib HTTP handler contract
        self._api_error(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "METHOD_NOT_ALLOWED",
            "This endpoint does not support deletion",
        )

    def log_message(self, format: str, *args: Any) -> None:
        # Access logs are intentionally disabled: paths and malformed requests can
        # contain user-provided data. Operators can add structured logging outside.
        return

    def _read_json(self) -> Mapping[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "0")
        except ValueError as exc:
            raise ValueError("Content-Length must be valid") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("request body size is invalid")
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid JSON") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("request body must be a JSON object")
        return payload

    def _static(self, path: str) -> None:
        assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/favicon.svg": ("favicon.svg", "image/svg+xml"),
        }
        asset = assets.get(path)
        if asset is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        name, content_type = asset
        payload = files("lexsond.web.static").joinpath(name).read_bytes()
        self._send(HTTPStatus.OK, payload, content_type, cache="no-cache")

    def _api_error(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
    ) -> None:
        self._json(status, {"error": {"code": code, "message": message}})

    def _json(self, status: HTTPStatus, value: Mapping[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self._send(status, payload, "application/json; charset=utf-8", cache="no-store")

    def _send(
        self,
        status: HTTPStatus,
        payload: bytes,
        content_type: str,
        *,
        cache: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(payload)


def create_server(
    host: str,
    port: int,
    service: ProbeWebService,
) -> ProbeWebServer:
    return ProbeWebServer((host, port), service)


def _validate_run_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "base_url",
        "api_key",
        "model",
        "target_kind",
        "run_mode",
        "stream",
        "timeout_seconds",
        "provider_id",
        "custom_target_confirmed",
        "probe_type",
    }
    if set(payload) - allowed:
        raise ValueError("request contains unknown fields")
    required = allowed - {"probe_type"}
    if not required.issubset(payload):
        raise ValueError("request is missing required fields")

    base_url = payload["base_url"]
    if not isinstance(base_url, str) or not base_url or len(base_url) > 2048:
        raise ValueError("base_url must be a non-empty HTTP(S) URL")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base_url must be an absolute credential-free HTTP(S) URL")
    validate_base_url_transport(base_url)

    target_kind = _target_kind(payload["target_kind"])
    api_key = _optional_api_key(payload["api_key"])
    if target_kind == "cloud" and api_key is None:
        raise ValueError("api_key is required for cloud targets")
    if api_key is not None and api_key in base_url:
        raise ValueError("base_url must not contain api_key")
    provider_id = payload["provider_id"]
    if provider_id is not None and (
        not isinstance(provider_id, str) or not provider_id
    ):
        raise ValueError("provider_id must be a non-empty string or null")
    custom_target_confirmed = payload["custom_target_confirmed"]
    if not isinstance(custom_target_confirmed, bool):
        raise ValueError("custom_target_confirmed must be a boolean")
    if provider_id is not None and custom_target_confirmed:
        raise ValueError("provider selection and custom target confirmation conflict")

    profile = get_provider(provider_id) if provider_id is not None else None
    if provider_id is not None and profile is None:
        raise ValueError("provider_id is not registered")
    if profile is not None and profile.target_kind != target_kind:
        raise ValueError("provider_id does not match target_kind")
    if profile is not None and base_url.rstrip("/") != profile.base_url.rstrip("/"):
        raise ValueError("base_url does not match the confirmed provider")
    if profile is None and not custom_target_confirmed:
        raise ValueError("unregistered endpoint requires custom target confirmation")
    if target_kind == "cloud":
        _validate_cloud_binding(api_key, profile, custom_target_confirmed)
    else:
        _validate_local_binding(base_url, api_key)
    model = payload["model"]
    if not isinstance(model, str) or not model.strip() or len(model) > 256:
        raise ValueError("model must be a non-empty string")
    if api_key is not None and api_key in model:
        raise ValueError("model must not contain api_key")
    run_mode = payload["run_mode"]
    if run_mode not in {"single", "canary"}:
        raise ValueError("run_mode must be single or canary")
    stream = payload["stream"]
    if not isinstance(stream, bool):
        raise ValueError("stream must be a boolean")
    try:
        probe_type = ProbeType(payload.get("probe_type", "chat"))
    except (TypeError, ValueError) as exc:
        raise ValueError("probe_type is not supported") from exc
    if run_mode == "canary" and probe_type != ProbeType.CHAT:
        raise ValueError("canary mode currently requires probe_type=chat")
    if stream and probe_type not in {ProbeType.CHAT, ProbeType.VISION}:
        raise ValueError("stream is only supported for chat and vision probes")
    timeout = payload["timeout_seconds"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0.1 <= float(timeout) <= 300
    ):
        raise ValueError("timeout_seconds must be between 0.1 and 300")

    return {
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "model": model.strip(),
        "target_kind": target_kind,
        "run_mode": run_mode,
        "probe_type": probe_type.value,
        "stream": stream,
        "timeout_seconds": float(timeout),
        "provider_id": provider_id,
        "custom_target_confirmed": custom_target_confirmed,
    }


def _validate_target_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "base_url",
        "api_key",
        "target_kind",
        "provider_id",
        "custom_target_confirmed",
    }
    if set(payload) - allowed:
        raise ValueError("request contains unknown fields")
    if set(payload) != allowed:
        raise ValueError("request is missing required fields")
    base_url = payload["base_url"]
    if not isinstance(base_url, str) or not base_url or len(base_url) > 2048:
        raise ValueError("base_url must be a non-empty HTTP(S) URL")
    validate_base_url_transport(base_url)
    target_kind = _target_kind(payload["target_kind"])
    api_key = _optional_api_key(payload["api_key"])
    if target_kind == "cloud" and api_key is None:
        raise ValueError("api_key is required for cloud targets")
    if api_key is not None and api_key in base_url:
        raise ValueError("base_url must not contain api_key")
    provider_id = payload["provider_id"]
    if provider_id is not None and (
        not isinstance(provider_id, str) or not provider_id
    ):
        raise ValueError("provider_id must be a non-empty string or null")
    custom_target_confirmed = payload["custom_target_confirmed"]
    if not isinstance(custom_target_confirmed, bool):
        raise ValueError("custom_target_confirmed must be a boolean")
    if provider_id is not None and custom_target_confirmed:
        raise ValueError("provider selection and custom target confirmation conflict")
    profile = get_provider(provider_id) if provider_id is not None else None
    if provider_id is not None and profile is None:
        raise ValueError("provider_id is not registered")
    if profile is not None and profile.target_kind != target_kind:
        raise ValueError("provider_id does not match target_kind")
    if profile is not None and base_url.rstrip("/") != profile.base_url.rstrip("/"):
        raise ValueError("base_url does not match the confirmed provider")
    if profile is None and not custom_target_confirmed:
        raise ValueError("unregistered endpoint requires custom target confirmation")
    if target_kind == "cloud":
        _validate_cloud_binding(api_key, profile, custom_target_confirmed)
    else:
        _validate_local_binding(base_url, api_key)
    return {
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "target_kind": target_kind,
        "provider_id": provider_id,
    }


def _target_kind(value: Any) -> str:
    if value not in {"local", "cloud"}:
        raise ValueError("target_kind must be local or cloud")
    return value


def _optional_api_key(value: Any) -> str | None:
    if value is None:
        return None
    validate_api_key_value(value)
    return value


def _validate_cloud_binding(
    api_key: str | None,
    profile: Any,
    custom_target_confirmed: bool,
) -> None:
    if api_key is None:
        raise ValueError("api_key is required for cloud targets")
    detection = detect_provider_key(api_key)
    if detection.status == "MATCHED" and not custom_target_confirmed:
        if profile is None or profile.provider_id != detection.provider.provider_id:
            raise ValueError("api_key does not match the confirmed provider")
    if detection.status == "AMBIGUOUS" and not custom_target_confirmed:
        candidate_ids = {candidate.provider_id for candidate in detection.candidates}
        if profile is None or profile.provider_id not in candidate_ids:
            raise ValueError("ambiguous api_key requires a confirmed provider")


def _validate_local_binding(base_url: str, api_key: str | None) -> None:
    hostname = urlsplit(base_url).hostname
    try:
        address = ipaddress.ip_address(hostname or "")
    except ValueError as exc:
        raise ValueError("local targets must use a numeric loopback address") from exc
    if not address.is_loopback:
        raise ValueError("local targets must use a numeric loopback address")
    if api_key is not None and detect_provider_key(api_key).status != "UNKNOWN":
        raise ValueError("cloud-formatted api_key is not allowed for local targets")


def _default_suite_path() -> Path:
    local = Path.cwd() / "suites/canary/openai-compatible.json"
    if local.is_file():
        return local
    return Path(str(files("lexsond.web").joinpath("default-suite.json")))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Lexsond, the local AI API quality console"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--database", type=Path, default=Path(".local/web.sqlite3"))
    parser.add_argument("--suite", type=Path, default=_default_suite_path())
    args = parser.parse_args()

    service = ProbeWebService(
        database_path=args.database,
        suite_path=args.suite,
    )
    server = create_server(args.host, args.port, service)
    host, port = server.server_address
    print(f"Lexsond listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
