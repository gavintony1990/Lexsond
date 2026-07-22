from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class EvaluationMigrationContractTests(unittest.TestCase):
    def test_postgres_migration_has_workspace_immutable_and_secret_boundaries(self) -> None:
        up = (ROOT / "migrations/0011_evaluations.sql").read_text(encoding="utf-8")
        down = (ROOT / "migrations/0011_evaluations.down.sql").read_text(encoding="utf-8")
        tables = (
            "evaluation_datasets",
            "evaluation_dataset_revisions",
            "evaluation_dataset_items",
            "evaluation_runs",
            "evaluation_run_models",
            "evaluation_run_items",
            "evaluation_run_events",
        )
        for table in tables:
            self.assertIn(f"CREATE TABLE lexsond.{table}", up)
            self.assertIn(f"DROP TABLE IF EXISTS lexsond.{table}", down)
        self.assertIn("evaluation_dataset_revisions_are_immutable", up)
        self.assertIn("evaluation_dataset_items_are_immutable", up)
        self.assertIn("evaluation_dataset_items_require_open_revision", up)
        self.assertIn("evaluation_dataset_revision_must_be_sealed", up)
        self.assertIn("v_actual_count <> v_declared_count", up)
        self.assertIn("evaluation_run_items_are_immutable", up)
        self.assertIn("evaluation_run_snapshot_is_immutable", up)
        self.assertIn("evaluation_run_model_result_is_immutable", up)
        self.assertIn("evaluation_run_items_require_running_parent", up)
        self.assertIn("terminal evaluation result is immutable", up)
        self.assertIn("current_user <> session_user", up)
        self.assertIn(
            "ALTER FUNCTION lexsond.purge_evaluation_run(UUID, UUID)\n    OWNER TO lexsond_runtime_owner",
            up,
        )
        self.assertNotIn(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON lexsond.evaluation_datasets",
            up,
        )
        self.assertIn(
            "GRANT SELECT, INSERT, UPDATE ON lexsond.evaluation_dataset_revisions "
            "TO lexsond_control",
            up,
        )
        self.assertNotIn(
            "GRANT SELECT, INSERT, UPDATE ON lexsond.evaluation_dataset_items",
            up,
        )
        self.assertIn("contains_forbidden_secret_key", up)
        for normalized_secret_key in ("'apikey'", "'clientsecret'", "'password'"):
            self.assertIn(normalized_secret_key, up)
        self.assertIn("contains_recognizable_secret_value", up)
        self.assertIn("CHECK (model_count BETWEEN 1 AND 10)", up)
        self.assertIn("CHECK (sample_count BETWEEN 1 AND 200)", up)
        self.assertIn("CHECK (concurrency BETWEEN 1 AND 2)", up)
        self.assertIn("FOREIGN KEY (workspace_id, channel_id)", up)
        self.assertIn("evaluation_run_dataset_scope_is_valid", up)
        self.assertIn("workspace evaluation dataset cannot be referenced across workspaces", up)
        self.assertIn("execution_lease_id UUID NOT NULL", up)
        self.assertIn("lease_expires_at TIMESTAMPTZ NOT NULL", up)
        self.assertIn("catalog_snapshot_id UUID NOT NULL", up)
        self.assertIn("credential_fingerprint CHAR(64)", up)
        self.assertIn("credential_version INTEGER", up)
        self.assertIn(
            "REVOKE SELECT ON lexsond.model_catalog_snapshots FROM lexsond_reader",
            up,
        )
        reader_projection = up[
            up.index("GRANT SELECT (\n    snapshot_id") :
            up.index(") ON lexsond.model_catalog_snapshots TO lexsond_reader")
        ]
        self.assertNotIn("credential_fingerprint", reader_projection)
        self.assertNotIn("credential_version", reader_projection)
        self.assertIn("target_base_url TEXT", up)
        self.assertIn("protocol TEXT", up)
        self.assertIn("is_safe_snapshot_base_url", up)
        self.assertIn("v_address << inet '127.0.0.0/8'", up)
        self.assertIn("v_address = inet '::1'", up)
        self.assertNotIn("127\\.0\\.0\\.1|\\[::1\\]", up)
        self.assertIn("UNIQUE (evaluation_run_id, sequence)", up)
        self.assertIn("CHECK (sequence BETWEEN 1 AND 2000)", up)
        self.assertIn("(state = 'RUNNING') = (aggregate_result_json IS NULL)", up)
        run_models_definition = up[
            up.index("CREATE TABLE lexsond.evaluation_run_models") :
            up.index("CREATE TABLE lexsond.evaluation_run_items")
        ]
        self.assertEqual(
            run_models_definition.count("completed_items INTEGER"),
            1,
            "evaluation_run_models must define completed_items exactly once",
        )
        self.assertNotIn("api_key TEXT", up.lower())
        self.assertNotIn("raw_output", up.lower())

    def test_terminal_event_and_run_projection_share_one_transactional_store_method(self) -> None:
        source = (ROOT / "src/lexsond/web/postgres_evaluation_store.py").read_text(
            encoding="utf-8"
        )
        finish = source[source.index("    def finish_run("):source.index("    def fail_run(")]
        self.assertIn("EVALUATION_FINISHED", finish)
        self.assertIn("INSERT INTO lexsond.evaluation_run_events", finish)
        fail = source[source.index("    def fail_run("):source.index("    def get_run(")]
        self.assertIn("EVALUATION_FINISHED", fail)
        recovery = source[source.index("    def fail_expired_runs("):source.index("    @staticmethod\n    def _insert_items")]
        self.assertIn("EVALUATION_FINISHED", recovery)
        self.assertIn('terminal_state = "CANCELLED" if cancelled else "FAILED"', recovery)
        self.assertIn("'cancel_intent_preserved'", recovery)

    def test_duplicate_upload_check_is_serialized_by_workspace_and_hash(self) -> None:
        source = (ROOT / "src/lexsond/web/postgres_evaluation_store.py").read_text(
            encoding="utf-8"
        )
        create = source[
            source.index("    def create_dataset(") : source.index(
                "    def _insert_revision("
            )
        ]
        self.assertIn("pg_advisory_xact_lock", create)
        self.assertIn("workspace_id}:{compiled.content_sha256", create)

    def test_public_dataset_schema_is_versioned_and_has_no_secret_fields(self) -> None:
        schema = (ROOT / "schemas/evaluation-dataset.schema.json").read_text(encoding="utf-8")
        self.assertIn('"$id": "https://lexsond.local/schemas/evaluation-dataset-v1.json"', schema)
        self.assertIn('"additionalProperties": false', schema)
        self.assertNotIn('"api_key"', schema)
        self.assertNotIn('"credential_ref"', schema)


if __name__ == "__main__":
    unittest.main()
