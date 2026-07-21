from __future__ import annotations

import json
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from lexsond.mock_relay import create_server as create_mock_relay
from lexsond.web.app import create_app
from lexsond.web.control_service import ControlPlaneService


class InlineExecutor:
    def submit(self, function, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, wait=True, cancel_futures=False):
        return None


class ApiV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.relay = create_mock_relay()
        cls.relay_thread = threading.Thread(target=cls.relay.serve_forever, daemon=True)
        cls.relay_thread.start()
        host, port = cls.relay.server_address
        cls.base_url = f"http://{host}:{port}/v1"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.relay.shutdown()
        cls.relay.server_close()
        cls.relay_thread.join(timeout=2)

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        project_root = Path(__file__).resolve().parents[1]
        self.database = Path(self.temporary.name) / "api.sqlite3"
        self.service = ControlPlaneService(
            database_path=self.database,
            default_suite_path=project_root / "suites/canary/openai-compatible.json",
            executor=InlineExecutor(),
        )
        self.addCleanup(self.service.close)
        self.client = TestClient(
            create_app(
                service=self.service,
                frontend_path=Path(self.temporary.name) / "missing-dist",
            )
        )

    def test_target_suite_and_run_flow_uses_only_v1_api(self) -> None:
        target_response = self.client.post(
            "/api/v1/targets",
            json={
                "name": "Mock relay",
                "target_kind": "local",
                "provider_id": None,
                "base_url": self.base_url,
                "default_model": "mock-model",
                "credential_ref": None,
            },
        )
        self.assertEqual(target_response.status_code, 201, target_response.text)
        target = target_response.json()["data"]

        suite_response = self.client.post(
            "/api/v1/suites",
            json={
                "name": "smoke",
                "description": "one bounded request",
                "document": self._suite_document(),
            },
        )
        self.assertEqual(suite_response.status_code, 201, suite_response.text)

        secret = "test-key"
        run_response = self.client.post(
            "/api/v1/runs",
            json={
                "target_id": target["id"],
                "run_kind": "component",
                "probe_type": "chat",
                "execution_backend": "local",
                "model": "mock-model",
                "stream": False,
                "timeout_seconds": 5,
                "api_key": secret,
            },
        )
        self.assertEqual(run_response.status_code, 202, run_response.text)
        run = run_response.json()["data"]
        self.assertEqual(run["state"], "COMPLETED")
        self.assertEqual(run["result"]["status"], "PASS")

        detail = self.client.get(f"/api/v1/runs/{run['run_id']}").json()["data"]
        self.assertNotIn(secret, json.dumps(detail))
        self.assertNotIn(secret.encode(), self.database.read_bytes())

        events = self.client.get(
            f"/api/v1/runs/{run['run_id']}/events",
            headers={"Last-Event-ID": "0"},
        )
        self.assertEqual(events.status_code, 200)
        self.assertIn("event: run_started", events.text)
        self.assertIn("event: run_completed", events.text)
        self.assertNotIn(secret, events.text)

        legacy = self.client.get("/api/bootstrap")
        self.assertEqual(legacy.status_code, 404)
        self.assertEqual(legacy.json()["error"]["code"], "NOT_FOUND")

    def test_archived_run_must_be_restored_or_purged_explicitly(self) -> None:
        run_id = "00000000-0000-4000-8000-000000000100"
        self.service.store.create_run(
            run_id,
            {
                "target_id": None,
                "suite_revision_id": None,
                "run_kind": "component",
                "execution_backend": "local",
                "base_url": self.base_url,
                "model": "mock-model",
                "target_kind": "local",
                "provider_id": None,
                "run_mode": "single",
                "probe_type": "chat",
                "stream": False,
                "timeout_seconds": 5,
            },
            {"schema_version": "probe.ai/component-run/v1alpha2", "status": "RUNNING"},
        )
        self.service.store.cancel_run(run_id)

        archived = self.client.delete(f"/api/v1/runs/{run_id}")
        self.assertEqual(archived.status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/runs/{run_id}").status_code, 404)
        restored = self.client.post(f"/api/v1/runs/{run_id}/restore")
        self.assertEqual(restored.status_code, 200)
        self.client.delete(f"/api/v1/runs/{run_id}")
        purged = self.client.delete(f"/api/v1/runs/{run_id}/purge")
        self.assertEqual(purged.status_code, 204)
        self.assertEqual(
            self.client.get(f"/api/v1/runs/{run_id}?include_archived=true").status_code,
            404,
        )

    def test_validation_error_has_stable_envelope_and_request_id(self) -> None:
        response = self.client.post(
            "/api/v1/targets",
            json={"name": "unsafe", "target_kind": "cloud", "api_key": "must-not-be-here"},
        )

        self.assertEqual(response.status_code, 422)
        error = response.json()["error"]
        self.assertEqual(error["code"], "VALIDATION_ERROR")
        self.assertTrue(error["request_id"])
        self.assertTrue(error["details"])

    def test_invalid_sse_resume_cursor_has_protocol_error_code(self) -> None:
        response = self.client.get(
            "/api/v1/runs/00000000-0000-4000-8000-000000000100/events",
            headers={"Last-Event-ID": "not-an-integer"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "SSE_PROTOCOL_ERROR")

    def test_transient_key_cannot_enter_a_persisted_model_field(self) -> None:
        target = self.client.post(
            "/api/v1/targets",
            json={
                "name": "Secret boundary",
                "target_kind": "local",
                "provider_id": None,
                "base_url": self.base_url,
                "default_model": "mock-model",
                "credential_ref": None,
            },
        ).json()["data"]
        secret = "test-key-not-for-storage"

        response = self.client.post(
            "/api/v1/runs",
            json={
                "target_id": target["id"],
                "run_kind": "component",
                "probe_type": "chat",
                "execution_backend": "local",
                "model": f"model-{secret}",
                "stream": False,
                "timeout_seconds": 5,
                "api_key": secret,
            },
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertNotIn(secret, response.text)
        self.assertNotIn(secret.encode(), self.database.read_bytes())

    def test_run_creation_is_idempotent_for_transport_retries(self) -> None:
        target = self.client.post(
            "/api/v1/targets",
            json={
                "name": "Idempotent relay",
                "target_kind": "local",
                "provider_id": None,
                "base_url": self.base_url,
                "default_model": "mock-model",
                "credential_ref": None,
            },
        ).json()["data"]
        payload = {
            "target_id": target["id"],
            "run_kind": "component",
            "probe_type": "chat",
            "execution_backend": "local",
            "model": "mock-model",
            "stream": False,
            "timeout_seconds": 5,
            "api_key": "test-key",
        }
        headers = {"Idempotency-Key": "00000000-0000-4000-8000-000000000777"}

        first = self.client.post("/api/v1/runs", json=payload, headers=headers)
        second = self.client.post("/api/v1/runs", json=payload, headers=headers)

        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(
            first.json()["data"]["run_id"], second.json()["data"]["run_id"]
        )
        self.assertEqual(len(self.service.store.list_runs()), 1)

        changed = self.client.post(
            "/api/v1/runs",
            json={**payload, "model": "another-model"},
            headers=headers,
        )
        self.assertEqual(changed.status_code, 409, changed.text)

    def test_archived_suite_revision_cannot_start_a_new_run(self) -> None:
        target = self.client.post(
            "/api/v1/targets",
            json={
                "name": "Suite archive target",
                "target_kind": "local",
                "provider_id": None,
                "base_url": self.base_url,
                "default_model": "mock-model",
                "credential_ref": None,
            },
        ).json()["data"]
        suite = self.client.post(
            "/api/v1/suites",
            json={
                "name": "archived-suite",
                "description": "must not run",
                "document": self._suite_document(),
            },
        ).json()["data"]
        self.client.delete(f"/api/v1/suites/{suite['id']}")

        response = self.client.post(
            "/api/v1/runs",
            json={
                "target_id": target["id"],
                "run_kind": "suite",
                "suite_revision_id": suite["latest_revision"]["id"],
                "execution_backend": "local",
                "model": "mock-model",
                "stream": True,
                "timeout_seconds": 5,
                "api_key": "test-key",
            },
        )

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(len(self.service.store.list_runs()), 0)

    @staticmethod
    def _suite_document() -> dict[str, object]:
        return {
            "apiVersion": "probe.ai/v1alpha1",
            "kind": "ProbeSuite",
            "metadata": {"name": "smoke", "version": "0.1.0"},
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
                    "requests": 1,
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
