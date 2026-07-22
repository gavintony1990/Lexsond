from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]
MIGRATIONS = PROJECT_ROOT / "migrations"


class CredentialPersistenceContractTests(unittest.TestCase):
    def test_metadata_schema_is_workspace_scoped_and_has_no_secret_column(self) -> None:
        up = (MIGRATIONS / "0008_credential_profiles.sql").read_text(
            encoding="utf-8"
        )
        down = (MIGRATIONS / "0008_credential_profiles.down.sql").read_text(
            encoding="utf-8"
        )

        for table in (
            "credential_profiles",
            "target_credential_bindings",
            "credential_audit_events",
        ):
            self.assertIn(f"CREATE TABLE lexsond.{table}", up)
            self.assertIn(f"DROP TABLE IF EXISTS lexsond.{table}", down)

        self.assertIn("workspace_id UUID NOT NULL", up)
        self.assertIn("secret_locator UUID NOT NULL", up)
        self.assertIn("fingerprint CHAR(64)", up)
        self.assertIn("FOREIGN KEY (workspace_id, credential_id)", up)
        self.assertIn("FOREIGN KEY (workspace_id, target_id)", up)
        self.assertIn("UNIQUE (workspace_id, credential_id)", up)
        self.assertIn("UNIQUE (workspace_id, fingerprint)", up)

        lowered = up.lower()
        for forbidden in (
            "api_key text",
            "secret text",
            "ciphertext",
            "encrypted_key",
            "authorization text",
        ):
            self.assertNotIn(forbidden, lowered)

    def test_keyring_dependency_is_pinned_and_license_is_recorded(self) -> None:
        project = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        adr = (PROJECT_ROOT / "docs/adr/010-auth-workspace-tenancy.md").read_text(
            encoding="utf-8"
        )
        self.assertIn('"keyring==25.7.0"', project)
        self.assertIn("keyring 25.7.0", adr)
        self.assertIn("MIT", adr)


if __name__ == "__main__":
    unittest.main()
