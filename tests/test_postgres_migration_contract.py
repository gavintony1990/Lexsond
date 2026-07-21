from __future__ import annotations

import unittest
from pathlib import Path


MIGRATIONS = Path(__file__).parents[1] / "migrations"


class PostgresMigrationContractTests(unittest.TestCase):
    def test_core_migration_contains_required_durability_boundaries(self) -> None:
        sql = (MIGRATIONS / "0001_core.sql").read_text(encoding="utf-8")
        required = (
            "CREATE TABLE lexsond.endpoint_snapshots",
            "CREATE TABLE lexsond.probe_suite_snapshots",
            "CREATE TABLE lexsond.workflow_runs",
            "CREATE TABLE lexsond.workflow_events",
            "CREATE TABLE lexsond.probe_results",
            "CREATE TABLE lexsond.evidence_objects",
            "CREATE TABLE lexsond.activity_executions",
            "PRIMARY KEY (run_id, sequence)",
            "CREATE OR REPLACE FUNCTION lexsond.append_workflow_event",
            "CREATE OR REPLACE FUNCTION lexsond.claim_activity_execution",
            "CREATE OR REPLACE FUNCTION lexsond.complete_activity_execution",
            "CREATE OR REPLACE FUNCTION lexsond.renew_activity_execution",
            "CREATE OR REPLACE FUNCTION lexsond.fail_activity_execution",
            "CREATE OR REPLACE FUNCTION lexsond.persist_probe_result",
            "AND last_sequence = p_expected_sequence",
            "AND event_json = p_event",
            "USING ERRCODE = '40001'",
            "workflow_events_are_append_only",
            "probe_results_are_append_only",
            "endpoint_snapshots_are_append_only",
            "probe_suite_snapshots_are_append_only",
        )
        for contract in required:
            self.assertIn(contract, sql)

    def test_snapshot_tables_forbid_top_level_secret_fields(self) -> None:
        sql = (MIGRATIONS / "0001_core.sql").read_text(encoding="utf-8")
        for forbidden_key in (
            "'api_key'",
            "'authorization'",
            "'credential_handle'",
            "'secret'",
        ):
            self.assertIn(forbidden_key, sql)

    def test_down_migration_covers_every_created_table(self) -> None:
        up = (MIGRATIONS / "0001_core.sql").read_text(encoding="utf-8")
        down = (MIGRATIONS / "0001_core.down.sql").read_text(encoding="utf-8")
        for table in (
            "endpoint_snapshots",
            "probe_suite_snapshots",
            "workflow_runs",
            "workflow_events",
            "probe_results",
            "evidence_objects",
            "activity_executions",
        ):
            self.assertIn(f"CREATE TABLE lexsond.{table}", up)
            self.assertIn(f"DROP TABLE IF EXISTS lexsond.{table}", down)

    def test_access_migration_enforces_worker_function_boundary(self) -> None:
        sql = (MIGRATIONS / "0002_access.sql").read_text(encoding="utf-8")

        self.assertIn("REVOKE ALL ON ALL TABLES IN SCHEMA lexsond FROM PUBLIC", sql)
        self.assertIn("GRANT EXECUTE ON FUNCTION", sql)
        self.assertIn("OWNER TO lexsond_runtime_owner", sql)
        self.assertIn("REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC", sql)
        self.assertNotIn(
            "GRANT INSERT ON lexsond.workflow_events TO lexsond_worker",
            sql,
        )

    def test_control_plane_migration_covers_crud_and_secret_boundaries(self) -> None:
        up = (MIGRATIONS / "0003_control_plane.sql").read_text(encoding="utf-8")
        down = (MIGRATIONS / "0003_control_plane.down.sql").read_text(
            encoding="utf-8"
        )
        for table in (
            "targets",
            "suites",
            "suite_revisions",
            "probe_runs",
            "probe_run_events",
        ):
            self.assertIn(f"CREATE TABLE lexsond.{table}", up)
            self.assertIn(f"DROP TABLE IF EXISTS lexsond.{table}", down)
        self.assertIn("contains_forbidden_secret_key(document_json)", up)
        self.assertIn("contains_forbidden_secret_key(result_json)", up)
        self.assertIn("suite_revisions_are_immutable", up)
        self.assertIn("TO lexsond_control", up)

    def test_agent_migration_covers_memory_events_and_secret_boundaries(self) -> None:
        up = (MIGRATIONS / "0004_agent_control_plane.sql").read_text(
            encoding="utf-8"
        )
        down = (MIGRATIONS / "0004_agent_control_plane.down.sql").read_text(
            encoding="utf-8"
        )
        for table in ("agent_sessions", "agent_messages", "agent_events"):
            self.assertIn(f"CREATE TABLE lexsond.{table}", up)
            self.assertIn(f"DROP TABLE IF EXISTS lexsond.{table}", down)
        self.assertIn("contains_forbidden_secret_key(metadata_json)", up)
        self.assertIn("contains_forbidden_secret_key(payload_json)", up)
        self.assertIn("turn_lease_token UUID", up)
        self.assertIn("turn_lease_until TIMESTAMPTZ", up)
        self.assertIn("authorization", up)
        self.assertIn("TO lexsond_control", up)

    def test_monitoring_migration_has_leases_state_events_and_reader_boundary(self) -> None:
        up = (MIGRATIONS / "0005_continuous_monitoring.sql").read_text(
            encoding="utf-8"
        )
        down = (MIGRATIONS / "0005_continuous_monitoring.down.sql").read_text(
            encoding="utf-8"
        )
        for table in (
            "monitor_policies",
            "monitor_states",
            "monitor_samples",
            "monitor_incident_events",
        ):
            self.assertIn(f"CREATE TABLE lexsond.{table}", up)
            self.assertIn(f"DROP TABLE IF EXISTS lexsond.{table}", down)
        self.assertIn("lease_token UUID", up)
        self.assertIn("schedule_offset_seconds", up)
        self.assertIn("monitor_samples_are_immutable", up)
        self.assertIn("monitor_incidents_are_immutable", up)
        self.assertIn("ON DELETE SET NULL", up)
        self.assertIn("TO lexsond_control", up)
        self.assertIn("TO lexsond_reader", up)


if __name__ == "__main__":
    unittest.main()
