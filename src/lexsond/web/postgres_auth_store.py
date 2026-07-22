from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg

from ..storage.postgres import PostgresPool
from .auth import LOCAL_USER_ID, LOCAL_WORKSPACE_ID
from .auth_service import AuthRejected
from .control_contracts import ControlPlaneConflict, ControlPlaneNotFound


class PostgresAuthStore:
    """Authentication repository; raw browser and action secrets never enter SQL."""

    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def ensure_local_principal(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Create the deterministic local identity without claiming legacy data."""

        timestamp = now or datetime.now(UTC)
        with self._pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO lexsond.users (
                    user_id, email_normalized, email_display, password_hash,
                    display_name, status, email_verified_at, created_at, updated_at
                ) VALUES (%s, 'local@lexsond.invalid', 'local@lexsond.invalid', NULL,
                          '本地用户', 'ACTIVE', %s, %s, %s)
                ON CONFLICT (user_id) DO NOTHING
                """,
                (LOCAL_USER_ID, timestamp, timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO lexsond.workspaces (
                    workspace_id, name, slug, workspace_kind, owner_user_id,
                    created_at, updated_at
                ) VALUES (%s, '本地个人工作区', 'local-personal-workspace',
                          'PERSONAL', %s, %s, %s)
                ON CONFLICT (workspace_id) DO NOTHING
                """,
                (LOCAL_WORKSPACE_ID, LOCAL_USER_ID, timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO lexsond.workspace_members (
                    workspace_id, user_id, role, created_at, updated_at
                ) VALUES (%s, %s, 'OWNER', %s, %s)
                ON CONFLICT (workspace_id, user_id) DO NOTHING
                """,
                (LOCAL_WORKSPACE_ID, LOCAL_USER_ID, timestamp, timestamp),
            )
        return {
            "session_id": None,
            "user_id": LOCAL_USER_ID,
            "workspace_id": LOCAL_WORKSPACE_ID,
            "workspace_name": "本地个人工作区",
            "workspace_role": "OWNER",
            "email": "local@lexsond.invalid",
            "email_normalized": "local@lexsond.invalid",
            "display_name": "本地用户",
            "avatar_url": None,
            "status": "ACTIVE",
            "system_role": "USER",
            "email_verified_at": timestamp.isoformat(),
            "auth_mode": "local-single-user",
        }

    def register_user(
        self,
        *,
        email_normalized: str,
        email_display: str,
        password_hash: str,
        display_name: str,
        verification_token_hash: bytes,
        verification_expires_at: datetime,
        now: datetime,
    ) -> dict[str, Any]:
        user_id = str(uuid4())
        workspace_id = str(uuid4())
        token_id = str(uuid4())
        workspace_slug = f"personal-{user_id.replace('-', '')[:20]}"
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO lexsond.users (
                        user_id, email_normalized, email_display, password_hash,
                        display_name, status, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, 'PENDING_VERIFICATION', %s, %s)
                    """,
                    (
                        user_id,
                        email_normalized,
                        email_display,
                        password_hash,
                        display_name,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO lexsond.workspaces (
                        workspace_id, name, slug, workspace_kind, owner_user_id,
                        created_at, updated_at
                    ) VALUES (%s, %s, %s, 'PERSONAL', %s, %s, %s)
                    """,
                    (
                        workspace_id,
                        f"{display_name} 的个人工作区",
                        workspace_slug,
                        user_id,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO lexsond.workspace_members (
                        workspace_id, user_id, role, created_at, updated_at
                    ) VALUES (%s, %s, 'OWNER', %s, %s)
                    """,
                    (workspace_id, user_id, now, now),
                )
                connection.execute(
                    """
                    INSERT INTO lexsond.auth_action_tokens (
                        token_id, user_id, purpose, token_hash, created_at, expires_at
                    ) VALUES (%s, %s, 'verify_email', %s, %s, %s)
                    """,
                    (
                        token_id,
                        user_id,
                        verification_token_hash,
                        now,
                        verification_expires_at,
                    ),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise ControlPlaneConflict("该邮箱无法注册") from exc
        return self._get_user(user_id, workspace_id=workspace_id)

    def find_login_user(self, email_normalized: str) -> dict[str, Any] | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT u.user_id, u.email_display AS email,
                       u.email_normalized, u.password_hash, u.display_name,
                       u.avatar_url, u.status, u.system_role,
                       u.email_verified_at, w.workspace_id, w.name AS workspace_name,
                       m.role AS workspace_role
                FROM lexsond.users u
                JOIN lexsond.workspaces w
                  ON w.owner_user_id = u.user_id
                 AND w.workspace_kind = 'PERSONAL'
                 AND w.deleted_at IS NULL
                JOIN lexsond.workspace_members m
                  ON m.workspace_id = w.workspace_id
                 AND m.user_id = u.user_id
                WHERE u.email_normalized = %s
                ORDER BY w.created_at, w.workspace_id
                LIMIT 1
                """,
                (email_normalized,),
            ).fetchone()
        return _auth_user(row) if row is not None else None

    def update_password_hash(self, user_id: str, password_hash: str) -> None:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE lexsond.users
                SET password_hash = %s, updated_at = clock_timestamp()
                WHERE user_id = %s AND status IN ('PENDING_VERIFICATION', 'ACTIVE')
                """,
                (password_hash, user_id),
            )
            if cursor.rowcount != 1:
                raise ControlPlaneNotFound("user was not found")

    def create_session(
        self,
        *,
        user_id: str,
        workspace_id: str,
        token_hash: bytes,
        csrf_secret_hash: bytes,
        user_agent_hash: bytes,
        ip_prefix: str | None,
        now: datetime,
        idle_expires_at: datetime,
        absolute_expires_at: datetime,
    ) -> dict[str, Any]:
        session_id = str(uuid4())
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO lexsond.auth_sessions (
                    session_id, user_id, active_workspace_id, token_hash,
                    csrf_secret_hash, user_agent_hash, ip_prefix, created_at,
                    last_seen_at, idle_expires_at, absolute_expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING session_id, user_id,
                          active_workspace_id AS workspace_id, created_at,
                          last_seen_at, idle_expires_at, absolute_expires_at,
                          revoked_at
                """,
                (
                    session_id,
                    user_id,
                    workspace_id,
                    token_hash,
                    csrf_secret_hash,
                    user_agent_hash,
                    ip_prefix,
                    now,
                    now,
                    idle_expires_at,
                    absolute_expires_at,
                ),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO lexsond.auth_session_csrf_tokens (
                    session_id, csrf_secret_hash, created_at
                ) VALUES (%s, %s, %s)
                """,
                (session_id, csrf_secret_hash, now),
            )
        return _session_public(row)

    def resolve_session(
        self,
        *,
        token_hash: bytes,
        now: datetime,
        idle_timeout: timedelta = timedelta(hours=12),
        touch_interval: timedelta = timedelta(minutes=5),
    ) -> dict[str, Any] | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT s.*, u.email_display AS email, u.email_normalized,
                       u.display_name, u.avatar_url, u.status AS user_status,
                       u.system_role, u.email_verified_at,
                       w.name AS workspace_name, m.role AS workspace_role
                FROM lexsond.auth_sessions s
                JOIN lexsond.users u ON u.user_id = s.user_id
                JOIN lexsond.workspaces w
                  ON w.workspace_id = s.active_workspace_id
                 AND w.deleted_at IS NULL
                JOIN lexsond.workspace_members m
                  ON m.workspace_id = s.active_workspace_id
                 AND m.user_id = s.user_id
                WHERE s.token_hash = %s
                FOR UPDATE OF s
                """,
                (token_hash,),
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                return None
            if (
                row["idle_expires_at"] <= now
                or row["absolute_expires_at"] <= now
                or row["user_status"] not in {"PENDING_VERIFICATION", "ACTIVE"}
            ):
                connection.execute(
                    """
                    UPDATE lexsond.auth_sessions
                    SET revoked_at = COALESCE(revoked_at, %s)
                    WHERE session_id = %s
                    """,
                    (now, row["session_id"]),
                )
                return None
            if row["last_seen_at"] <= now - touch_interval:
                new_idle_expiry = min(now + idle_timeout, row["absolute_expires_at"])
                connection.execute(
                    """
                    UPDATE lexsond.auth_sessions
                    SET last_seen_at = %s, idle_expires_at = %s
                    WHERE session_id = %s
                    """,
                    (now, new_idle_expiry, row["session_id"]),
                )
                row["last_seen_at"] = now
                row["idle_expires_at"] = new_idle_expiry
            csrf_rows = connection.execute(
                """
                SELECT csrf_secret_hash
                FROM lexsond.auth_session_csrf_tokens
                WHERE session_id = %s
                ORDER BY created_at DESC
                LIMIT 8
                """,
                (row["session_id"],),
            ).fetchall()
            row["csrf_secret_hashes"] = [
                bytes(value["csrf_secret_hash"]) for value in csrf_rows
            ] or [bytes(row["csrf_secret_hash"])]
        return _principal(row)

    def rotate_csrf(self, *, session_id: str, csrf_secret_hash: bytes) -> None:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE lexsond.auth_sessions SET csrf_secret_hash = %s
                WHERE session_id = %s AND revoked_at IS NULL
                """,
                (csrf_secret_hash, session_id),
            )
            if cursor.rowcount != 1:
                raise AuthRejected("会话已失效")
            connection.execute(
                """
                INSERT INTO lexsond.auth_session_csrf_tokens (
                    session_id, csrf_secret_hash
                ) VALUES (%s, %s)
                ON CONFLICT (session_id, csrf_secret_hash) DO NOTHING
                """,
                (session_id, csrf_secret_hash),
            )
            connection.execute(
                """
                DELETE FROM lexsond.auth_session_csrf_tokens
                WHERE session_id = %s AND csrf_secret_hash IN (
                    SELECT csrf_secret_hash
                    FROM lexsond.auth_session_csrf_tokens
                    WHERE session_id = %s
                    ORDER BY created_at DESC, csrf_secret_hash DESC
                    OFFSET 8
                )
                """,
                (session_id, session_id),
            )

    def revoke_session_by_token(self, *, token_hash: bytes, now: datetime) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """
                UPDATE lexsond.auth_sessions
                SET revoked_at = COALESCE(revoked_at, %s)
                WHERE token_hash = %s
                """,
                (now, token_hash),
            )

    def revoke_user_session(
        self, *, user_id: str, session_id: str, now: datetime
    ) -> None:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE lexsond.auth_sessions
                SET revoked_at = COALESCE(revoked_at, %s)
                WHERE session_id = %s AND user_id = %s
                """,
                (now, session_id, user_id),
            )
            if cursor.rowcount != 1:
                raise ControlPlaneNotFound("session was not found")

    def revoke_all_sessions(self, *, user_id: str, now: datetime) -> int:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE lexsond.auth_sessions SET revoked_at = %s
                WHERE user_id = %s AND revoked_at IS NULL
                """,
                (now, user_id),
            )
        return cursor.rowcount

    def list_user_sessions(self, *, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("session limit is out of bounds")
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT session_id, user_id, active_workspace_id AS workspace_id,
                       user_agent_hash, ip_prefix, created_at, last_seen_at,
                       idle_expires_at, absolute_expires_at, revoked_at
                FROM lexsond.auth_sessions
                WHERE user_id = %s
                ORDER BY created_at DESC, session_id
                LIMIT %s
                """,
                (user_id, limit),
            ).fetchall()
        return [_session_public(row) for row in rows]

    def consume_email_verification(
        self, *, token_hash: bytes, now: datetime
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            token = connection.execute(
                """
                SELECT token_id, user_id, expires_at, consumed_at
                FROM lexsond.auth_action_tokens
                WHERE token_hash = %s AND purpose = 'verify_email'
                FOR UPDATE
                """,
                (token_hash,),
            ).fetchone()
            if (
                token is None
                or token["consumed_at"] is not None
                or token["expires_at"] <= now
            ):
                raise AuthRejected("验证链接无效或已过期")
            connection.execute(
                """
                UPDATE lexsond.auth_action_tokens SET consumed_at = %s
                WHERE token_id = %s
                """,
                (now, token["token_id"]),
            )
            connection.execute(
                """
                UPDATE lexsond.users
                SET status = 'ACTIVE', email_verified_at = COALESCE(email_verified_at, %s),
                    updated_at = %s
                WHERE user_id = %s AND status = 'PENDING_VERIFICATION'
                """,
                (now, now, token["user_id"]),
            )
            connection.execute(
                """
                UPDATE lexsond.auth_action_tokens SET consumed_at = COALESCE(consumed_at, %s)
                WHERE user_id = %s AND purpose = 'verify_email'
                """,
                (now, token["user_id"]),
            )
        return self._get_user(str(token["user_id"]))

    def create_email_verification(
        self,
        *,
        user_id: str,
        token_hash: bytes,
        expires_at: datetime,
        now: datetime,
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            user = connection.execute(
                """
                SELECT user_id, email_display AS email, status
                FROM lexsond.users WHERE user_id = %s FOR UPDATE
                """,
                (user_id,),
            ).fetchone()
            if user is None or user["status"] != "PENDING_VERIFICATION":
                raise AuthRejected("邮箱已经验证")
            connection.execute(
                """
                UPDATE lexsond.auth_action_tokens
                SET consumed_at = COALESCE(consumed_at, %s)
                WHERE user_id = %s AND purpose = 'verify_email'
                """,
                (now, user_id),
            )
            connection.execute(
                """
                INSERT INTO lexsond.auth_action_tokens (
                    token_id, user_id, purpose, token_hash, created_at, expires_at
                ) VALUES (%s, %s, 'verify_email', %s, %s, %s)
                """,
                (str(uuid4()), user_id, token_hash, now, expires_at),
            )
        return {"user_id": str(user["user_id"]), "email": user["email"]}

    def create_password_reset(
        self,
        *,
        email_normalized: str,
        token_hash: bytes,
        expires_at: datetime,
        now: datetime,
    ) -> dict[str, Any] | None:
        with self._pool.connection() as connection:
            user = connection.execute(
                """
                SELECT user_id, email_display AS email, status
                FROM lexsond.users
                WHERE email_normalized = %s
                FOR UPDATE
                """,
                (email_normalized,),
            ).fetchone()
            if user is None or user["status"] not in {
                "PENDING_VERIFICATION",
                "ACTIVE",
            }:
                return None
            connection.execute(
                """
                UPDATE lexsond.auth_action_tokens
                SET consumed_at = COALESCE(consumed_at, %s)
                WHERE user_id = %s AND purpose = 'reset_password'
                """,
                (now, user["user_id"]),
            )
            connection.execute(
                """
                INSERT INTO lexsond.auth_action_tokens (
                    token_id, user_id, purpose, token_hash, created_at, expires_at
                ) VALUES (%s, %s, 'reset_password', %s, %s, %s)
                """,
                (str(uuid4()), user["user_id"], token_hash, now, expires_at),
            )
        return {"user_id": str(user["user_id"]), "email": user["email"]}

    def consume_password_reset(
        self,
        *,
        token_hash: bytes,
        password_hash: str,
        now: datetime,
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            token = connection.execute(
                """
                SELECT token_id, user_id, expires_at, consumed_at
                FROM lexsond.auth_action_tokens
                WHERE token_hash = %s AND purpose = 'reset_password'
                FOR UPDATE
                """,
                (token_hash,),
            ).fetchone()
            if (
                token is None
                or token["consumed_at"] is not None
                or token["expires_at"] <= now
            ):
                raise AuthRejected("重置链接无效或已过期")
            user = connection.execute(
                """
                UPDATE lexsond.users
                SET password_hash = %s, updated_at = %s
                WHERE user_id = %s
                  AND status IN ('PENDING_VERIFICATION', 'ACTIVE')
                RETURNING user_id, status
                """,
                (password_hash, now, token["user_id"]),
            ).fetchone()
            if user is None:
                raise AuthRejected("重置链接无效或已过期")
            connection.execute(
                """
                UPDATE lexsond.auth_action_tokens
                SET consumed_at = COALESCE(consumed_at, %s)
                WHERE user_id = %s AND purpose = 'reset_password'
                """,
                (now, token["user_id"]),
            )
            connection.execute(
                """
                UPDATE lexsond.auth_sessions
                SET revoked_at = COALESCE(revoked_at, %s)
                WHERE user_id = %s
                """,
                (now, token["user_id"]),
            )
        return {"user_id": str(user["user_id"]), "status": user["status"]}

    def change_password(
        self, *, user_id: str, password_hash: str, now: datetime
    ) -> int:
        with self._pool.connection() as connection:
            user = connection.execute(
                """
                UPDATE lexsond.users
                SET password_hash = %s, updated_at = %s
                WHERE user_id = %s
                  AND status IN ('PENDING_VERIFICATION', 'ACTIVE')
                RETURNING user_id
                """,
                (password_hash, now, user_id),
            ).fetchone()
            if user is None:
                raise ControlPlaneNotFound("user was not found")
            revoked = connection.execute(
                """
                UPDATE lexsond.auth_sessions
                SET revoked_at = COALESCE(revoked_at, %s)
                WHERE user_id = %s AND revoked_at IS NULL
                """,
                (now, user_id),
            ).rowcount
        return revoked

    def consume_auth_rate_limit(
        self,
        *,
        bucket: str,
        subject_hash: bytes,
        now: datetime,
        window: timedelta,
        max_attempts: int,
        block: timedelta,
    ) -> bool:
        if max_attempts < 1 or window <= timedelta(0) or block <= timedelta(0):
            raise ValueError("authentication rate-limit policy is invalid")
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO lexsond.auth_rate_limits AS current_limit (
                    bucket, subject_hash, window_started_at, attempts, blocked_until
                ) VALUES (%s, %s, %s, 1, NULL)
                ON CONFLICT (bucket, subject_hash) DO UPDATE SET
                    attempts = CASE
                        WHEN current_limit.window_started_at <= %s - %s
                            THEN 1
                        ELSE current_limit.attempts + 1
                    END,
                    window_started_at = CASE
                        WHEN current_limit.window_started_at <= %s - %s
                            THEN %s
                        ELSE current_limit.window_started_at
                    END,
                    blocked_until = CASE
                        WHEN current_limit.blocked_until > %s
                            THEN current_limit.blocked_until
                        WHEN current_limit.window_started_at <= %s - %s
                            THEN NULL
                        WHEN current_limit.attempts + 1 > %s
                            THEN %s + %s
                        ELSE NULL
                    END
                RETURNING blocked_until
                """,
                (
                    bucket,
                    subject_hash,
                    now,
                    now,
                    window,
                    now,
                    window,
                    now,
                    now,
                    now,
                    window,
                    max_attempts,
                    now,
                    block,
                ),
            ).fetchone()
            connection.execute(
                """
                DELETE FROM lexsond.auth_rate_limits
                WHERE ctid IN (
                    SELECT ctid FROM lexsond.auth_rate_limits
                    WHERE window_started_at < %s - INTERVAL '7 days'
                      AND (blocked_until IS NULL OR blocked_until <= %s)
                    ORDER BY window_started_at
                    LIMIT 100
                )
                """,
                (now, now),
            )
        return row["blocked_until"] is None or row["blocked_until"] <= now

    def record_auth_audit(
        self,
        *,
        user_id: str | None,
        provider: str,
        category: str,
        outcome: str,
        ip_prefix: str | None,
        user_agent_hash: bytes | None,
        occurred_at: datetime,
    ) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO lexsond.auth_audit_events (
                    event_id, user_id, provider, category, outcome, ip_prefix,
                    user_agent_hash, occurred_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(uuid4()),
                    user_id,
                    provider,
                    category,
                    outcome,
                    ip_prefix,
                    user_agent_hash,
                    occurred_at,
                ),
            )

    def _get_user(
        self, user_id: str, *, workspace_id: str | None = None
    ) -> dict[str, Any]:
        params: tuple[str, ...]
        workspace_filter = ""
        if workspace_id is None:
            params = (user_id,)
        else:
            workspace_filter = "AND w.workspace_id = %s"
            params = (user_id, workspace_id)
        with self._pool.connection() as connection:
            row = connection.execute(
                f"""
                SELECT u.user_id, u.email_display AS email,
                       u.email_normalized, u.display_name, u.avatar_url,
                       u.status, u.system_role, u.email_verified_at,
                       w.workspace_id, w.name AS workspace_name,
                       m.role AS workspace_role
                FROM lexsond.users u
                JOIN lexsond.workspace_members m ON m.user_id = u.user_id
                JOIN lexsond.workspaces w
                  ON w.workspace_id = m.workspace_id AND w.deleted_at IS NULL
                WHERE u.user_id = %s {workspace_filter}
                ORDER BY (w.owner_user_id = u.user_id) DESC, w.created_at, w.workspace_id
                LIMIT 1
                """,
                params,
            ).fetchone()
        if row is None:
            raise ControlPlaneNotFound("user was not found")
        return _auth_user(row, include_password=False)


def _auth_user(
    row: dict[str, Any], *, include_password: bool = True
) -> dict[str, Any]:
    value = {
        "user_id": str(row["user_id"]),
        "email": row["email"],
        "email_normalized": row["email_normalized"],
        "display_name": row["display_name"],
        "avatar_url": row["avatar_url"],
        "status": row["status"],
        "system_role": row["system_role"],
        "email_verified_at": _timestamp(row["email_verified_at"]),
        "workspace_id": str(row["workspace_id"]),
        "workspace_name": row["workspace_name"],
        "workspace_role": row["workspace_role"],
    }
    if include_password:
        value["password_hash"] = row["password_hash"]
    return value


def _session_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": str(row["session_id"]),
        "user_id": str(row["user_id"]),
        "workspace_id": str(row["workspace_id"]),
        "created_at": _timestamp(row["created_at"]),
        "last_seen_at": _timestamp(row.get("last_seen_at")),
        "idle_expires_at": _timestamp(row["idle_expires_at"]),
        "absolute_expires_at": _timestamp(row["absolute_expires_at"]),
        "revoked_at": _timestamp(row.get("revoked_at")),
        "ip_prefix": str(row["ip_prefix"]) if row.get("ip_prefix") else None,
        "device_id": (
            bytes(row["user_agent_hash"]).hex()[:12]
            if row.get("user_agent_hash") is not None
            else None
        ),
    }


def _principal(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": str(row["session_id"]),
        "user_id": str(row["user_id"]),
        "workspace_id": str(row["active_workspace_id"]),
        "workspace_name": row["workspace_name"],
        "workspace_role": row["workspace_role"],
        "email": row["email"],
        "email_normalized": row["email_normalized"],
        "display_name": row["display_name"],
        "avatar_url": row["avatar_url"],
        "status": row["user_status"],
        "system_role": row["system_role"],
        "email_verified_at": _timestamp(row["email_verified_at"]),
        "csrf_secret_hash": bytes(row["csrf_secret_hash"]),
        "csrf_secret_hashes": row.get("csrf_secret_hashes")
        or [bytes(row["csrf_secret_hash"])],
        "created_at": _timestamp(row["created_at"]),
        "last_seen_at": _timestamp(row["last_seen_at"]),
        "idle_expires_at": _timestamp(row["idle_expires_at"]),
        "absolute_expires_at": _timestamp(row["absolute_expires_at"]),
    }


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()
