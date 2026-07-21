from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lexsond.targets import (
    MAX_MODELS,
    TargetConnectionError,
    fetch_model_catalog,
    fetch_model_catalog_entries,
)


class CatalogHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def do_GET(self):  # noqa: N802
        self.server.authorization = self.headers.get("Authorization")
        self.server.last_path = self.path
        if self.path == "/api/tags":
            if self.server.mode == "empty":
                self._json(200, {"models": []})
                return
            self._json(
                200,
                {
                    "models": [
                        {"name": "qwen3:8b", "model": "qwen3:8b"},
                        {"name": "deepseek-r1:7b", "model": "deepseek-r1:7b"},
                    ]
                },
            )
            return
        if self.path not in {"/v1/models", "/v1/models?output_modalities=all"}:
            self._json(404, {"error": "not found"})
            return
        if self.server.mode == "error":
            self._json(503, {"error": "secret response must not escape"})
            return
        if self.server.mode == "reflect":
            self._json(
                200,
                {"data": [{"id": f"leaked-{self.server.authorization}"}]},
            )
            return
        if self.server.mode == "malformed":
            body = b"{not-json}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.server.mode == "slow_drip":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            for byte in b'{"data":[{"id":"slow-model"}]}':
                try:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                time.sleep(0.02)
            self.close_connection = True
            return
        if self.server.mode == "slow_header_drip":
            raw_response = (
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: 11\r\nConnection: close\r\n\r\n{\"data\":[]}"
            )
            for byte in raw_response:
                try:
                    self.connection.sendall(bytes([byte]))
                except (BrokenPipeError, ConnectionResetError):
                    break
                time.sleep(0.02)
            self.close_connection = True
            return
        if self.server.mode == "multimodal":
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "vision-model",
                            "name": "Vision Model",
                            "owned_by": "fixture",
                            "architecture": {
                                "input_modalities": ["text", "image"],
                                "output_modalities": ["text"],
                            },
                            "supported_parameters": ["max_tokens", "tools"],
                        },
                        {
                            "id": "embedding-model",
                            "architecture": {
                                "input_modalities": ["text"],
                                "output_modalities": ["embeddings"],
                            },
                        },
                        {
                            "id": "image-model",
                            "architecture": {
                                "input_modalities": ["text"],
                                "output_modalities": ["image"],
                            },
                        },
                        {
                            "id": "speech-model",
                            "architecture": {
                                "input_modalities": ["text"],
                                "output_modalities": ["speech"],
                            },
                            "supported_voices": ["en-US-Harper:MAI-Voice-2"],
                        },
                        {
                            "id": "transcription-model",
                            "architecture": {
                                "input_modalities": ["audio"],
                                "output_modalities": ["transcription"],
                            },
                        },
                        {
                            "id": "audio-understanding-model",
                            "architecture": {
                                "input_modalities": ["audio"],
                                "output_modalities": ["text"],
                            },
                        },
                        {
                            "id": "audio-output-chat-model",
                            "architecture": {
                                "input_modalities": ["text"],
                                "output_modalities": ["audio"],
                            },
                        },
                        {
                            "id": "image-only-to-text-model",
                            "architecture": {
                                "input_modalities": ["image"],
                                "output_modalities": ["text"],
                            },
                        },
                        {
                            "id": "image-to-image-model",
                            "architecture": {
                                "input_modalities": ["image"],
                                "output_modalities": ["image"],
                            },
                        },
                        {
                            "id": "audio-to-embedding-model",
                            "architecture": {
                                "input_modalities": ["audio"],
                                "output_modalities": ["embeddings"],
                            },
                        },
                        {
                            "id": "video-model",
                            "architecture": {
                                "input_modalities": ["text", "image"],
                                "output_modalities": ["video"],
                            },
                        },
                        {"id": "unknown-model", "object": "model"},
                    ],
                },
            )
            return
        if self.server.mode == "oversized":
            self._json(
                200,
                {"data": [{"id": f"model-{index}"} for index in range(MAX_MODELS + 1)]},
            )
            return
        self._json(
            200,
            {
                "object": "list",
                "data": [
                    {"id": "qwen-local", "object": "model"},
                    {"id": "deepseek-r1-local", "object": "model"},
                ],
            },
        )

    def _json(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TargetCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), CatalogHandler)
        cls.server.mode = "ok"
        cls.server.authorization = None
        cls.server.last_path = None
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base_url = f"http://{host}:{port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def tearDown(self):
        self.server.mode = "ok"
        self.server.authorization = None
        self.server.last_path = None

    def test_fetches_real_model_catalog_without_api_key(self):
        models = fetch_model_catalog(self.base_url, api_key=None)
        self.assertEqual(models, ["qwen-local", "deepseek-r1-local"])
        self.assertIsNone(self.server.authorization)

    def test_optional_bearer_key_is_sent_but_never_returned(self):
        secret = "local-private-key"
        models = fetch_model_catalog(self.base_url, api_key=secret)
        self.assertEqual(models[0], "qwen-local")
        self.assertEqual(self.server.authorization, f"Bearer {secret}")
        self.assertNotIn(secret, json.dumps(models))

    def test_ollama_uses_its_real_native_model_catalog(self):
        models = fetch_model_catalog(
            self.base_url,
            api_key=None,
            provider_id="ollama",
        )
        self.assertEqual(models, ["qwen3:8b", "deepseek-r1:7b"])
        self.assertEqual(self.server.last_path, "/api/tags")

    def test_empty_ollama_catalog_is_a_successful_empty_result(self):
        self.server.mode = "empty"
        models = fetch_model_catalog(
            self.base_url,
            api_key=None,
            provider_id="ollama",
        )
        self.assertEqual(models, [])
        self.assertEqual(self.server.last_path, "/api/tags")

    def test_provider_errors_and_malformed_json_are_safe(self):
        for mode in ("error", "malformed"):
            with self.subTest(mode=mode):
                self.server.mode = mode
                with self.assertRaises(TargetConnectionError) as context:
                    fetch_model_catalog(self.base_url, api_key="do-not-reflect")
                message = str(context.exception)
                self.assertNotIn("do-not-reflect", message)
                self.assertNotIn("secret response", message)

    def test_catalog_slow_drip_respects_absolute_timeout(self):
        self.server.mode = "slow_drip"

        with self.assertRaisesRegex(TargetConnectionError, "timed out"):
            fetch_model_catalog_entries(
                self.base_url,
                api_key=None,
                timeout_seconds=0.12,
            )

    def test_catalog_slow_header_respects_absolute_timeout(self):
        self.server.mode = "slow_header_drip"

        with self.assertRaisesRegex(TargetConnectionError, "timed out"):
            fetch_model_catalog_entries(
                self.base_url,
                api_key=None,
                timeout_seconds=0.12,
            )

    def test_reflected_api_key_is_discarded_from_model_catalog(self):
        secret = "local-private-key"
        self.server.mode = "reflect"
        models = fetch_model_catalog(self.base_url, api_key=secret)
        self.assertEqual(models, [])
        self.assertNotIn(secret, json.dumps(models))

    def test_rich_catalog_preserves_explicit_modalities_and_probe_types(self):
        self.server.mode = "multimodal"

        entries = fetch_model_catalog_entries(
            self.base_url,
            api_key=None,
            provider_id="openrouter",
        )

        self.assertEqual(self.server.last_path, "/v1/models?output_modalities=all")
        self.assertEqual([entry.model_id for entry in entries], [
            "vision-model",
            "embedding-model",
            "image-model",
            "speech-model",
            "transcription-model",
            "audio-understanding-model",
            "audio-output-chat-model",
            "image-only-to-text-model",
            "image-to-image-model",
            "audio-to-embedding-model",
            "video-model",
            "unknown-model",
        ])
        by_id = {entry.model_id: entry for entry in entries}
        self.assertEqual(by_id["vision-model"].input_modalities, ("text", "image"))
        self.assertEqual(by_id["vision-model"].probe_types, ("chat", "vision"))
        self.assertEqual(by_id["embedding-model"].probe_types, ("embedding",))
        self.assertEqual(by_id["image-model"].probe_types, ("image_generation",))
        self.assertEqual(by_id["speech-model"].probe_types, ("audio_speech",))
        self.assertEqual(
            by_id["speech-model"].supported_voices,
            ("en-US-Harper:MAI-Voice-2",),
        )
        self.assertEqual(
            by_id["transcription-model"].probe_types,
            ("audio_transcription",),
        )
        self.assertEqual(by_id["audio-understanding-model"].probe_types, ())
        self.assertEqual(
            by_id["audio-understanding-model"].endpoint_types,
            ("chat_completions",),
        )
        self.assertEqual(by_id["audio-output-chat-model"].probe_types, ())
        self.assertEqual(
            by_id["audio-output-chat-model"].endpoint_types,
            ("chat_completions",),
        )
        self.assertEqual(by_id["image-only-to-text-model"].probe_types, ())
        self.assertEqual(by_id["image-to-image-model"].probe_types, ())
        self.assertEqual(by_id["audio-to-embedding-model"].probe_types, ())
        self.assertEqual(by_id["video-model"].endpoint_types, ("video_generation",))
        self.assertEqual(by_id["video-model"].probe_types, ())
        self.assertEqual(by_id["unknown-model"].capability_source, "UNSPECIFIED")
        self.assertEqual(by_id["unknown-model"].probe_types, ())
        public = [entry.to_public_dict() for entry in entries]
        self.assertNotIn("description", json.dumps(public))

    def test_basic_catalog_wrapper_remains_backward_compatible(self):
        self.server.mode = "multimodal"

        models = fetch_model_catalog(self.base_url, api_key=None)

        self.assertEqual(models[0], "vision-model")
        self.assertIn("unknown-model", models)

    def test_catalog_rejects_header_unsafe_keys_without_reflecting_them(self):
        for secret in ("opaque\r\nprivate-value", "opaque value", "opaque-密钥"):
            with self.subTest(secret=repr(secret)):
                with self.assertRaises(ValueError) as context:
                    fetch_model_catalog(self.base_url, api_key=secret)
                self.assertNotIn(secret, str(context.exception))
                self.assertNotIn("private-value", str(context.exception))

    def test_oversized_catalog_is_not_misreported_as_complete(self):
        self.server.mode = "oversized"

        with self.assertRaisesRegex(TargetConnectionError, "model count limit"):
            fetch_model_catalog_entries(self.base_url, api_key=None)


if __name__ == "__main__":
    unittest.main()
