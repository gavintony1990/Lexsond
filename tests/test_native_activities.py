from __future__ import annotations

import hashlib
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from lexsond.mock_relay import create_server
from lexsond.storage import FileEvidenceStore
from lexsond.workflows import (
    ActivityName,
    CanaryWorkflow,
    CanaryWorkflowInput,
    InMemoryWorkflowJournal,
    WorkflowStatus,
)
from lexsond.workflows.native_activities import (
    EndpointSnapshot,
    MappingEndpointSnapshotResolver,
    MappingSecretResolver,
    NativeCanaryActivities,
)

from tests.in_memory_runtime import InMemoryCanaryRuntimeStore


class _MappingSuiteDocumentResolver:
    def __init__(self, values: dict[str, bytes]) -> None:
        self._values = values

    def read(self, suite_uri: str) -> bytes:
        try:
            return self._values[suite_uri]
        except KeyError as exc:
            raise LookupError("suite snapshot was not found") from exc


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
        document = self._suite_document()
        document["spec"]["sampling"].update({"warmup": 0, "requests": 2})
        self.suite_bytes = json.dumps(
            document, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.suite_uri = "https://control.lexsond.invalid/suites/test-canary"
        self.suite_sha256 = hashlib.sha256(self.suite_bytes).hexdigest()
        self.evidence_root = self.root / "evidence"
        self.evidence_root.mkdir()
        self.runtime_store = InMemoryCanaryRuntimeStore()

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
            suite_uri=self.suite_uri,
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
            suite_resolver=_MappingSuiteDocumentResolver(
                {self.suite_uri: self.suite_bytes}
            ),
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
