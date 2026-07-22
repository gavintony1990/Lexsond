from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Protocol
from uuid import UUID, uuid4

from pydantic import SecretStr

from ..credentials import (
    CredentialFingerprinter,
    CredentialNotFound,
    CredentialVault,
    VaultUnavailable,
)
from ..probe import validate_api_key_value
from ..providers import get_provider
from .control_contracts import ControlPlaneConflict


class CredentialMetadataStore(Protocol):
    def find_credential_profile_by_idempotency(
        self,
        idempotency_key: str,
        request_sha256: str,
        *,
        workspace_id: str,
    ) -> dict[str, Any] | None: ...

    def create_credential_profile(
        self, value: Mapping[str, Any], *, workspace_id: str
    ) -> dict[str, Any]: ...

    def list_credential_profiles(
        self, *, workspace_id: str, include_archived: bool = False
    ) -> list[dict[str, Any]]: ...

    def get_credential_profile(
        self,
        credential_id: str,
        *,
        workspace_id: str,
        include_archived: bool = False,
    ) -> dict[str, Any]: ...

    def get_credential_locator(
        self, credential_id: str, *, workspace_id: str
    ) -> UUID: ...

    def update_credential_profile(
        self,
        credential_id: str,
        changes: Mapping[str, Any],
        *,
        workspace_id: str,
        expected_version: int,
        audit_action: str,
        actor_user_id: str,
    ) -> dict[str, Any]: ...


class CredentialProfileService:
    def __init__(
        self,
        *,
        store: CredentialMetadataStore,
        vault: CredentialVault,
        storage_backend: str,
    ) -> None:
        if storage_backend not in {"SYSTEM_KEYRING", "EXTERNAL_SECRET_MANAGER"}:
            raise ValueError("unsupported credential storage backend")
        self._store = store
        self._vault = vault
        self._storage_backend = storage_backend
        self._fingerprinter = CredentialFingerprinter(vault)

    def status(self) -> dict[str, Any]:
        value = self._vault.status().to_public_dict()
        value["storage_backend"] = self._storage_backend
        value["persistence_enabled"] = value["available"]
        return value

    def list(
        self, *, workspace_id: str, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        return self._store.list_credential_profiles(
            workspace_id=workspace_id, include_archived=include_archived
        )

    def get(
        self,
        credential_id: str,
        *,
        workspace_id: str,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        return self._store.get_credential_profile(
            credential_id,
            workspace_id=workspace_id,
            include_archived=include_archived,
        )

    def get_for_execution(
        self,
        credential_id: str,
        *,
        workspace_id: str,
        provider_id: str | None,
    ) -> SecretStr:
        """Resolve a secret only at a provider execution boundary.

        The public metadata lookup remains workspace scoped.  Neither the
        locator nor the returned SecretStr is suitable for durable payloads.
        """
        profile = self._store.get_credential_profile(
            credential_id,
            workspace_id=workspace_id,
            include_archived=True,
        )
        if profile["status"] != "ACTIVE":
            raise ValueError("credential profile is not active")
        if provider_id is not None and profile["provider_id"] != provider_id:
            raise ValueError("credential provider does not match the selected channel")
        locator = self._store.get_credential_locator(
            credential_id,
            workspace_id=workspace_id,
        )
        return self._vault.get_for_execution(locator)

    def create(
        self,
        *,
        workspace_id: str,
        actor_user_id: str,
        label: str,
        provider_id: str,
        api_key: SecretStr,
        idempotency_key: str,
    ) -> dict[str, Any]:
        normalized_idempotency = str(UUID(idempotency_key))
        request_sha256 = hashlib.sha256(
            json.dumps(
                {"label": label.strip(), "provider_id": provider_id},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        existing = self._store.find_credential_profile_by_idempotency(
            normalized_idempotency,
            request_sha256,
            workspace_id=workspace_id,
        )
        if existing is not None:
            return existing

        provider = get_provider(provider_id)
        if provider is None or provider.target_kind != "cloud":
            raise ValueError("provider_id must identify a registered cloud provider")
        raw = api_key.get_secret_value()
        validate_api_key_value(raw)
        locator = uuid4()
        fingerprint = self._fingerprinter.fingerprint(api_key)
        suffix = raw[-4:] if re.fullmatch(r"[A-Za-z0-9_-]{1,4}", raw[-4:]) else ""
        credential_id = str(uuid4())
        self._vault.put(locator, api_key)
        try:
            result = self._store.create_credential_profile(
                {
                    "credential_id": credential_id,
                    "label": label.strip(),
                    "provider_id": provider_id,
                    "storage_backend": self._storage_backend,
                    "secret_locator": str(locator),
                    "masked_suffix": suffix,
                    "fingerprint": fingerprint,
                    "idempotency_key": normalized_idempotency,
                    "request_sha256": request_sha256,
                    "actor_user_id": actor_user_id,
                },
                workspace_id=workspace_id,
            )
            if result["id"] != credential_id:
                self._vault.delete(locator)
            return result
        except BaseException:
            try:
                self._vault.delete(locator)
            except VaultUnavailable as cleanup_error:
                raise ControlPlaneConflict(
                    "credential metadata failed and secure-store cleanup must be retried"
                ) from cleanup_error
            raise

    def rename(
        self,
        credential_id: str,
        *,
        workspace_id: str,
        actor_user_id: str,
        label: str,
        version: int,
    ) -> dict[str, Any]:
        return self._store.update_credential_profile(
            credential_id,
            {"label": label.strip()},
            workspace_id=workspace_id,
            expected_version=version,
            audit_action="RENAME",
            actor_user_id=actor_user_id,
        )

    def replace(
        self,
        credential_id: str,
        *,
        workspace_id: str,
        actor_user_id: str,
        api_key: SecretStr,
        version: int,
    ) -> dict[str, Any]:
        raw = api_key.get_secret_value()
        validate_api_key_value(raw)
        locator = self._store.get_credential_locator(
            credential_id, workspace_id=workspace_id
        )
        previous = self._vault.get_for_execution(locator)
        fingerprint = self._fingerprinter.fingerprint(api_key)
        suffix = raw[-4:] if re.fullmatch(r"[A-Za-z0-9_-]{1,4}", raw[-4:]) else ""
        self._vault.replace(locator, api_key)
        try:
            return self._store.update_credential_profile(
                credential_id,
                {
                    "fingerprint": fingerprint,
                    "masked_suffix": suffix,
                    "status": "ACTIVE",
                    "last_verified_at": None,
                },
                workspace_id=workspace_id,
                expected_version=version,
                audit_action="REPLACE",
                actor_user_id=actor_user_id,
            )
        except BaseException:
            self._vault.replace(locator, previous)
            raise

    def archive(
        self,
        credential_id: str,
        *,
        workspace_id: str,
        actor_user_id: str,
        version: int,
    ) -> dict[str, Any]:
        current = self._store.get_credential_profile(
            credential_id, workspace_id=workspace_id, include_archived=True
        )
        if current["status"] == "ARCHIVED":
            return current
        if int(current["version"]) != version:
            raise ControlPlaneConflict("credential profile version is stale")
        if current["status"] != "DELETION_PENDING":
            current = self._store.update_credential_profile(
                credential_id,
                {"status": "DELETION_PENDING"},
                workspace_id=workspace_id,
                expected_version=version,
                audit_action="ARCHIVE",
                actor_user_id=actor_user_id,
            )
        locator = self._store.get_credential_locator(
            credential_id, workspace_id=workspace_id
        )
        try:
            self._vault.delete(locator)
        except CredentialNotFound:
            pass
        except VaultUnavailable:
            self._store.update_credential_profile(
                credential_id,
                {"status": "VAULT_UNAVAILABLE"},
                workspace_id=workspace_id,
                expected_version=int(current["version"]),
                audit_action="ARCHIVE",
                actor_user_id=actor_user_id,
            )
            raise
        return self._store.update_credential_profile(
            credential_id,
            {"status": "ARCHIVED", "archived_at": "NOW"},
            workspace_id=workspace_id,
            expected_version=int(current["version"]),
            audit_action="DELETE_SECRET",
            actor_user_id=actor_user_id,
        )
