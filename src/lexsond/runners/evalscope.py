from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..models import (
    Dimension,
    DimensionScore,
    NormalizedRunResult,
    ProbeCaseResult,
    RunStatus,
)


SUPPORTED_EVALSCOPE_VERSION = "1.9.0"
_AGGREGATE_TOLERANCE = 0.0002


class EvalScopeArtifactError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EvalScopeMetricRule:
    metric_name: str
    minimum: float | None = None
    maximum: float | None = None

    def __post_init__(self) -> None:
        if not self.metric_name:
            raise ValueError("EvalScope metric_name must not be empty")
        if (self.minimum is None) == (self.maximum is None):
            raise ValueError("EvalScope metric rule requires exactly one bound")
        bound = self.minimum if self.minimum is not None else self.maximum
        if (
            isinstance(bound, bool)
            or not isinstance(bound, (int, float))
            or not math.isfinite(bound)
            or bound < 0
        ):
            raise ValueError("EvalScope metric bound must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class EvalScopePolicy:
    dataset_name: str
    rules: tuple[EvalScopeMetricRule, ...]

    def __post_init__(self) -> None:
        if not self.dataset_name:
            raise ValueError("EvalScope policy dataset_name must not be empty")
        if not self.rules:
            raise ValueError("EvalScope policy must contain at least one metric rule")
        names = [rule.metric_name for rule in self.rules]
        if len(names) != len(set(names)):
            raise ValueError("EvalScope policy metric rules must be unique")


def vendor_verifier_policy(dataset_name: str) -> EvalScopePolicy:
    """Return the strict EvalScope 1.9.0 policy for a Vendor Verifier dataset."""

    if dataset_name == "kimi_verifier":
        rules = (
            EvalScopeMetricRule("param_immutable_reject_rate", minimum=1.0),
            EvalScopeMetricRule("param_default_accept_rate", minimum=1.0),
            EvalScopeMetricRule("inference_error_rate", maximum=0.0),
        )
    elif dataset_name == "k2_verifier":
        rules = (
            EvalScopeMetricRule("trigger_similarity", minimum=1.0),
            EvalScopeMetricRule("schema_accuracy", minimum=1.0),
        )
    elif dataset_name == "minimax_verifier":
        rules = tuple(
            EvalScopeMetricRule(name, minimum=1.0)
            for name in (
                "tool_calls_match_rate",
                "schema_accuracy",
                "error_only_reasoning_rate",
                "language_following_success_rate",
                "repeat_ngram_pass_rate",
                "scenario_check_pass_rate",
            )
        )
    else:
        raise ValueError(f"unsupported EvalScope Vendor Verifier: {dataset_name}")
    return EvalScopePolicy(dataset_name=dataset_name, rules=rules)


def import_evalscope_report(
    artifact: Mapping[str, Any],
    *,
    evalscope_version: str,
    suite_name: str,
    suite_version: str,
    policy: EvalScopePolicy,
) -> NormalizedRunResult:
    """Normalize an EvalScope 1.9.0 report without retaining review content.

    EvalScope reports do not embed their producer version, so the isolated
    runner must supply its pinned version as a separate, trusted input.
    """

    if evalscope_version != SUPPORTED_EVALSCOPE_VERSION:
        raise EvalScopeArtifactError(
            f"only EvalScope {SUPPORTED_EVALSCOPE_VERSION} reports are supported"
        )
    if not isinstance(artifact, Mapping):
        raise EvalScopeArtifactError("EvalScope report must be an object")

    dataset_name = _nonempty_string(artifact.get("dataset_name"), "dataset_name")
    if dataset_name != policy.dataset_name:
        raise EvalScopeArtifactError(
            f"report dataset {dataset_name!r} does not match policy {policy.dataset_name!r}"
        )
    model_name = _nonempty_string(artifact.get("model_name"), "model_name")
    report_name = _nonempty_string(artifact.get("name"), "name")
    metrics = _parse_and_validate_report(artifact)

    missing = [rule.metric_name for rule in policy.rules if rule.metric_name not in metrics]
    if missing:
        raise EvalScopeArtifactError(
            "report is missing policy metrics: " + ", ".join(sorted(missing))
        )

    run = NormalizedRunResult(suite_name=suite_name, suite_version=suite_version)
    component_scores: list[float] = []
    observed_metrics: dict[str, float] = {}
    failures: list[ProbeCaseResult] = []
    unknown: list[ProbeCaseResult] = []

    for rule in policy.rules:
        observed, sample_count = metrics[rule.metric_name]
        observed_metrics[rule.metric_name] = observed
        case = _evaluate_rule(
            rule,
            observed=observed,
            sample_count=sample_count,
            report_name=report_name,
            dataset_name=dataset_name,
            model_name=model_name,
            evalscope_version=evalscope_version,
        )
        run.case_results.append(case)
        if case.score is not None:
            component_scores.append(case.score)
        if case.status is RunStatus.FAIL:
            failures.append(case)
        elif case.status is RunStatus.UNKNOWN:
            unknown.append(case)

    reason_codes: list[str] = []
    if failures:
        status = RunStatus.FAIL
        reason_codes.append("EVALSCOPE_POLICY_FAILED")
    elif len(unknown) == len(run.case_results):
        status = RunStatus.UNKNOWN
        reason_codes.append("EVALSCOPE_NO_EVALUATED_SAMPLES")
    elif unknown:
        status = RunStatus.WARN
        reason_codes.append("EVALSCOPE_METRICS_UNKNOWN")
    else:
        status = RunStatus.PASS

    score = sum(component_scores) / len(component_scores) if component_scores else None
    ignored_metrics = sorted(set(metrics) - {rule.metric_name for rule in policy.rules})
    run.dimension_scores = [
        DimensionScore(
            dimension=Dimension.QUALITY,
            score=round(score, 3) if score is not None else None,
            status=status,
            sample_count=max((metrics[rule.metric_name][1] for rule in policy.rules), default=0),
            reason_codes=reason_codes.copy(),
            metrics={
                "source_format": "evalscope-report-v1",
                "evalscope_version": evalscope_version,
                "report_name": report_name,
                "dataset_name": dataset_name,
                "model_name": model_name,
                "observed": observed_metrics,
                "ignored_report_metrics": ignored_metrics,
            },
        )
    ]
    run.finish(status, *reason_codes)
    return run


def _parse_and_validate_report(
    artifact: Mapping[str, Any],
) -> dict[str, tuple[float, int]]:
    raw_metrics = artifact.get("metrics")
    if not isinstance(raw_metrics, list) or not raw_metrics:
        raise EvalScopeArtifactError("metrics must be a non-empty array")

    parsed: dict[str, tuple[float, int]] = {}
    for metric_index, raw_metric in enumerate(raw_metrics):
        metric = _mapping(raw_metric, f"metrics[{metric_index}]")
        name = _nonempty_string(metric.get("name"), f"metrics[{metric_index}].name")
        if name in parsed:
            raise EvalScopeArtifactError(f"duplicate metric name: {name}")
        metric_score = _nonnegative_number(
            metric.get("score"), f"metrics[{metric_index}].score"
        )
        metric_num = _nonnegative_int(metric.get("num"), f"metrics[{metric_index}].num")
        categories = metric.get("categories")
        if not isinstance(categories, list):
            raise EvalScopeArtifactError(f"metrics[{metric_index}].categories must be an array")

        real_categories: list[tuple[float, int]] = []
        for category_index, raw_category in enumerate(categories):
            field = f"metrics[{metric_index}].categories[{category_index}]"
            category = _mapping(raw_category, field)
            names = category.get("name")
            if not isinstance(names, list) or any(
                not isinstance(value, str) for value in names
            ):
                raise EvalScopeArtifactError(f"{field}.name must be an array of strings")
            category_score = _nonnegative_number(category.get("score"), f"{field}.score")
            category_num = _nonnegative_int(category.get("num"), f"{field}.num")
            subsets = category.get("subsets")
            if not isinstance(subsets, list):
                raise EvalScopeArtifactError(f"{field}.subsets must be an array")

            real_subsets: list[tuple[float, int]] = []
            for subset_index, raw_subset in enumerate(subsets):
                subset_field = f"{field}.subsets[{subset_index}]"
                subset = _mapping(raw_subset, subset_field)
                _nonempty_string(subset.get("name"), f"{subset_field}.name")
                subset_score = _nonnegative_number(
                    subset.get("score"), f"{subset_field}.score"
                )
                subset_num = _nonnegative_int(subset.get("num"), f"{subset_field}.num")
                aggregate = subset.get("is_aggregate")
                if not isinstance(aggregate, bool):
                    raise EvalScopeArtifactError(f"{subset_field}.is_aggregate must be boolean")
                if not aggregate:
                    real_subsets.append((subset_score, subset_num))

            expected_num, expected_score = _micro_mean(real_subsets)
            _assert_aggregate(field, category_num, category_score, expected_num, expected_score)
            if category_num > 0:
                real_categories.append((category_score, category_num))

        expected_num, expected_score = _micro_mean(real_categories)
        _assert_aggregate(
            f"metrics[{metric_index}]",
            metric_num,
            metric_score,
            expected_num,
            expected_score,
        )
        parsed[name] = (metric_score, metric_num)

    report_score = _nonnegative_number(artifact.get("score"), "score")
    first_metric_score = next(iter(parsed.values()))[0]
    if not math.isclose(
        report_score, first_metric_score, rel_tol=0.0, abs_tol=_AGGREGATE_TOLERANCE
    ):
        raise EvalScopeArtifactError("report score does not match the first metric")
    report_num = _nonnegative_int(artifact.get("num"), "num")
    first_metric_num = next(iter(parsed.values()))[1]
    if report_num != first_metric_num:
        raise EvalScopeArtifactError("report num does not match the first metric")
    return parsed


def _evaluate_rule(
    rule: EvalScopeMetricRule,
    *,
    observed: float,
    sample_count: int,
    report_name: str,
    dataset_name: str,
    model_name: str,
    evalscope_version: str,
) -> ProbeCaseResult:
    evidence: dict[str, Any] = {
        "source": "evalscope",
        "evalscope_version": evalscope_version,
        "report_name": report_name,
        "dataset_name": dataset_name,
        "model_name": model_name,
        "metric_name": rule.metric_name,
        "observed": observed,
        "sample_count": sample_count,
    }
    if sample_count == 0:
        return ProbeCaseResult(
            case_id=f"evalscope:{dataset_name}:{rule.metric_name}",
            status=RunStatus.UNKNOWN,
            score=None,
            reason_codes=["EVALSCOPE_METRIC_NO_SAMPLES"],
            evidence=evidence,
        )

    if rule.minimum is not None:
        threshold = float(rule.minimum)
        passed = observed >= threshold
        component_score = min(100.0, observed / threshold * 100) if threshold else 100.0
        reason = _metric_reason(rule.metric_name, "BELOW_MINIMUM")
        evidence.update({"direction": "minimum", "threshold": threshold})
    else:
        threshold = float(rule.maximum)
        passed = observed <= threshold
        if threshold == 0:
            component_score = 100.0 if observed == 0 else 0.0
        else:
            component_score = min(100.0, threshold / max(observed, 1e-12) * 100)
        reason = _metric_reason(rule.metric_name, "ABOVE_MAXIMUM")
        evidence.update({"direction": "maximum", "threshold": threshold})

    return ProbeCaseResult(
        case_id=f"evalscope:{dataset_name}:{rule.metric_name}",
        status=RunStatus.PASS if passed else RunStatus.FAIL,
        score=round(component_score, 3),
        reason_codes=[] if passed else [reason],
        evidence=evidence,
    )


def _micro_mean(items: Sequence[tuple[float, int]]) -> tuple[int, float]:
    total = sum(num for _, num in items)
    if total == 0:
        return 0, 0.0
    return total, sum(score * num for score, num in items) / total


def _assert_aggregate(
    field: str,
    actual_num: int,
    actual_score: float,
    expected_num: int,
    expected_score: float,
) -> None:
    if actual_num != expected_num:
        raise EvalScopeArtifactError(f"{field}.num is inconsistent with child rows")
    if not math.isclose(
        actual_score, expected_score, rel_tol=0.0, abs_tol=_AGGREGATE_TOLERANCE
    ):
        raise EvalScopeArtifactError(f"{field}.score is inconsistent with child rows")


def _metric_reason(metric_name: str, suffix: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", "_", metric_name.upper()).strip("_")
    return f"EVALSCOPE_{normalized}_{suffix}"


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvalScopeArtifactError(f"{field} must be an object")
    return value


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvalScopeArtifactError(f"{field} must be a non-empty string")
    return value


def _nonnegative_number(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise EvalScopeArtifactError(f"{field} must be a finite non-negative number")
    return float(value)


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvalScopeArtifactError(f"{field} must be a non-negative integer")
    return value
