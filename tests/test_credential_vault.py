from __future__ import annotations

import unittest
from unittest.mock import patch
from uuid import uuid4

from pydantic import SecretStr

from lexsond.credentials import (
    EphemeralCredentialVault,
    ExecutionCredentialBinder,
    SystemCredentialVault,
    VaultUnavailable,
)


class CredentialVaultTests(unittest.TestCase):
    def test_execution_binding_is_stable_keyed_and_independent_of_vault(self) -> None:
        binder = ExecutionCredentialBinder(b"b" * 32)
        workspace = str(uuid4())
        first = binder.fingerprint(
            SecretStr("sk-catalog-binding-one"), workspace_id=workspace
        )
        second = binder.fingerprint(
            SecretStr("sk-catalog-binding-one"), workspace_id=workspace
        )
        other = binder.fingerprint(
            SecretStr("sk-catalog-binding-two"), workspace_id=workspace
        )
        other_workspace = binder.fingerprint(
            SecretStr("sk-catalog-binding-one"), workspace_id=str(uuid4())
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertNotEqual(first, other_workspace)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotIn("sk-catalog-binding-one", repr(binder))

    def test_required_cloud_binding_fails_closed_without_shared_key(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError):
                ExecutionCredentialBinder.from_environment(require_configured=True)

    def test_ephemeral_vault_never_exposes_values_in_repr_or_status(self) -> None:
        credential_id = uuid4()
        secret = "vault-test-secret-sentinel"
        vault = EphemeralCredentialVault()
        vault.put(credential_id, SecretStr(secret))

        self.assertEqual(
            vault.get_for_execution(credential_id).get_secret_value(), secret
        )
        self.assertNotIn(secret, repr(vault))
        self.assertNotIn(secret, str(vault.status().to_public_dict()))
        vault.replace(credential_id, SecretStr("replacement-secret"))
        self.assertEqual(
            vault.get_for_execution(credential_id).get_secret_value(),
            "replacement-secret",
        )
        vault.delete(credential_id)
        with self.assertRaises(VaultUnavailable):
            vault.get_for_execution(credential_id)

    def test_system_vault_uses_only_an_internal_uuid_locator(self) -> None:
        backend = _Backend()
        vault = SystemCredentialVault(backend)
        credential_id = uuid4()
        secret = "system-keyring-secret-sentinel"
        vault.put(credential_id, SecretStr(secret))

        self.assertEqual(
            backend.calls[0][:2], (SystemCredentialVault.SERVICE_NAME, str(credential_id))
        )
        self.assertNotIn(secret, repr(vault))
        self.assertEqual(vault.get_for_execution(credential_id).get_secret_value(), secret)
        vault.delete(credential_id)

    def test_unavailable_backend_fails_closed_without_memory_fallback(self) -> None:
        backend = _Backend(priority=0)
        vault = SystemCredentialVault(backend)
        secret = "must-not-fall-back-to-memory"

        with self.assertRaisesRegex(VaultUnavailable, "没有可用"):
            vault.put(uuid4(), SecretStr(secret))
        self.assertEqual(backend.values, {})
        self.assertNotIn(secret, repr(vault))

    def test_replace_failure_preserves_the_previous_secret(self) -> None:
        backend = _Backend()
        vault = SystemCredentialVault(backend)
        credential_id = uuid4()
        vault.put(credential_id, SecretStr("old-secret"))
        backend.fail_next_set = True

        with self.assertRaisesRegex(VaultUnavailable, "write failed"):
            vault.replace(credential_id, SecretStr("new-secret"))
        self.assertEqual(
            vault.get_for_execution(credential_id).get_secret_value(), "old-secret"
        )

    def test_secret_shape_rejects_control_characters_and_oversize(self) -> None:
        vault = EphemeralCredentialVault()
        for value in ("line\nbreak", "x" * 8193):
            with self.subTest(length=len(value)):
                with self.assertRaises(ValueError):
                    vault.put(uuid4(), SecretStr(value))


class _Backend:
    def __init__(self, *, priority: float = 1) -> None:
        self.priority = priority
        self.values: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str, str]] = []
        self.fail_next_set = False

    def set_password(self, service: str, locator: str, secret: str) -> None:
        if self.fail_next_set:
            self.fail_next_set = False
            raise RuntimeError("backend failure containing no public detail")
        self.calls.append((service, locator, secret))
        self.values[(service, locator)] = secret

    def get_password(self, service: str, locator: str) -> str | None:
        return self.values.get((service, locator))

    def delete_password(self, service: str, locator: str) -> None:
        self.values.pop((service, locator))


if __name__ == "__main__":
    unittest.main()
