from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]


class PostgresOnlyPersistenceContractTests(unittest.TestCase):
    def test_runtime_contains_no_sqlite_or_legacy_web_implementation(self) -> None:
        removed = (
            "src/lexsond/storage/sqlite_journal.py",
            "src/lexsond/storage/sqlite_runtime.py",
            "src/lexsond/web/control_store.py",
            "src/lexsond/web/server.py",
            "config/local-endpoints.example.json",
            "schemas/local-endpoint-snapshots.schema.json",
        )
        self.assertEqual(
            [path for path in removed if (PROJECT_ROOT / path).exists()],
            [],
        )

    def test_package_exposes_no_sqlite_or_legacy_entrypoint(self) -> None:
        project = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotIn("lexsond-web-legacy", project)
        self.assertNotIn("sqlite", project.lower())

    def test_web_and_worker_configuration_are_postgres_only(self) -> None:
        app = (PROJECT_ROOT / "src/lexsond/web/app.py").read_text(encoding="utf-8")
        worker = (
            PROJECT_ROOT / "src/lexsond/workflows/temporal_worker.py"
        ).read_text(encoding="utf-8")
        starter = (
            PROJECT_ROOT / "src/lexsond/workflows/temporal_start.py"
        ).read_text(encoding="utf-8")
        activities = (
            PROJECT_ROOT / "src/lexsond/workflows/native_activities.py"
        ).read_text(encoding="utf-8")
        workflow_schema = (
            PROJECT_ROOT / "schemas/canary-workflow-input.schema.json"
        ).read_text(encoding="utf-8")
        postgres_store = (
            PROJECT_ROOT / "src/lexsond/web/postgres_control_store.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("LEXSOND_CONTROL_STORE", app)
        self.assertNotIn("database_path", app)
        self.assertIn(
            "temporal_launcher_from_environment(postgres_dsn=dsn)",
            app,
        )
        self.assertIn(
            "app.add_middleware(ControlOperationLeaseMiddleware, service=service)",
            app,
        )
        self.assertIn("with self._service.operation():", app)
        self.assertNotIn(
            'temporal_launcher = DisabledTemporalLauncher("UNAVAILABLE")',
            app,
        )
        self.assertNotIn("storage-backend", worker)
        self.assertNotIn("sqlite", worker.lower())
        self.assertNotIn("suite-file", starter)
        self.assertNotIn("JsonEndpointSnapshotResolver", activities)
        self.assertNotIn("FileSuiteDocumentResolver", activities)
        self.assertNotIn("class EnvironmentSecretResolver", activities)
        self.assertNotIn("file|s3|https", workflow_schema)
        self.assertIn("validate_sanitized_result(run_id, result)", postgres_store)

    def test_control_service_has_no_removed_store_capability_fallbacks(self) -> None:
        service = (
            PROJECT_ROOT / "src/lexsond/web/control_service.py"
        ).read_text(encoding="utf-8")
        scheduler = (
            PROJECT_ROOT / "src/lexsond/monitoring/scheduler.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("getattr(self.store", service)
        self.assertNotIn("getattr(self._store", scheduler)
        self.assertIn("self._closing.set()", service)
        self.assertIn("self._closing.wait(delay)", service)
        self.assertLess(
            service.index("self.temporal.close()"),
            service.index("self.executor.shutdown"),
        )

if __name__ == "__main__":
    unittest.main()
