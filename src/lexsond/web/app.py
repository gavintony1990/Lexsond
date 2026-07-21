from __future__ import annotations

import argparse
import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Mapping
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles

from ..agent.chat_model import AgentModelError
from ..providers import public_providers
from ..targets import TargetConnectionError
from .api_models import (
    AgentMessageCreate,
    AgentSessionCreate,
    AgentSessionPatch,
    CatalogRequest,
    MonitorPolicyCreate,
    MonitorPolicyPatch,
    ProviderDetectRequest,
    RunCreate,
    SuiteCreate,
    SuitePatch,
    TargetCreate,
    TargetPatch,
)
from .control_service import (
    ControlPlaneService,
    DisabledTemporalLauncher,
    TemporalUnavailable,
)
from .control_store import ControlPlaneConflict, ControlPlaneNotFound


class SSEProtocolError(ValueError):
    pass


def create_app(
    *,
    service: ControlPlaneService | None = None,
    database_path: str | Path = ".local/web.sqlite3",
    suite_path: str | Path | None = None,
    frontend_path: str | Path | None = None,
) -> FastAPI:
    owns_service = service is None
    if suite_path is None:
        suite_path = _default_suite_path()
    if service is None:
        control_store: Any = None
        if os.environ.get("LEXSOND_CONTROL_STORE", "sqlite") == "postgres":
            dsn = os.environ.get("LEXSOND_POSTGRES_DSN")
            if not dsn:
                raise ValueError(
                    "LEXSOND_POSTGRES_DSN is required for the PostgreSQL control store"
                )
            from .postgres_control_store import PostgresControlPlaneStore

            control_store = PostgresControlPlaneStore.from_dsn(dsn)
        temporal_launcher: Any = None
        try:
            from .temporal_backend import temporal_launcher_from_environment

            temporal_launcher = temporal_launcher_from_environment()
        except Exception:
            temporal_launcher = DisabledTemporalLauncher("UNAVAILABLE")
        service = ControlPlaneService(
            database_path=None if control_store is not None else database_path,
            default_suite_path=suite_path,
            temporal_launcher=temporal_launcher,
            store=control_store,
            monitor_sample_retention_days=_retention_days(
                "LEXSOND_MONITOR_SAMPLE_RETENTION_DAYS", 30
            ),
            monitor_incident_retention_days=_retention_days(
                "LEXSOND_MONITOR_INCIDENT_RETENTION_DAYS", 365
            ),
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_service:
            service.close()

    app = FastAPI(
        title="Lexsond API",
        version="0.8.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.service = service

    @app.middleware("http")
    async def security_and_request_id(request: Request, call_next: Any):
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self' data:; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        details = [
            {
                "field": ".".join(str(item) for item in error["loc"] if item != "body"),
                "message": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        return _error(request, status.HTTP_422_UNPROCESSABLE_CONTENT, "VALIDATION_ERROR", "Request validation failed", details)

    @app.exception_handler(ControlPlaneNotFound)
    async def not_found(request: Request, exc: ControlPlaneNotFound):
        return _error(request, status.HTTP_404_NOT_FOUND, "NOT_FOUND", str(exc))

    @app.exception_handler(TemporalUnavailable)
    async def temporal_unavailable(request: Request, exc: TemporalUnavailable):
        return _error(request, status.HTTP_503_SERVICE_UNAVAILABLE, "TEMPORAL_UNAVAILABLE", str(exc))

    @app.exception_handler(AgentModelError)
    async def agent_model_error(request: Request, exc: AgentModelError):
        return _error(
            request,
            exc.http_status,
            exc.code,
            str(exc),
        )

    @app.exception_handler(ControlPlaneConflict)
    async def conflict(request: Request, exc: ControlPlaneConflict):
        return _error(request, status.HTTP_409_CONFLICT, "CONFLICT", str(exc))

    @app.exception_handler(TargetConnectionError)
    async def target_error(request: Request, exc: TargetConnectionError):
        message = str(exc)
        code = _target_error_code(message)
        status_code = {
            "AUTHENTICATION_FAILED": status.HTTP_401_UNAUTHORIZED,
            "PAYMENT_REQUIRED": status.HTTP_402_PAYMENT_REQUIRED,
            "AUTHORIZATION_FAILED": status.HTTP_403_FORBIDDEN,
            "MODEL_CATALOG_NOT_FOUND": status.HTTP_404_NOT_FOUND,
            "RATE_LIMITED": status.HTTP_429_TOO_MANY_REQUESTS,
        }.get(code, status.HTTP_502_BAD_GATEWAY)
        return _error(request, status_code, code, message)

    @app.exception_handler(ValueError)
    async def value_error(request: Request, exc: ValueError):
        return _error(request, status.HTTP_400_BAD_REQUEST, "VALIDATION_ERROR", str(exc))

    @app.exception_handler(SSEProtocolError)
    async def sse_protocol_error(request: Request, exc: SSEProtocolError):
        return _error(
            request,
            status.HTTP_400_BAD_REQUEST,
            "SSE_PROTOCOL_ERROR",
            str(exc),
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        del exc
        return _error(
            request,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "INTERNAL_ERROR",
            "The request could not be completed safely",
        )

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return {"data": {"status": "ok"}}

    @app.get("/api/v1/bootstrap")
    def bootstrap() -> dict[str, Any]:
        return {"data": service.bootstrap()}

    @app.get("/api/v1/providers")
    def providers() -> dict[str, Any]:
        return {"data": public_providers()}

    @app.post("/api/v1/providers/detect")
    def detect_provider(payload: ProviderDetectRequest) -> dict[str, Any]:
        return {
            "data": service.detect_provider(
                payload.api_key.get_secret_value(), payload.provider_id
            )
        }

    @app.get("/api/v1/agent/bootstrap")
    def agent_bootstrap() -> dict[str, Any]:
        return {"data": service.agent.bootstrap()}

    @app.get("/api/v1/agent/tools")
    def agent_tools() -> dict[str, Any]:
        values = service.agent.bootstrap()["tools"]
        return {"data": values, "meta": {"total": len(values)}}

    @app.get("/api/v1/agent/skills")
    def agent_skills() -> dict[str, Any]:
        values = service.agent.bootstrap()["skills"]
        return {"data": values, "meta": {"total": len(values)}}

    @app.get("/api/v1/agent/sessions")
    def list_agent_sessions(
        include_archived: bool = False,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        values = service.store.list_agent_sessions(
            include_archived=include_archived,
            limit=limit,
        )
        return {"data": values, "meta": {"total": len(values), "limit": limit}}

    @app.post(
        "/api/v1/agent/sessions",
        status_code=status.HTTP_201_CREATED,
    )
    def create_agent_session(payload: AgentSessionCreate) -> dict[str, Any]:
        return {
            "data": service.agent.create_session(
                title=payload.title,
                target_id=str(payload.target_id),
                model=payload.model,
                skill_id=payload.skill_id,
            )
        }

    @app.get("/api/v1/agent/sessions/{session_id}")
    def get_agent_session(
        session_id: str, include_archived: bool = False
    ) -> dict[str, Any]:
        return {
            "data": service.store.get_agent_session(
                session_id, include_archived=include_archived
            )
        }

    @app.patch("/api/v1/agent/sessions/{session_id}")
    def update_agent_session(
        session_id: str, payload: AgentSessionPatch
    ) -> dict[str, Any]:
        return {
            "data": service.agent.update_session(
                session_id,
                version=payload.version,
                title=payload.title if "title" in payload.model_fields_set else None,
                skill_id=(
                    payload.skill_id if "skill_id" in payload.model_fields_set else None
                ),
            )
        }

    @app.delete("/api/v1/agent/sessions/{session_id}")
    def archive_agent_session(session_id: str) -> dict[str, Any]:
        return {"data": service.store.archive_agent_session(session_id)}

    @app.post("/api/v1/agent/sessions/{session_id}/restore")
    def restore_agent_session(session_id: str) -> dict[str, Any]:
        return {"data": service.store.restore_agent_session(session_id)}

    @app.delete(
        "/api/v1/agent/sessions/{session_id}/purge",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def purge_agent_session(session_id: str) -> None:
        service.store.purge_agent_session(session_id)

    @app.get("/api/v1/agent/sessions/{session_id}/messages")
    def list_agent_messages(
        session_id: str,
        limit: int = Query(default=100, ge=1, le=100),
    ) -> dict[str, Any]:
        values = service.store.list_agent_messages(session_id, limit=limit)
        return {"data": values, "meta": {"total": len(values), "limit": limit}}

    @app.post("/api/v1/agent/sessions/{session_id}/messages")
    def create_agent_message(
        session_id: str, payload: AgentMessageCreate
    ) -> dict[str, Any]:
        return {
            "data": service.agent.respond(
                session_id,
                content=payload.content,
                api_key=(
                    payload.api_key.get_secret_value() if payload.api_key else None
                ),
                timeout_seconds=payload.timeout_seconds,
            )
        }

    @app.get("/api/v1/agent/sessions/{session_id}/events")
    def list_agent_events(
        session_id: str,
        after_sequence: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        values = service.store.list_agent_events(
            session_id, after_sequence=after_sequence
        )
        return {"data": values, "meta": {"total": len(values)}}

    @app.get("/api/v1/targets")
    def list_targets(include_archived: bool = False) -> dict[str, Any]:
        values = service.store.list_targets(include_archived=include_archived)
        return {"data": values, "meta": {"total": len(values)}}

    @app.post("/api/v1/targets", status_code=status.HTTP_201_CREATED)
    def create_target(payload: TargetCreate) -> dict[str, Any]:
        return {"data": service.create_target(payload)}

    @app.get("/api/v1/targets/{target_id}")
    def get_target(target_id: str, include_archived: bool = False) -> dict[str, Any]:
        return {"data": service.store.get_target(target_id, include_archived=include_archived)}

    @app.patch("/api/v1/targets/{target_id}")
    def update_target(target_id: str, payload: TargetPatch) -> dict[str, Any]:
        return {"data": service.update_target(target_id, payload)}

    @app.delete("/api/v1/targets/{target_id}")
    def archive_target(target_id: str) -> dict[str, Any]:
        return {"data": service.store.archive_target(target_id)}

    @app.post("/api/v1/targets/{target_id}/restore")
    def restore_target(target_id: str) -> dict[str, Any]:
        return {"data": service.store.restore_target(target_id)}

    @app.delete("/api/v1/targets/{target_id}/purge", status_code=status.HTTP_204_NO_CONTENT)
    def purge_target(target_id: str) -> None:
        service.store.purge_target(target_id)

    @app.post("/api/v1/targets/{target_id}/catalog")
    def target_catalog(target_id: str, payload: CatalogRequest) -> dict[str, Any]:
        key = payload.api_key.get_secret_value() if payload.api_key else None
        return {"data": service.target_catalog(target_id, key)}

    @app.get("/api/v1/suites")
    def list_suites(include_archived: bool = False) -> dict[str, Any]:
        values = service.store.list_suites(include_archived=include_archived)
        return {"data": values, "meta": {"total": len(values)}}

    @app.post("/api/v1/suites", status_code=status.HTTP_201_CREATED)
    def create_suite(payload: SuiteCreate) -> dict[str, Any]:
        return {"data": service.create_suite(payload)}

    @app.get("/api/v1/suites/{suite_id}")
    def get_suite(suite_id: str, include_archived: bool = False) -> dict[str, Any]:
        return {"data": service.store.get_suite(suite_id, include_archived=include_archived)}

    @app.patch("/api/v1/suites/{suite_id}")
    def update_suite(suite_id: str, payload: SuitePatch) -> dict[str, Any]:
        return {"data": service.update_suite(suite_id, payload)}

    @app.get("/api/v1/suites/{suite_id}/revisions")
    def suite_revisions(suite_id: str) -> dict[str, Any]:
        values = service.store.list_suite_revisions(suite_id)
        return {"data": values, "meta": {"total": len(values)}}

    @app.delete("/api/v1/suites/{suite_id}")
    def archive_suite(suite_id: str) -> dict[str, Any]:
        return {"data": service.store.archive_suite(suite_id)}

    @app.post("/api/v1/suites/{suite_id}/restore")
    def restore_suite(suite_id: str) -> dict[str, Any]:
        return {"data": service.store.restore_suite(suite_id)}

    @app.delete("/api/v1/suites/{suite_id}/purge", status_code=status.HTTP_204_NO_CONTENT)
    def purge_suite(suite_id: str) -> None:
        service.store.purge_suite(suite_id)

    @app.get("/api/v1/monitor-policies")
    def list_monitor_policies(include_archived: bool = False) -> dict[str, Any]:
        values = service.store.list_monitor_policies(
            include_archived=include_archived
        )
        return {"data": values, "meta": {"total": len(values)}}

    @app.post("/api/v1/monitor-policies", status_code=status.HTTP_201_CREATED)
    def create_monitor_policy(payload: MonitorPolicyCreate) -> dict[str, Any]:
        return {"data": service.create_monitor_policy(payload)}

    @app.get("/api/v1/monitor-policies/{policy_id}")
    def get_monitor_policy(
        policy_id: str, include_archived: bool = False
    ) -> dict[str, Any]:
        return {
            "data": service.store.get_monitor_policy(
                policy_id, include_archived=include_archived
            )
        }

    @app.patch("/api/v1/monitor-policies/{policy_id}")
    def update_monitor_policy(
        policy_id: str, payload: MonitorPolicyPatch
    ) -> dict[str, Any]:
        return {"data": service.update_monitor_policy(policy_id, payload)}

    @app.delete("/api/v1/monitor-policies/{policy_id}")
    def archive_monitor_policy(policy_id: str) -> dict[str, Any]:
        return {"data": service.store.archive_monitor_policy(policy_id)}

    @app.post("/api/v1/monitor-policies/{policy_id}/restore")
    def restore_monitor_policy(policy_id: str) -> dict[str, Any]:
        return {"data": service.store.restore_monitor_policy(policy_id)}

    @app.delete(
        "/api/v1/monitor-policies/{policy_id}/purge",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def purge_monitor_policy(policy_id: str) -> None:
        service.store.purge_monitor_policy(policy_id)

    @app.post("/api/v1/monitor-policies/{policy_id}/run-now")
    def run_monitor_policy_now(policy_id: str) -> dict[str, Any]:
        return {"data": service.request_monitor_policy_run(policy_id)}

    @app.get("/api/v1/monitoring/overview")
    def monitoring_overview(
        window: str = Query(default="24h", pattern=r"^(90m|24h|7d|30d)$"),
        include_archived: bool = False,
    ) -> dict[str, Any]:
        return {
            "data": service.store.monitoring_overview(
                window=window, include_archived=include_archived
            )
        }

    @app.get("/api/v1/monitoring/incidents")
    def monitoring_incidents(
        policy_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        values = service.store.list_monitor_incidents(
            policy_id=policy_id, limit=limit
        )
        return {"data": values, "meta": {"total": len(values), "limit": limit}}

    @app.get("/api/v1/runs")
    def list_runs(
        include_archived: bool = False,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        values = service.store.list_runs(include_archived=include_archived, limit=limit)
        return {"data": values, "meta": {"total": len(values), "limit": limit}}

    @app.post("/api/v1/runs", status_code=status.HTTP_202_ACCEPTED)
    def create_run(
        payload: RunCreate,
        idempotency_key: UUID | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        return {
            "data": service.start_run(
                payload,
                idempotency_key=str(idempotency_key) if idempotency_key else None,
            )
        }

    @app.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str, include_archived: bool = False) -> dict[str, Any]:
        return {"data": service.store.get_run(run_id, include_archived=include_archived)}

    @app.post("/api/v1/runs/{run_id}/cancel")
    def cancel_run(run_id: str) -> dict[str, Any]:
        return {"data": service.cancel_run(run_id)}

    @app.delete("/api/v1/runs/{run_id}")
    def archive_run(run_id: str) -> dict[str, Any]:
        return {"data": service.store.archive_run(run_id)}

    @app.post("/api/v1/runs/{run_id}/restore")
    def restore_run(run_id: str) -> dict[str, Any]:
        return {"data": service.store.restore_run(run_id)}

    @app.delete("/api/v1/runs/{run_id}/purge", status_code=status.HTTP_204_NO_CONTENT)
    def purge_run(run_id: str) -> None:
        service.store.purge_run(run_id)

    def event_cursor(
        run_id: str,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> int:
        try:
            cursor = max(int(last_event_id or "0"), 0)
        except ValueError as exc:
            raise SSEProtocolError(
                "Last-Event-ID must be an integer sequence"
            ) from exc
        service.store.get_run(run_id, include_archived=True)
        return cursor

    @app.get("/api/v1/runs/{run_id}/events", response_class=EventSourceResponse)
    async def run_events(
        request: Request,
        run_id: str,
        after_sequence: int = Depends(event_cursor),
    ) -> AsyncIterator[ServerSentEvent]:
        cursor = after_sequence
        idle_rounds = 0
        while not await request.is_disconnected():
            events = service.store.list_run_events(run_id, after_sequence=cursor)
            for event in events:
                cursor = event["sequence"]
                yield ServerSentEvent(
                    data=event,
                    event=event["event_type"].lower(),
                    id=str(cursor),
                )
            run = service.store.get_run(run_id, include_archived=True)
            if run["state"] != "RUNNING" and not events:
                return
            idle_rounds = idle_rounds + 1 if not events else 0
            if idle_rounds >= 25:
                idle_rounds = 0
                yield ServerSentEvent(comment="keepalive")
            await asyncio.sleep(0.2)

    dist = Path(frontend_path) if frontend_path is not None else Path(__file__).with_name("dist")
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path.startswith("api/"):
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={
                    "error": {
                        "code": "NOT_FOUND",
                        "message": "API route not found",
                        "details": [],
                        "request_id": None,
                    }
                },
            )
        candidate = dist / path
        if path and candidate.is_file() and dist in candidate.resolve().parents:
            return FileResponse(candidate)
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": {"code": "FRONTEND_NOT_BUILT", "message": "Run npm build in frontend/"}},
        )

    return app


def _error(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: list[Mapping[str, Any]] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": list(details or ()),
                "request_id": getattr(request.state, "request_id", None),
            }
        },
    )


def _target_error_code(message: str) -> str:
    if "blocked network" in message:
        return "TARGET_ADDRESS_BLOCKED"
    for status_code, code in (
        ("HTTP 401", "AUTHENTICATION_FAILED"),
        ("HTTP 402", "PAYMENT_REQUIRED"),
        ("HTTP 403", "AUTHORIZATION_FAILED"),
        ("HTTP 404", "MODEL_CATALOG_NOT_FOUND"),
        ("HTTP 429", "RATE_LIMITED"),
    ):
        if status_code in message:
            return code
    if "unreachable" in message or "timeout" in message or "timed out" in message:
        return (
            "TARGET_TIMEOUT"
            if "timeout" in message or "timed out" in message
            else "TARGET_UNREACHABLE"
        )
    if "TLS" in message:
        return "TLS_ERROR"
    if "JSON" in message or "format" in message:
        return "TARGET_PROTOCOL_ERROR"
    return "TARGET_CONNECTION_ERROR"


def _default_suite_path() -> Path:
    project_suite = Path.cwd() / "suites/canary/openai-compatible.json"
    if project_suite.is_file():
        return project_suite
    return Path(__file__).with_name("default-suite.json")


def _retention_days(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not 1 <= value <= 3650:
        raise ValueError(f"{name} must be between 1 and 3650")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Lexsond FastAPI console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--database", type=Path, default=Path(".local/web.sqlite3"))
    parser.add_argument("--suite", type=Path, default=_default_suite_path())
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    import uvicorn

    if args.reload:
        uvicorn.run(
            "lexsond.web.app:create_app",
            host=args.host,
            port=args.port,
            reload=True,
            factory=True,
        )
        return
    uvicorn.run(
        create_app(database_path=args.database, suite_path=args.suite),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
