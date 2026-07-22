from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager, suppress
from email import policy as email_policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, AsyncIterator, Mapping
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles

from ..agent.chat_model import AgentModelError
from ..credentials import (
    ExecutionCredentialBinder,
    SystemCredentialVault,
    UnavailableCredentialVault,
    VaultUnavailable,
)
from ..providers import public_providers
from ..targets import TargetConnectionError
from ..evaluations.compiler import DatasetValidationError, MAX_UPLOAD_BYTES
from .api_models import (
    ApiDataEnvelope,
    ApiErrorEnvelope,
    ApiListEnvelope,
    AgentMessageCreate,
    AgentSessionCreate,
    AgentSessionPatch,
    AuthChangePasswordRequest,
    AuthForgotPasswordRequest,
    AuthLoginRequest,
    AuthRegisterRequest,
    AuthResetPasswordRequest,
    AuthTokenRequest,
    CatalogRequest,
    CredentialProfileCreate,
    CredentialProfilePatch,
    CredentialProfileReplace,
    EvaluationDatasetMetadata,
    EvaluationDatasetRevisionView,
    EvaluationDatasetView,
    EvaluationRunItemView,
    EvaluationRunPreviewView,
    EvaluationRunView,
    EvaluationScorerView,
    EvaluationUploadPreviewView,
    EvaluationDatasetPatch,
    EvaluationRunCreate,
    EvaluationRunPreview,
    MonitorPolicyCreate,
    MonitorPolicyPatch,
    PartnerApplicationCreate,
    PartnerApplicationPatch,
    ProbeBatchCreate,
    ProviderDetectRequest,
    RunCreate,
    SuiteCreate,
    SuitePatch,
    TargetCreate,
    TargetPatch,
)
from .auth import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    AuthConfiguration,
    AuthMode,
    PasswordManager,
    issue_one_time_secret,
    safe_return_to,
    secret_matches,
)
from .auth_http import (
    PREAUTH_CSRF_COOKIE,
    AuthDeliveryUnavailable,
    AuthMailer,
    PreAuthCsrf,
    SmtpAuthMailer,
)
from .auth_service import (
    AuthenticationRequired,
    AuthenticationService,
    AuthRateLimited,
    AuthRejected,
    CsrfRejected,
)
from .control_service import (
    ControlPlaneService,
    DisabledTemporalLauncher,
    TemporalUnavailable,
)
from .control_contracts import ControlPlaneConflict, ControlPlaneNotFound
from .credential_service import CredentialProfileService
from .evaluation_service import (
    EvaluationService,
    EvaluationUnavailable,
    UnavailableEvaluationService,
)


class SSEProtocolError(ValueError):
    pass


EVALUATION_FILE_REQUEST_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {"type": "string", "format": "binary"},
                    },
                }
            }
        },
    }
}
EVALUATION_DATASET_REQUEST_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file", "metadata"],
                    "properties": {
                        "file": {"type": "string", "format": "binary"},
                        "metadata": {
                            "type": "string",
                            "description": "EvaluationDatasetMetadata JSON",
                        },
                    },
                }
            }
        },
    }
}


class ControlOperationLeaseMiddleware:
    """Hold the store lifecycle lease through the final ASGI response body."""

    def __init__(self, app: Any, *, service: ControlPlaneService) -> None:
        self._app = app
        self._service = service

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        try:
            with self._service.operation():
                await self._app(scope, receive, send)
        except ControlPlaneConflict as exc:
            request_id = str(uuid4())
            for key, value in scope.get("headers", ()):
                if key.lower() == b"x-request-id":
                    request_id = value.decode("utf-8", errors="replace")
                    break
            response = JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "error": {
                        "code": "CONFLICT",
                        "message": str(exc),
                        "details": [],
                        "request_id": request_id,
                    }
                },
                headers={
                    "X-Request-ID": request_id,
                    "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer",
                    "X-Frame-Options": "DENY",
                },
            )
            await response(scope, receive, send)


def create_app(
    *,
    service: ControlPlaneService | None = None,
    postgres_dsn: str | None = None,
    suite_path: str | Path | None = None,
    frontend_path: str | Path | None = None,
    auth_configuration: AuthConfiguration | None = None,
    authentication: AuthenticationService | None = None,
    auth_mailer: AuthMailer | None = None,
    credential_profiles: CredentialProfileService | None = None,
    credential_binder: ExecutionCredentialBinder | None = None,
    evaluations: EvaluationService | UnavailableEvaluationService | None = None,
) -> FastAPI:
    owns_service = service is None
    owns_evaluations = evaluations is None
    if suite_path is None:
        suite_path = _default_suite_path()
    auth_configuration = auth_configuration or AuthConfiguration.from_values(
        auth_mode=os.environ.get("LEXSOND_AUTH_MODE"),
        listen_host=os.environ.get("LEXSOND_LISTEN_HOST", "127.0.0.1"),
        cookie_secure=os.environ.get("LEXSOND_COOKIE_SECURE"),
    )
    if service is None:
        dsn = postgres_dsn or os.environ.get("LEXSOND_POSTGRES_DSN")
        if not dsn:
            raise ValueError(
                "LEXSOND_POSTGRES_DSN is required; PostgreSQL is the only "
                "persistent control store"
            )
        try:
            from .postgres_control_store import PostgresControlPlaneStore
        except ModuleNotFoundError as exc:
            if exc.name and (
                exc.name.startswith("psycopg") or exc.name == "psycopg_pool"
            ):
                raise ModuleNotFoundError(
                    "PostgreSQL support requires: pip install -e '.[production]'"
                ) from exc
            raise

        control_store = PostgresControlPlaneStore.from_dsn(dsn)
        temporal_launcher: Any = DisabledTemporalLauncher()
        try:
            from .temporal_backend import temporal_launcher_from_environment

            temporal_launcher = (
                temporal_launcher_from_environment(postgres_dsn=dsn)
                or temporal_launcher
            )
            service = ControlPlaneService(
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
        except BaseException:
            with suppress(Exception):
                temporal_launcher.close()
            with suppress(Exception):
                control_store.close()
            raise

    auth_store = (
        service.store.authentication_store()
        if hasattr(service.store, "authentication_store")
        else None
    )
    local_principal: dict[str, Any] | None = None
    local_csrf_raw: str | None = None
    local_csrf_hash: bytes | None = None
    if auth_configuration.mode is AuthMode.LOCAL_SINGLE_USER:
        if auth_store is None:
            raise ValueError("local-single-user requires the PostgreSQL auth store")
        local_principal = auth_store.ensure_local_principal()
        local_secret, local_csrf_hash = issue_one_time_secret()
        local_csrf_raw = local_secret.consume()
    elif authentication is None:
        if auth_store is None:
            raise ValueError("required authentication needs the PostgreSQL auth store")
        passwords = PasswordManager()
        authentication = AuthenticationService(
            store=auth_store,
            password_manager=passwords,
            dummy_password_hash=passwords.hash(secrets.token_urlsafe(32)),
        )
    auth_mailer = auth_mailer or SmtpAuthMailer.from_environment()
    preauth_csrf = PreAuthCsrf()
    credential_binder = credential_binder or ExecutionCredentialBinder.from_environment(
        require_configured=auth_configuration.mode is AuthMode.REQUIRED
    )
    if credential_profiles is None:
        if auth_configuration.mode is AuthMode.LOCAL_SINGLE_USER:
            try:
                credential_vault: Any = SystemCredentialVault()
            except VaultUnavailable as exc:
                credential_vault = UnavailableCredentialVault(str(exc))
            credential_backend = "SYSTEM_KEYRING"
        else:
            credential_vault = UnavailableCredentialVault(
                "云端 Secret Manager 尚未配置，已禁用 API Key 持久化"
            )
            credential_backend = "EXTERNAL_SECRET_MANAGER"
        credential_profiles = CredentialProfileService(
            store=service.store,
            vault=credential_vault,
            storage_backend=credential_backend,
        )
    if evaluations is None:
        factory = getattr(service.store, "evaluation_store", None)
        if callable(factory):
            evaluations = EvaluationService(
                store=factory(),
                submit_background=service._submit_background,
                maintenance_interval_seconds=30.0,
            )
        else:
            evaluations = UnavailableEvaluationService()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        close_error: BaseException | None = None
        if owns_evaluations:
            evaluation_close = getattr(evaluations, "close", None)
            if callable(evaluation_close):
                try:
                    evaluation_close()
                except BaseException as exc:
                    close_error = exc
        if owns_service:
            try:
                service.close()
            except BaseException as exc:
                if close_error is None:
                    close_error = exc
        if close_error is not None:
            raise close_error

    app = FastAPI(
        title="Lexsond API",
        version="0.8.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        lifespan=lifespan,
        responses={
            code: {"model": ApiErrorEnvelope, "description": "Lexsond error envelope"}
            for code in (400, 401, 402, 403, 404, 409, 422, 429, 500, 502, 503)
        },
    )
    app.state.service = service
    app.state.auth_configuration = auth_configuration
    app.state.authentication = authentication
    app.state.credential_profiles = credential_profiles
    app.state.evaluations = evaluations
    app.add_middleware(ControlOperationLeaseMiddleware, service=service)

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

    @app.middleware("http")
    async def authentication_boundary(request: Request, call_next: Any):
        path = request.url.path
        if not path.startswith("/api/v1/") or path == "/api/v1/health":
            return await call_next(request)

        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        request.state.request_id = request_id
        public_auth_writes = {
            "/api/v1/auth/register",
            "/api/v1/auth/login",
            "/api/v1/auth/verify-email",
            "/api/v1/auth/forgot-password",
            "/api/v1/auth/reset-password",
        }
        if path == "/api/v1/auth/csrf" and request.method == "GET":
            return await call_next(request)
        if path in public_auth_writes:
            if request.method != "POST":
                return await call_next(request)
            try:
                preauth_csrf.verify(
                    raw_header=request.headers.get(CSRF_HEADER_NAME),
                    cookie_hash=request.cookies.get(PREAUTH_CSRF_COOKIE),
                )
            except CsrfRejected as exc:
                return _error(
                    request,
                    status.HTTP_403_FORBIDDEN,
                    "CSRF_REJECTED",
                    str(exc),
                )
            return await call_next(request)

        if auth_configuration.mode is AuthMode.LOCAL_SINGLE_USER:
            assert local_principal is not None and local_csrf_hash is not None
            principal = local_principal
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                raw_csrf = request.headers.get(CSRF_HEADER_NAME)
                if not raw_csrf or not secret_matches(raw_csrf, local_csrf_hash):
                    return _error(
                        request,
                        status.HTTP_403_FORBIDDEN,
                        "CSRF_REJECTED",
                        "CSRF 校验失败",
                    )
        else:
            assert authentication is not None
            try:
                principal = authentication.resolve_principal(
                    request.cookies.get(SESSION_COOKIE_NAME)
                )
                if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                    authentication.require_csrf(
                        principal, request.headers.get(CSRF_HEADER_NAME)
                    )
            except CsrfRejected as exc:
                return _error(
                    request,
                    status.HTTP_403_FORBIDDEN,
                    "CSRF_REJECTED",
                    str(exc),
                )
            except AuthenticationRequired as exc:
                response = _error(
                    request,
                    status.HTTP_401_UNAUTHORIZED,
                    "AUTHENTICATION_REQUIRED",
                    str(exc),
                )
                response.delete_cookie(SESSION_COOKIE_NAME, path="/")
                return response
        if (
            request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and not path.startswith("/api/v1/auth/")
            and not _workspace_write_allowed(
                str(principal.get("workspace_role") or ""), request.method, path
            )
        ):
            return _error(
                request,
                status.HTTP_403_FORBIDDEN,
                "WORKSPACE_PERMISSION_DENIED",
                "当前工作区角色无权执行此操作",
            )
        if (
            request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and principal.get("email_verified_at") is None
            and not path.startswith("/api/v1/auth/")
        ):
            return _error(
                request,
                status.HTTP_403_FORBIDDEN,
                "EMAIL_VERIFICATION_REQUIRED",
                "验证邮箱后才能修改工作区资源或发起探测",
            )
        request.state.principal = principal
        request.state.workspace_store = service.store.for_workspace(
            str(principal["workspace_id"])
        )
        return await call_next(request)

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

    @app.exception_handler(AuthDeliveryUnavailable)
    async def auth_delivery_unavailable(
        request: Request, exc: AuthDeliveryUnavailable
    ):
        return _error(
            request,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "AUTH_DELIVERY_UNAVAILABLE",
            str(exc),
        )

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

    @app.exception_handler(AuthRejected)
    async def auth_rejected(request: Request, exc: AuthRejected):
        return _error(
            request,
            status.HTTP_401_UNAUTHORIZED,
            "AUTH_REJECTED",
            str(exc),
        )

    @app.exception_handler(AuthRateLimited)
    async def auth_rate_limited(request: Request, exc: AuthRateLimited):
        response = _error(
            request,
            status.HTTP_429_TOO_MANY_REQUESTS,
            "AUTH_RATE_LIMITED",
            str(exc),
        )
        response.headers["Retry-After"] = str(exc.retry_after_seconds)
        return response

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

    @app.exception_handler(DatasetValidationError)
    async def dataset_validation_error(request: Request, exc: DatasetValidationError):
        details = []
        if exc.line_number is not None or exc.field is not None:
            details.append({"line": exc.line_number, "field": exc.field})
        return _error(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            exc.reason_code,
            str(exc),
            details,
        )

    @app.exception_handler(EvaluationUnavailable)
    async def evaluation_unavailable(request: Request, exc: EvaluationUnavailable):
        return _error(
            request,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "EVALUATION_UNAVAILABLE",
            str(exc),
        )

    @app.exception_handler(VaultUnavailable)
    async def credential_vault_unavailable(request: Request, exc: VaultUnavailable):
        return _error(
            request,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "CREDENTIAL_VAULT_UNAVAILABLE",
            str(exc),
        )

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

    @app.get("/api/v1/auth/csrf")
    def auth_csrf(response: Response) -> dict[str, Any]:
        secret, cookie_hash = preauth_csrf.issue()
        raw = secret.consume()
        response.set_cookie(
            PREAUTH_CSRF_COOKIE,
            cookie_hash,
            max_age=600,
            httponly=True,
            secure=auth_configuration.cookie_secure,
            samesite="lax",
            path="/api/v1/auth",
        )
        response.headers["Cache-Control"] = "no-store"
        return {"data": {"csrf_token": raw, "expires_in": 600}}

    @app.post("/api/v1/auth/register", status_code=status.HTTP_202_ACCEPTED)
    def register(payload: AuthRegisterRequest, request: Request) -> dict[str, Any]:
        if auth_configuration.mode is AuthMode.LOCAL_SINGLE_USER:
            raise ControlPlaneConflict("本地单用户模式不提供注册")
        if auth_mailer is None:
            raise AuthDeliveryUnavailable("邮件发送服务尚未配置")
        assert authentication is not None
        delivery = authentication.register(
            email=payload.email,
            password=payload.password.get_secret_value(),
            display_name=payload.display_name,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("User-Agent"),
        )
        auth_mailer.send_verification(
            email=str(delivery.user["email"]),
            secret=delivery.verification_secret.consume(),
        )
        return {
            "data": {
                "status": "PENDING_VERIFICATION",
                "message": "验证邮件已发送",
            }
        }

    @app.post("/api/v1/auth/login")
    def login(
        payload: AuthLoginRequest, request: Request, response: Response
    ) -> dict[str, Any]:
        if auth_configuration.mode is AuthMode.LOCAL_SINGLE_USER:
            raise ControlPlaneConflict("本地单用户模式无需登录")
        assert authentication is not None
        grant = authentication.login(
            email=payload.email,
            password=payload.password.get_secret_value(),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("User-Agent"),
        )
        session_secret = grant.session_secret.consume()
        csrf_secret = grant.csrf_secret.consume()
        response.set_cookie(
            SESSION_COOKIE_NAME,
            session_secret,
            max_age=auth_configuration.session_absolute_seconds,
            httponly=True,
            secure=auth_configuration.cookie_secure,
            samesite="lax",
            path="/",
        )
        response.delete_cookie(PREAUTH_CSRF_COOKIE, path="/api/v1/auth")
        response.headers["Cache-Control"] = "no-store"
        return {
            "data": {
                "user": dict(grant.user),
                "session": dict(grant.session),
                "csrf_token": csrf_secret,
                "return_to": safe_return_to(payload.return_to),
                "auth_mode": auth_configuration.mode.value,
            }
        }

    @app.post("/api/v1/auth/verify-email")
    def verify_email(payload: AuthTokenRequest, request: Request) -> dict[str, Any]:
        if auth_configuration.mode is AuthMode.LOCAL_SINGLE_USER:
            raise ControlPlaneConflict("本地单用户模式无需验证邮箱")
        assert authentication is not None
        user = authentication.verify_email(
            payload.token.get_secret_value(),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("User-Agent"),
        )
        return {"data": {"status": user["status"]}}

    @app.post(
        "/api/v1/auth/forgot-password", status_code=status.HTTP_202_ACCEPTED
    )
    def forgot_password(
        payload: AuthForgotPasswordRequest,
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        if auth_configuration.mode is AuthMode.LOCAL_SINGLE_USER:
            raise ControlPlaneConflict("本地单用户模式无需重置密码")
        if auth_mailer is None:
            raise AuthDeliveryUnavailable("邮件发送服务尚未配置")
        assert authentication is not None
        delivery = authentication.request_password_reset(
            payload.email,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("User-Agent"),
        )
        if delivery is not None:
            background_tasks.add_task(
                _deliver_password_reset_safely,
                auth_mailer,
                delivery.email,
                delivery.reset_secret.consume(),
            )
        return {
            "data": {
                "status": "ACCEPTED",
                "message": "如果账号存在，密码重置邮件已发送",
            }
        }

    @app.post("/api/v1/auth/reset-password")
    def reset_password(
        payload: AuthResetPasswordRequest, request: Request, response: Response
    ) -> dict[str, Any]:
        if auth_configuration.mode is AuthMode.LOCAL_SINGLE_USER:
            raise ControlPlaneConflict("本地单用户模式无需重置密码")
        assert authentication is not None
        authentication.reset_password(
            payload.token.get_secret_value(),
            payload.new_password.get_secret_value(),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("User-Agent"),
        )
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        response.headers["Cache-Control"] = "no-store"
        return {"data": {"status": "PASSWORD_RESET"}}

    @app.post("/api/v1/auth/resend-verification")
    def resend_verification(request: Request) -> dict[str, Any]:
        if auth_configuration.mode is AuthMode.LOCAL_SINGLE_USER:
            raise ControlPlaneConflict("本地单用户模式无需验证邮箱")
        if auth_mailer is None:
            raise AuthDeliveryUnavailable("邮件发送服务尚未配置")
        assert authentication is not None
        delivery = authentication.resend_verification(
            request.state.principal,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("User-Agent"),
        )
        auth_mailer.send_verification(
            email=delivery.email,
            secret=delivery.verification_secret.consume(),
        )
        return {"data": {"status": "PENDING_VERIFICATION"}}

    @app.post("/api/v1/auth/change-password")
    def change_password(
        payload: AuthChangePasswordRequest, request: Request, response: Response
    ) -> dict[str, Any]:
        if auth_configuration.mode is AuthMode.LOCAL_SINGLE_USER:
            raise ControlPlaneConflict("本地单用户模式没有账号密码")
        assert authentication is not None
        revoked = authentication.change_password(
            request.state.principal,
            current_password=payload.current_password.get_secret_value(),
            new_password=payload.new_password.get_secret_value(),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("User-Agent"),
        )
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        response.headers["Cache-Control"] = "no-store"
        return {
            "data": {
                "status": "PASSWORD_CHANGED",
                "revoked_sessions": revoked,
            }
        }

    @app.get("/api/v1/auth/session")
    def auth_session(request: Request, response: Response) -> dict[str, Any]:
        if auth_configuration.mode is AuthMode.LOCAL_SINGLE_USER:
            assert local_csrf_raw is not None and local_principal is not None
            value = {
                "user": _public_principal(local_principal),
                "csrf_token": local_csrf_raw,
                "auth_mode": auth_configuration.mode.value,
            }
        else:
            assert authentication is not None
            resumed = authentication.resume_session(
                request.cookies.get(SESSION_COOKIE_NAME)
            )
            value = {
                "user": dict(resumed.principal),
                "csrf_token": resumed.csrf_secret.consume(),
                "auth_mode": auth_configuration.mode.value,
            }
        response.headers["Cache-Control"] = "no-store"
        return {"data": value}

    @app.post("/api/v1/auth/logout")
    def logout(request: Request, response: Response) -> dict[str, Any]:
        if authentication is not None:
            authentication.logout(request.cookies.get(SESSION_COOKIE_NAME))
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        response.headers["Cache-Control"] = "no-store"
        return {"data": {"status": "LOGGED_OUT"}}

    @app.post("/api/v1/auth/logout-all")
    def logout_all(request: Request, response: Response) -> dict[str, Any]:
        if auth_configuration.mode is AuthMode.LOCAL_SINGLE_USER:
            raise ControlPlaneConflict("本地单用户模式没有可撤销的登录会话")
        assert authentication is not None
        revoked = authentication.logout_all(
            request.state.principal,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("User-Agent"),
        )
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        response.headers["Cache-Control"] = "no-store"
        return {"data": {"status": "LOGGED_OUT", "revoked_sessions": revoked}}

    @app.get("/api/v1/auth/sessions")
    def list_auth_sessions(
        request: Request, limit: int = Query(default=50, ge=1, le=100)
    ) -> dict[str, Any]:
        if auth_configuration.mode is AuthMode.LOCAL_SINGLE_USER:
            return {"data": [], "meta": {"total": 0, "limit": limit}}
        assert authentication is not None
        values = authentication.list_sessions(request.state.principal, limit=limit)
        return {"data": values, "meta": {"total": len(values), "limit": limit}}

    @app.delete("/api/v1/auth/sessions/{session_id}")
    def revoke_auth_session(
        session_id: str, request: Request, response: Response
    ) -> dict[str, Any]:
        if auth_configuration.mode is AuthMode.LOCAL_SINGLE_USER:
            raise ControlPlaneConflict("本地单用户模式没有可撤销的登录会话")
        assert authentication is not None
        revoked_current = authentication.revoke_session(
            request.state.principal,
            session_id,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("User-Agent"),
        )
        if revoked_current:
            response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        response.headers["Cache-Control"] = "no-store"
        return {
            "data": {
                "status": "REVOKED",
                "session_id": session_id,
                "current": revoked_current,
            }
        }

    @app.get("/api/v1/bootstrap")
    def bootstrap(request: Request) -> dict[str, Any]:
        return {
            "data": service.bootstrap(
                workspace_id=str(request.state.principal["workspace_id"])
            )
        }

    @app.get("/api/v1/providers")
    def providers() -> dict[str, Any]:
        return {"data": public_providers()}

    @app.get("/api/v1/credential-vault/status")
    def credential_vault_status() -> dict[str, Any]:
        assert credential_profiles is not None
        return {"data": credential_profiles.status()}

    @app.get("/api/v1/credential-profiles")
    def list_credential_profiles(
        request: Request, include_archived: bool = False
    ) -> dict[str, Any]:
        assert credential_profiles is not None
        values = credential_profiles.list(
            workspace_id=str(request.state.principal["workspace_id"]),
            include_archived=include_archived,
        )
        return {"data": values, "meta": {"total": len(values)}}

    @app.post(
        "/api/v1/credential-profiles", status_code=status.HTTP_201_CREATED
    )
    def create_credential_profile(
        payload: CredentialProfileCreate,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        assert credential_profiles is not None
        principal = request.state.principal
        return {
            "data": credential_profiles.create(
                workspace_id=str(principal["workspace_id"]),
                actor_user_id=str(principal["user_id"]),
                label=payload.label,
                provider_id=payload.provider_id,
                api_key=payload.api_key,
                idempotency_key=idempotency_key,
            )
        }

    @app.get("/api/v1/credential-profiles/{credential_id}")
    def get_credential_profile(
        credential_id: str,
        request: Request,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        assert credential_profiles is not None
        return {
            "data": credential_profiles.get(
                credential_id,
                workspace_id=str(request.state.principal["workspace_id"]),
                include_archived=include_archived,
            )
        }

    @app.patch("/api/v1/credential-profiles/{credential_id}")
    def rename_credential_profile(
        credential_id: str,
        payload: CredentialProfilePatch,
        request: Request,
    ) -> dict[str, Any]:
        assert credential_profiles is not None
        principal = request.state.principal
        return {
            "data": credential_profiles.rename(
                credential_id,
                workspace_id=str(principal["workspace_id"]),
                actor_user_id=str(principal["user_id"]),
                label=payload.label,
                version=payload.version,
            )
        }

    @app.post("/api/v1/credential-profiles/{credential_id}/replace")
    def replace_credential_profile(
        credential_id: str,
        payload: CredentialProfileReplace,
        request: Request,
    ) -> dict[str, Any]:
        assert credential_profiles is not None
        principal = request.state.principal
        return {
            "data": credential_profiles.replace(
                credential_id,
                workspace_id=str(principal["workspace_id"]),
                actor_user_id=str(principal["user_id"]),
                api_key=payload.api_key,
                version=payload.version,
            )
        }

    @app.delete("/api/v1/credential-profiles/{credential_id}")
    def archive_credential_profile(
        credential_id: str,
        request: Request,
        version: int = Query(ge=1),
    ) -> dict[str, Any]:
        assert credential_profiles is not None
        principal = request.state.principal
        return {
            "data": credential_profiles.archive(
                credential_id,
                workspace_id=str(principal["workspace_id"]),
                actor_user_id=str(principal["user_id"]),
                version=version,
            )
        }

    @app.get("/api/v1/partner-applications")
    def list_partner_applications(
        request: Request, limit: int = Query(default=50, ge=1, le=100)
    ) -> dict[str, Any]:
        values = request.state.workspace_store.list_partner_applications(limit=limit)
        return {"data": values, "meta": {"total": len(values), "limit": limit}}

    @app.post(
        "/api/v1/partner-applications", status_code=status.HTTP_201_CREATED
    )
    def create_partner_application(
        payload: PartnerApplicationCreate,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        return {
            "data": service.create_partner_application(
                payload,
                idempotency_key=idempotency_key,
                workspace_id=str(request.state.principal["workspace_id"]),
            )
        }

    @app.get("/api/v1/partner-applications/{application_id}")
    def get_partner_application(
        application_id: str, request: Request
    ) -> dict[str, Any]:
        return {
            "data": request.state.workspace_store.get_partner_application(
                application_id
            )
        }

    @app.patch("/api/v1/partner-applications/{application_id}")
    def update_partner_application(
        application_id: str,
        payload: PartnerApplicationPatch,
        request: Request,
    ) -> dict[str, Any]:
        return {
            "data": service.update_partner_application(
                application_id,
                payload,
                workspace_id=str(request.state.principal["workspace_id"]),
            )
        }

    @app.post("/api/v1/partner-applications/{application_id}/submit")
    def submit_partner_application(
        application_id: str,
        request: Request,
        version: int = Query(ge=1),
    ) -> dict[str, Any]:
        return {
            "data": request.state.workspace_store.submit_partner_application(
                application_id, expected_version=version
            )
        }

    @app.post("/api/v1/providers/detect")
    def detect_provider(payload: ProviderDetectRequest) -> dict[str, Any]:
        return {
            "data": service.detect_provider(
                payload.api_key.get_secret_value(), payload.provider_id
            )
        }

    @app.get("/api/v1/agent/bootstrap")
    def agent_bootstrap(request: Request) -> dict[str, Any]:
        return {
            "data": service.agent_for_workspace(
                str(request.state.principal["workspace_id"])
            ).bootstrap()
        }

    @app.get("/api/v1/agent/tools")
    def agent_tools(request: Request) -> dict[str, Any]:
        values = service.agent_for_workspace(
            str(request.state.principal["workspace_id"])
        ).bootstrap()["tools"]
        return {"data": values, "meta": {"total": len(values)}}

    @app.get("/api/v1/agent/skills")
    def agent_skills(request: Request) -> dict[str, Any]:
        values = service.agent_for_workspace(
            str(request.state.principal["workspace_id"])
        ).bootstrap()["skills"]
        return {"data": values, "meta": {"total": len(values)}}

    @app.get("/api/v1/agent/sessions")
    def list_agent_sessions(
        request: Request,
        include_archived: bool = False,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        values = request.state.workspace_store.list_agent_sessions(
            include_archived=include_archived,
            limit=limit,
        )
        return {"data": values, "meta": {"total": len(values), "limit": limit}}

    @app.post(
        "/api/v1/agent/sessions",
        status_code=status.HTTP_201_CREATED,
    )
    def create_agent_session(
        payload: AgentSessionCreate, request: Request
    ) -> dict[str, Any]:
        return {
            "data": service.agent_for_workspace(
                str(request.state.principal["workspace_id"])
            ).create_session(
                title=payload.title,
                target_id=str(payload.target_id),
                model=payload.model,
                skill_id=payload.skill_id,
            )
        }

    @app.get("/api/v1/agent/sessions/{session_id}")
    def get_agent_session(
        session_id: str, request: Request, include_archived: bool = False
    ) -> dict[str, Any]:
        return {
            "data": request.state.workspace_store.get_agent_session(
                session_id, include_archived=include_archived
            )
        }

    @app.patch("/api/v1/agent/sessions/{session_id}")
    def update_agent_session(
        session_id: str, payload: AgentSessionPatch, request: Request
    ) -> dict[str, Any]:
        return {
            "data": service.agent_for_workspace(
                str(request.state.principal["workspace_id"])
            ).update_session(
                session_id,
                version=payload.version,
                title=payload.title if "title" in payload.model_fields_set else None,
                skill_id=(
                    payload.skill_id if "skill_id" in payload.model_fields_set else None
                ),
            )
        }

    @app.delete("/api/v1/agent/sessions/{session_id}")
    def archive_agent_session(session_id: str, request: Request) -> dict[str, Any]:
        return {"data": request.state.workspace_store.archive_agent_session(session_id)}

    @app.post("/api/v1/agent/sessions/{session_id}/restore")
    def restore_agent_session(session_id: str, request: Request) -> dict[str, Any]:
        return {"data": request.state.workspace_store.restore_agent_session(session_id)}

    @app.delete(
        "/api/v1/agent/sessions/{session_id}/purge",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def purge_agent_session(session_id: str, request: Request) -> None:
        request.state.workspace_store.purge_agent_session(session_id)

    @app.get("/api/v1/agent/sessions/{session_id}/messages")
    def list_agent_messages(
        session_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=100),
    ) -> dict[str, Any]:
        values = request.state.workspace_store.list_agent_messages(
            session_id, limit=limit
        )
        return {"data": values, "meta": {"total": len(values), "limit": limit}}

    @app.post("/api/v1/agent/sessions/{session_id}/messages")
    def create_agent_message(
        session_id: str, payload: AgentMessageCreate, request: Request
    ) -> dict[str, Any]:
        return {
            "data": service.agent_for_workspace(
                str(request.state.principal["workspace_id"])
            ).respond(
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
        request: Request,
        after_sequence: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        values = request.state.workspace_store.list_agent_events(
            session_id, after_sequence=after_sequence
        )
        return {"data": values, "meta": {"total": len(values)}}

    @app.get("/api/v1/targets")
    def list_targets(request: Request, include_archived: bool = False) -> dict[str, Any]:
        values = request.state.workspace_store.list_targets(
            include_archived=include_archived
        )
        return {"data": values, "meta": {"total": len(values)}}

    @app.post("/api/v1/targets", status_code=status.HTTP_201_CREATED)
    def create_target(payload: TargetCreate, request: Request) -> dict[str, Any]:
        return {
            "data": service.create_target(
                payload, workspace_id=str(request.state.principal["workspace_id"])
            )
        }

    @app.get("/api/v1/targets/{target_id}")
    def get_target(
        target_id: str, request: Request, include_archived: bool = False
    ) -> dict[str, Any]:
        return {
            "data": request.state.workspace_store.get_target(
                target_id, include_archived=include_archived
            )
        }

    @app.patch("/api/v1/targets/{target_id}")
    def update_target(
        target_id: str, payload: TargetPatch, request: Request
    ) -> dict[str, Any]:
        return {
            "data": service.update_target(
                target_id,
                payload,
                workspace_id=str(request.state.principal["workspace_id"]),
            )
        }

    @app.delete("/api/v1/targets/{target_id}")
    def archive_target(target_id: str, request: Request) -> dict[str, Any]:
        return {"data": request.state.workspace_store.archive_target(target_id)}

    @app.post("/api/v1/targets/{target_id}/restore")
    def restore_target(target_id: str, request: Request) -> dict[str, Any]:
        return {"data": request.state.workspace_store.restore_target(target_id)}

    @app.delete("/api/v1/targets/{target_id}/purge", status_code=status.HTTP_204_NO_CONTENT)
    def purge_target(target_id: str, request: Request) -> None:
        request.state.workspace_store.purge_target(target_id)

    @app.post("/api/v1/targets/{target_id}/catalog")
    def target_catalog(
        target_id: str, payload: CatalogRequest, request: Request
    ) -> dict[str, Any]:
        workspace_id = str(request.state.principal["workspace_id"])
        execution_secret = payload.api_key
        credential_version = None
        if payload.credential_profile_id is not None:
            assert credential_profiles is not None
            target = request.state.workspace_store.get_target(target_id)
            execution_secret = credential_profiles.get_for_execution(
                str(payload.credential_profile_id),
                workspace_id=workspace_id,
                provider_id=target.get("provider_id"),
            )
            credential_version = credential_profiles.get(
                str(payload.credential_profile_id), workspace_id=workspace_id
            )["version"]
        key = execution_secret.get_secret_value() if execution_secret else None
        credential_fingerprint = (
            credential_binder.fingerprint(
                execution_secret, workspace_id=workspace_id
            )
            if execution_secret is not None
            else None
        )
        return {
            "data": service.target_catalog(
                target_id,
                key,
                workspace_id=workspace_id,
                credential_profile_id=(
                    str(payload.credential_profile_id)
                    if payload.credential_profile_id is not None
                    else None
                ),
                credential_fingerprint=credential_fingerprint,
                credential_version=credential_version,
            )
        }

    @app.get("/api/v1/suites")
    def list_suites(request: Request, include_archived: bool = False) -> dict[str, Any]:
        values = request.state.workspace_store.list_suites(
            include_archived=include_archived
        )
        return {"data": values, "meta": {"total": len(values)}}

    @app.post("/api/v1/suites", status_code=status.HTTP_201_CREATED)
    def create_suite(payload: SuiteCreate, request: Request) -> dict[str, Any]:
        return {
            "data": service.create_suite(
                payload, workspace_id=str(request.state.principal["workspace_id"])
            )
        }

    @app.get("/api/v1/suites/{suite_id}")
    def get_suite(
        suite_id: str, request: Request, include_archived: bool = False
    ) -> dict[str, Any]:
        return {
            "data": request.state.workspace_store.get_suite(
                suite_id, include_archived=include_archived
            )
        }

    @app.patch("/api/v1/suites/{suite_id}")
    def update_suite(
        suite_id: str, payload: SuitePatch, request: Request
    ) -> dict[str, Any]:
        return {
            "data": service.update_suite(
                suite_id,
                payload,
                workspace_id=str(request.state.principal["workspace_id"]),
            )
        }

    @app.get("/api/v1/suites/{suite_id}/revisions")
    def suite_revisions(suite_id: str, request: Request) -> dict[str, Any]:
        values = request.state.workspace_store.list_suite_revisions(suite_id)
        return {"data": values, "meta": {"total": len(values)}}

    @app.delete("/api/v1/suites/{suite_id}")
    def archive_suite(suite_id: str, request: Request) -> dict[str, Any]:
        return {"data": request.state.workspace_store.archive_suite(suite_id)}

    @app.post("/api/v1/suites/{suite_id}/restore")
    def restore_suite(suite_id: str, request: Request) -> dict[str, Any]:
        return {"data": request.state.workspace_store.restore_suite(suite_id)}

    @app.delete("/api/v1/suites/{suite_id}/purge", status_code=status.HTTP_204_NO_CONTENT)
    def purge_suite(suite_id: str, request: Request) -> None:
        request.state.workspace_store.purge_suite(suite_id)

    @app.post(
        "/api/v1/evaluation-datasets/validate-upload",
        response_model=ApiDataEnvelope[EvaluationUploadPreviewView],
        openapi_extra=EVALUATION_FILE_REQUEST_BODY,
    )
    async def validate_evaluation_upload(
        request: Request,
        format: str = Query(pattern=r"^(jsonl|csv)$"),
        csv_mapping: str | None = Query(default=None, max_length=4096),
    ) -> dict[str, Any]:
        upload = await _read_evaluation_multipart(request, require_metadata=False)
        return {
            "data": evaluations.validate_upload(
                upload["file"], format, _parse_csv_mapping(csv_mapping)
            )
        }

    @app.get(
        "/api/v1/evaluation-datasets",
        response_model=ApiListEnvelope[EvaluationDatasetView],
    )
    def list_evaluation_datasets(
        request: Request,
        include_archived: bool = False,
        limit: int = Query(default=100, ge=1, le=100),
    ) -> dict[str, Any]:
        values = evaluations.list_datasets(
            workspace_id=str(request.state.principal["workspace_id"]),
            include_archived=include_archived,
            limit=limit,
        )
        return {"data": values, "meta": {"total": len(values), "limit": limit}}

    @app.post(
        "/api/v1/evaluation-datasets",
        status_code=status.HTTP_201_CREATED,
        response_model=ApiDataEnvelope[EvaluationDatasetView],
        openapi_extra=EVALUATION_DATASET_REQUEST_BODY,
    )
    async def create_evaluation_dataset(request: Request) -> dict[str, Any]:
        upload = await _read_evaluation_multipart(request, require_metadata=True)
        try:
            metadata = EvaluationDatasetMetadata.model_validate_json(upload["metadata"])
        except ValueError as exc:
            raise DatasetValidationError(
                "UPLOAD_METADATA_INVALID",
                "dataset metadata is not valid for the versioned contract",
                field="metadata",
            ) from exc
        principal = request.state.principal
        return {
            "data": evaluations.create_dataset(
                metadata,
                upload["file"],
                workspace_id=str(principal["workspace_id"]),
                actor_user_id=str(principal["user_id"]),
            )
        }

    @app.get(
        "/api/v1/evaluation-datasets/{dataset_id}",
        response_model=ApiDataEnvelope[EvaluationDatasetView],
    )
    def get_evaluation_dataset(
        dataset_id: str,
        request: Request,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        return {
            "data": evaluations.get_dataset(
                dataset_id,
                workspace_id=str(request.state.principal["workspace_id"]),
                include_archived=include_archived,
            )
        }

    @app.patch(
        "/api/v1/evaluation-datasets/{dataset_id}",
        response_model=ApiDataEnvelope[EvaluationDatasetView],
    )
    def update_evaluation_dataset(
        dataset_id: str,
        payload: EvaluationDatasetPatch,
        request: Request,
    ) -> dict[str, Any]:
        return {
            "data": evaluations.update_dataset(
                dataset_id,
                payload,
                workspace_id=str(request.state.principal["workspace_id"]),
            )
        }

    @app.delete(
        "/api/v1/evaluation-datasets/{dataset_id}",
        response_model=ApiDataEnvelope[EvaluationDatasetView],
    )
    def archive_evaluation_dataset(dataset_id: str, request: Request) -> dict[str, Any]:
        return {
            "data": evaluations.archive_dataset(
                dataset_id,
                workspace_id=str(request.state.principal["workspace_id"]),
            )
        }

    @app.post(
        "/api/v1/evaluation-datasets/{dataset_id}/restore",
        response_model=ApiDataEnvelope[EvaluationDatasetView],
    )
    def restore_evaluation_dataset(dataset_id: str, request: Request) -> dict[str, Any]:
        return {
            "data": evaluations.restore_dataset(
                dataset_id,
                workspace_id=str(request.state.principal["workspace_id"]),
            )
        }

    @app.delete(
        "/api/v1/evaluation-datasets/{dataset_id}/purge",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def purge_evaluation_dataset(dataset_id: str, request: Request) -> None:
        evaluations.purge_dataset(
            dataset_id,
            workspace_id=str(request.state.principal["workspace_id"]),
        )

    @app.get(
        "/api/v1/evaluation-datasets/{dataset_id}/revisions",
        response_model=ApiListEnvelope[EvaluationDatasetRevisionView],
    )
    def list_evaluation_revisions(
        dataset_id: str,
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        values = evaluations.list_revisions(
            dataset_id,
            workspace_id=str(request.state.principal["workspace_id"]),
            limit=limit,
        )
        return {"data": values, "meta": {"total": len(values), "limit": limit}}

    @app.get(
        "/api/v1/evaluation-datasets/{dataset_id}/revisions/{revision}",
        response_model=ApiDataEnvelope[EvaluationDatasetRevisionView],
    )
    def get_evaluation_revision(
        dataset_id: str,
        revision: int,
        request: Request,
    ) -> dict[str, Any]:
        return {
            "data": evaluations.get_revision(
                dataset_id,
                revision,
                workspace_id=str(request.state.principal["workspace_id"]),
            )
        }

    @app.post(
        "/api/v1/evaluation-datasets/{dataset_id}/revisions",
        status_code=status.HTTP_201_CREATED,
        response_model=ApiDataEnvelope[EvaluationDatasetRevisionView],
        openapi_extra=EVALUATION_FILE_REQUEST_BODY,
    )
    async def create_evaluation_revision(
        dataset_id: str,
        request: Request,
        format: str = Query(pattern=r"^(jsonl|csv)$"),
        csv_mapping: str | None = Query(default=None, max_length=4096),
    ) -> dict[str, Any]:
        upload = await _read_evaluation_multipart(request, require_metadata=False)
        principal = request.state.principal
        return {
            "data": evaluations.create_revision(
                dataset_id,
                upload["file"],
                format=format,
                csv_mapping=_parse_csv_mapping(csv_mapping),
                workspace_id=str(principal["workspace_id"]),
                actor_user_id=str(principal["user_id"]),
            )
        }

    @app.get(
        "/api/v1/evaluation-scorers",
        response_model=ApiListEnvelope[EvaluationScorerView],
    )
    def evaluation_scorers() -> dict[str, Any]:
        values = evaluations.scorer_catalog()
        return {"data": values, "meta": {"total": len(values)}}

    @app.post(
        "/api/v1/evaluation-runs/preview",
        response_model=ApiDataEnvelope[EvaluationRunPreviewView],
    )
    def preview_evaluation_run(
        payload: EvaluationRunPreview,
        request: Request,
    ) -> dict[str, Any]:
        return {
            "data": evaluations.preview_run(
                payload,
                workspace_id=str(request.state.principal["workspace_id"]),
            )
        }

    @app.post(
        "/api/v1/evaluation-runs",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=ApiDataEnvelope[EvaluationRunView],
    )
    def create_evaluation_run(
        payload: EvaluationRunCreate,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        principal = request.state.principal
        workspace_id = str(principal["workspace_id"])
        saved_secret = None
        credential_version = None
        if payload.credential_profile_id is not None:
            target = request.state.workspace_store.get_target(str(payload.channel_id))
            saved_secret = credential_profiles.get_for_execution(
                str(payload.credential_profile_id),
                workspace_id=workspace_id,
                provider_id=target.get("provider_id"),
            )
            credential_version = credential_profiles.get(
                str(payload.credential_profile_id), workspace_id=workspace_id
            )["version"]
        execution_secret = saved_secret or payload.api_key
        credential_fingerprint = (
            credential_binder.fingerprint(
                execution_secret, workspace_id=workspace_id
            )
            if execution_secret is not None
            else None
        )
        return {
            "data": evaluations.start_run(
                payload,
                workspace_id=workspace_id,
                actor_user_id=str(principal["user_id"]),
                idempotency_key=idempotency_key,
                api_key_override=saved_secret,
                credential_fingerprint=credential_fingerprint,
                credential_version=credential_version,
            )
        }

    @app.get(
        "/api/v1/evaluation-runs",
        response_model=ApiListEnvelope[EvaluationRunView],
    )
    def list_evaluation_runs(
        request: Request,
        include_archived: bool = False,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        values = evaluations.list_runs(
            workspace_id=str(request.state.principal["workspace_id"]),
            include_archived=include_archived,
            limit=limit,
        )
        return {"data": values, "meta": {"total": len(values), "limit": limit}}

    @app.get(
        "/api/v1/evaluation-runs/{run_id}",
        response_model=ApiDataEnvelope[EvaluationRunView],
    )
    def get_evaluation_run(
        run_id: str,
        request: Request,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        return {
            "data": evaluations.get_run(
                run_id,
                workspace_id=str(request.state.principal["workspace_id"]),
                include_archived=include_archived,
            )
        }

    @app.post(
        "/api/v1/evaluation-runs/{run_id}/cancel",
        response_model=ApiDataEnvelope[EvaluationRunView],
    )
    def cancel_evaluation_run(run_id: str, request: Request) -> dict[str, Any]:
        return {
            "data": evaluations.cancel_run(
                run_id,
                workspace_id=str(request.state.principal["workspace_id"]),
            )
        }

    @app.delete(
        "/api/v1/evaluation-runs/{run_id}",
        response_model=ApiDataEnvelope[EvaluationRunView],
    )
    def archive_evaluation_run(run_id: str, request: Request) -> dict[str, Any]:
        return {
            "data": evaluations.archive_run(
                run_id,
                workspace_id=str(request.state.principal["workspace_id"]),
            )
        }

    @app.post(
        "/api/v1/evaluation-runs/{run_id}/restore",
        response_model=ApiDataEnvelope[EvaluationRunView],
    )
    def restore_evaluation_run(run_id: str, request: Request) -> dict[str, Any]:
        return {
            "data": evaluations.restore_run(
                run_id,
                workspace_id=str(request.state.principal["workspace_id"]),
            )
        }

    @app.delete(
        "/api/v1/evaluation-runs/{run_id}/purge",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def purge_evaluation_run(run_id: str, request: Request) -> None:
        evaluations.purge_run(
            run_id,
            workspace_id=str(request.state.principal["workspace_id"]),
        )

    @app.get(
        "/api/v1/evaluation-runs/{run_id}/items",
        response_model=ApiListEnvelope[EvaluationRunItemView],
    )
    def evaluation_run_items(
        run_id: str,
        request: Request,
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=2000, ge=1, le=2000),
    ) -> dict[str, Any]:
        values = evaluations.list_run_items(
            run_id,
            workspace_id=str(request.state.principal["workspace_id"]),
            after_sequence=after_sequence,
            limit=limit,
        )
        return {"data": values, "meta": {"total": len(values), "limit": limit}}

    @app.get("/api/v1/evaluation-runs/{run_id}/events")
    async def evaluation_run_events(
        run_id: str,
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> EventSourceResponse:
        workspace_id = str(request.state.principal["workspace_id"])
        after_sequence = _parse_last_event_id(last_event_id)

        async def stream() -> AsyncIterator[ServerSentEvent]:
            cursor = after_sequence
            idle_rounds = 0
            while True:
                events = evaluations.list_run_events(
                    run_id,
                    workspace_id=workspace_id,
                    after_sequence=cursor,
                    limit=200,
                )
                for event in events:
                    cursor = int(event["sequence"])
                    yield ServerSentEvent(
                        data=json.dumps(event, ensure_ascii=False, separators=(",", ":")),
                        event=str(event["event_type"]).lower(),
                        id=str(cursor),
                    )
                run = evaluations.get_run(
                    run_id,
                    workspace_id=workspace_id,
                    include_archived=True,
                )
                if run["state"] != "RUNNING" and not events:
                    return
                idle_rounds += 1
                if idle_rounds >= 25:
                    idle_rounds = 0
                    yield ServerSentEvent(comment="keepalive")
                await asyncio.sleep(0.2)

        return EventSourceResponse(stream())

    @app.get("/api/v1/monitor-policies")
    def list_monitor_policies(
        request: Request, include_archived: bool = False
    ) -> dict[str, Any]:
        values = request.state.workspace_store.list_monitor_policies(
            include_archived=include_archived
        )
        return {"data": values, "meta": {"total": len(values)}}

    @app.post("/api/v1/monitor-policies", status_code=status.HTTP_201_CREATED)
    def create_monitor_policy(
        payload: MonitorPolicyCreate, request: Request
    ) -> dict[str, Any]:
        return {
            "data": service.create_monitor_policy(
                payload, workspace_id=str(request.state.principal["workspace_id"])
            )
        }

    @app.get("/api/v1/monitor-policies/{policy_id}")
    def get_monitor_policy(
        policy_id: str, request: Request, include_archived: bool = False
    ) -> dict[str, Any]:
        return {
            "data": request.state.workspace_store.get_monitor_policy(
                policy_id, include_archived=include_archived
            )
        }

    @app.patch("/api/v1/monitor-policies/{policy_id}")
    def update_monitor_policy(
        policy_id: str, payload: MonitorPolicyPatch, request: Request
    ) -> dict[str, Any]:
        return {
            "data": service.update_monitor_policy(
                policy_id,
                payload,
                workspace_id=str(request.state.principal["workspace_id"]),
            )
        }

    @app.delete("/api/v1/monitor-policies/{policy_id}")
    def archive_monitor_policy(policy_id: str, request: Request) -> dict[str, Any]:
        return {
            "data": request.state.workspace_store.archive_monitor_policy(policy_id)
        }

    @app.post("/api/v1/monitor-policies/{policy_id}/restore")
    def restore_monitor_policy(policy_id: str, request: Request) -> dict[str, Any]:
        return {
            "data": request.state.workspace_store.restore_monitor_policy(policy_id)
        }

    @app.delete(
        "/api/v1/monitor-policies/{policy_id}/purge",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def purge_monitor_policy(policy_id: str, request: Request) -> None:
        request.state.workspace_store.purge_monitor_policy(policy_id)

    @app.post("/api/v1/monitor-policies/{policy_id}/run-now")
    def run_monitor_policy_now(policy_id: str, request: Request) -> dict[str, Any]:
        return {
            "data": service.request_monitor_policy_run(
                policy_id,
                workspace_id=str(request.state.principal["workspace_id"]),
            )
        }

    @app.get("/api/v1/monitoring/overview")
    def monitoring_overview(
        request: Request,
        window: str = Query(default="24h", pattern=r"^(90m|24h|7d|30d)$"),
        include_archived: bool = False,
    ) -> dict[str, Any]:
        return {
            "data": request.state.workspace_store.monitoring_overview(
                window=window, include_archived=include_archived
            )
        }

    @app.get("/api/v1/monitoring/incidents")
    def monitoring_incidents(
        request: Request,
        policy_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        values = request.state.workspace_store.list_monitor_incidents(
            policy_id=policy_id, limit=limit
        )
        return {"data": values, "meta": {"total": len(values), "limit": limit}}

    @app.get("/api/v1/probe-batches")
    def list_probe_batches(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        values = request.state.workspace_store.list_probe_batches(limit=limit)
        return {"data": values, "meta": {"total": len(values), "limit": limit}}

    @app.post("/api/v1/probe-batches", status_code=status.HTTP_202_ACCEPTED)
    def create_probe_batch(
        payload: ProbeBatchCreate,
        request: Request,
        idempotency_key: UUID = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        workspace_id = str(request.state.principal["workspace_id"])
        execution_secret = None
        if payload.credential_profile_id is not None:
            assert credential_profiles is not None
            target = request.state.workspace_store.get_target(str(payload.target_id))
            execution_secret = credential_profiles.get_for_execution(
                str(payload.credential_profile_id),
                workspace_id=workspace_id,
                provider_id=target.get("provider_id"),
            )
        return {
            "data": service.start_probe_batch(
                payload,
                workspace_id=workspace_id,
                idempotency_key=str(idempotency_key),
                api_key_override=execution_secret,
            )
        }

    @app.get("/api/v1/probe-batches/{batch_id}")
    def get_probe_batch(batch_id: str, request: Request) -> dict[str, Any]:
        return {"data": request.state.workspace_store.get_probe_batch(batch_id)}

    @app.post("/api/v1/probe-batches/{batch_id}/cancel")
    def cancel_probe_batch(batch_id: str, request: Request) -> dict[str, Any]:
        return {
            "data": service.cancel_probe_batch(
                batch_id,
                workspace_id=str(request.state.principal["workspace_id"]),
            )
        }

    def batch_event_cursor(
        batch_id: str,
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> int:
        try:
            cursor = max(int(last_event_id or "0"), 0)
        except ValueError as exc:
            raise SSEProtocolError(
                "Last-Event-ID must be an integer sequence"
            ) from exc
        request.state.workspace_store.get_probe_batch(batch_id)
        return cursor

    @app.get(
        "/api/v1/probe-batches/{batch_id}/events",
        response_class=EventSourceResponse,
    )
    async def probe_batch_events(
        request: Request,
        batch_id: str,
        after_sequence: int = Depends(batch_event_cursor),
    ) -> AsyncIterator[ServerSentEvent]:
        cursor = after_sequence
        idle_rounds = 0
        while not await request.is_disconnected():
            events = request.state.workspace_store.list_probe_batch_events(
                batch_id, after_sequence=cursor
            )
            for event in events:
                cursor = event["sequence"]
                yield ServerSentEvent(
                    data=event,
                    event=event["event_type"].lower(),
                    id=str(cursor),
                )
            batch = request.state.workspace_store.get_probe_batch(batch_id)
            if batch["state"] != "RUNNING" and not events:
                return
            idle_rounds = idle_rounds + 1 if not events else 0
            if idle_rounds >= 25:
                idle_rounds = 0
                yield ServerSentEvent(comment="keepalive")
            await asyncio.sleep(0.2)

    @app.get("/api/v1/runs")
    def list_runs(
        request: Request,
        include_archived: bool = False,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        values = request.state.workspace_store.list_runs(
            include_archived=include_archived, limit=limit
        )
        return {"data": values, "meta": {"total": len(values), "limit": limit}}

    @app.post("/api/v1/runs", status_code=status.HTTP_202_ACCEPTED)
    def create_run(
        payload: RunCreate,
        request: Request,
        idempotency_key: UUID | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        workspace_id = str(request.state.principal["workspace_id"])
        execution_secret = None
        if payload.credential_profile_id is not None:
            assert credential_profiles is not None
            target = request.state.workspace_store.get_target(str(payload.target_id))
            execution_secret = credential_profiles.get_for_execution(
                str(payload.credential_profile_id),
                workspace_id=workspace_id,
                provider_id=target.get("provider_id"),
            )
        return {
            "data": service.start_run(
                payload,
                workspace_id=workspace_id,
                idempotency_key=str(idempotency_key) if idempotency_key else None,
                api_key_override=execution_secret,
            )
        }

    @app.get("/api/v1/runs/{run_id}")
    def get_run(
        run_id: str, request: Request, include_archived: bool = False
    ) -> dict[str, Any]:
        return {
            "data": request.state.workspace_store.get_run(
                run_id, include_archived=include_archived
            )
        }

    @app.post("/api/v1/runs/{run_id}/cancel")
    def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
        return {
            "data": service.cancel_run(
                run_id, workspace_id=str(request.state.principal["workspace_id"])
            )
        }

    @app.delete("/api/v1/runs/{run_id}")
    def archive_run(run_id: str, request: Request) -> dict[str, Any]:
        return {"data": request.state.workspace_store.archive_run(run_id)}

    @app.post("/api/v1/runs/{run_id}/restore")
    def restore_run(run_id: str, request: Request) -> dict[str, Any]:
        return {"data": request.state.workspace_store.restore_run(run_id)}

    @app.delete("/api/v1/runs/{run_id}/purge", status_code=status.HTTP_204_NO_CONTENT)
    def purge_run(run_id: str, request: Request) -> None:
        request.state.workspace_store.purge_run(run_id)

    def event_cursor(
        run_id: str,
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> int:
        try:
            cursor = max(int(last_event_id or "0"), 0)
        except ValueError as exc:
            raise SSEProtocolError(
                "Last-Event-ID must be an integer sequence"
            ) from exc
        request.state.workspace_store.get_run(run_id, include_archived=True)
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
            events = request.state.workspace_store.list_run_events(
                run_id, after_sequence=cursor
            )
            for event in events:
                cursor = event["sequence"]
                yield ServerSentEvent(
                    data=event,
                    event=event["event_type"].lower(),
                    id=str(cursor),
                )
            run = request.state.workspace_store.get_run(
                run_id, include_archived=True
            )
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


async def _read_evaluation_multipart(
    request: Request,
    *,
    require_metadata: bool,
) -> dict[str, bytes]:
    """Read one bounded multipart upload without an implicit temp-file spill."""

    content_type = request.headers.get("Content-Type", "")
    if (
        len(content_type) > 1024
        or "\r" in content_type
        or "\n" in content_type
        or not content_type.lower().startswith("multipart/form-data;")
    ):
        raise DatasetValidationError(
            "UPLOAD_CONTENT_TYPE_INVALID",
            "evaluation upload must use multipart/form-data",
        )
    request_limit = MAX_UPLOAD_BYTES + 64 * 1024
    declared = request.headers.get("Content-Length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError as exc:
            raise DatasetValidationError(
                "CONTENT_LENGTH_INVALID", "Content-Length must be an integer"
            ) from exc
        if declared_size > request_limit:
            raise DatasetValidationError("FILE_TOO_LARGE", "upload exceeds 10 MiB")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > request_limit:
            body.clear()
            raise DatasetValidationError("FILE_TOO_LARGE", "upload exceeds 10 MiB")
    try:
        content_type_bytes = content_type.encode("ascii")
    except UnicodeEncodeError as exc:
        raise DatasetValidationError(
            "UPLOAD_CONTENT_TYPE_INVALID", "multipart content type is invalid"
        ) from exc
    message = BytesParser(policy=email_policy.default).parsebytes(
        b"Content-Type: " + content_type_bytes + b"\r\nMIME-Version: 1.0\r\n\r\n" + bytes(body)
    )
    body.clear()
    if not message.is_multipart():
        raise DatasetValidationError(
            "MALFORMED_MULTIPART", "multipart upload could not be parsed"
        )
    parts: dict[str, bytes] = {}
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            raise DatasetValidationError(
                "MALFORMED_MULTIPART", "multipart part disposition is invalid"
            )
        name = part.get_param("name", header="content-disposition")
        if name not in {"file", "metadata"} or name in parts:
            raise DatasetValidationError(
                "MULTIPART_FIELDS_INVALID", "multipart fields must be unique file and metadata parts"
            )
        value = part.get_payload(decode=True)
        if not isinstance(value, bytes):
            raise DatasetValidationError(
                "MALFORMED_MULTIPART", "multipart part is not a byte payload"
            )
        if name == "file" and len(value) > MAX_UPLOAD_BYTES:
            raise DatasetValidationError("FILE_TOO_LARGE", "upload exceeds 10 MiB")
        if name == "metadata" and len(value) > 64 * 1024:
            raise DatasetValidationError(
                "METADATA_TOO_LARGE", "upload metadata exceeds 64 KiB"
            )
        if name == "file" and part.get_content_type() in {
            "text/html",
            "application/zip",
            "application/x-tar",
            "application/gzip",
        }:
            raise DatasetValidationError(
                "UPLOAD_MEDIA_TYPE_REJECTED",
                "HTML, archive, and executable upload types are not accepted",
            )
        parts[str(name)] = value
    if "file" not in parts or (require_metadata and "metadata" not in parts):
        raise DatasetValidationError(
            "MULTIPART_FIELDS_MISSING", "multipart upload is missing a required part"
        )
    if not require_metadata and "metadata" in parts:
        raise DatasetValidationError(
            "MULTIPART_FIELDS_INVALID", "validation upload accepts only the file part"
        )
    return parts


def _parse_csv_mapping(value: str | None) -> dict[str, str] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise DatasetValidationError(
            "CSV_MAPPING_INVALID", "CSV mapping must be a JSON object", field="mapping"
        ) from exc
    if not isinstance(parsed, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in parsed.items()
    ):
        raise DatasetValidationError(
            "CSV_MAPPING_INVALID", "CSV mapping must contain string fields", field="mapping"
        )
    return parsed


def _parse_last_event_id(value: str | None) -> int:
    if value is None or not value.strip():
        return 0
    try:
        sequence = int(value)
    except ValueError as exc:
        raise SSEProtocolError("Last-Event-ID must be a non-negative integer") from exc
    if sequence < 0:
        raise SSEProtocolError("Last-Event-ID must be a non-negative integer")
    return sequence


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


def _workspace_write_allowed(role: str, method: str, path: str) -> bool:
    if role in {"OWNER", "ADMIN"}:
        return True
    if role != "MEMBER" or method != "POST":
        return False
    if path in {
        "/api/v1/runs",
        "/api/v1/probe-batches",
        "/api/v1/evaluation-runs",
        "/api/v1/evaluation-runs/preview",
    }:
        return True
    if path.startswith("/api/v1/runs/") and path.endswith("/cancel"):
        return True
    if path.startswith("/api/v1/probe-batches/") and path.endswith("/cancel"):
        return True
    if path.startswith("/api/v1/evaluation-runs/") and path.endswith("/cancel"):
        return True
    return path.startswith("/api/v1/agent/")


def _deliver_password_reset_safely(
    mailer: AuthMailer, email: str, secret: str
) -> None:
    try:
        mailer.send_password_reset(email=email, secret=secret)
    except AuthDeliveryUnavailable:
        # The public forgot-password response must not reveal account existence.
        return


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
    parser.add_argument("--suite", type=Path, default=_default_suite_path())
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    os.environ["LEXSOND_LISTEN_HOST"] = args.host

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
        create_app(suite_path=args.suite),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
