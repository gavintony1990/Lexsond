from __future__ import annotations

import json
import threading
import time
import unittest

from lexsond.mock_relay import create_server
from lexsond.models import ErrorClass, RunStatus
from lexsond.probe import (
    OpenAIChatProbe,
    ProbeConfig,
    ProbeType,
    run_openai_probe,
)


class ProbeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = create_server()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base_url = f"http://{host}:{port}/v1"
        cls.keyless_server = create_server(
            require_api_key=False,
            reject_authorization=True,
        )
        cls.keyless_thread = threading.Thread(
            target=cls.keyless_server.serve_forever,
            daemon=True,
        )
        cls.keyless_thread.start()
        keyless_host, keyless_port = cls.keyless_server.server_address
        cls.keyless_base_url = f"http://{keyless_host}:{keyless_port}/v1"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.keyless_server.shutdown()
        cls.keyless_server.server_close()
        cls.keyless_thread.join(timeout=2)

    def run_probe(
        self,
        *,
        mode: str | None = None,
        stream: bool = True,
        api_key: str = "test-key",
    ):
        return OpenAIChatProbe(
            ProbeConfig(
                base_url=self.base_url,
                api_key=api_key,
                model="mock-model",
                stream=stream,
                mock_mode=mode,
            )
        ).run()

    def test_streaming_probe_collects_timing_and_usage(self) -> None:
        result = self.run_probe()
        measurement = result.measurements[0]

        self.assertEqual(result.status, RunStatus.PASS)
        self.assertEqual(measurement.output_text, "PROBE_OK")
        self.assertEqual(measurement.finish_reason, "stop")
        self.assertEqual(measurement.provider_reported_total_tokens, 11)
        self.assertIsNotNone(measurement.response_headers_ms)
        self.assertIsNotNone(measurement.ttfb_ms)
        self.assertIsNotNone(measurement.ttft_ms)
        self.assertIsNotNone(measurement.e2e_ms)
        self.assertGreaterEqual(measurement.chunk_count, 5)

    def test_dynamic_expected_text_detects_fixed_or_replayed_output(self) -> None:
        matching = OpenAIChatProbe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="mock-model",
                stream=True,
                expected_text="PROBE_OK",
            )
        ).run()
        replayed = OpenAIChatProbe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="mock-model",
                stream=True,
                expected_text="LEXSOND_RESULT=123",
            )
        ).run()
        self.assertEqual(matching.status, RunStatus.PASS)
        self.assertEqual(replayed.status, RunStatus.FAIL)
        self.assertIn("EXPECTED_TEXT_ASSERTION_FAILED", replayed.reason_codes)

    def test_each_multimodal_component_emits_ordered_progress(self) -> None:
        scenarios = (
            (ProbeType.CHAT, True, None),
            (ProbeType.VISION, False, None),
            (ProbeType.EMBEDDING, False, None),
            (ProbeType.IMAGE_GENERATION, False, None),
            (ProbeType.AUDIO_SPEECH, False, None),
            (ProbeType.AUDIO_TRANSCRIPTION, False, None),
        )
        expected_steps = (
            "fixture_prepare",
            "request_dispatch",
            "transport_check",
            "response_validate",
            "quality_assert",
        )
        for probe_type, stream, voice in scenarios:
            with self.subTest(probe_type=probe_type.value):
                events = []
                result = run_openai_probe(
                    ProbeConfig(
                        base_url=self.base_url,
                        api_key="test-key",
                        model=f"{probe_type.value}-model",
                        stream=stream,
                        probe_type=probe_type,
                        audio_voice=voice,
                    ),
                    progress=lambda step_id, status: events.append(
                        (step_id, status.value)
                    ),
                )

                self.assertEqual(result.status, RunStatus.PASS)
                self.assertEqual(
                    events,
                    [
                        event
                        for step_id in expected_steps
                        for event in ((step_id, "RUNNING"), (step_id, "PASS"))
                    ],
                )

    def test_component_failure_event_stays_on_the_observed_stage(self) -> None:
        events = []
        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="image-model",
                stream=False,
                probe_type=ProbeType.IMAGE_GENERATION,
                mock_mode="truncated_image",
            ),
            progress=lambda step_id, status: events.append((step_id, status.value)),
        )

        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertIn(("response_validate", "PASS"), events)
        self.assertIn(("quality_assert", "FAIL"), events)

    def test_progress_observer_time_is_excluded_from_probe_metrics_and_deadline(self) -> None:
        started = time.perf_counter()

        def slow_observer(step_id, status) -> None:
            time.sleep(0.02)

        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="mock-model",
                stream=False,
                timeout_seconds=0.05,
            ),
            progress=slow_observer,
        )
        wall_ms = (time.perf_counter() - started) * 1_000
        measurement = result.measurements[0]

        self.assertEqual(result.status, RunStatus.PASS)
        self.assertGreater(wall_ms, 150)
        self.assertLess(measurement.response_headers_ms or 1_000, 50)
        self.assertLess(measurement.e2e_ms or 1_000, 50)

    def test_progress_observer_failure_does_not_change_provider_result(self) -> None:
        def failing_observer(step_id, status) -> None:
            raise RuntimeError("observer unavailable")

        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="mock-model",
                stream=False,
            ),
            progress=failing_observer,
        )

        self.assertEqual(result.status, RunStatus.PASS)

    def test_non_streaming_probe(self) -> None:
        result = self.run_probe(stream=False)
        measurement = result.measurements[0]
        self.assertEqual(result.status, RunStatus.PASS)
        self.assertEqual(measurement.output_text, "PROBE_OK")
        self.assertIsNone(measurement.ttft_ms)
        self.assertIsNotNone(measurement.ttfb_ms)

    def test_reasoning_stream_contributes_timing_without_persisting_reasoning(self) -> None:
        result = self.run_probe(mode="reasoning_stream")
        measurement = result.measurements[0]
        serialized = json.dumps(result.to_dict())

        self.assertEqual(result.status, RunStatus.PASS)
        self.assertEqual(measurement.output_text, "PROBE_OK")
        self.assertTrue(measurement.evidence["reasoning_content_observed"])
        self.assertEqual(measurement.evidence["reasoning_output_chars"], 34)
        self.assertEqual(sum(chunk.reasoning_chars for chunk in measurement.chunks), 34)
        self.assertFalse(measurement.evidence["pseudo_stream_suspected"])
        self.assertTrue(measurement.evidence["final_content_burst_observed"])
        self.assertLess(
            measurement.ttft_ms or 0,
            measurement.evidence["first_content_token_ms"],
        )
        self.assertLess(measurement.output_tps or 0, 1_000)
        self.assertNotIn("private-reasoning", serialized)

    def test_non_streaming_reasoning_is_counted_without_raw_retention(self) -> None:
        result = self.run_probe(mode="reasoning_stream", stream=False)
        measurement = result.measurements[0]

        self.assertEqual(result.status, RunStatus.PASS)
        self.assertEqual(measurement.evidence["reasoning_output_chars"], 34)
        self.assertNotIn("private-reasoning", json.dumps(result.to_dict()))

    def test_non_string_reasoning_delta_is_a_protocol_failure(self) -> None:
        result = self.run_probe(mode="invalid_reasoning_content")

        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertEqual(result.measurements[0].error_class, ErrorClass.PROTOCOL)

    def test_whitespace_only_output_is_not_a_successful_probe(self) -> None:
        result = self.run_probe(mode="whitespace_output")

        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertIn("EMPTY_OR_INCOMPLETE_RESPONSE", result.reason_codes)

    def test_http_error_retains_safe_code_without_reasoning_body(self) -> None:
        result = self.run_probe(mode="reasoning_error")
        measurement = result.measurements[0]
        serialized = json.dumps(result.to_dict())

        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertEqual(measurement.error_class, ErrorClass.PROTOCOL)
        self.assertEqual(measurement.error_message, "HTTP 400")
        self.assertNotIn("private-reasoning", serialized)
        self.assertNotIn("reasoning_content", serialized)

    def test_keyless_local_probe_sends_no_authorization_header(self) -> None:
        result = OpenAIChatProbe(
            ProbeConfig(
                base_url=self.keyless_base_url,
                api_key=None,
                model="local-real-model",
                stream=False,
            )
        ).run()
        self.assertEqual(result.status, RunStatus.PASS)
        self.assertEqual(result.measurements[0].response_model, "local-real-model")

    def test_rate_limit_is_classified(self) -> None:
        result = self.run_probe(mode="rate_limit")
        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertEqual(result.measurements[0].error_class, ErrorClass.RATE_LIMIT)

    def test_insufficient_balance_is_classified_without_error_body(self) -> None:
        result = self.run_probe(mode="payment_required")
        measurement = result.measurements[0]
        self.assertEqual(measurement.error_class, ErrorClass.PAYMENT_REQUIRED)
        self.assertNotIn("Insufficient balance", json.dumps(result.to_dict()))

    def test_missing_model_is_classified(self) -> None:
        result = self.run_probe(mode="model_not_found")
        self.assertEqual(
            result.measurements[0].error_class,
            ErrorClass.MODEL_NOT_FOUND,
        )

    def test_invalid_key_is_classified_without_leaking_key(self) -> None:
        secret = "do-not-leak-this-key"
        result = self.run_probe(api_key=secret)
        measurement = result.measurements[0]
        self.assertEqual(measurement.error_class, ErrorClass.AUTHENTICATION)
        self.assertNotIn(secret, measurement.error_message or "")
        self.assertNotIn(secret, str(result.to_dict()))

    def test_malformed_sse_is_protocol_failure(self) -> None:
        result = self.run_probe(mode="malformed_sse")
        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertEqual(result.measurements[0].error_class, ErrorClass.PROTOCOL)

    def test_disconnect_without_done_is_protocol_failure(self) -> None:
        result = self.run_probe(mode="disconnect")
        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertEqual(result.measurements[0].error_class, ErrorClass.PROTOCOL)

    def test_done_terminates_stream_without_waiting_for_http_eof(self) -> None:
        result = OpenAIChatProbe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="mock-model",
                stream=True,
                timeout_seconds=0.05,
                mock_mode="done_then_hang",
            )
        ).run()

        self.assertEqual(result.status, RunStatus.PASS)
        self.assertTrue(result.measurements[0].evidence["sse_done_received"])

    def test_slow_ttft_is_observable(self) -> None:
        normal = self.run_probe().measurements[0]
        slow = self.run_probe(mode="slow_ttft").measurements[0]
        self.assertIsNotNone(normal.ttft_ms)
        self.assertIsNotNone(slow.ttft_ms)
        self.assertGreater(slow.ttft_ms, normal.ttft_ms + 80)

    def test_wrong_usage_is_preserved_as_evidence(self) -> None:
        result = self.run_probe(mode="wrong_usage")
        self.assertEqual(result.status, RunStatus.PASS)
        self.assertEqual(result.measurements[0].provider_reported_total_tokens, 18_000)

    def test_pseudo_stream_is_detected_from_chunk_timing(self) -> None:
        result = self.run_probe(mode="pseudo_stream")
        self.assertEqual(result.status, RunStatus.PASS)
        self.assertTrue(result.measurements[0].evidence["pseudo_stream_suspected"])

    def test_single_full_answer_event_is_detected_as_pseudo_stream(self) -> None:
        result = self.run_probe(mode="single_chunk_stream")

        self.assertEqual(result.status, RunStatus.PASS)
        self.assertEqual(result.measurements[0].output_text, "PROBE_OK")
        self.assertTrue(result.measurements[0].evidence["pseudo_stream_suspected"])

    def test_vision_probe_sends_built_in_image_and_validates_visual_answer(self) -> None:
        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="vision-model",
                stream=False,
                probe_type=ProbeType.VISION,
            )
        )

        measurement = result.measurements[0]
        self.assertEqual(result.status, RunStatus.PASS)
        self.assertEqual(measurement.output_text, "RED")
        self.assertEqual(measurement.evidence["probe_type"], "vision")
        self.assertEqual(measurement.evidence["input_modalities"], ["text", "image"])
        self.assertNotIn("image_url", json.dumps(result.to_dict()))

    def test_embedding_probe_records_shape_without_retaining_vector(self) -> None:
        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="embedding-model",
                stream=False,
                probe_type=ProbeType.EMBEDDING,
            )
        )

        measurement = result.measurements[0]
        serialized = json.dumps(result.to_dict())
        self.assertEqual(result.status, RunStatus.PASS)
        self.assertEqual(measurement.evidence["probe_type"], "embedding")
        self.assertEqual(measurement.evidence["embedding_dimensions"], 4)
        # Assert the wire-shape key is absent. A vector value such as 0.125 can
        # legitimately equal a millisecond timing field and is not a safe
        # string-level sentinel for disclosure.
        self.assertNotIn('"embedding": [', serialized)
        self.assertEqual(measurement.output_text, "")

    def test_image_generation_probe_keeps_only_output_metadata(self) -> None:
        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="image-model",
                stream=False,
                probe_type=ProbeType.IMAGE_GENERATION,
            )
        )

        measurement = result.measurements[0]
        serialized = json.dumps(result.to_dict())
        self.assertEqual(result.status, RunStatus.PASS)
        self.assertEqual(measurement.evidence["generated_image_count"], 1)
        self.assertEqual(measurement.evidence["image_transport"], "b64_json")
        self.assertNotIn("private-image-payload", serialized)

    def test_audio_speech_probe_counts_bytes_without_retaining_audio(self) -> None:
        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="speech-model",
                stream=False,
                probe_type=ProbeType.AUDIO_SPEECH,
            )
        )

        measurement = result.measurements[0]
        self.assertEqual(result.status, RunStatus.PASS)
        self.assertGreater(measurement.evidence["audio_bytes"], 0)
        self.assertEqual(measurement.evidence["output_modalities"], ["audio"])
        self.assertNotIn("RIFF", json.dumps(result.to_dict()))

    def test_audio_transcription_probe_uses_bounded_built_in_wav(self) -> None:
        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="transcription-model",
                stream=False,
                probe_type=ProbeType.AUDIO_TRANSCRIPTION,
            )
        )

        measurement = result.measurements[0]
        self.assertEqual(result.status, RunStatus.PASS)
        self.assertEqual(measurement.evidence["input_modalities"], ["audio"])
        self.assertEqual(measurement.evidence["probe_audio_seconds"], 1.0)
        self.assertNotIn("probe.wav", json.dumps(result.to_dict()))

    def test_non_chat_malformed_response_is_a_protocol_failure(self) -> None:
        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="embedding-model",
                stream=False,
                probe_type=ProbeType.EMBEDDING,
                mock_mode="malformed_endpoint",
            )
        )

        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertEqual(result.measurements[0].error_class, ErrorClass.PROTOCOL)

    def test_malformed_generated_image_is_a_protocol_failure(self) -> None:
        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="image-model",
                stream=False,
                probe_type=ProbeType.IMAGE_GENERATION,
                mock_mode="malformed_endpoint",
            )
        )

        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertEqual(result.measurements[0].error_class, ErrorClass.PROTOCOL)

    def test_truncated_image_with_valid_magic_is_a_protocol_failure(self) -> None:
        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="image-model",
                stream=False,
                probe_type=ProbeType.IMAGE_GENERATION,
                mock_mode="truncated_image",
            )
        )

        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertEqual(result.measurements[0].error_class, ErrorClass.PROTOCOL)

    def test_compressed_image_bomb_is_rejected_with_a_bounded_decode(self) -> None:
        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="image-model",
                stream=False,
                probe_type=ProbeType.IMAGE_GENERATION,
                mock_mode="image_bomb",
            )
        )

        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertEqual(result.measurements[0].error_class, ErrorClass.PROTOCOL)

    def test_marker_only_non_png_images_cannot_pass_strict_validation(self) -> None:
        for mode in ("fake_jpeg", "fake_gif", "fake_webp"):
            with self.subTest(mode=mode):
                result = run_openai_probe(
                    ProbeConfig(
                        base_url=self.base_url,
                        api_key="test-key",
                        model="image-model",
                        stream=False,
                        probe_type=ProbeType.IMAGE_GENERATION,
                        mock_mode=mode,
                    )
                )
                self.assertEqual(result.status, RunStatus.FAIL)
                self.assertEqual(
                    result.measurements[0].error_class,
                    ErrorClass.PROTOCOL,
                )

    def test_semantically_invalid_pngs_cannot_pass_strict_validation(self) -> None:
        for mode in (
            "png_invalid_filter",
            "png_missing_palette",
            "png_invalid_chunk_type",
            "png_invalid_reserved_bit",
            "png_too_many_chunks",
        ):
            with self.subTest(mode=mode):
                result = run_openai_probe(
                    ProbeConfig(
                        base_url=self.base_url,
                        api_key="test-key",
                        model="image-model",
                        stream=False,
                        probe_type=ProbeType.IMAGE_GENERATION,
                        mock_mode=mode,
                    )
                )
                self.assertEqual(result.status, RunStatus.FAIL)
                self.assertEqual(
                    result.measurements[0].error_class,
                    ErrorClass.PROTOCOL,
                )

    def test_image_url_is_not_misreported_as_strictly_validated(self) -> None:
        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="image-model",
                stream=False,
                probe_type=ProbeType.IMAGE_GENERATION,
                mock_mode="image_url",
            )
        )

        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertEqual(result.measurements[0].error_class, ErrorClass.PROTOCOL)

    def test_malformed_speech_audio_is_a_protocol_failure(self) -> None:
        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="speech-model",
                stream=False,
                probe_type=ProbeType.AUDIO_SPEECH,
                mock_mode="malformed_endpoint",
            )
        )

        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertEqual(result.measurements[0].error_class, ErrorClass.PROTOCOL)

    def test_truncated_wav_frame_is_a_protocol_failure(self) -> None:
        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="speech-model",
                stream=False,
                probe_type=ProbeType.AUDIO_SPEECH,
                mock_mode="truncated_wav",
            )
        )

        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertEqual(result.measurements[0].error_class, ErrorClass.PROTOCOL)

    def test_non_chat_timeout_is_classified(self) -> None:
        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="speech-model",
                stream=False,
                timeout_seconds=0.02,
                probe_type=ProbeType.AUDIO_SPEECH,
                mock_mode="slow_endpoint",
            )
        )

        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertEqual(result.measurements[0].error_class, ErrorClass.TIMEOUT)

    def test_non_chat_provider_error_body_is_never_retained(self) -> None:
        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="image-model",
                stream=False,
                probe_type=ProbeType.IMAGE_GENERATION,
                mock_mode="endpoint_secret_error",
            )
        )

        measurement = result.measurements[0]
        serialized = json.dumps(result.to_dict())
        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertEqual(measurement.error_message, "HTTP 400")
        self.assertNotIn("private-endpoint-payload", serialized)

    def test_provider_cannot_reflect_key_through_response_model(self) -> None:
        chat = self.run_probe(mode="reflect_response_model", stream=False)
        embedding = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="embedding-model",
                stream=False,
                probe_type=ProbeType.EMBEDDING,
                mock_mode="reflect_response_model",
            )
        )

        self.assertNotIn("test-key", json.dumps(chat.to_dict()))
        self.assertNotIn("test-key", json.dumps(embedding.to_dict()))

    def test_provider_cannot_reflect_key_through_output_or_metadata(self) -> None:
        reflected_output = self.run_probe(mode="reflect_output_key", stream=False)
        reflected_stream_output = self.run_probe(mode="reflect_output_key", stream=True)
        reflected_failed_stream = self.run_probe(
            mode="reflect_then_disconnect",
            stream=True,
        )
        reflected_metadata = self.run_probe(mode="reflect_metadata", stream=True)
        reflected_content_type = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="speech-model",
                stream=False,
                probe_type=ProbeType.AUDIO_SPEECH,
                mock_mode="reflect_metadata",
            )
        )

        for result in (
            reflected_output,
            reflected_stream_output,
            reflected_failed_stream,
            reflected_metadata,
            reflected_content_type,
        ):
            self.assertNotIn("test-key", json.dumps(result.to_dict()))

    def test_non_streaming_chat_response_is_bounded(self) -> None:
        result = self.run_probe(mode="oversized_json", stream=False)

        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertEqual(result.measurements[0].error_class, ErrorClass.PROTOCOL)

    def test_streaming_absolute_deadline_stops_slow_drip(self) -> None:
        result = OpenAIChatProbe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="mock-model",
                stream=True,
                timeout_seconds=0.06,
                mock_mode="slow_drip",
            )
        ).run()

        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertEqual(result.measurements[0].error_class, ErrorClass.TIMEOUT)

    def test_streaming_event_count_is_bounded(self) -> None:
        result = self.run_probe(mode="event_flood", stream=True)

        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertEqual(result.measurements[0].error_class, ErrorClass.PROTOCOL)

    def test_error_response_slow_drip_respects_absolute_deadline(self) -> None:
        chat = OpenAIChatProbe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="mock-model",
                stream=False,
                timeout_seconds=0.06,
                mock_mode="slow_error_drip",
            )
        ).run()
        endpoint = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="embedding-model",
                stream=False,
                timeout_seconds=0.06,
                probe_type=ProbeType.EMBEDDING,
                mock_mode="slow_error_drip",
            )
        )

        self.assertEqual(chat.measurements[0].error_class, ErrorClass.TIMEOUT)
        self.assertEqual(endpoint.measurements[0].error_class, ErrorClass.TIMEOUT)

    def test_status_line_slow_drip_respects_absolute_deadline(self) -> None:
        for probe_type in (ProbeType.CHAT, ProbeType.EMBEDDING):
            with self.subTest(probe_type=probe_type):
                result = run_openai_probe(
                    ProbeConfig(
                        base_url=self.base_url,
                        api_key="test-key",
                        model="mock-model",
                        stream=False,
                        timeout_seconds=0.06,
                        probe_type=probe_type,
                        mock_mode="slow_header_drip",
                    )
                )
                self.assertEqual(result.status, RunStatus.FAIL)
                self.assertEqual(
                    result.measurements[0].error_class,
                    ErrorClass.TIMEOUT,
                )

    def test_openrouter_uses_its_declared_image_generation_contract(self) -> None:
        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="image-model",
                stream=False,
                probe_type=ProbeType.IMAGE_GENERATION,
                provider_id="openrouter",
            )
        )

        self.assertEqual(result.status, RunStatus.PASS)
        self.assertEqual(self.server.last_post_path, "/v1/images")

    def test_openrouter_transcription_uses_json_input_audio(self) -> None:
        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="transcription-model",
                stream=False,
                probe_type=ProbeType.AUDIO_TRANSCRIPTION,
                provider_id="openrouter",
            )
        )

        self.assertEqual(result.status, RunStatus.PASS)
        self.assertEqual(self.server.last_post_path, "/v1/audio/transcriptions")
        self.assertEqual(self.server.last_content_type, "application/json")
        self.assertNotIn("probe.wav", json.dumps(result.to_dict()))

    def test_openrouter_speech_uses_declared_voice_and_mp3_contract(self) -> None:
        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="speech-model",
                stream=False,
                probe_type=ProbeType.AUDIO_SPEECH,
                provider_id="openrouter",
                audio_voice="fixture-voice",
            )
        )

        self.assertEqual(result.status, RunStatus.PASS)
        self.assertEqual(self.server.last_json_request["voice"], "fixture-voice")
        self.assertEqual(self.server.last_json_request["response_format"], "mp3")
        self.assertIn("audio/mpeg", self.server.last_accept)
        self.assertEqual(result.measurements[0].evidence["audio_format"], "mp3")

    def test_openrouter_speech_without_declared_voice_sends_no_request(self) -> None:
        self.server.last_post_path = None
        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="speech-model",
                stream=False,
                probe_type=ProbeType.AUDIO_SPEECH,
                provider_id="openrouter",
            )
        )

        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertIsNone(self.server.last_post_path)

    def test_openrouter_speech_rejects_fake_mp3_header(self) -> None:
        result = run_openai_probe(
            ProbeConfig(
                base_url=self.base_url,
                api_key="test-key",
                model="speech-model",
                stream=False,
                probe_type=ProbeType.AUDIO_SPEECH,
                provider_id="openrouter",
                audio_voice="fixture-voice",
                mock_mode="malformed_mp3_header",
            )
        )

        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertEqual(result.measurements[0].error_class, ErrorClass.PROTOCOL)


if __name__ == "__main__":
    unittest.main()
