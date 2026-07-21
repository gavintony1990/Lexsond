from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path

from lexsond.models import Dimension, RunStatus
from lexsond.mock_relay import create_server
from lexsond.suite import (
    ProbeLayer,
    SuiteExecutionCancelled,
    SuiteExecutionError,
    SuiteValidationError,
    compile_suite,
    run_suite,
)


def suite_document() -> dict:
    path = Path(__file__).parents[1] / "suites" / "canary" / "openai-compatible.json"
    return json.loads(path.read_text(encoding="utf-8"))


class SuiteCompilationTests(unittest.TestCase):
    def test_compiles_bounded_canary(self) -> None:
        suite = compile_suite(suite_document())
        self.assertEqual(suite.layer, ProbeLayer.L2)
        self.assertEqual(suite.sampling.requests, 10)
        self.assertTrue(suite.scoring_policy.require_sse_done)

    def test_rejects_inline_secret(self) -> None:
        document = suite_document()
        document["spec"]["request"]["api_key"] = "should-never-be-here"
        with self.assertRaisesRegex(SuiteValidationError, "secret material"):
            compile_suite(document)

    def test_rejects_recognizable_credential_in_prompt_value(self) -> None:
        document = suite_document()
        document["spec"]["request"]["prompt"] = (
            "Never persist " + "sk-" + "x" * 32
        )

        with self.assertRaisesRegex(SuiteValidationError, "credential"):
            compile_suite(document)

    def test_secret_prefix_matching_remains_case_sensitive(self) -> None:
        document = suite_document()
        document["spec"]["request"]["prompt"] = "A harmless SK-ABCDEFGH label"

        compiled = compile_suite(document)

        self.assertEqual(compiled.request.prompt, "A harmless SK-ABCDEFGH label")

    def test_rejects_unbounded_or_incompatible_suite(self) -> None:
        document = suite_document()
        document["spec"]["request"]["stream"] = False
        with self.assertRaisesRegex(SuiteValidationError, "requires request.stream=true"):
            compile_suite(document)

    def test_all_model_call_suites_require_cost_budget(self) -> None:
        document = suite_document()
        document["spec"]["sampling"]["max_cost_usd"] = None
        with self.assertRaisesRegex(SuiteValidationError, "positive number"):
            compile_suite(document)

    def test_native_canary_concurrency_is_bounded(self) -> None:
        document = suite_document()
        document["spec"]["sampling"].update({"requests": 11, "concurrency": 11})
        with self.assertRaisesRegex(SuiteValidationError, "concurrency 10"):
            compile_suite(document)


class SuiteExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = create_server()
        cls.thread = __import__("threading").Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base_url = f"http://{host}:{port}/v1"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_suite_aggregates_and_scores_requests(self) -> None:
        document = suite_document()
        document["spec"]["sampling"].update({"warmup": 0, "requests": 3})
        suite = compile_suite(document)
        result = run_suite(
            suite,
            base_url=self.base_url,
            api_key="test-key",
            model="mock-model",
        )
        self.assertEqual(result.status, RunStatus.PASS)
        self.assertEqual(len(result.measurements), 3)
        self.assertEqual(
            {score.dimension for score in result.dimension_scores}, set(Dimension)
        )

    def test_suite_rejects_wrong_output(self) -> None:
        document = suite_document()
        document["spec"]["sampling"].update({"warmup": 0, "requests": 1})
        result = run_suite(
            compile_suite(document),
            base_url=self.base_url,
            api_key="test-key",
            model="mock-model",
            mock_mode="wrong_output",
        )
        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertIn("EXACT_TEXT_MISMATCH", result.reason_codes)

    def test_reasoning_stream_passes_protocol_without_weakening_pseudo_stream_gate(self) -> None:
        document = suite_document()
        document["spec"]["sampling"].update({"warmup": 0, "requests": 3})
        result = run_suite(
            compile_suite(document),
            base_url=self.base_url,
            api_key="test-key",
            model="mock-model",
            mock_mode="reasoning_stream",
        )

        self.assertEqual(result.status, RunStatus.PASS)
        protocol = next(
            score for score in result.dimension_scores if score.dimension is Dimension.PROTOCOL
        )
        self.assertEqual(protocol.score, 100)
        self.assertNotIn("PSEUDO_STREAM_SUSPECTED", result.reason_codes)
        self.assertTrue(
            all(
                measurement.evidence["reasoning_content_observed"]
                for measurement in result.measurements
            )
        )

    def test_single_full_answer_event_fails_pseudo_stream_gate(self) -> None:
        document = suite_document()
        document["spec"]["sampling"].update({"warmup": 0, "requests": 1})
        result = run_suite(
            compile_suite(document),
            base_url=self.base_url,
            api_key="test-key",
            model="mock-model",
            mock_mode="single_chunk_stream",
        )

        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertIn("PSEUDO_STREAM_SUSPECTED", result.reason_codes)

    def test_native_runner_refuses_l6_even_with_budget(self) -> None:
        document = suite_document()
        document["spec"]["layer"] = "L6"
        document["spec"]["sampling"].update({"warmup": 0, "requests": 1})
        with self.assertRaisesRegex(SuiteExecutionError, "specialized approved workflow"):
            run_suite(
                compile_suite(document),
                base_url=self.base_url,
                api_key="test-key",
                model="mock-model",
            )

    def test_pre_cancelled_suite_does_not_start_sampling(self) -> None:
        document = suite_document()
        document["spec"]["sampling"].update({"warmup": 0, "requests": 3})
        cancelled = threading.Event()
        cancelled.set()

        with self.assertRaises(SuiteExecutionCancelled):
            run_suite(
                compile_suite(document),
                base_url=self.base_url,
                api_key="test-key",
                model="mock-model",
                cancel_signal=cancelled,
            )


if __name__ == "__main__":
    unittest.main()
