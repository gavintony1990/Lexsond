from __future__ import annotations

import http.client
import json
import sqlite3
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from tempfile import TemporaryDirectory

from lexsond.mock_relay import create_server as create_mock_relay
from lexsond.web.server import ProbeWebService, RunHistoryStore, create_server


class InlineExecutor:
    def submit(self, function, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as exc:  # pragma: no cover - mirrors Executor boundary
            future.set_exception(exc)
        return future

    def shutdown(self, wait=True, cancel_futures=False):
        return None


class DeferredExecutor:
    def __init__(self) -> None:
        self.pending = []

    def submit(self, function, *args, **kwargs):
        future = Future()
        self.pending.append((future, function, args, kwargs))
        return future

    def run_next(self) -> None:
        future, function, args, kwargs = self.pending.pop(0)
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as exc:  # pragma: no cover - mirrors Executor boundary
            future.set_exception(exc)

    def shutdown(self, wait=True, cancel_futures=False):
        return None


class ProbeWebServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.relay = create_mock_relay()
        cls.relay_thread = threading.Thread(
            target=cls.relay.serve_forever,
            daemon=True,
        )
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
        self.database = Path(self.temporary.name) / "console.sqlite3"
        self.service = ProbeWebService(
            database_path=self.database,
            suite_path=project_root / "suites/canary/openai-compatible.json",
            executor=InlineExecutor(),
        )
        self.addCleanup(self.service.close)

    def test_canary_run_is_persisted_without_key_or_raw_output(self) -> None:
        secret = "test-key"
        started = self.service.start_run(
            {
                "base_url": self.base_url,
                "api_key": secret,
                "model": "mock-model",
                "target_kind": "local",
                "run_mode": "canary",
                "stream": True,
                "timeout_seconds": 30,
                "provider_id": None,
                "custom_target_confirmed": True,
            }
        )

        run = self.service.get_run(started["run_id"])
        self.assertEqual(run["state"], "COMPLETED")
        self.assertEqual(run["result"]["status"], "PASS")
        self.assertEqual(len(run["result"]["dimension_scores"]), 4)
        self.assertEqual(run["result"]["measurements"][0]["output_text"], "")
        self.assertEqual(
            run["result"]["measurements"][0]["evidence"]["output_text_chars"],
            8,
        )

        persisted = self.database.read_bytes()
        self.assertNotIn(secret.encode(), persisted)
        self.assertNotIn(b"PROBE_OK", persisted)
        self.assertNotIn(secret, json.dumps(self.service.list_runs()))

    def test_bootstrap_exposes_each_multimodal_detection_component(self) -> None:
        components = self.service.bootstrap()["probe_components"]
        self.assertEqual(
            {component["id"] for component in components},
            {
                "chat",
                "vision",
                "embedding",
                "image_generation",
                "audio_speech",
                "audio_transcription",
            },
        )
        self.assertTrue(all(len(component["steps"]) == 7 for component in components))

    def test_run_persists_live_component_workflow_without_secrets(self) -> None:
        deferred = DeferredExecutor()
        project_root = Path(__file__).resolve().parents[1]
        service = ProbeWebService(
            database_path=Path(self.temporary.name) / "workflow.sqlite3",
            suite_path=project_root / "suites/canary/openai-compatible.json",
            executor=deferred,
        )
        self.addCleanup(service.close)
        started = service.start_run(
            {
                "base_url": self.base_url,
                "api_key": "test-key",
                "model": "vision-model",
                "target_kind": "local",
                "run_mode": "single",
                "probe_type": "vision",
                "stream": False,
                "timeout_seconds": 5,
                "provider_id": None,
                "custom_target_confirmed": True,
            }
        )

        self.assertEqual(started["state"], "RUNNING")
        self.assertEqual(started["workflow"]["component_id"], "vision")
        self.assertEqual(started["workflow"]["binding_source"], "MANUAL_CONFIRMATION")
        self.assertEqual(
            started["workflow"]["steps"][0]["facts"],
            ["MANUAL PROBE TYPE CONFIRMED"],
        )
        self.assertEqual(started["workflow"]["steps"][0]["status"], "PASS")
        self.assertTrue(
            all(step["status"] == "PENDING" for step in started["workflow"]["steps"][1:])
        )

        deferred.run_next()
        completed = service.get_run(started["run_id"])
        self.assertEqual(completed["state"], "COMPLETED")
        self.assertEqual(completed["workflow"]["status"], "PASS")
        self.assertTrue(
            all(step["status"] == "PASS" for step in completed["workflow"]["steps"])
        )
        persisted = (Path(self.temporary.name) / "workflow.sqlite3").read_bytes()
        self.assertNotIn(b"test-key", persisted)
        self.assertNotIn(b"PROBE_OK", persisted)

    def test_evidence_persistence_failure_is_not_reported_as_quality_failure(self) -> None:
        original_complete = self.service.store.complete

        def fail_complete(run_id, result) -> None:
            raise RuntimeError("simulated persistence failure")

        self.service.store.complete = fail_complete
        self.addCleanup(setattr, self.service.store, "complete", original_complete)
        started = self.service.start_run(
            {
                "base_url": self.base_url,
                "api_key": "test-key",
                "model": "embedding-model",
                "target_kind": "local",
                "run_mode": "single",
                "probe_type": "embedding",
                "stream": False,
                "timeout_seconds": 5,
                "provider_id": None,
                "custom_target_confirmed": True,
            }
        )

        run = self.service.get_run(started["run_id"])
        by_id = {step["id"]: step for step in run["workflow"]["steps"]}
        self.assertEqual(run["state"], "FAILED")
        self.assertEqual(by_id["quality_assert"]["status"], "PASS")
        self.assertEqual(by_id["evidence_seal"]["status"], "FAIL")

    def test_transient_live_progress_failure_does_not_discard_provider_result(self) -> None:
        original_advance = self.service.store.advance
        calls = 0

        def fail_first_advance(run_id, step_id, status) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("simulated live progress failure")
            original_advance(run_id, step_id, status)

        self.service.store.advance = fail_first_advance
        self.addCleanup(setattr, self.service.store, "advance", original_advance)
        started = self.service.start_run(
            {
                "base_url": self.base_url,
                "api_key": "test-key",
                "model": "mock-model",
                "target_kind": "local",
                "run_mode": "single",
                "probe_type": "chat",
                "stream": False,
                "timeout_seconds": 5,
                "provider_id": None,
                "custom_target_confirmed": True,
            }
        )

        run = self.service.get_run(started["run_id"])

        self.assertEqual(run["state"], "COMPLETED")
        self.assertEqual(run["result"]["status"], "PASS")
        self.assertTrue(all(step["status"] == "PASS" for step in run["workflow"]["steps"]))

    def test_existing_workflow_binding_claim_is_migrated_to_legacy_unknown(self) -> None:
        started = self.service.start_run(
            {
                "base_url": self.base_url,
                "api_key": "test-key",
                "model": "mock-model",
                "target_kind": "local",
                "run_mode": "single",
                "probe_type": "chat",
                "stream": False,
                "timeout_seconds": 5,
                "provider_id": None,
                "custom_target_confirmed": True,
            }
        )
        with sqlite3.connect(self.database) as connection:
            encoded = connection.execute(
                "SELECT workflow_json FROM web_probe_runs WHERE run_id = ?",
                (started["run_id"],),
            ).fetchone()[0]
            workflow = json.loads(encoded)
            workflow["schema_version"] = "probe.ai/component-run/v1alpha1"
            workflow["binding_source"] = "PROVIDER_METADATA"
            workflow["steps"][0]["facts"] = ["CAPABILITY BINDING VERIFIED"]
            connection.execute(
                "UPDATE web_probe_runs SET workflow_json = ? WHERE run_id = ?",
                (json.dumps(workflow), started["run_id"]),
            )

        migrated = RunHistoryStore(self.database).get(started["run_id"])

        self.assertEqual(migrated["workflow"]["binding_source"], "LEGACY_UNSPECIFIED")
        self.assertEqual(
            migrated["workflow"]["schema_version"],
            "probe.ai/component-run/v1alpha2",
        )
        self.assertEqual(
            migrated["workflow"]["steps"][0]["facts"],
            ["LEGACY BINDING SOURCE UNKNOWN"],
        )

    def test_keyless_local_run_sends_no_authorization_header(self) -> None:
        keyless_relay = create_mock_relay(
            require_api_key=False,
            reject_authorization=True,
        )
        thread = threading.Thread(target=keyless_relay.serve_forever, daemon=True)
        thread.start()

        def close_keyless_relay() -> None:
            keyless_relay.shutdown()
            keyless_relay.server_close()
            thread.join(timeout=2)

        self.addCleanup(close_keyless_relay)
        host, port = keyless_relay.server_address
        started = self.service.start_run(
            {
                "base_url": f"http://{host}:{port}/v1",
                "api_key": None,
                "model": "local-real-model",
                "target_kind": "local",
                "run_mode": "single",
                "stream": False,
                "timeout_seconds": 5,
                "provider_id": None,
                "custom_target_confirmed": True,
            }
        )

        run = self.service.get_run(started["run_id"])
        self.assertEqual(run["result"]["status"], "PASS")
        self.assertEqual(run["config"]["target_kind"], "local")
        self.assertNotIn("mock_mode", run["config"])

    def test_validation_rejects_unknown_fields_and_unbounded_values(self) -> None:
        valid = {
            "base_url": self.base_url,
            "api_key": "test-key",
            "model": "mock-model",
            "target_kind": "local",
            "run_mode": "single",
            "stream": True,
            "timeout_seconds": 5,
            "provider_id": None,
            "custom_target_confirmed": True,
        }
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            self.service.start_run({**valid, "credential": "inline"})
        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            self.service.start_run({**valid, "timeout_seconds": 301})
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            self.service.start_run({**valid, "mock_mode": "arbitrary-header"})
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            self.service.start_run(
                {**valid, "base_url": "http://api.example.com/v1"}
            )
        with self.assertRaisesRegex(ValueError, "target_kind"):
            self.service.start_run(
                {
                    **valid,
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "gsk_abcdefghijk12345",
                    "target_kind": "cloud",
                    "provider_id": "ollama",
                    "custom_target_confirmed": False,
                }
            )
        with self.assertRaisesRegex(ValueError, "required for cloud"):
            self.service.start_run(
                {
                    **valid,
                    "base_url": "https://api.openai.com/v1",
                    "api_key": None,
                    "target_kind": "cloud",
                    "provider_id": "openai",
                    "custom_target_confirmed": False,
                }
            )
        with self.assertRaisesRegex(ValueError, "cloud-formatted"):
            self.service.start_run(
                {
                    **valid,
                    "base_url": "http://127.0.0.1:11434/v1",
                    "api_key": "gsk_abcdefghijk12345",
                    "provider_id": "ollama",
                    "custom_target_confirmed": False,
                }
            )
        with self.assertRaisesRegex(ValueError, "numeric loopback"):
            self.service.start_run(
                {
                    **valid,
                    "base_url": "https://example.com/v1",
                    "api_key": None,
                }
            )


class ProbeWebHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        project_root = Path(__file__).resolve().parents[1]
        self.service = ProbeWebService(
            database_path=Path(self.temporary.name) / "console.sqlite3",
            suite_path=project_root / "suites/canary/openai-compatible.json",
            executor=InlineExecutor(),
        )
        self.server = create_server("127.0.0.1", 0, self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.relay = create_mock_relay(
            require_api_key=False,
            reject_authorization=True,
        )
        self.relay_thread = threading.Thread(
            target=self.relay.serve_forever,
            daemon=True,
        )
        self.relay_thread.start()
        relay_host, relay_port = self.relay.server_address
        self.base_url = f"http://{relay_host}:{relay_port}/v1"
        self.addCleanup(self._close)

    def _close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.relay.shutdown()
        self.relay.server_close()
        self.relay_thread.join(timeout=2)
        self.service.close()

    def request(self, method: str, path: str, body: dict | None = None):
        host, port = self.server.server_address
        connection = http.client.HTTPConnection(host, port, timeout=3)
        encoded = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if encoded else {}
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        headers = dict(response.getheaders())
        connection.close()
        return response.status, headers, payload

    def test_bootstrap_and_static_console_are_served_with_security_headers(self) -> None:
        status, headers, payload = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        data = json.loads(payload)["data"]
        self.assertNotIn("fault_modes", data)
        self.assertEqual(data["defaults"]["target_kind"], "local")
        self.assertEqual(data["suite"]["name"], "openai-compatible-canary")
        self.assertEqual(data["product"]["name"], "Lexsond")
        self.assertGreaterEqual(len(data["providers"]), 12)

        status, headers, payload = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Lexsond".encode(), payload)
        self.assertIn("本地部署".encode(), payload)
        self.assertIn("云服务".encode(), payload)
        self.assertIn(b'id="probe-type"', payload)
        self.assertIn(b'id="model-capability"', payload)
        self.assertIn(b'value="audio_transcription"', payload)
        self.assertNotIn("故障注入".encode(), payload)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

        status, _, script = self.request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertNotIn(b"mock_mode", script)
        self.assertNotIn(b"fault_modes", script)
        self.assertIn(b"reasoning_content_observed", script)
        self.assertIn(b"FINAL BURST", script)
        self.assertIn(b"model_catalog", script)
        self.assertIn(b"probe_type", script)
        self.assertIn(b"renderWorkflow", script)
        self.assertIn(b"workflow-steps", payload)
        self.assertIn(b"LEGACY RUN / WORKFLOW UNAVAILABLE", script)
        self.assertIn(b'button.addEventListener("click", async () =>', script)
        self.assertIn(b"await detectProvider();", script)

    def test_provider_detection_api_autofills_and_does_not_persist_key(self) -> None:
        secret = "gsk_abcdefghijk12345"
        status, _, payload = self.request(
            "POST",
            "/api/providers/detect",
            {"api_key": secret},
        )

        self.assertEqual(status, 200)
        result = json.loads(payload)["data"]
        self.assertEqual(result["status"], "MATCHED")
        self.assertEqual(result["provider"]["id"], "groq")
        self.assertEqual(result["provider"]["base_url"], "https://api.groq.com/openai/v1")
        self.assertNotIn(secret.encode(), payload)
        self.assertNotIn(secret.encode(), self.service.store.database_path.read_bytes())

    def test_ambiguous_provider_detection_returns_candidates_without_guessing(self) -> None:
        status, _, payload = self.request(
            "POST",
            "/api/providers/detect",
            {"api_key": "sk-abcdefghijk123456789"},
        )

        self.assertEqual(status, 200)
        result = json.loads(payload)["data"]
        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertIsNone(result["provider"])
        self.assertGreaterEqual(len(result["candidates"]), 5)

    def test_ambiguous_key_keeps_explicit_deepseek_selection(self) -> None:
        secret = "sk-abcdefghijk123456789"
        status, _, payload = self.request(
            "POST",
            "/api/providers/detect",
            {"api_key": secret, "provider_id": "deepseek"},
        )

        self.assertEqual(status, 200)
        result = json.loads(payload)["data"]
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertEqual(result["provider"]["id"], "deepseek")
        self.assertNotIn(secret.encode(), payload)

    def test_unknown_key_selection_is_not_reported_as_confirmed(self) -> None:
        secret = "opaque-unrecognized-secret-value"
        status, _, payload = self.request(
            "POST",
            "/api/providers/detect",
            {"api_key": secret, "provider_id": "deepseek"},
        )

        self.assertEqual(status, 200)
        result = json.loads(payload)["data"]
        self.assertEqual(result["status"], "MANUAL")
        self.assertEqual(result["provider"]["id"], "deepseek")
        self.assertNotIn(secret.encode(), payload)

    def test_provider_detection_errors_never_reflect_key(self) -> None:
        secret = "do-not-reflect-provider-secret"
        status, _, payload = self.request(
            "POST",
            "/api/providers/detect",
            {"api_key": secret, "provider": "attacker-controlled"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(payload)["error"]["code"], "VALIDATION_ERROR")
        self.assertNotIn(secret.encode(), payload)

    def test_run_rejects_key_provider_mismatch_before_execution(self) -> None:
        secret = "gsk_abcdefghijk12345"
        status, _, payload = self.request(
            "POST",
            "/api/runs",
            {
                "base_url": "https://api.openai.com/v1",
                "api_key": secret,
                "model": "gpt-4.1-mini",
                "target_kind": "cloud",
                "run_mode": "single",
                "stream": True,
                "timeout_seconds": 5,
                "provider_id": "openai",
                "custom_target_confirmed": False,
            },
        )

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(payload)["error"]["code"], "VALIDATION_ERROR")
        self.assertNotIn(secret.encode(), payload)
        self.assertEqual(self.service.list_runs(), [])

    def test_real_model_catalog_endpoint_supports_keyless_local_target(self) -> None:
        status, _, payload = self.request(
            "POST",
            "/api/targets/models",
            {
                "base_url": self.base_url,
                "api_key": None,
                "target_kind": "local",
                "provider_id": None,
                "custom_target_confirmed": True,
            },
        )

        self.assertEqual(status, 200)
        result = json.loads(payload)["data"]
        self.assertEqual(result["status"], "CONNECTED")
        self.assertEqual(result["auth_mode"], "none")
        self.assertEqual(result["models"], ["mock-model"])
        self.assertEqual(result["model_catalog"][0]["id"], "mock-model")
        self.assertEqual(result["model_catalog"][0]["capability_source"], "UNSPECIFIED")

    def test_multimodal_probe_type_is_persisted_without_probe_payload(self) -> None:
        status, _, payload = self.request(
            "POST",
            "/api/runs",
            {
                "base_url": self.base_url,
                "api_key": None,
                "model": "vision-model",
                "target_kind": "local",
                "run_mode": "single",
                "stream": False,
                "timeout_seconds": 5,
                "provider_id": None,
                "custom_target_confirmed": True,
                "probe_type": "vision",
            },
        )

        self.assertEqual(status, 202)
        run = json.loads(payload)["data"]
        self.assertEqual(run["config"]["probe_type"], "vision")
        completed = self.service.get_run(run["run_id"])
        self.assertEqual(completed["result"]["status"], "PASS")
        persisted = self.service.store.database_path.read_bytes()
        self.assertNotIn(b"image_url", persisted)
        self.assertNotIn(b"iVBOR", persisted)

    def test_non_chat_canary_and_streaming_are_rejected(self) -> None:
        base = {
            "base_url": self.base_url,
            "api_key": None,
            "model": "embedding-model",
            "target_kind": "local",
            "run_mode": "single",
            "stream": False,
            "timeout_seconds": 5,
            "provider_id": None,
            "custom_target_confirmed": True,
            "probe_type": "embedding",
        }
        with self.assertRaisesRegex(ValueError, "canary"):
            self.service.start_run({**base, "run_mode": "canary"})
        with self.assertRaisesRegex(ValueError, "stream"):
            self.service.start_run({**base, "stream": True})

    def test_declared_catalog_capabilities_block_wrong_billable_endpoint(self) -> None:
        relay = create_mock_relay(
            require_api_key=False,
            reject_authorization=True,
            rich_catalog=True,
        )
        thread = threading.Thread(target=relay.serve_forever, daemon=True)
        thread.start()

        def close_relay() -> None:
            relay.shutdown()
            relay.server_close()
            thread.join(timeout=2)

        self.addCleanup(close_relay)
        host, port = relay.server_address
        with self.assertRaisesRegex(ValueError, "declared model capabilities"):
            self.service.start_run(
                {
                    "base_url": f"http://{host}:{port}/v1",
                    "api_key": None,
                    "model": "vision-model",
                    "target_kind": "local",
                    "run_mode": "single",
                    "stream": False,
                    "timeout_seconds": 5,
                    "provider_id": None,
                    "custom_target_confirmed": True,
                    "probe_type": "image_generation",
                }
            )
        self.assertEqual(self.service.list_runs(), [])

    def test_declared_speech_voice_is_used_for_the_billable_request(self) -> None:
        relay = create_mock_relay(
            require_api_key=False,
            reject_authorization=True,
            rich_catalog=True,
        )
        thread = threading.Thread(target=relay.serve_forever, daemon=True)
        thread.start()

        def close_relay() -> None:
            relay.shutdown()
            relay.server_close()
            thread.join(timeout=2)

        self.addCleanup(close_relay)
        host, port = relay.server_address
        started = self.service.start_run(
            {
                "base_url": f"http://{host}:{port}/v1",
                "api_key": None,
                "model": "speech-model",
                "target_kind": "local",
                "run_mode": "single",
                "stream": False,
                "timeout_seconds": 5,
                "provider_id": None,
                "custom_target_confirmed": True,
                "probe_type": "audio_speech",
            }
        )

        self.assertEqual(self.service.get_run(started["run_id"])["result"]["status"], "PASS")
        workflow = self.service.get_run(started["run_id"])["workflow"]
        self.assertEqual(workflow["binding_source"], "PROVIDER_METADATA")
        self.assertEqual(
            workflow["steps"][0]["facts"],
            ["PROVIDER CAPABILITY VERIFIED"],
        )
        self.assertEqual(
            relay.last_json_request["voice"],
            "en-US-Harper:MAI-Voice-2",
        )

    def test_header_unsafe_key_is_rejected_without_http_error_reflection(self) -> None:
        secret = "opaque\r\nprivate-value"
        status, _, payload = self.request(
            "POST",
            "/api/targets/models",
            {
                "base_url": "https://example.test/v1",
                "api_key": secret,
                "target_kind": "cloud",
                "provider_id": None,
                "custom_target_confirmed": True,
            },
        )

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(payload)["error"]["code"], "VALIDATION_ERROR")
        self.assertNotIn(secret.encode(), payload)
        self.assertNotIn(b"private-value", payload)

    def test_model_catalog_rejects_cloud_key_target_mismatch_without_leak(self) -> None:
        secret = "gsk_abcdefghijk12345"
        status, _, payload = self.request(
            "POST",
            "/api/targets/models",
            {
                "base_url": "https://api.openai.com/v1",
                "api_key": secret,
                "target_kind": "cloud",
                "provider_id": "openai",
                "custom_target_confirmed": False,
            },
        )

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(payload)["error"]["code"], "VALIDATION_ERROR")
        self.assertNotIn(secret.encode(), payload)
        self.assertNotIn(secret.encode(), self.service.store.database_path.read_bytes())

    def test_model_catalog_discards_reflected_key_and_cannot_persist_it(self) -> None:
        secret = "local-private-key"
        relay = create_mock_relay(
            require_api_key=False,
            reflect_catalog_authorization=True,
        )
        thread = threading.Thread(target=relay.serve_forever, daemon=True)
        thread.start()

        def close_relay() -> None:
            relay.shutdown()
            relay.server_close()
            thread.join(timeout=2)

        self.addCleanup(close_relay)
        host, port = relay.server_address
        base_url = f"http://{host}:{port}/v1"
        status, _, payload = self.request(
            "POST",
            "/api/targets/models",
            {
                "base_url": base_url,
                "api_key": secret,
                "target_kind": "local",
                "provider_id": None,
                "custom_target_confirmed": True,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["data"]["models"], [])
        self.assertNotIn(secret.encode(), payload)

        status, _, payload = self.request(
            "POST",
            "/api/runs",
            {
                "base_url": base_url,
                "api_key": secret,
                "model": f"leaked-Bearer {secret}",
                "target_kind": "local",
                "run_mode": "single",
                "stream": False,
                "timeout_seconds": 5,
                "provider_id": None,
                "custom_target_confirmed": True,
            },
        )
        self.assertEqual(status, 400)
        self.assertNotIn(secret.encode(), payload)
        self.assertNotIn(secret.encode(), self.service.store.database_path.read_bytes())

    def test_api_errors_are_structured_and_never_reflect_key(self) -> None:
        secret = "do-not-reflect-this"
        status, _, payload = self.request(
            "POST",
            "/api/runs",
            {"api_key": secret, "base_url": "http://127.0.0.1:1/v1"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(payload)["error"]["code"], "VALIDATION_ERROR")
        self.assertNotIn(secret.encode(), payload)

    def test_unknown_route_is_json_for_api_and_plain_for_static(self) -> None:
        status, _, payload = self.request("GET", "/api/missing")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(payload)["error"]["code"], "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
