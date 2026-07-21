from __future__ import annotations

import hashlib
import json
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from lexsond.mock_relay import create_server
from lexsond.storage import (
    FileEvidenceStore,
    SqliteCanaryRuntimeStore,
)
from lexsond.workflows import (
    ActivityName,
    CanaryWorkflow,
    CanaryWorkflowInput,
    InMemoryWorkflowJournal,
    WorkflowStatus,
)
from lexsond.workflows.native_activities import (
    EndpointSnapshot,
    FileSuiteDocumentResolver,
    EnvironmentSecretResolver,
    JsonEndpointSnapshotResolver,
    MappingEndpointSnapshotResolver,
    MappingSecretResolver,
    NativeCanaryActivities,
)


class NativeCanaryActivitiesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = create_server()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base_url = f"http://{host}:{port}/v1"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.suite_root = self.root / "suites"
        self.suite_root.mkdir()
        document = self._suite_document()
        document["spec"]["sampling"].update({"warmup": 0, "requests": 2})
        self.suite_path = self.suite_root / "canary.json"
        self.suite_bytes = json.dumps(
            document, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.suite_path.write_bytes(self.suite_bytes)
        self.suite_sha256 = hashlib.sha256(self.suite_bytes).hexdigest()
        self.evidence_root = self.root / "evidence"
        self.evidence_root.mkdir()
        self.runtime_store = SqliteCanaryRuntimeStore(self.root / "runtime.sqlite3")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_real_native_probe_completes_all_steps_and_persists_sanitized_result(
        self,
    ) -> None:
        workflow_input, activities = self._build()
        journal = InMemoryWorkflowJournal()

        state = CanaryWorkflow(journal).run(workflow_input, activities)

        self.assertEqual(state.status, WorkflowStatus.SUCCEEDED)
        self.assertFalse(state.target_failed_seen)
        result = self.runtime_store.load_result(workflow_input.run_id)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["measurements"])
        for measurement in result["measurements"]:
            self.assertEqual(measurement["output_text"], "")
            self.assertIsNone(measurement["error_message"])
            self.assertIn("output_text_sha256", measurement["evidence"])
        serialized = json.dumps(result).lower()
        self.assertNotIn("test-key", serialized)
        self.assertNotIn("authorization", serialized)
        completed = [
            event.activity_name
            for event in journal.load(workflow_input.run_id)
            if event.event_type.value == "ACTIVITY_COMPLETED"
        ]
        self.assertEqual(completed, list(ActivityName))

    def test_target_rate_limit_is_measured_by_the_single_execute_activity(self) -> None:
        workflow_input, activities = self._build(mock_mode="rate_limit")
        journal = InMemoryWorkflowJournal()

        state = CanaryWorkflow(journal).run(workflow_input, activities)

        self.assertEqual(state.status, WorkflowStatus.SUCCEEDED)
        self.assertTrue(state.target_failed_seen)
        started = [
            event.activity_name
            for event in journal.load(workflow_input.run_id)
            if event.event_type.value == "ACTIVITY_STARTED"
        ]
        self.assertIn(ActivityName.EXECUTE, started)
        result = self.runtime_store.load_result(workflow_input.run_id)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("SUCCESS_RATE_BELOW_THRESHOLD", result["reason_codes"])

    def test_missing_secret_is_a_non_retryable_configuration_rejection(self) -> None:
        workflow_input, activities = self._build(include_secret=False)

        state = CanaryWorkflow(InMemoryWorkflowJournal()).run(
            workflow_input, activities
        )

        self.assertEqual(state.status, WorkflowStatus.REJECTED)
        self.assertEqual(state.terminal_error_code, "CREDENTIAL_NOT_FOUND")

    def test_suite_digest_mismatch_is_rejected_before_calling_target(self) -> None:
        workflow_input, activities = self._build()
        workflow_input = CanaryWorkflowInput(
            **{
                **workflow_input.to_dict(),
                "retry_policy": workflow_input.retry_policy,
                "suite_sha256": "0" * 64,
            }
        )

        state = CanaryWorkflow(InMemoryWorkflowJournal()).run(
            workflow_input, activities
        )

        self.assertEqual(state.status, WorkflowStatus.REJECTED)
        self.assertEqual(state.terminal_error_code, "SUITE_DIGEST_MISMATCH")

    def test_local_endpoint_file_and_environment_secret_keep_values_separate(
        self,
    ) -> None:
        endpoint_path = self.root / "endpoints.json"
        endpoint_path.write_text(
            json.dumps(
                {
                    "apiVersion": "probe.ai/endpoints/v1alpha1",
                    "kind": "EndpointSnapshotList",
                    "items": [
                        {
                            "endpoint_snapshot_id": "endpoint-v1",
                            "protocol": "openai-chat",
                            "base_url": self.base_url,
                            "model": "mock-model",
                            "credential_handle": "env://LEXSOND_SECRET_RELAY",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        resolver = JsonEndpointSnapshotResolver.from_file(endpoint_path)
        snapshot = resolver.resolve("endpoint-v1")
        secret_resolver = EnvironmentSecretResolver()
        previous = os.environ.get("LEXSOND_SECRET_RELAY")
        os.environ["LEXSOND_SECRET_RELAY"] = "never-render-this-value"
        try:
            self.assertEqual(
                secret_resolver.resolve(snapshot.credential_handle),
                "never-render-this-value",
            )
            self.assertNotIn("never-render-this-value", repr(secret_resolver))
            self.assertNotIn("never-render-this-value", repr(snapshot))
        finally:
            if previous is None:
                os.environ.pop("LEXSOND_SECRET_RELAY", None)
            else:
                os.environ["LEXSOND_SECRET_RELAY"] = previous

    def test_local_endpoint_file_rejects_inline_secret_field(self) -> None:
        endpoint_path = self.root / "bad-endpoints.json"
        endpoint_path.write_text(
            json.dumps(
                {
                    "apiVersion": "probe.ai/endpoints/v1alpha1",
                    "kind": "EndpointSnapshotList",
                    "items": [
                        {
                            "endpoint_snapshot_id": "endpoint-v1",
                            "protocol": "openai-chat",
                            "base_url": self.base_url,
                            "model": "mock-model",
                            "credential_handle": "env://LEXSOND_SECRET_RELAY",
                            "api_key": "forbidden",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "fields differ"):
            JsonEndpointSnapshotResolver.from_file(endpoint_path)

    def _build(
        self,
        *,
        mock_mode: str | None = None,
        include_secret: bool = True,
    ) -> tuple[CanaryWorkflowInput, NativeCanaryActivities]:
        endpoint_id = "endpoint-snapshot-v1"
        secrets = {"secret://relay/test": "test-key"} if include_secret else {}
        workflow_input = CanaryWorkflowInput(
            run_id=str(uuid4()),
            endpoint_snapshot_id=endpoint_id,
            suite_name="openai-compatible-canary",
            suite_version="0.1.0",
            suite_uri=self.suite_path.as_uri(),
            suite_sha256=self.suite_sha256,
            region="local-test",
        )
        activities = NativeCanaryActivities(
            endpoint_resolver=MappingEndpointSnapshotResolver(
                {
                    endpoint_id: EndpointSnapshot(
                        endpoint_snapshot_id=endpoint_id,
                        protocol="openai-chat",
                        base_url=self.base_url,
                        model="mock-model",
                        credential_handle="secret://relay/test",
                        mock_mode=mock_mode,
                    )
                }
            ),
            suite_resolver=FileSuiteDocumentResolver(self.suite_root),
            secret_resolver=MappingSecretResolver(secrets),
            evidence_store=FileEvidenceStore(self.evidence_root),
            runtime_store=self.runtime_store,
        )
        return workflow_input, activities

    @staticmethod
    def _suite_document() -> dict:
        path = Path(__file__).parents[1] / "suites" / "canary" / "openai-compatible.json"
        return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
