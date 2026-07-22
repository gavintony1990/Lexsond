from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from pydantic import SecretStr


class VaultUnavailable(RuntimeError):
    pass


class CredentialNotFound(VaultUnavailable):
    pass


@dataclass(frozen=True, slots=True)
class VaultStatus:
    available: bool
    backend: str
    reason: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "backend": self.backend,
            "reason": self.reason,
        }


class CredentialVault(Protocol):
    def status(self) -> VaultStatus: ...

    def put(self, credential_id: UUID, secret: SecretStr) -> None: ...

    def get_for_execution(self, credential_id: UUID) -> SecretStr: ...

    def replace(self, credential_id: UUID, secret: SecretStr) -> None: ...

    def delete(self, credential_id: UUID) -> None: ...


class EphemeralCredentialVault:
    """Process-local vault for explicit one-session use."""

    def __init__(self) -> None:
        self._values: dict[UUID, str] = {}
        self._lock = threading.RLock()

    def status(self) -> VaultStatus:
        return VaultStatus(True, "ephemeral")

    def put(self, credential_id: UUID, secret: SecretStr) -> None:
        value = _secret_value(secret)
        with self._lock:
            if credential_id in self._values:
                raise ValueError("credential already exists")
            self._values[credential_id] = value

    def get_for_execution(self, credential_id: UUID) -> SecretStr:
        with self._lock:
            value = self._values.get(credential_id)
        if value is None:
            raise CredentialNotFound("credential is unavailable")
        return SecretStr(value)

    def replace(self, credential_id: UUID, secret: SecretStr) -> None:
        value = _secret_value(secret)
        with self._lock:
            if credential_id not in self._values:
                raise CredentialNotFound("credential is unavailable")
            self._values[credential_id] = value

    def delete(self, credential_id: UUID) -> None:
        with self._lock:
            if self._values.pop(credential_id, None) is None:
                raise CredentialNotFound("credential is unavailable")

    def __repr__(self) -> str:
        return f"EphemeralCredentialVault(credentials={len(self._values)}, values=[REDACTED])"


class SystemCredentialVault:
    """Thin adapter over a mature operating-system keyring backend."""

    SERVICE_NAME = "io.lexsond.credentials"

    def __init__(self, backend: Any | None = None) -> None:
        if backend is None:
            try:
                import keyring
            except ModuleNotFoundError as exc:
                raise VaultUnavailable(
                    "system credential storage requires keyring==25.7.0"
                ) from exc
            backend = keyring.get_keyring()
        self._backend = backend
        self._lock = threading.RLock()

    def status(self) -> VaultStatus:
        name = f"{type(self._backend).__module__}.{type(self._backend).__name__}"
        try:
            priority = float(self._backend.priority)
        except Exception:
            return VaultStatus(False, name, "系统密钥库状态不可用")
        if priority <= 0:
            return VaultStatus(False, name, "没有可用的系统密钥库后端")
        return VaultStatus(True, name)

    def put(self, credential_id: UUID, secret: SecretStr) -> None:
        value = _secret_value(secret)
        with self._lock:
            self._require_available()
            if self._safe_get(credential_id) is not None:
                raise ValueError("credential already exists")
            self._safe_set(credential_id, value)

    def get_for_execution(self, credential_id: UUID) -> SecretStr:
        with self._lock:
            self._require_available()
            value = self._safe_get(credential_id)
        if value is None:
            raise CredentialNotFound("credential is unavailable")
        return SecretStr(value)

    def replace(self, credential_id: UUID, secret: SecretStr) -> None:
        value = _secret_value(secret)
        with self._lock:
            self._require_available()
            previous = self._safe_get(credential_id)
            if previous is None:
                raise CredentialNotFound("credential is unavailable")
            try:
                self._safe_set(credential_id, value)
            except VaultUnavailable:
                try:
                    self._safe_set(credential_id, previous)
                except VaultUnavailable:
                    pass
                raise

    def delete(self, credential_id: UUID) -> None:
        with self._lock:
            self._require_available()
            if self._safe_get(credential_id) is None:
                raise CredentialNotFound("credential is unavailable")
            try:
                self._backend.delete_password(self.SERVICE_NAME, str(credential_id))
            except Exception as exc:
                raise VaultUnavailable("system credential deletion failed") from exc

    def _require_available(self) -> None:
        current = self.status()
        if not current.available:
            raise VaultUnavailable(current.reason or "system credential storage unavailable")

    def _safe_get(self, credential_id: UUID) -> str | None:
        try:
            value = self._backend.get_password(self.SERVICE_NAME, str(credential_id))
        except Exception as exc:
            raise VaultUnavailable("system credential lookup failed") from exc
        return value if isinstance(value, str) and value else None

    def _safe_set(self, credential_id: UUID, value: str) -> None:
        try:
            self._backend.set_password(self.SERVICE_NAME, str(credential_id), value)
        except Exception as exc:
            raise VaultUnavailable("system credential write failed") from exc

    def __repr__(self) -> str:
        return f"SystemCredentialVault(backend={self.status().backend!r}, values=[REDACTED])"


class UnavailableCredentialVault:
    """Fail-closed capability used when no approved secure backend is configured."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def status(self) -> VaultStatus:
        return VaultStatus(False, "unavailable", self._reason)

    def put(self, credential_id: UUID, secret: SecretStr) -> None:
        del credential_id, secret
        raise VaultUnavailable(self._reason)

    def get_for_execution(self, credential_id: UUID) -> SecretStr:
        del credential_id
        raise VaultUnavailable(self._reason)

    def replace(self, credential_id: UUID, secret: SecretStr) -> None:
        del credential_id, secret
        raise VaultUnavailable(self._reason)

    def delete(self, credential_id: UUID) -> None:
        del credential_id
        raise VaultUnavailable(self._reason)

    def __repr__(self) -> str:
        return "UnavailableCredentialVault(reason=[PUBLIC], values=[REDACTED])"


class CredentialFingerprinter:
    """Compute duplicate-detection digests using a key kept outside PostgreSQL."""

    KEY_LOCATOR = UUID("00000000-0000-4000-8000-00000000000f")

    def __init__(self, vault: CredentialVault) -> None:
        self._vault = vault
        self._lock = threading.Lock()

    def fingerprint(self, secret: SecretStr) -> str:
        value = _secret_value(secret)
        key = self._get_or_create_key().get_secret_value().encode("utf-8")
        return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def _get_or_create_key(self) -> SecretStr:
        with self._lock:
            try:
                return self._vault.get_for_execution(self.KEY_LOCATOR)
            except CredentialNotFound:
                generated = SecretStr(secrets.token_urlsafe(64))
                try:
                    self._vault.put(self.KEY_LOCATOR, generated)
                except ValueError:
                    return self._vault.get_for_execution(self.KEY_LOCATOR)
                return generated


class ExecutionCredentialBinder:
    """Short-lived catalog/run binding independent of credential persistence.

    Deployments with multiple web workers set LEXSOND_CREDENTIAL_BINDING_KEY to
    the same high-entropy secret. Without it, a process-local key is generated;
    snapshots created by a previous/different process then fail closed.
    """

    ENV_NAME = "LEXSOND_CREDENTIAL_BINDING_KEY"

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("credential binding key must contain at least 32 bytes")
        self._key = key

    @classmethod
    def from_environment(
        cls, *, require_configured: bool = False
    ) -> "ExecutionCredentialBinder":
        configured = os.environ.get(cls.ENV_NAME)
        if configured is None:
            if require_configured:
                raise ValueError(
                    f"{cls.ENV_NAME} is required for authenticated cloud mode"
                )
            return cls(secrets.token_bytes(32))
        if len(configured) < 32 or any(ord(character) < 32 for character in configured):
            raise ValueError(
                f"{cls.ENV_NAME} must contain at least 32 printable characters"
            )
        return cls(configured.encode("utf-8"))

    def fingerprint(self, secret: SecretStr, *, workspace_id: str) -> str:
        value = _secret_value(secret).encode("utf-8")
        scope = str(UUID(workspace_id)).encode("ascii")
        message = b"lexsond-execution-binding-v2\0" + scope + b"\0" + value
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def __repr__(self) -> str:
        return "ExecutionCredentialBinder(key=[REDACTED])"


def _secret_value(secret: SecretStr) -> str:
    if not isinstance(secret, SecretStr):
        raise TypeError("secret must be SecretStr")
    value = secret.get_secret_value()
    if not 1 <= len(value) <= 8192 or any(ord(character) < 32 for character in value):
        raise ValueError("credential has an invalid shape")
    return value
