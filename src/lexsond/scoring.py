from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import quantiles

from .models import Dimension, DimensionScore, NormalizedRunResult, RequestMeasurement, RunStatus


@dataclass(frozen=True, slots=True)
class ScoringPolicy:
    expected_http_status: int = 200
    minimum_success_rate: float = 1.0
    expected_text: str | None = None
    require_nonempty_output: bool = True
    require_sse_done: bool = False
    require_finish_reason: bool = False
    reject_pseudo_stream: bool = False
    max_p95_ttft_ms: float | None = None
    max_p95_e2e_ms: float | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.minimum_success_rate <= 1:
            raise ValueError("minimum_success_rate must be between 0 and 1")
        for value in (self.max_p95_ttft_ms, self.max_p95_e2e_ms):
            if value is not None and value <= 0:
                raise ValueError("latency thresholds must be positive")


def score_run(run: NormalizedRunResult, policy: ScoringPolicy) -> NormalizedRunResult:
    """Score normalized measurements without trusting provider identity metadata."""

    dimensions = [
        _score_availability(run.measurements, policy),
        _score_protocol(run.measurements, policy),
        _score_performance(run.measurements, policy),
        _score_quality(run.measurements, policy),
    ]
    run.dimension_scores = dimensions

    statuses = {dimension.status for dimension in dimensions}
    reason_codes = _deduplicate([
        reason
        for dimension in dimensions
        if dimension.status in {RunStatus.FAIL, RunStatus.WARN}
        for reason in dimension.reason_codes
    ])
    if RunStatus.FAIL in statuses:
        status = RunStatus.FAIL
    elif RunStatus.WARN in statuses:
        status = RunStatus.WARN
    elif RunStatus.PASS in statuses:
        status = RunStatus.PASS
    else:
        status = RunStatus.UNKNOWN
    run.finish(status, *reason_codes)
    return run


def _score_availability(
    measurements: list[RequestMeasurement], policy: ScoringPolicy
) -> DimensionScore:
    attempted = len(measurements)
    if attempted == 0:
        return DimensionScore(
            dimension=Dimension.AVAILABILITY,
            score=None,
            status=RunStatus.UNKNOWN,
            sample_count=0,
            reason_codes=["NO_REQUESTS_ATTEMPTED"],
        )
    succeeded = sum(
        measurement.error_class is None
        and measurement.status_code == policy.expected_http_status
        for measurement in measurements
    )
    rate = succeeded / attempted
    status = RunStatus.PASS if rate >= policy.minimum_success_rate else RunStatus.FAIL
    reasons = [] if status is RunStatus.PASS else ["SUCCESS_RATE_BELOW_THRESHOLD"]
    lower, upper = _wilson_interval(succeeded, attempted)
    return DimensionScore(
        dimension=Dimension.AVAILABILITY,
        score=round(rate * 100, 3),
        status=status,
        sample_count=attempted,
        reason_codes=reasons,
        metrics={
            "attempted_requests": attempted,
            "successful_requests": succeeded,
            "success_rate": round(rate, 6),
            "minimum_success_rate": policy.minimum_success_rate,
        },
        confidence_interval={"lower": round(lower, 6), "upper": round(upper, 6)},
    )


def _score_protocol(
    measurements: list[RequestMeasurement], policy: ScoringPolicy
) -> DimensionScore:
    checks: list[bool] = []
    reasons: list[str] = []
    successful = _successful(measurements, policy.expected_http_status)
    if not successful:
        return DimensionScore(
            dimension=Dimension.PROTOCOL,
            score=None,
            status=RunStatus.UNKNOWN,
            sample_count=0,
            reason_codes=["NO_SUCCESSFUL_RESPONSES"],
        )

    for measurement in successful:
        if policy.require_sse_done and measurement.streaming:
            passed = measurement.evidence.get("sse_done_received") is True
            checks.append(passed)
            if not passed:
                reasons.append("SSE_DONE_MISSING")
        if policy.require_finish_reason:
            passed = bool(measurement.finish_reason)
            checks.append(passed)
            if not passed:
                reasons.append("FINISH_REASON_MISSING")
        if policy.reject_pseudo_stream and measurement.streaming:
            passed = measurement.evidence.get("pseudo_stream_suspected") is not True
            checks.append(passed)
            if not passed:
                reasons.append("PSEUDO_STREAM_SUSPECTED")

    if not checks:
        return DimensionScore(
            dimension=Dimension.PROTOCOL,
            score=None,
            status=RunStatus.UNKNOWN,
            sample_count=len(successful),
            reason_codes=["NO_PROTOCOL_ASSERTIONS"],
        )
    passed_count = sum(checks)
    score = passed_count / len(checks) * 100
    return DimensionScore(
        dimension=Dimension.PROTOCOL,
        score=round(score, 3),
        status=RunStatus.PASS if passed_count == len(checks) else RunStatus.FAIL,
        sample_count=len(successful),
        reason_codes=_deduplicate(reasons),
        metrics={"checks": len(checks), "passed_checks": passed_count},
    )


def _score_performance(
    measurements: list[RequestMeasurement], policy: ScoringPolicy
) -> DimensionScore:
    successful = _successful(measurements, policy.expected_http_status)
    limits = {
        "p95_ttft_ms": (policy.max_p95_ttft_ms, "TTFT_P95_ABOVE_THRESHOLD"),
        "p95_e2e_ms": (policy.max_p95_e2e_ms, "E2E_P95_ABOVE_THRESHOLD"),
    }
    metric_sources = {
        "p95_ttft_ms": [m.ttft_ms for m in successful if m.ttft_ms is not None],
        "p95_e2e_ms": [m.e2e_ms for m in successful if m.e2e_ms is not None],
    }
    configured = {name: item for name, item in limits.items() if item[0] is not None}
    if not configured:
        return DimensionScore(
            dimension=Dimension.PERFORMANCE,
            score=None,
            status=RunStatus.UNKNOWN,
            sample_count=0,
            reason_codes=["NO_PERFORMANCE_ASSERTIONS"],
        )

    scores: list[float] = []
    metrics: dict[str, float] = {}
    reasons: list[str] = []
    for name, (limit, reason) in configured.items():
        values = metric_sources[name]
        if not values:
            scores.append(0)
            reasons.append(f"{name.upper()}_MISSING")
            continue
        observed = _percentile95(values)
        metrics[name] = round(observed, 3)
        metrics[f"{name}_limit"] = float(limit)
        scores.append(min(100.0, float(limit) / max(observed, 0.001) * 100))
        if observed > float(limit):
            reasons.append(reason)

    status = RunStatus.PASS if not reasons else RunStatus.FAIL
    return DimensionScore(
        dimension=Dimension.PERFORMANCE,
        score=round(sum(scores) / len(scores), 3),
        status=status,
        sample_count=max((len(metric_sources[name]) for name in configured), default=0),
        reason_codes=reasons,
        metrics=metrics,
    )


def _score_quality(
    measurements: list[RequestMeasurement], policy: ScoringPolicy
) -> DimensionScore:
    if policy.expected_text is None and not policy.require_nonempty_output:
        return DimensionScore(
            dimension=Dimension.QUALITY,
            score=None,
            status=RunStatus.UNKNOWN,
            sample_count=0,
            reason_codes=["NO_QUALITY_ASSERTIONS"],
        )
    successful = _successful(measurements, policy.expected_http_status)
    if not successful:
        return DimensionScore(
            dimension=Dimension.QUALITY,
            score=None,
            status=RunStatus.UNKNOWN,
            sample_count=0,
            reason_codes=["NO_SUCCESSFUL_RESPONSES"],
        )

    checks: list[bool] = []
    reasons: list[str] = []
    for measurement in successful:
        if policy.require_nonempty_output:
            passed = bool(measurement.output_text.strip())
            checks.append(passed)
            if not passed:
                reasons.append("OUTPUT_EMPTY")
        if policy.expected_text is not None:
            passed = measurement.output_text.strip() == policy.expected_text.strip()
            checks.append(passed)
            if not passed:
                reasons.append("EXACT_TEXT_MISMATCH")
    passed_count = sum(checks)
    return DimensionScore(
        dimension=Dimension.QUALITY,
        score=round(passed_count / len(checks) * 100, 3),
        status=RunStatus.PASS if passed_count == len(checks) else RunStatus.FAIL,
        sample_count=len(successful),
        reason_codes=_deduplicate(reasons),
        metrics={"checks": len(checks), "passed_checks": passed_count},
    )


def _successful(
    measurements: list[RequestMeasurement], expected_status: int
) -> list[RequestMeasurement]:
    return [
        measurement
        for measurement in measurements
        if measurement.error_class is None and measurement.status_code == expected_status
    ]


def _percentile95(values: list[float]) -> float:
    if len(values) == 1:
        return values[0]
    return quantiles(values, n=100, method="inclusive")[94]


def _wilson_interval(successes: int, trials: int, z: float = 1.959964) -> tuple[float, float]:
    if trials == 0:
        return 0.0, 1.0
    rate = successes / trials
    denominator = 1 + z * z / trials
    center = (rate + z * z / (2 * trials)) / denominator
    margin = (
        z
        * math.sqrt(rate * (1 - rate) / trials + z * z / (4 * trials * trials))
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
