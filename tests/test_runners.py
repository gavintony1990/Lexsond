from __future__ import annotations

import json
import unittest
from pathlib import Path

from lexsond.models import Dimension, RunStatus
from lexsond.runners import (
    AIPerfArtifactError,
    AIPerfThresholds,
    EvalScopeArtifactError,
    PromptfooArtifactError,
    RunnerArtifact,
    RunnerJob,
    RunnerOutcome,
    RunnerStatus,
    import_aiperf_summary,
    import_evalscope_report,
    import_promptfoo_artifact,
    vendor_verifier_policy,
)


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class RunnerContractTests(unittest.TestCase):
    def test_job_repr_hides_credential_handle(self) -> None:
        handle = "vault:relay/test-key"
        job = RunnerJob(
            runner_name="promptfoo",
            runner_version="0.121.19",
            endpoint_snapshot_id="endpoint-v1",
            model="relay-model",
            suite_uri="s3://probe/suite.json",
            suite_sha256="a" * 64,
            credential_handle=handle,
        )
        self.assertNotIn(handle, repr(job))

    def test_job_rejects_invalid_digest_and_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "suite_sha256"):
            RunnerJob(
                runner_name="promptfoo",
                runner_version="0.121.19",
                endpoint_snapshot_id="endpoint-v1",
                model="relay-model",
                suite_uri="s3://probe/suite.json",
                suite_sha256="not-a-digest",
                credential_handle="vault:key",
            )

    def test_outcome_requires_evidence_for_target_result(self) -> None:
        with self.assertRaisesRegex(ValueError, "require artifacts"):
            RunnerOutcome(
                job_id="job-id",
                runner_status=RunnerStatus.TARGET_FAILED,
                runner_version="0.121.19",
                result_schema_version="probe.ai/result/v1alpha1",
                exit_code=1,
            )

        outcome = RunnerOutcome(
            job_id="job-id",
            runner_status=RunnerStatus.TARGET_FAILED,
            runner_version="0.121.19",
            result_schema_version="probe.ai/result/v1alpha1",
            exit_code=1,
            artifacts=(
                RunnerArtifact(
                    uri="s3://probe/results/result.json",
                    sha256="b" * 64,
                    media_type="application/json",
                ),
            ),
        )
        self.assertEqual(outcome.runner_status, RunnerStatus.TARGET_FAILED)


class PromptfooAdapterTests(unittest.TestCase):
    def test_imports_v3_quality_results_without_raw_outputs(self) -> None:
        artifact = fixture("promptfoo-v3-pass.json")
        result = import_promptfoo_artifact(
            artifact,
            suite_name="business-regression",
            suite_version="2026.07.19",
        )
        self.assertEqual(result.status, RunStatus.PASS)
        self.assertEqual(len(result.case_results), 2)
        self.assertEqual(result.dimension_scores[0].dimension, Dimension.QUALITY)
        self.assertEqual(result.dimension_scores[0].score, 90)
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("sensitive raw output", serialized)
        self.assertNotIn("another raw output", serialized)
        self.assertEqual(result.case_results[0].provider_reported_total_tokens, 17)

    def test_distinguishes_assertion_and_case_execution_failures(self) -> None:
        result = import_promptfoo_artifact(
            fixture("promptfoo-v3-fail.json"),
            suite_name="business-regression",
            suite_version="2026.07.19",
        )
        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertEqual(
            result.case_results[0].reason_codes, ["PROMPTFOO_ASSERTION_FAILED"]
        )
        self.assertEqual(
            result.case_results[1].reason_codes,
            ["PROMPTFOO_CASE_EXECUTION_ERROR"],
        )

    def test_rejects_unknown_version_and_inconsistent_stats(self) -> None:
        artifact = fixture("promptfoo-v3-pass.json")
        artifact["results"]["version"] = 4
        with self.assertRaisesRegex(PromptfooArtifactError, "version 3"):
            import_promptfoo_artifact(artifact, suite_name="x", suite_version="1")

        artifact = fixture("promptfoo-v3-pass.json")
        artifact["results"]["stats"]["successes"] = 99
        with self.assertRaisesRegex(PromptfooArtifactError, "row count"):
            import_promptfoo_artifact(artifact, suite_name="x", suite_version="1")


class AIPerfAdapterTests(unittest.TestCase):
    def test_imports_versioned_summary_and_applies_slo(self) -> None:
        result = import_aiperf_summary(
            fixture("aiperf-summary-v1.3.json"),
            suite_name="controlled-load",
            suite_version="2026.07.19",
            thresholds=AIPerfThresholds(
                max_p95_ttft_ms=1000,
                max_p95_request_latency_ms=4000,
                min_output_token_throughput=100,
            ),
        )
        self.assertEqual(result.status, RunStatus.PASS)
        performance = result.dimension_scores[0]
        self.assertEqual(performance.dimension, Dimension.PERFORMANCE)
        self.assertEqual(performance.sample_count, 20)
        self.assertEqual(performance.metrics["p95_ttft_ms"], 700)
        self.assertEqual(result.finished_at, "2026-07-19T02:01:00+00:00")

    def test_fails_slo_and_cancelled_runs(self) -> None:
        artifact = fixture("aiperf-summary-v1.3.json")
        artifact["was_cancelled"] = True
        artifact["error_summary"] = [{"type": "RateLimitError", "count": 2}]
        result = import_aiperf_summary(
            artifact,
            suite_name="controlled-load",
            suite_version="2026.07.19",
            thresholds=AIPerfThresholds(
                max_p95_ttft_ms=500,
                min_output_token_throughput=200,
            ),
        )
        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertIn("TTFT_P95_ABOVE_THRESHOLD", result.reason_codes)
        self.assertIn("OUTPUT_TOKEN_THROUGHPUT_BELOW_THRESHOLD", result.reason_codes)
        self.assertIn("AIPERF_RUN_CANCELLED", result.reason_codes)
        self.assertIn("AIPERF_REQUEST_ERRORS_REPORTED", result.reason_codes)

    def test_rejects_unknown_major_schema_and_wrong_units(self) -> None:
        artifact = fixture("aiperf-summary-v1.3.json")
        artifact["schema_version"] = "2.0"
        with self.assertRaisesRegex(AIPerfArtifactError, "major version 1"):
            import_aiperf_summary(
                artifact,
                suite_name="x",
                suite_version="1",
                thresholds=AIPerfThresholds(max_p95_ttft_ms=1000),
            )

        artifact = fixture("aiperf-summary-v1.3.json")
        artifact["time_to_first_token"]["unit"] = "seconds"
        with self.assertRaisesRegex(AIPerfArtifactError, "units are authoritative"):
            import_aiperf_summary(
                artifact,
                suite_name="x",
                suite_version="1",
                thresholds=AIPerfThresholds(max_p95_ttft_ms=1000),
            )


class EvalScopeAdapterTests(unittest.TestCase):
    def test_imports_pinned_kimi_vendor_verifier_report(self) -> None:
        result = import_evalscope_report(
            fixture("evalscope-v1.9.0-kimi-report.json"),
            evalscope_version="1.9.0",
            suite_name="vendor-fidelity",
            suite_version="2026.07.19",
            policy=vendor_verifier_policy("kimi_verifier"),
        )
        self.assertEqual(result.status, RunStatus.PASS)
        self.assertEqual(len(result.case_results), 3)
        self.assertEqual(result.dimension_scores[0].dimension, Dimension.QUALITY)
        self.assertEqual(result.dimension_scores[0].score, 100)
        self.assertEqual(result.dimension_scores[0].sample_count, 4)
        self.assertEqual(
            result.dimension_scores[0].metrics["evalscope_version"], "1.9.0"
        )

    def test_fails_vendor_policy_without_treating_errors_as_rejections(self) -> None:
        artifact = fixture("evalscope-v1.9.0-kimi-report.json")
        metric = artifact["metrics"][2]
        metric["score"] = 0.25
        metric["macro_score"] = 0.25
        metric["categories"][0]["score"] = 0.25
        metric["categories"][0]["macro_score"] = 0.25
        metric["categories"][0]["subsets"][0]["score"] = 0.25
        result = import_evalscope_report(
            artifact,
            evalscope_version="1.9.0",
            suite_name="vendor-fidelity",
            suite_version="2026.07.19",
            policy=vendor_verifier_policy("kimi_verifier"),
        )
        self.assertEqual(result.status, RunStatus.FAIL)
        self.assertIn("EVALSCOPE_POLICY_FAILED", result.reason_codes)
        self.assertEqual(
            result.case_results[2].reason_codes,
            ["EVALSCOPE_INFERENCE_ERROR_RATE_ABOVE_MAXIMUM"],
        )

    def test_rejects_unpinned_version_and_inconsistent_aggregate(self) -> None:
        artifact = fixture("evalscope-v1.9.0-kimi-report.json")
        with self.assertRaisesRegex(EvalScopeArtifactError, "only EvalScope 1.9.0"):
            import_evalscope_report(
                artifact,
                evalscope_version="1.10.0",
                suite_name="x",
                suite_version="1",
                policy=vendor_verifier_policy("kimi_verifier"),
            )

        artifact["metrics"][0]["num"] = 99
        with self.assertRaisesRegex(EvalScopeArtifactError, "inconsistent"):
            import_evalscope_report(
                artifact,
                evalscope_version="1.9.0",
                suite_name="x",
                suite_version="1",
                policy=vendor_verifier_policy("kimi_verifier"),
            )

    def test_golden_review_fixture_contains_no_raw_messages_or_predictions(self) -> None:
        line = (FIXTURES / "evalscope-v1.9.0-kimi-review.sanitized.jsonl").read_text(
            encoding="utf-8"
        )
        review = json.loads(line)
        self.assertEqual(review["messages"], [])
        self.assertIsNone(review["sample_score"]["score"]["prediction"])

if __name__ == "__main__":
    unittest.main()
