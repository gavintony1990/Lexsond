from __future__ import annotations

import json
import unittest

from lexsond.providers import (
    PROVIDERS,
    detect_provider_key,
    public_providers,
    resolve_provider_key,
)


class ProviderRegistryTests(unittest.TestCase):
    def test_every_provider_has_a_safe_openai_compatible_profile(self) -> None:
        ids = {provider.provider_id for provider in PROVIDERS}
        self.assertEqual(len(ids), len(PROVIDERS))
        self.assertGreaterEqual(len(PROVIDERS), 17)
        self.assertNotIn("local-mock", ids)
        for provider in PROVIDERS:
            self.assertTrue(provider.base_url.startswith(("https://", "http://127.0.0.1:")))
            self.assertNotIn("{", provider.base_url)
            self.assertNotIn("?", provider.base_url)
            self.assertEqual(provider.protocol, "openai-chat")
            self.assertIn(provider.target_kind, {"local", "cloud"})
            self.assertTrue(provider.docs_url.startswith("https://"))
            if provider.target_kind == "cloud":
                self.assertTrue(provider.requires_api_key)
                self.assertTrue(provider.default_model)
                self.assertTrue(provider.base_url.startswith("https://"))
            else:
                self.assertFalse(provider.requires_api_key)
                self.assertTrue(provider.base_url.startswith("http://127.0.0.1:"))

    def test_unique_prefixes_autofill_without_returning_key_material(self) -> None:
        samples = {
            "sk-or-v1-abcdefghijk12345": "openrouter",
            "gsk_abcdefghijk12345": "groq",
            "AIzaSyExampleKeyMaterial1234567890": "gemini",
            "xai-abcdefghijk12345": "xai",
            "nvapi-abcdefghijk12345": "nvidia",
            "csk-abcdefghijk12345": "cerebras",
            "pplx-abcdefghijk12345": "perplexity",
            "sk-proj-abcdefghijk12345": "openai",
        }
        for secret, provider_id in samples.items():
            with self.subTest(provider_id=provider_id):
                result = detect_provider_key(secret).to_dict()
                self.assertEqual(result["status"], "MATCHED")
                self.assertEqual(result["provider"]["id"], provider_id)
                self.assertNotIn(secret, json.dumps(result))

    def test_generic_sk_key_is_ambiguous_and_never_guessed(self) -> None:
        secret = "sk-abcdefghijk123456789"
        result = detect_provider_key(secret).to_dict()

        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertIsNone(result["provider"])
        candidate_ids = {candidate["id"] for candidate in result["candidates"]}
        self.assertTrue(
            {"openai", "deepseek", "siliconflow", "dashscope", "moonshot"}
            <= candidate_ids
        )
        self.assertNotIn(secret, json.dumps(result))

    def test_explicit_deepseek_selection_confirms_shared_sk_prefix(self) -> None:
        secret = "sk-abcdefghijk123456789"
        result = resolve_provider_key(secret, "deepseek").to_dict()

        self.assertEqual(result["status"], "CONFIRMED")
        self.assertEqual(result["provider"]["id"], "deepseek")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["reason_code"], "SHARED_PREFIX_CONFIRMED")
        self.assertNotIn(secret, json.dumps(result))

    def test_explicit_provider_must_match_detected_candidates(self) -> None:
        with self.assertRaisesRegex(ValueError, "not compatible"):
            resolve_provider_key("sk-abcdefghijk123456789", "openrouter")
        with self.assertRaisesRegex(ValueError, "does not match"):
            resolve_provider_key("gsk_abcdefghijk12345", "deepseek")

    def test_unknown_key_selection_is_manual_not_confirmed(self) -> None:
        secret = "opaque-unrecognized-secret-value"
        result = resolve_provider_key(secret, "deepseek").to_dict()

        self.assertEqual(result["status"], "MANUAL")
        self.assertEqual(result["confidence"], "NONE")
        self.assertEqual(result["provider"]["id"], "deepseek")
        self.assertEqual(result["reason_code"], "MANUAL_PROVIDER_UNVERIFIED")
        self.assertNotIn(secret, json.dumps(result))

    def test_unknown_and_invalid_keys_do_not_echo_input(self) -> None:
        secret = "opaque-unrecognized-secret-value"
        result = detect_provider_key(secret).to_dict()
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["candidates"], [])
        self.assertNotIn(secret, json.dumps(result))

        for invalid in ("", "   ", "x" * 8193):
            with self.subTest(length=len(invalid)):
                with self.assertRaisesRegex(ValueError, "api_key"):
                    detect_provider_key(invalid)

    def test_public_registry_contains_no_detection_regexes_or_secrets(self) -> None:
        public = public_providers()
        serialized = json.dumps(public)
        self.assertNotIn("pattern", serialized)
        self.assertNotIn("regex", serialized)
        self.assertNotIn("local-mock", {item["id"] for item in public})
        self.assertGreaterEqual(len(public_providers("local")), 5)
        self.assertGreaterEqual(len(public_providers("cloud")), 12)
        self.assertTrue(all(item["target_kind"] == "local" for item in public_providers("local")))


if __name__ == "__main__":
    unittest.main()
