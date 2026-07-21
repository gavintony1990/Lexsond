from __future__ import annotations

import json
import unittest
from pathlib import Path

from lexsond.models import ChunkMeasurement, NormalizedRunResult, RequestMeasurement, RunStatus
from lexsond.probe import ProbeConfig, _validated_usage_token
from lexsond.targets import ModelCatalogEntry


class ModelContractTests(unittest.TestCase):
    def test_result_has_all_top_level_schema_fields(self) -> None:
        schema_path = Path(__file__).parents[1] / "schemas" / "normalized-run-result.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        result = NormalizedRunResult(measurements=[RequestMeasurement()])
        result.finish(RunStatus.PASS, "TEST")
        serialized = result.to_dict()

        self.assertTrue(set(schema["required"]).issubset(serialized))
        self.assertEqual(serialized["schema_version"], "probe.ai/result/v1alpha1")
        self.assertEqual(serialized["status"], "PASS")

    def test_chunk_contract_records_reasoning_length_without_reasoning_text(self) -> None:
        schema_path = Path(__file__).parents[1] / "schemas" / "normalized-run-result.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        chunk = ChunkMeasurement(
            sequence=0,
            received_after_ms=1.0,
            event_type=None,
            content_chars=0,
            reasoning_chars=12,
        )
        serialized = NormalizedRunResult(
            measurements=[RequestMeasurement(chunks=[chunk])]
        ).to_dict()["measurements"][0]["chunks"][0]

        properties = schema["$defs"]["chunkMeasurement"]["properties"]
        required = schema["$defs"]["chunkMeasurement"]["required"]
        self.assertIn("reasoning_chars", properties)
        self.assertNotIn("reasoning_chars", required)
        self.assertEqual(serialized["reasoning_chars"], 12)
        self.assertNotIn("reasoning_content", serialized)

    def test_probe_config_repr_does_not_expose_api_key(self) -> None:
        secret = "top-secret-key"
        config = ProbeConfig(
            base_url="https://example.com/v1",
            api_key=secret,
            model="test-model",
        )
        self.assertNotIn(secret, repr(config))

    def test_probe_config_allows_keyless_local_endpoint(self) -> None:
        config = ProbeConfig(
            base_url="http://127.0.0.1:11434/v1",
            api_key=None,
            model="qwen-local",
        )
        self.assertIsNone(config.api_key)

    def test_probe_config_rejects_credential_bearing_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not contain credentials"):
            ProbeConfig(
                base_url="https://user:secret@example.test/v1?api_key=bad",
                api_key="test-key",
                model="model",
            )

    def test_probe_config_rejects_key_reflected_in_model_or_url_path(self) -> None:
        secret = "private-runtime-key"
        for base_url, model in (
            (f"https://example.test/v1/{secret}", "model"),
            ("https://example.test/v1", f"model-{secret}"),
        ):
            with self.subTest(base_url=base_url, model=model):
                with self.assertRaisesRegex(ValueError, "must not contain api_key"):
                    ProbeConfig(
                        base_url=base_url,
                        api_key=secret,
                        model=model,
                    )

        with self.assertRaisesRegex(ValueError, "prompt must not contain api_key"):
            ProbeConfig(
                base_url="https://example.test/v1",
                api_key=secret,
                model="model",
                prompt=f"diagnose with {secret}",
            )
        with self.assertRaisesRegex(ValueError, "audio_voice must not contain api_key"):
            ProbeConfig(
                base_url="https://example.test/v1",
                api_key=secret,
                model="model",
                audio_voice=f"voice-{secret}",
            )

    def test_probe_config_rejects_header_unsafe_api_key(self) -> None:
        for secret in ("opaque\r\nprivate-value", "opaque value", "opaque-密钥"):
            with self.subTest(secret=repr(secret)):
                with self.assertRaises(ValueError) as context:
                    ProbeConfig(
                        base_url="https://example.test/v1",
                        api_key=secret,
                        model="model",
                    )
                self.assertNotIn(secret, str(context.exception))
                self.assertNotIn("private-value", str(context.exception))

    def test_probe_config_only_allows_plain_http_on_numeric_loopback(self) -> None:
        for unsafe_url in (
            "http://api.example.com/v1",
            "http://192.168.1.20/v1",
            "http://localhost:8080/v1",
        ):
            with self.subTest(base_url=unsafe_url):
                with self.assertRaisesRegex(ValueError, "HTTPS"):
                    ProbeConfig(
                        base_url=unsafe_url,
                        api_key="test-key",
                        model="model",
                    )

        config = ProbeConfig(
            base_url="http://127.0.0.1:8089/v1",
            api_key="test-key",
            model="model",
        )
        self.assertEqual(config.base_url, "http://127.0.0.1:8089/v1")

    def test_provider_usage_tokens_must_be_non_negative_integers(self) -> None:
        self.assertEqual(_validated_usage_token(12, "total_tokens"), 12)
        self.assertIsNone(_validated_usage_token(None, "total_tokens"))
        for invalid in ("12", 1.5, True, -1):
            with self.subTest(value=invalid):
                with self.assertRaisesRegex(ValueError, "non-negative integer"):
                    _validated_usage_token(invalid, "total_tokens")

    def test_model_catalog_public_contract_matches_schema(self) -> None:
        schema_path = Path(__file__).parents[1] / "schemas" / "model-catalog.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        entry = ModelCatalogEntry(
            model_id="fixture/vision",
            name="Fixture Vision",
            owned_by="fixture",
            created=1,
            context_length=8192,
            input_modalities=("text", "image"),
            output_modalities=("text",),
            endpoint_types=("chat_completions",),
            probe_types=("chat", "vision"),
            supported_parameters=("max_tokens",),
            supported_voices=(),
            capability_source="PROVIDER_METADATA",
        )

        public = entry.to_public_dict()
        self.assertTrue(set(schema["$defs"]["model"]["required"]).issubset(public))
        self.assertEqual(public["probe_types"], ["chat", "vision"])
        self.assertNotIn("description", public)
        self.assertNotIn("pricing", public)


if __name__ == "__main__":
    unittest.main()
