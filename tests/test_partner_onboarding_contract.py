from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class PartnerOnboardingContractTests(unittest.TestCase):
    def test_workspace_scoped_versioned_partner_application_schema(self) -> None:
        up = (ROOT / "migrations/0009_partner_onboarding.sql").read_text(
            encoding="utf-8"
        )
        down = (ROOT / "migrations/0009_partner_onboarding.down.sql").read_text(
            encoding="utf-8"
        )
        for table in (
            "partner_applications",
            "partner_application_revisions",
            "partner_domain_challenges",
        ):
            self.assertIn(f"CREATE TABLE lexsond.{table}", up)
            self.assertIn(f"DROP TABLE IF EXISTS lexsond.{table}", down)
        self.assertIn("workspace_id UUID NOT NULL", up)
        self.assertIn("FOREIGN KEY (workspace_id, monitoring_credential_id)", up)
        self.assertIn("contains_forbidden_secret_key(snapshot_json)", up)
        self.assertIn("contains_recognizable_secret_value(snapshot_json)", up)
        self.assertIn("token_hash BYTEA", up)
        self.assertNotIn("challenge_token TEXT", up)
        for status in (
            "DRAFT", "SUBMITTED", "OWNERSHIP_PENDING", "MANUAL_REVIEW",
            "BASELINE_TEST", "PROBATION", "APPROVED", "REJECTED", "PUBLISHED",
        ):
            self.assertIn(status, up)


if __name__ == "__main__":
    unittest.main()
