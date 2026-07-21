from __future__ import annotations

import unittest

from langchain_core.tracers.context import collect_runs

from lexsond.models import NormalizedRunResult, RequestMeasurement, RunStatus
from lexsond.probe import ProbeConfig, ProbeType
from lexsond.web.langchain_runtime import NativeProbeChatModel, invoke_native_probe


class LangChainRuntimeTests(unittest.TestCase):
    def test_chat_model_calls_native_transport_once_and_hides_key(self) -> None:
        calls = []

        def runner(config, *, progress=None):
            calls.append((config, progress))
            return NormalizedRunResult(status=RunStatus.PASS)

        config = ProbeConfig(
            base_url="https://api.example.com/v1",
            api_key="sk-test-secret-value",
            model="model-a",
            probe_type=ProbeType.CHAT,
            stream=False,
        )
        model = NativeProbeChatModel(config, native_runner=runner)

        result = model.probe()

        self.assertEqual(result.status, RunStatus.PASS)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("sk-test-secret-value", repr(model))
        self.assertNotIn("sk-test-secret-value", str(model.model_dump()))

    def test_non_chat_probe_uses_one_runnable_invocation(self) -> None:
        calls = []

        def runner(config, *, progress=None):
            calls.append(config.probe_type)
            return NormalizedRunResult(status=RunStatus.PASS)

        result = invoke_native_probe(
            ProbeConfig(
                base_url="https://api.example.com/v1",
                api_key="sk-test-secret-value",
                model="embedding-a",
                probe_type=ProbeType.EMBEDDING,
                stream=False,
            ),
            native_runner=runner,
        )

        self.assertEqual(result.status, RunStatus.PASS)
        self.assertEqual(calls, [ProbeType.EMBEDDING])

    def test_non_chat_global_trace_contains_only_safe_status(self) -> None:
        secret_observation = "provider-raw-transcript-secret"

        def runner(config, *, progress=None):
            del config, progress
            return NormalizedRunResult(
                status=RunStatus.PASS,
                measurements=[RequestMeasurement(output_text=secret_observation)],
            )

        with collect_runs() as collector:
            result = invoke_native_probe(
                ProbeConfig(
                    base_url="https://api.example.com/v1",
                    api_key="sk-test-secret-value",
                    model="audio-a",
                    probe_type=ProbeType.AUDIO_TRANSCRIPTION,
                    stream=False,
                ),
                native_runner=runner,
            )

        self.assertEqual(result.measurements[0].output_text, secret_observation)
        traced = collector.traced_runs
        self.assertEqual(len(traced), 1)
        self.assertNotIn(secret_observation, str(traced[0].outputs))
        self.assertEqual(traced[0].outputs, {"probe_status": "PASS"})
