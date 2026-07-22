from __future__ import annotations

import unittest
from uuid import UUID, uuid4

from pydantic import SecretStr

from lexsond.credentials import EphemeralCredentialVault
from lexsond.web.credential_service import CredentialProfileService


class CredentialProfileServiceTests(unittest.TestCase):
    def test_execution_secret_is_workspace_scoped_active_and_provider_bound(self) -> None:
        store = _Store()
        vault = EphemeralCredentialVault()
        service = CredentialProfileService(
            store=store, vault=vault, storage_backend="SYSTEM_KEYRING"
        )
        secret = "sk-execution-boundary-sentinel"
        created = service.create(
            workspace_id=_WORKSPACE_ID,
            actor_user_id=_USER_ID,
            label="Primary",
            provider_id="openai",
            api_key=SecretStr(secret),
            idempotency_key=str(uuid4()),
        )

        resolved = service.get_for_execution(
            created["id"],
            workspace_id=_WORKSPACE_ID,
            provider_id="openai",
        )

        self.assertEqual(resolved.get_secret_value(), secret)
        self.assertNotIn(secret, repr(resolved))
        with self.assertRaisesRegex(ValueError, "provider"):
            service.get_for_execution(
                created["id"],
                workspace_id=_WORKSPACE_ID,
                provider_id="deepseek",
            )
        store.created["status"] = "ARCHIVED"
        with self.assertRaisesRegex(ValueError, "not active"):
            service.get_for_execution(
                created["id"],
                workspace_id=_WORKSPACE_ID,
                provider_id="openai",
            )

    def test_create_writes_secret_only_to_vault_and_returns_public_metadata(self) -> None:
        store = _Store()
        vault = EphemeralCredentialVault()
        service = CredentialProfileService(
            store=store, vault=vault, storage_backend="SYSTEM_KEYRING"
        )
        secret = "sk-service-secret-sentinel"
        idempotency_key = str(uuid4())

        result = service.create(
            workspace_id=_WORKSPACE_ID,
            actor_user_id=_USER_ID,
            label="Primary",
            provider_id="openai",
            api_key=SecretStr(secret),
            idempotency_key=idempotency_key,
        )

        self.assertEqual(result["masked_suffix"], "inel")
        self.assertNotIn(secret, repr(store.created))
        self.assertNotIn("fingerprint", result)
        self.assertNotIn("secret_locator", result)
        self.assertEqual(
            vault.get_for_execution(UUID(store.locator)).get_secret_value(), secret
        )

        replay = service.create(
            workspace_id=_WORKSPACE_ID,
            actor_user_id=_USER_ID,
            label="Primary",
            provider_id="openai",
            api_key=SecretStr("sk-different-replay-value"),
            idempotency_key=idempotency_key,
        )
        self.assertEqual(replay["id"], result["id"])
        self.assertEqual(store.create_count, 1)

    def test_metadata_failure_removes_the_orphaned_secret(self) -> None:
        store = _Store(fail_create=True)
        vault = EphemeralCredentialVault()
        service = CredentialProfileService(
            store=store, vault=vault, storage_backend="SYSTEM_KEYRING"
        )

        with self.assertRaisesRegex(RuntimeError, "metadata unavailable"):
            service.create(
                workspace_id=_WORKSPACE_ID,
                actor_user_id=_USER_ID,
                label="Primary",
                provider_id="openai",
                api_key=SecretStr("sk-cleanup-after-failure"),
                idempotency_key=str(uuid4()),
            )
        with self.assertRaises(Exception):
            vault.get_for_execution(UUID(store.locator))

    def test_replace_rolls_back_vault_if_metadata_update_fails(self) -> None:
        store = _Store()
        vault = EphemeralCredentialVault()
        service = CredentialProfileService(
            store=store, vault=vault, storage_backend="SYSTEM_KEYRING"
        )
        created = service.create(
            workspace_id=_WORKSPACE_ID,
            actor_user_id=_USER_ID,
            label="Primary",
            provider_id="openai",
            api_key=SecretStr("sk-original-value"),
            idempotency_key=str(uuid4()),
        )
        store.fail_update = True

        with self.assertRaisesRegex(RuntimeError, "metadata update unavailable"):
            service.replace(
                created["id"],
                workspace_id=_WORKSPACE_ID,
                actor_user_id=_USER_ID,
                api_key=SecretStr("sk-replacement-value"),
                version=1,
            )
        self.assertEqual(
            vault.get_for_execution(UUID(store.locator)).get_secret_value(),
            "sk-original-value",
        )


_WORKSPACE_ID = "20000000-0000-4000-8000-000000000001"
_USER_ID = "10000000-0000-4000-8000-000000000001"


class _Store:
    def __init__(self, *, fail_create: bool = False) -> None:
        self.fail_create = fail_create
        self.fail_update = False
        self.created: dict = {}
        self.locator = str(uuid4())
        self.create_count = 0

    def find_credential_profile_by_idempotency(
        self, key, request_sha256, *, workspace_id
    ):
        if (
            self.created
            and key == self.created["idempotency_key"]
            and workspace_id == _WORKSPACE_ID
        ):
            if request_sha256 != self.created["request_sha256"]:
                raise RuntimeError("idempotency mismatch")
            return self._public()
        return None

    def create_credential_profile(self, value, *, workspace_id):
        self.locator = value["secret_locator"]
        if self.fail_create:
            raise RuntimeError("metadata unavailable")
        self.create_count += 1
        self.created = {**value, "workspace_id": workspace_id, "version": 1}
        return self._public()

    def list_credential_profiles(self, *, workspace_id, include_archived=False):
        del workspace_id, include_archived
        return [self._public()] if self.created else []

    def get_credential_profile(self, credential_id, *, workspace_id, include_archived=False):
        del credential_id, include_archived
        if workspace_id != self.created["workspace_id"]:
            raise KeyError("credential profile not found")
        return self._public()

    def get_credential_locator(self, credential_id, *, workspace_id):
        del credential_id, workspace_id
        return UUID(self.locator)

    def update_credential_profile(self, credential_id, changes, **kwargs):
        del credential_id, kwargs
        if self.fail_update:
            raise RuntimeError("metadata update unavailable")
        self.created.update(changes)
        self.created["version"] += 1
        return self._public()

    def _public(self):
        return {
            "id": self.created["credential_id"],
            "workspace_id": self.created["workspace_id"],
            "label": self.created["label"],
            "provider_id": self.created["provider_id"],
            "storage_backend": self.created["storage_backend"],
            "masked_suffix": self.created["masked_suffix"],
            "status": self.created.get("status", "ACTIVE"),
            "version": self.created["version"],
        }


if __name__ == "__main__":
    unittest.main()
