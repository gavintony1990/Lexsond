from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from lexsond.web.control_store import ControlPlaneConflict, ControlPlaneStore


class ControlPlaneStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "control.sqlite3"
        self.store = ControlPlaneStore(self.database)

    def test_target_lifecycle_never_persists_a_transient_key(self) -> None:
        target = self.store.create_target(
            {
                "name": "DeepSeek production",
                "target_kind": "cloud",
                "provider_id": "deepseek",
                "base_url": "https://api.deepseek.com",
                "default_model": "deepseek-chat",
                "credential_ref": "vault://ai/deepseek",
            }
        )

        updated = self.store.update_target(
            target["id"],
            {"default_model": "deepseek-reasoner"},
            expected_version=target["version"],
        )
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["default_model"], "deepseek-reasoner")

        archived = self.store.archive_target(target["id"])
        self.assertIsNotNone(archived["archived_at"])
        self.assertEqual(self.store.list_targets(), [])
        self.assertEqual(len(self.store.list_targets(include_archived=True)), 1)
        restored = self.store.restore_target(target["id"])
        self.assertIsNone(restored["archived_at"])

        persisted = self.database.read_bytes()
        self.assertNotIn(b"api_key", persisted)
        self.assertNotIn(b"sk-test", persisted)

    def test_suite_update_creates_an_immutable_revision(self) -> None:
        document = self._suite_document("0.1.0", requests=1)
        suite = self.store.create_suite(
            {"name": "smoke", "description": "bounded", "document": document}
        )
        first_revision = suite["latest_revision"]

        updated = self.store.update_suite(
            suite["id"],
            {"document": self._suite_document("0.2.0", requests=2)},
            expected_version=suite["version"],
        )

        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["latest_revision"]["revision"], 2)
        revisions = self.store.list_suite_revisions(suite["id"])
        self.assertEqual([item["revision"] for item in revisions], [2, 1])
        self.assertEqual(first_revision["document"]["metadata"]["version"], "0.1.0")

    def test_purge_requires_archive_and_is_blocked_by_run_reference(self) -> None:
        target = self.store.create_target(
            {
                "name": "local",
                "target_kind": "local",
                "provider_id": "ollama",
                "base_url": "http://127.0.0.1:11434/v1",
                "default_model": "tiny",
                "credential_ref": None,
            }
        )
        self.store.create_run(
            "00000000-0000-4000-8000-000000000001",
            {
                "target_id": target["id"],
                "suite_revision_id": None,
                "run_kind": "component",
                "execution_backend": "local",
                "base_url": target["base_url"],
                "model": "tiny",
                "target_kind": "local",
                "provider_id": "ollama",
                "run_mode": "single",
                "probe_type": "chat",
                "stream": False,
                "timeout_seconds": 5.0,
            },
            {"schema_version": "probe.ai/component-run/v1alpha2", "status": "RUNNING"},
        )

        with self.assertRaises(ControlPlaneConflict):
            self.store.purge_target(target["id"])
        self.store.archive_target(target["id"])
        with self.assertRaisesRegex(ControlPlaneConflict, "referenced"):
            self.store.purge_target(target["id"])

    def test_events_are_ordered_and_contain_no_result_payload(self) -> None:
        run_id = "00000000-0000-4000-8000-000000000002"
        self.store.create_run(
            run_id,
            {
                "target_id": None,
                "suite_revision_id": None,
                "run_kind": "component",
                "execution_backend": "local",
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "tiny",
                "target_kind": "local",
                "provider_id": "ollama",
                "run_mode": "single",
                "probe_type": "chat",
                "stream": False,
                "timeout_seconds": 5.0,
            },
            {"schema_version": "probe.ai/component-run/v1alpha2", "status": "RUNNING"},
        )
        self.store.append_run_event(
            run_id,
            event_type="STEP_STARTED",
            phase="request_dispatch",
            status="RUNNING",
        )

        events = self.store.list_run_events(run_id)
        self.assertEqual([event["sequence"] for event in events], [1, 2])
        encoded = json.dumps(events)
        self.assertNotIn("result", encoded)
        self.assertNotIn("api_key", encoded)

    def test_temporal_source_event_projection_is_idempotent(self) -> None:
        run_id = "00000000-0000-4000-8000-000000000003"
        self.store.create_run(
            run_id,
            {
                "target_id": None,
                "suite_revision_id": None,
                "run_kind": "component",
                "execution_backend": "temporal",
                "base_url": "https://models.example.invalid/v1",
                "model": "chat-model",
                "target_kind": "cloud",
                "provider_id": None,
                "run_mode": "single",
                "probe_type": "chat",
                "stream": True,
                "timeout_seconds": 5.0,
            },
            {"schema_version": "probe.ai/component-run/v1alpha2", "status": "RUNNING"},
        )
        source_event_id = "00000000-0000-5000-8000-000000000003"
        for _ in range(2):
            self.store.update_run_workflow(
                run_id,
                {"schema_version": "probe.ai/component-run/v1alpha2", "status": "RUNNING"},
                event_type="TEMPORAL_ACTIVITY_STARTED",
                phase="execute",
                status="RUNNING",
                source_event_id=source_event_id,
            )

        events = self.store.list_run_events(run_id)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1]["source_event_id"], source_event_id)

    def test_legacy_web_database_is_backed_up_and_imported(self) -> None:
        legacy = Path(self.temporary.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy) as connection:
            connection.executescript(
                """
                CREATE TABLE web_probe_runs (
                    run_id TEXT PRIMARY KEY, state TEXT NOT NULL,
                    result_status TEXT, created_at TEXT NOT NULL,
                    finished_at TEXT, base_url TEXT NOT NULL, model TEXT NOT NULL,
                    target_kind TEXT NOT NULL, provider_id TEXT,
                    run_mode TEXT NOT NULL, probe_type TEXT NOT NULL,
                    streaming INTEGER NOT NULL, timeout_seconds REAL NOT NULL,
                    result_json TEXT, failure_code TEXT, workflow_json TEXT
                );
                """
            )
            connection.execute(
                """
                INSERT INTO web_probe_runs VALUES (
                    ?, 'COMPLETED', 'PASS', ?, ?, ?, ?, 'local', NULL,
                    'single', 'chat', 0, 5, ?, NULL, ?
                )
                """,
                (
                    "00000000-0000-4000-8000-000000000099",
                    "2026-07-20T00:00:00+00:00",
                    "2026-07-20T00:00:01+00:00",
                    "http://127.0.0.1:8000/v1",
                    "legacy-model",
                    json.dumps({"status": "PASS"}),
                    json.dumps({"schema_version": "legacy", "status": "PASS"}),
                ),
            )

        migrated = ControlPlaneStore(legacy)
        runs = migrated.list_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["execution_backend"], "local")
        self.assertEqual(
            migrated.list_run_events(runs[0]["run_id"])[0]["event_type"],
            "LEGACY_RUN_IMPORTED",
        )
        self.assertTrue(
            legacy.with_suffix(".sqlite3.pre-control-plane.bak").is_file()
        )
        with sqlite3.connect(legacy) as connection:
            versions = [
                row[0]
                for row in connection.execute(
                    "SELECT version FROM control_schema_migrations ORDER BY version"
                )
            ]
        self.assertEqual(versions, [1, 2, 3, 4, 5, 6, 7, 8])

    @staticmethod
    def _suite_document(version: str, *, requests: int) -> dict[str, object]:
        return {
            "apiVersion": "probe.ai/v1alpha1",
            "kind": "ProbeSuite",
            "metadata": {"name": "smoke", "version": version},
            "spec": {
                "layer": "L2",
                "protocol": "openai-chat",
                "request": {
                    "prompt": "Reply with exactly: PROBE_OK",
                    "stream": True,
                    "max_output_tokens": 32,
                },
                "sampling": {
                    "warmup": 0,
                    "requests": requests,
                    "concurrency": 1,
                    "timeout_seconds": 5,
                    "max_cost_usd": 0.1,
                },
                "assertions": [
                    {"type": "http_status", "equals": 200},
                    {"type": "output_nonempty"},
                ],
            },
        }
