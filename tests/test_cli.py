from __future__ import annotations

import unittest

from lexsond.cli import build_parser


class CliContractTests(unittest.TestCase):
    def test_probe_type_supports_multimodal_endpoint_families(self) -> None:
        parser = build_parser()
        for probe_type in (
            "chat",
            "vision",
            "embedding",
            "image_generation",
            "audio_speech",
            "audio_transcription",
        ):
            with self.subTest(probe_type=probe_type):
                args = parser.parse_args(
                    [
                        "--base-url",
                        "https://api.example.test/v1",
                        "--model",
                        "fixture-model",
                        "--probe-type",
                        probe_type,
                    ]
                )
                self.assertEqual(args.probe_type, probe_type)

    def test_probe_type_defaults_to_chat(self) -> None:
        args = build_parser().parse_args(
            [
                "--base-url",
                "https://api.example.test/v1",
                "--model",
                "fixture-model",
            ]
        )
        self.assertEqual(args.probe_type, "chat")

    def test_provider_id_selects_a_provider_protocol_adapter(self) -> None:
        args = build_parser().parse_args(
            [
                "--base-url",
                "https://openrouter.ai/api/v1",
                "--model",
                "fixture-model",
                "--provider-id",
                "openrouter",
                "--audio-voice",
                "fixture-voice",
            ]
        )
        self.assertEqual(args.provider_id, "openrouter")
        self.assertEqual(args.audio_voice, "fixture-voice")


if __name__ == "__main__":
    unittest.main()
