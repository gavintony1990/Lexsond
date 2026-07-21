from __future__ import annotations

import unittest

from lexsond.models import Dimension, NormalizedRunResult, RequestMeasurement, RunStatus
from lexsond.scoring import ScoringPolicy, score_run


class ScoringTests(unittest.TestCase):
    def test_scores_dimensions_and_confidence(self) -> None:
        measurements = [
            RequestMeasurement(
                status_code=200,
                streaming=True,
                output_text="PROBE_OK",
                finish_reason="stop",
                ttft_ms=100 + index,
                e2e_ms=300 + index,
                evidence={"sse_done_received": True, "pseudo_stream_suspected": False},
            )
            for index in range(10)
        ]
        result = score_run(
            NormalizedRunResult(measurements=measurements),
            ScoringPolicy(
                expected_text="PROBE_OK",
                require_sse_done=True,
                require_finish_reason=True,
                reject_pseudo_stream=True,
                max_p95_ttft_ms=500,
                max_p95_e2e_ms=1000,
            ),
        )

        self.assertEqual(result.status, RunStatus.PASS)
        scores = {score.dimension: score for score in result.dimension_scores}
        self.assertEqual(set(scores), set(Dimension))
        self.assertEqual(scores[Dimension.AVAILABILITY].score, 100)
        self.assertIsNotNone(scores[Dimension.AVAILABILITY].confidence_interval)
        self.assertEqual(scores[Dimension.PROTOCOL].status, RunStatus.PASS)
        self.assertEqual(scores[Dimension.PERFORMANCE].status, RunStatus.PASS)
        self.assertEqual(scores[Dimension.QUALITY].status, RunStatus.PASS)

    def test_exact_text_mismatch_is_quality_failure(self) -> None:
        measurement = RequestMeasurement(
            status_code=200,
            output_text="not expected",
            finish_reason="stop",
        )
        result = score_run(
            NormalizedRunResult(measurements=[measurement]),
            ScoringPolicy(expected_text="PROBE_OK"),
        )
        quality = next(
            score for score in result.dimension_scores if score.dimension is Dimension.QUALITY
        )
        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertEqual(quality.reason_codes, ["EXACT_TEXT_MISMATCH"])

    def test_whitespace_only_output_fails_nonempty_quality_assertion(self) -> None:
        measurement = RequestMeasurement(
            status_code=200,
            output_text=" \n\t ",
            finish_reason="stop",
        )
        result = score_run(
            NormalizedRunResult(measurements=[measurement]),
            ScoringPolicy(require_nonempty_output=True),
        )
        quality = next(
            score for score in result.dimension_scores if score.dimension is Dimension.QUALITY
        )

        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertEqual(quality.reason_codes, ["OUTPUT_EMPTY"])

    def test_missing_required_latency_is_not_silently_accepted(self) -> None:
        measurement = RequestMeasurement(status_code=200, output_text="ok")
        result = score_run(
            NormalizedRunResult(measurements=[measurement]),
            ScoringPolicy(max_p95_ttft_ms=1000),
        )
        performance = next(
            score for score in result.dimension_scores if score.dimension is Dimension.PERFORMANCE
        )
        self.assertEqual(performance.status, RunStatus.FAIL)
        self.assertIn("P95_TTFT_MS_MISSING", performance.reason_codes)


if __name__ == "__main__":
    unittest.main()
