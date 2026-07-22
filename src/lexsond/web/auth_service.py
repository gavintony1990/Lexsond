from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping, Protocol

from .auth import (
    OneTimeSecret,
    coarse_ip_prefix,
    issue_one_time_secret,
    normalize_email,
    secret_hash,
    secret_matches,
    user_agent_hash,
)


class AuthRejected(RuntimeError):
    pass


class AuthenticationRequired(AuthRejected):
    pass


class CsrfRejected(AuthRejected):
    pass


class AuthRateLimited(AuthRejected):
    def __init__(self, retry_after_seconds: int = 900) -> None:
        super().__init__("请求过于频繁，请稍后再试")
        self.retry_after_seconds = retry_after_seconds


class PasswordManagerPort(Protocol):
    def hash(self, password: str) -> str: ...

    def verify(self, password: str, encoded_hash: str) -> bool: ...

    def needs_rehash(self, encoded_hash: str) -> bool: ...


class AuthStorePort(Protocol):
    def register_user(self, **value: Any) -> dict[str, Any]: ...

    def find_login_user(self, email_normalized: str) -> dict[str, Any] | None: ...

    def update_password_hash(self, user_id: str, password_hash: str) -> None: ...

    def create_session(self, **value: Any) -> dict[str, Any]: ...

    def consume_email_verification(
        self, *, token_hash: bytes, now: datetime
    ) -> dict[str, Any]: ...

    def create_email_verification(self, **value: Any) -> dict[str, Any]: ...

    def create_password_reset(self, **value: Any) -> dict[str, Any] | None: ...

    def consume_password_reset(self, **value: Any) -> dict[str, Any]: ...

    def change_password(self, **value: Any) -> int: ...

    def consume_auth_rate_limit(self, **value: Any) -> bool: ...

    def resolve_session(
        self, *, token_hash: bytes, now: datetime
    ) -> dict[str, Any] | None: ...

    def rotate_csrf(self, *, session_id: str, csrf_secret_hash: bytes) -> None: ...

    def revoke_session_by_token(self, *, token_hash: bytes, now: datetime) -> None: ...

    def list_user_sessions(
        self, *, user_id: str, limit: int = 50
    ) -> list[dict[str, Any]]: ...

    def revoke_user_session(
        self, *, user_id: str, session_id: str, now: datetime
    ) -> None: ...

    def revoke_all_sessions(self, *, user_id: str, now: datetime) -> int: ...

    def record_auth_audit(self, **value: Any) -> None: ...


@dataclass(frozen=True, slots=True)
class RegistrationDelivery:
    user: Mapping[str, Any]
    verification_secret: OneTimeSecret

    def __repr__(self) -> str:
        return "RegistrationDelivery(user=[PUBLIC], verification_secret=[REDACTED])"


@dataclass(frozen=True, slots=True)
class VerificationDelivery:
    email: str
    verification_secret: OneTimeSecret

    def __repr__(self) -> str:
        return "VerificationDelivery(email=[PUBLIC], verification_secret=[REDACTED])"


@dataclass(frozen=True, slots=True)
class PasswordResetDelivery:
    email: str
    reset_secret: OneTimeSecret

    def __repr__(self) -> str:
        return "PasswordResetDelivery(email=[PUBLIC], reset_secret=[REDACTED])"


@dataclass(frozen=True, slots=True)
class SessionGrant:
    user: Mapping[str, Any]
    session: Mapping[str, Any]
    session_secret: OneTimeSecret
    csrf_secret: OneTimeSecret

    def __repr__(self) -> str:
        return (
            "SessionGrant(user=[PUBLIC], session=[PUBLIC], "
            "session_secret=[REDACTED], csrf_secret=[REDACTED])"
        )


@dataclass(frozen=True, slots=True)
class SessionResume:
    principal: Mapping[str, Any]
    csrf_secret: OneTimeSecret

    def __repr__(self) -> str:
        return "SessionResume(principal=[PUBLIC], csrf_secret=[REDACTED])"


class AuthenticationService:
    def __init__(
        self,
        *,
        store: AuthStorePort,
        password_manager: PasswordManagerPort,
        dummy_password_hash: str,
        idle_timeout: timedelta = timedelta(hours=12),
        absolute_timeout: timedelta = timedelta(days=7),
        verification_timeout: timedelta = timedelta(hours=24),
        password_reset_timeout: timedelta = timedelta(hours=1),
    ) -> None:
        if not dummy_password_hash:
            raise ValueError("dummy password hash is required")
        if idle_timeout <= timedelta(0) or absolute_timeout < idle_timeout:
            raise ValueError("session timeouts are invalid")
        self._store = store
        self._passwords = password_manager
        self._dummy_password_hash = dummy_password_hash
        self._idle_timeout = idle_timeout
        self._absolute_timeout = absolute_timeout
        self._verification_timeout = verification_timeout
        self._password_reset_timeout = password_reset_timeout

    def register(
        self,
        *,
        email: str,
        password: str,
        display_name: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
        now: datetime | None = None,
    ) -> RegistrationDelivery:
        timestamp = _now(now)
        email_normalized = normalize_email(email)
        self._rate_limit(
            "REGISTER", email_normalized, ip_address, timestamp,
            max_attempts=5, window=timedelta(hours=1), block=timedelta(hours=1),
        )
        if not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= 120:
            raise ValueError("display name must contain between 1 and 120 characters")
        password_hash = self._passwords.hash(password)
        verification_secret, verification_hash = issue_one_time_secret()
        user = self._store.register_user(
            email_normalized=email_normalized,
            email_display=email.strip(),
            password_hash=password_hash,
            display_name=display_name.strip(),
            verification_token_hash=verification_hash,
            verification_expires_at=timestamp + self._verification_timeout,
            now=timestamp,
        )
        self._audit(
            user_id=str(user["user_id"]),
            category="REGISTER",
            outcome="SUCCESS",
            provider="password",
            ip_address=ip_address,
            user_agent=user_agent,
            now=timestamp,
        )
        return RegistrationDelivery(user=user, verification_secret=verification_secret)

    def login(
        self,
        *,
        email: str,
        password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
        now: datetime | None = None,
    ) -> SessionGrant:
        timestamp = _now(now)
        try:
            normalized = normalize_email(email)
        except ValueError:
            normalized = "invalid-login@invalid"
        self._rate_limit(
            "LOGIN", normalized, ip_address, timestamp,
            max_attempts=10, window=timedelta(minutes=15), block=timedelta(minutes=15),
        )
        user = self._store.find_login_user(normalized)
        encoded_hash = (
            str(user["password_hash"])
            if user is not None and user.get("password_hash")
            else self._dummy_password_hash
        )
        verified = self._passwords.verify(password, encoded_hash)
        allowed_status = user is not None and user.get("status") in {
            "PENDING_VERIFICATION",
            "ACTIVE",
        }
        if user is None or not verified or not allowed_status:
            self._audit(
                user_id=str(user["user_id"]) if user is not None else None,
                category="LOGIN",
                outcome="INVALID_CREDENTIALS",
                provider="password",
                ip_address=ip_address,
                user_agent=user_agent,
                now=timestamp,
            )
            raise AuthRejected("邮箱或密码错误")

        if self._passwords.needs_rehash(encoded_hash):
            self._store.update_password_hash(
                str(user["user_id"]), self._passwords.hash(password)
            )

        session_secret, token_hash = issue_one_time_secret()
        csrf_secret, csrf_secret_hash = issue_one_time_secret()
        session = self._store.create_session(
            user_id=str(user["user_id"]),
            workspace_id=str(user["workspace_id"]),
            token_hash=token_hash,
            csrf_secret_hash=csrf_secret_hash,
            user_agent_hash=user_agent_hash(user_agent),
            ip_prefix=coarse_ip_prefix(ip_address),
            now=timestamp,
            idle_expires_at=timestamp + self._idle_timeout,
            absolute_expires_at=timestamp + self._absolute_timeout,
        )
        self._audit(
            user_id=str(user["user_id"]),
            category="LOGIN",
            outcome="SUCCESS",
            provider="password",
            ip_address=ip_address,
            user_agent=user_agent,
            now=timestamp,
        )
        return SessionGrant(
            user=_public_user(user),
            session=session,
            session_secret=session_secret,
            csrf_secret=csrf_secret,
        )

    def verify_email(
        self,
        raw_token: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = _now(now)
        self._rate_limit(
            "VERIFY_EMAIL", None, ip_address, timestamp,
            max_attempts=10, window=timedelta(minutes=15), block=timedelta(minutes=15),
        )
        try:
            token_hash = secret_hash(raw_token)
        except (UnicodeError, ValueError) as exc:
            raise AuthRejected("验证链接无效或已过期") from exc
        try:
            user = self._store.consume_email_verification(
                token_hash=token_hash, now=timestamp
            )
        except AuthRejected:
            raise
        self._audit(
            user_id=str(user["user_id"]) if user.get("user_id") else None,
            category="VERIFY_EMAIL",
            outcome="SUCCESS",
            provider="password",
            ip_address=ip_address,
            user_agent=user_agent,
            now=timestamp,
        )
        return user

    def resend_verification(
        self,
        principal: Mapping[str, Any],
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
        now: datetime | None = None,
    ) -> VerificationDelivery:
        if principal.get("status") != "PENDING_VERIFICATION":
            raise AuthRejected("邮箱已经验证")
        timestamp = _now(now)
        self._rate_limit(
            "VERIFY_EMAIL_RESEND", str(principal.get("email_normalized") or ""),
            ip_address, timestamp, max_attempts=5,
            window=timedelta(hours=1), block=timedelta(hours=1),
        )
        verification_secret, verification_hash = issue_one_time_secret()
        user = self._store.create_email_verification(
            user_id=str(principal["user_id"]),
            token_hash=verification_hash,
            expires_at=timestamp + self._verification_timeout,
            now=timestamp,
        )
        self._audit(
            user_id=str(principal["user_id"]),
            category="VERIFY_EMAIL_RESEND",
            outcome="SUCCESS",
            provider="password",
            ip_address=ip_address,
            user_agent=user_agent,
            now=timestamp,
        )
        return VerificationDelivery(
            email=str(user["email"]), verification_secret=verification_secret
        )

    def request_password_reset(
        self,
        email: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
        now: datetime | None = None,
    ) -> PasswordResetDelivery | None:
        timestamp = _now(now)
        try:
            email_normalized = normalize_email(email)
        except ValueError:
            email_normalized = "invalid-reset@invalid"
        self._rate_limit(
            "PASSWORD_RESET_REQUEST", email_normalized, ip_address, timestamp,
            max_attempts=5, window=timedelta(hours=1), block=timedelta(hours=1),
        )
        reset_secret, reset_hash = issue_one_time_secret()
        user = self._store.create_password_reset(
            email_normalized=email_normalized,
            token_hash=reset_hash,
            expires_at=timestamp + self._password_reset_timeout,
            now=timestamp,
        )
        self._audit(
            user_id=str(user["user_id"]) if user is not None else None,
            category="PASSWORD_RESET_REQUEST",
            outcome="SUCCESS",
            provider="password",
            ip_address=ip_address,
            user_agent=user_agent,
            now=timestamp,
        )
        if user is None:
            reset_secret.consume()
            return None
        return PasswordResetDelivery(
            email=str(user["email"]), reset_secret=reset_secret
        )

    def reset_password(
        self,
        raw_token: str,
        new_password: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = _now(now)
        self._rate_limit(
            "PASSWORD_RESET", None, ip_address, timestamp,
            max_attempts=10, window=timedelta(minutes=15), block=timedelta(minutes=15),
        )
        try:
            token_hash = secret_hash(raw_token)
        except (UnicodeError, ValueError) as exc:
            raise AuthRejected("重置链接无效或已过期") from exc
        password_hash = self._passwords.hash(new_password)
        user = self._store.consume_password_reset(
            token_hash=token_hash,
            password_hash=password_hash,
            now=timestamp,
        )
        self._audit(
            user_id=str(user["user_id"]),
            category="PASSWORD_RESET",
            outcome="SUCCESS",
            provider="password",
            ip_address=ip_address,
            user_agent=user_agent,
            now=timestamp,
        )
        return _public_user(user)

    def change_password(
        self,
        principal: Mapping[str, Any],
        *,
        current_password: str,
        new_password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
        now: datetime | None = None,
    ) -> int:
        timestamp = _now(now)
        user = self._store.find_login_user(str(principal["email_normalized"]))
        encoded_hash = str(user.get("password_hash")) if user else self._dummy_password_hash
        if user is None or not user.get("password_hash") or not self._passwords.verify(
            current_password, encoded_hash
        ):
            self._audit(
                user_id=str(principal["user_id"]),
                category="PASSWORD_CHANGE",
                outcome="INVALID_CREDENTIALS",
                provider="password",
                ip_address=ip_address,
                user_agent=user_agent,
                now=timestamp,
            )
            raise AuthRejected("当前密码错误")
        new_hash = self._passwords.hash(new_password)
        revoked = self._store.change_password(
            user_id=str(principal["user_id"]),
            password_hash=new_hash,
            now=timestamp,
        )
        self._audit(
            user_id=str(principal["user_id"]),
            category="PASSWORD_CHANGE",
            outcome="SUCCESS",
            provider="password",
            ip_address=ip_address,
            user_agent=user_agent,
            now=timestamp,
        )
        return revoked

    def resolve_principal(
        self, raw_session: str | None, *, now: datetime | None = None
    ) -> dict[str, Any]:
        if not raw_session:
            raise AuthenticationRequired("请先登录")
        try:
            token_hash = secret_hash(raw_session)
        except (UnicodeError, ValueError) as exc:
            raise AuthenticationRequired("请先登录") from exc
        principal = self._store.resolve_session(token_hash=token_hash, now=_now(now))
        if principal is None:
            raise AuthenticationRequired("请先登录")
        return principal

    def resume_session(
        self, raw_session: str | None, *, now: datetime | None = None
    ) -> SessionResume:
        principal = self.resolve_principal(raw_session, now=now)
        csrf_secret, csrf_secret_hash = issue_one_time_secret()
        self._store.rotate_csrf(
            session_id=str(principal["session_id"]),
            csrf_secret_hash=csrf_secret_hash,
        )
        return SessionResume(
            principal=_public_user(principal), csrf_secret=csrf_secret
        )

    def require_csrf(self, principal: Mapping[str, Any], raw_csrf: str | None) -> None:
        expected_hashes = principal.get("csrf_secret_hashes")
        if not isinstance(expected_hashes, list):
            expected = principal.get("csrf_secret_hash")
            expected_hashes = [expected] if isinstance(expected, bytes) else []
        if not raw_csrf or not any(
            isinstance(expected, bytes) and secret_matches(raw_csrf, expected)
            for expected in expected_hashes
        ):
            raise CsrfRejected("CSRF 校验失败")

    def logout(
        self, raw_session: str | None, *, now: datetime | None = None
    ) -> None:
        if not raw_session:
            return
        try:
            token_hash = secret_hash(raw_session)
        except (UnicodeError, ValueError):
            return
        self._store.revoke_session_by_token(token_hash=token_hash, now=_now(now))

    def list_sessions(
        self, principal: Mapping[str, Any], *, limit: int = 50
    ) -> list[dict[str, Any]]:
        user_id = str(principal["user_id"])
        current_session_id = principal.get("session_id")
        return [
            {
                **value,
                "current": str(value["session_id"]) == str(current_session_id),
            }
            for value in self._store.list_user_sessions(user_id=user_id, limit=limit)
        ]

    def revoke_session(
        self,
        principal: Mapping[str, Any],
        session_id: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        timestamp = _now(now)
        user_id = str(principal["user_id"])
        self._store.revoke_user_session(
            user_id=user_id, session_id=session_id, now=timestamp
        )
        self._audit(
            user_id=user_id,
            category="SESSION_REVOKE",
            outcome="SUCCESS",
            provider="password",
            ip_address=ip_address,
            user_agent=user_agent,
            now=timestamp,
        )
        return str(principal.get("session_id")) == session_id

    def logout_all(
        self,
        principal: Mapping[str, Any],
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
        now: datetime | None = None,
    ) -> int:
        timestamp = _now(now)
        user_id = str(principal["user_id"])
        count = self._store.revoke_all_sessions(user_id=user_id, now=timestamp)
        self._audit(
            user_id=user_id,
            category="LOGOUT_ALL",
            outcome="SUCCESS",
            provider="password",
            ip_address=ip_address,
            user_agent=user_agent,
            now=timestamp,
        )
        return count

    def _audit(
        self,
        *,
        user_id: str | None,
        category: str,
        outcome: str,
        provider: str,
        ip_address: str | None,
        user_agent: str | None,
        now: datetime,
    ) -> None:
        self._store.record_auth_audit(
            user_id=user_id,
            category=category,
            outcome=outcome,
            provider=provider,
            ip_prefix=coarse_ip_prefix(ip_address),
            user_agent_hash=user_agent_hash(user_agent),
            occurred_at=now,
        )

    def _rate_limit(
        self,
        bucket: str,
        email_normalized: str | None,
        ip_address: str | None,
        now: datetime,
        *,
        max_attempts: int,
        window: timedelta,
        block: timedelta,
    ) -> None:
        subjects = [("ip", coarse_ip_prefix(ip_address) or "unknown")]
        if email_normalized:
            subjects.append(("email", email_normalized))
        for subject_kind, subject in subjects:
            digest = hashlib.sha256(
                f"lexsond-auth-rate:{bucket}:{subject_kind}:{subject}".encode("utf-8")
            ).digest()
            allowed = self._store.consume_auth_rate_limit(
                bucket=bucket,
                subject_hash=digest,
                now=now,
                window=window,
                max_attempts=max_attempts,
                block=block,
            )
            if not allowed:
                self._audit(
                    user_id=None,
                    category=bucket,
                    outcome="RATE_LIMITED",
                    provider="password",
                    ip_address=ip_address,
                    user_agent=None,
                    now=now,
                )
                raise AuthRateLimited(max(1, int(block.total_seconds())))


def _public_user(user: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in user.items()
        if not key.endswith(("_hash", "_hashes")) and key not in {"password_hash"}
    }


def _now(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise ValueError("authentication timestamps must include a timezone")
    return timestamp.astimezone(UTC)
