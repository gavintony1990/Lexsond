from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from lexsond.workflows.native_activities import (
    CredentialReferenceEnvironmentSecretResolver,
)


class CredentialReferenceEnvironmentSecretResolverTests(unittest.TestCase):
    def test_binding_file_contains_no_secret_and_resolves_injected_value(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.json"
            path.write_text(
                json.dumps(
                    {
                        "apiVersion": "probe.ai/credential-bindings/v1alpha1",
                        "kind": "CredentialEnvironmentBindingList",
                        "items": [
                            {
                                "credential_ref": "vault://ai-probe/relay",
                                "environment_variable": "LEXSOND_SECRET_RELAY",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            resolver = CredentialReferenceEnvironmentSecretResolver.from_file(
                path.resolve()
            )
            previous = os.environ.get("LEXSOND_SECRET_RELAY")
            os.environ["LEXSOND_SECRET_RELAY"] = "never-serialize-this-value"
            try:
                self.assertEqual(
                    resolver.resolve("vault://ai-probe/relay"),
                    "never-serialize-this-value",
                )
                self.assertNotIn("never-serialize-this-value", repr(resolver))
                self.assertNotIn("never-serialize-this-value", path.read_text())
            finally:
                if previous is None:
                    os.environ.pop("LEXSOND_SECRET_RELAY", None)
                else:
                    os.environ["LEXSOND_SECRET_RELAY"] = previous

    def test_rejects_inline_secret_unknown_fields_and_unsafe_reference(self) -> None:
        invalid_items = (
            {
                "credential_ref": "vault://ai-probe/relay",
                "environment_variable": "LEXSOND_SECRET_RELAY",
                "secret": "forbidden",
            },
            {
                "credential_ref": "vault://user:password@ai-probe/relay",
                "environment_variable": "LEXSOND_SECRET_RELAY",
            },
            {
                "credential_ref": "vault://ai-probe/relay?version=1",
                "environment_variable": "LEXSOND_SECRET_RELAY",
            },
        )
        for item in invalid_items:
            with self.subTest(item=item), TemporaryDirectory() as directory:
                path = Path(directory) / "bindings.json"
                path.write_text(
                    json.dumps(
                        {
                            "apiVersion": "probe.ai/credential-bindings/v1alpha1",
                            "kind": "CredentialEnvironmentBindingList",
                            "items": [item],
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError):
                    CredentialReferenceEnvironmentSecretResolver.from_file(
                        path.resolve()
                    )


if __name__ == "__main__":
    unittest.main()
