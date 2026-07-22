from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]
MIGRATIONS = PROJECT_ROOT / "migrations"


class AuthWorkspaceContractTests(unittest.TestCase):
    def test_auth_and_workspace_adr_freezes_security_boundaries(self) -> None:
        adr = (PROJECT_ROOT / "docs/adr/010-auth-workspace-tenancy.md").read_text(
            encoding="utf-8"
        )
        for contract in (
            "LEXSOND_AUTH_MODE",
            "local-single-user",
            "loopback",
            "Argon2id",
            "lexsond_session",
            "HttpOnly",
            "SameSite=Lax",
            "X-CSRF-Token",
            "Legacy Workspace",
            "workspace_id",
            "provider_subject",
            "PostgreSQL",
        ):
            self.assertIn(contract, adr)
        self.assertIn("OAuth access token", adr)
        self.assertIn("never persisted", adr)

    def test_auth_dependency_is_pinned_with_recorded_license(self) -> None:
        project = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        adr = (PROJECT_ROOT / "docs/adr/010-auth-workspace-tenancy.md").read_text(
            encoding="utf-8"
        )
        self.assertIn('"argon2-cffi==25.1.0"', project)
        self.assertIn("argon2-cffi 25.1.0", adr)
        self.assertIn("MIT", adr)

    def test_identity_migration_has_hashed_session_and_action_token_boundaries(self) -> None:
        up = (MIGRATIONS / "0007_auth_workspaces.sql").read_text(encoding="utf-8")
        down = (MIGRATIONS / "0007_auth_workspaces.down.sql").read_text(
            encoding="utf-8"
        )
        for table in (
            "users",
            "oauth_identities",
            "auth_sessions",
            "auth_action_tokens",
            "workspaces",
            "workspace_members",
            "auth_audit_events",
            "auth_rate_limits",
            "auth_session_csrf_tokens",
        ):
            self.assertIn(f"CREATE TABLE lexsond.{table}", up)
            self.assertIn(f"DROP TABLE IF EXISTS lexsond.{table}", down)

        self.assertIn("token_hash BYTEA", up)
        self.assertIn("csrf_secret_hash BYTEA", up)
        self.assertNotIn("session_token TEXT", up)
        self.assertNotIn("csrf_token TEXT", up)
        self.assertIn("PENDING_VERIFICATION", up)
        self.assertIn("SUSPENDED", up)
        self.assertIn("OWNER", up)
        self.assertIn("VIEWER", up)
        self.assertIn("provider_subject", up)
        self.assertIn("UNIQUE (provider, provider_subject)", up)

    def test_existing_mutable_resources_are_assigned_to_legacy_workspace(self) -> None:
        up = (MIGRATIONS / "0007_auth_workspaces.sql").read_text(encoding="utf-8")
        for table in (
            "targets",
            "suites",
            "suite_revisions",
            "probe_runs",
            "agent_sessions",
            "monitor_policies",
        ):
            self.assertIn(f"ALTER TABLE lexsond.{table}", up)
        self.assertIn("Legacy Workspace", up)
        self.assertIn("legacy_workspace", up)
        self.assertIn("SET workspace_id = legacy_workspace", up)
        self.assertIn("SET NOT NULL", up)
        self.assertIn("FOREIGN KEY (workspace_id, target_id)", up)
        self.assertIn("FOREIGN KEY (workspace_id, suite_id)", up)
        self.assertIn(
            "REFERENCES lexsond.suite_revisions(workspace_id, revision_id)", up
        )

    def test_workspace_uniqueness_replaces_global_user_resource_names(self) -> None:
        up = (MIGRATIONS / "0007_auth_workspaces.sql").read_text(encoding="utf-8")
        self.assertIn("DROP CONSTRAINT targets_name_key", up)
        self.assertIn("DROP CONSTRAINT suites_name_key", up)
        self.assertIn("DROP CONSTRAINT monitor_policies_name_key", up)
        self.assertIn("UNIQUE (workspace_id, name)", up)

    def test_down_migration_disambiguates_cross_workspace_collisions(self) -> None:
        down = (MIGRATIONS / "0007_auth_workspaces.down.sql").read_text(
            encoding="utf-8"
        )
        self.assertIn("GROUP BY name HAVING count(*) > 1", down)
        self.assertIn("SET idempotency_key = NULL", down)


if __name__ == "__main__":
    unittest.main()
