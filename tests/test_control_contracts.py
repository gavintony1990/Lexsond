from __future__ import annotations

import unittest

from lexsond.web.control_contracts import (
    ControlPlaneConflict,
    _contains_forbidden_agent_key,
    _require_postgres_store_contract,
    _require_agent_turn_token,
    _validate_agent_session_value,
)


class ControlContractTests(unittest.TestCase):
    def test_postgres_store_contract_fails_fast_for_missing_capability(self) -> None:
        class IncompleteStore:
            def close(self) -> None:
                return None

        with self.assertRaisesRegex(ValueError, "claim_due_monitor_policies"):
            _require_postgres_store_contract(IncompleteStore())

    def test_agent_memory_rejects_credentials_in_durable_snapshot_fields(self) -> None:
        base = {
            "title": "Connection review",
            "model": "model-a",
            "base_url": "https://example.invalid/v1",
            "skill_id": "connection-diagnosis",
        }
        credential_shape = "sk-" + "x" * 32
        for field, value in (
            ("title", credential_shape),
            ("model", credential_shape),
            ("base_url", "https://user:password@example.invalid/v1"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    _validate_agent_session_value({**base, field: value})

    def test_agent_payload_detects_nested_secret_keys(self) -> None:
        self.assertTrue(
            _contains_forbidden_agent_key(
                {"safe": [{"nested": {"authorization": "sentinel"}}]}
            )
        )
        self.assertFalse(
            _contains_forbidden_agent_key(
                {"safe": [{"nested": {"status": "PASS"}}]}
            )
        )

    def test_agent_turn_fencing_rejects_stale_writer(self) -> None:
        row = {"turn_lease_token": "current-token"}
        _require_agent_turn_token(row, "current-token")
        with self.assertRaisesRegex(ControlPlaneConflict, "stale"):
            _require_agent_turn_token(row, "expired-token")


if __name__ == "__main__":
    unittest.main()
