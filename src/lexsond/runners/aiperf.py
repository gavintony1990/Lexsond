from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from ..models import Dimension, DimensionScore, NormalizedRunResult, RunStatus


class AIPerfArtifactError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AIPerfThresholds:
    max_p95_ttft_ms: float | None = None
    max_p95_request_latency_ms: float | None = None
    min_output_token_throughput: float | None = None

    def __post_init__(self) -> None:
        values = (
            self.max_p95_ttft_ms,
            self.max_p95_request_latency_ms,
            self.min_output_token_throughput,
        )
        if any(value is not None and value <= 0 for value in values):
            raise ValueError("AIPerf thresholds must be positive")


def import_aiperf_summary(
    artifact: Mapping[str, Any],
    *,
    suite_name: str,
    suite_version: str,
    thresholds: AIPerfThresholds,
) -> NormalizedRunResult:
    """Normalize AIPerf profile_export_aiperf.json schema 1.x."""

    if not isinstance(artifact, Mapping):
        raise AIPerfArtifactError("AIPerf artifact must be an object")
    schema_version = artifact.get("schema_version")
    if not isinstance(schema_version, str) or schema_version.split(".", 1)[0] != "1":
        raise AIPerfArtifactError("only AIPerf summary schema major version 1 is supported")
    aiperf_version = artifact.get("aiperf_version")
    if not isinstance(aiperf_version, str) or not aiperf_version:
        raise AIPerfArtifactError("aiperf_version must be present")
    was_cancelled = artifact.get("was_cancelled", False)
    if not isinstance(was_cancelled, bool):
        raise AIPerfArtifactError("was_cancelled must be boolean")
    error_summary = artifact.get("error_summary", [])
    if not isinstance(error_summary, list):
        raise AIPerfArtifactError("error_summary must be an array")

    start_time = _timestamp(artifact.get("start_time"), "start_time")
    end_time = _timestamp(artifact.get("end_time"), "end_time")
    run = NormalizedRunResult(
        suite_name=suite_name,
        suite_version=suite_version,
        started_at=start_time,
    )
    metrics: dict[str, Any] = {
        "source_format": f"aiperf-summary-{schema_version}",
        "aiperf_version": aiperf_version,
        "benchmark_id": artifact.get("benchmark_id"),
        "was_cancelled": was_cancelled,
        "error_group_count": len(error_summary),
    }
    reasons: list[str] = []
    component_scores: list[float] = []
    sample_counts: list[int] = []

    _evaluate_upper_latency(
        artifact,
        metric_name="time_to_first_token",
        output_name="p95_ttft_ms",
        threshold=thresholds.max_p95_ttft_ms,
        reason="TTFT_P95_ABOVE_THRESHOLD",
        metrics=metrics,
        reasons=reasons,
        component_scores=component_scores,
        sample_counts=sample_counts,
    )
    _evaluate_upper_latency(
        artifact,
        metric_name="request_latency",
        output_name="p95_request_latency_ms",
        threshold=thresholds.max_p95_request_latency_ms,
        reason="REQUEST_LATENCY_P95_ABOVE_THRESHOLD",
        metrics=metrics,
        reasons=reasons,
        component_scores=component_scores,
        sample_counts=sample_counts,
    )
    _evaluate_lower_throughput(
        artifact,
        metric_name="output_token_throughput",
        output_name="output_token_throughput",
        threshold=thresholds.min_output_token_throughput,
        reason="OUTPUT_TOKEN_THROUGHPUT_BELOW_THRESHOLD",
        metrics=metrics,
        reasons=reasons,
        component_scores=component_scores,
    )
    if was_cancelled:
        reasons.append("AIPERF_RUN_CANCELLED")
    if error_summary:
        reasons.append("AIPERF_REQUEST_ERRORS_REPORTED")

    if not component_scores:
        status = RunStatus.FAIL if was_cancelled else RunStatus.UNKNOWN
        score = None
        if not reasons:
            reasons.append("NO_AIPERF_THRESHOLDS_CONFIGURED")
    elif reasons:
        status = RunStatus.FAIL
        score = sum(component_scores) / len(component_scores)
    else:
        status = RunStatus.PASS
        score = sum(component_scores) / len(component_scores)

    dimension = DimensionScore(
        dimension=Dimension.PERFORMANCE,
        score=round(score, 3) if score is not None else None,
        status=status,
        sample_count=max(sample_counts, default=0),
        reason_codes=reasons.copy(),
        metrics=metrics,
    )
    run.dimension_scores = [dimension]
    run.finish(status, *reasons)
    run.finished_at = end_time
    return run


def _evaluate_upper_latency(
    artifact: Mapping[str, Any],
    *,
    metric_name: str,
    output_name: str,
    threshold: float | None,
    reason: str,
    metrics: dict[str, Any],
    reasons: list[str],
    component_scores: list[float],
    sample_counts: list[int],
) -> None:
    if threshold is None:
        return
    block = _metric_block(artifact, metric_name, expected_unit="ms")
    observed = _required_nonnegative_number(block.get("p95"), f"{metric_name}.p95")
    metrics[output_name] = observed
    metrics[f"{output_name}_limit"] = threshold
    component_scores.append(min(100.0, threshold / max(observed, 0.001) * 100))
    count = block.get("count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        sample_counts.append(count)
    if observed > threshold:
        reasons.append(reason)


def _evaluate_lower_throughput(
    artifact: Mapping[str, Any],
    *,
    metric_name: str,
    output_name: str,
    threshold: float | None,
    reason: str,
    metrics: dict[str, Any],
    reasons: list[str],
    component_scores: list[float],
) -> None:
    if threshold is None:
        return
    block = _metric_block(artifact, metric_name, expected_unit="tokens/sec")
    observed = _required_nonnegative_number(block.get("avg"), f"{metric_name}.avg")
    metrics[output_name] = observed
    metrics[f"{output_name}_minimum"] = threshold
    component_scores.append(min(100.0, observed / threshold * 100))
    if observed < threshold:
        reasons.append(reason)


def _metric_block(
    artifact: Mapping[str, Any], metric_name: str, *, expected_unit: str
) -> Mapping[str, Any]:
    block = artifact.get(metric_name)
    if not isinstance(block, Mapping):
        raise AIPerfArtifactError(f"required metric is missing: {metric_name}")
    if block.get("unit") != expected_unit:
        raise AIPerfArtifactError(
            f"{metric_name}.unit must be {expected_unit!r}; units are authoritative"
        )
    return block


def _required_nonnegative_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise AIPerfArtifactError(f"{field} must be a non-negative number")
    return float(value)


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise AIPerfArtifactError(f"{field} must be present")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AIPerfArtifactError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise AIPerfArtifactError(f"{field} must include a timezone")
    return parsed.isoformat()
