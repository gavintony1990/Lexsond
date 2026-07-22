from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
from dataclasses import dataclass
from enum import Enum
from typing import Final


SESSION_COOKIE_NAME: Final = "lexsond_session"
CSRF_HEADER_NAME: Final = "X-CSRF-Token"
DEFAULT_RETURN_TO: Final = "/overview"
MINIMUM_PASSWORD_CHARACTERS: Final = 12
LOCAL_USER_ID: Final = "00000000-0000-4000-8000-000000000010"
LOCAL_WORKSPACE_ID: Final = "00000000-0000-4000-8000-000000000011"


class AuthMode(str, Enum):
    REQUIRED = "required"
    LOCAL_SINGLE_USER = "local-single-user"


@dataclass(frozen=True, slots=True)
class AuthConfiguration:
    mode: AuthMode
    listen_host: str
    cookie_secure: bool
    session_idle_seconds: int = 12 * 60 * 60
    session_absolute_seconds: int = 7 * 24 * 60 * 60

    @classmethod
    def from_values(
        cls,
        *,
        auth_mode: str | None,
        listen_host: str,
        cookie_secure: str | bool | None = None,
    ) -> AuthConfiguration:
        try:
            mode = AuthMode(auth_mode or AuthMode.REQUIRED.value)
        except ValueError as exc:
            raise ValueError(
                "LEXSOND_AUTH_MODE must be required or local-single-user"
            ) from exc
        if not isinstance(listen_host, str) or not listen_host.strip():
            raise ValueError("listen_host must be a non-empty numeric address")
        listen_host = listen_host.strip()
        if mode is AuthMode.LOCAL_SINGLE_USER:
            try:
                address = ipaddress.ip_address(listen_host)
            except ValueError as exc:
                raise ValueError(
                    "local-single-user requires a numeric loopback listen host"
                ) from exc
            if not address.is_loopback:
                raise ValueError(
                    "local-single-user requires a numeric loopback listen host"
                )
        secure_default = mode is AuthMode.REQUIRED
        return cls(
            mode=mode,
            listen_host=listen_host,
            cookie_secure=_boolean(cookie_secure, default=secure_default),
        )


class OneTimeSecret:
    """A single-consumption in-memory secret with a permanently redacted repr."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value: str | None = value

    def consume(self) -> str:
        value = self._value
        if value is None:
            raise RuntimeError("one-time secret has already been consumed")
        self._value = None
        return value

    def __repr__(self) -> str:
        return "OneTimeSecret('[REDACTED]')"


def issue_one_time_secret() -> tuple[OneTimeSecret, bytes]:
    raw = secrets.token_urlsafe(32)
    return OneTimeSecret(raw), secret_hash(raw)


def secret_hash(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("secret value must be a non-empty string")
    return hashlib.sha256(value.encode("ascii", errors="strict")).digest()


def secret_matches(value: str, expected_hash: bytes) -> bool:
    try:
        actual = secret_hash(value)
    except (UnicodeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected_hash)


def normalize_email(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("email must be a string")
    normalized = value.strip().casefold()
    if not 3 <= len(normalized) <= 320 or normalized.count("@") != 1:
        raise ValueError("email address is invalid")
    local, domain = normalized.split("@", 1)
    if not local or not domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError("email address is invalid")
    if any(ord(character) < 33 or character.isspace() for character in normalized):
        raise ValueError("email address is invalid")
    return normalized


def coarse_ip_prefix(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    prefix = 24 if address.version == 4 else 56
    return str(ipaddress.ip_network((address, prefix), strict=False))


def user_agent_hash(value: str | None) -> bytes:
    bounded = (value or "")[:2048]
    return hashlib.sha256(bounded.encode("utf-8", errors="replace")).digest()


def safe_return_to(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return DEFAULT_RETURN_TO
    if "\\" in value or any(ord(character) < 32 for character in value):
        return DEFAULT_RETURN_TO
    return value


class PasswordManager:
    """Pinned Argon2id boundary; importing the core package stays dependency-free."""

    def __init__(self) -> None:
        try:
            from argon2 import PasswordHasher
            from argon2.exceptions import InvalidHashError, VerificationError
            from argon2.low_level import Type
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                "password authentication requires argon2-cffi==25.1.0"
            ) from exc
        self._hasher = PasswordHasher(
            time_cost=3,
            memory_cost=65536,
            parallelism=4,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self._verification_errors = (InvalidHashError, VerificationError)

    def hash(self, password: str) -> str:
        _validate_password(password)
        return self._hasher.hash(password)

    def verify(self, password: str, encoded_hash: str) -> bool:
        if not isinstance(password, str) or not isinstance(encoded_hash, str):
            return False
        try:
            return bool(self._hasher.verify(encoded_hash, password))
        except self._verification_errors:
            return False

    def needs_rehash(self, encoded_hash: str) -> bool:
        return bool(self._hasher.check_needs_rehash(encoded_hash))


def _validate_password(password: str) -> None:
    if not isinstance(password, str):
        raise ValueError("password must be a string")
    if len(password) < MINIMUM_PASSWORD_CHARACTERS:
        raise ValueError("password must contain at least 12 characters")


def _boolean(value: str | bool | None, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("boolean configuration must be true or false")
