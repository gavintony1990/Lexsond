from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Mapping

from ..models import (
    Dimension,
    DimensionScore,
    NormalizedRunResult,
    ProbeCaseResult,
    RunStatus,
)


class PromptfooArtifactError(ValueError):
    pass


def import_promptfoo_artifact(
    artifact: Mapping[str, Any],
    *,
    suite_name: str,
    suite_version: str,
) -> NormalizedRunResult:
    """Normalize Promptfoo JSON export v3 without retaining raw model outputs."""

    if not isinstance(artifact, Mapping):
        raise PromptfooArtifactError("Promptfoo artifact must be an object")
    results = _mapping(artifact.get("results"), "results")
    if results.get("version") != 3:
        raise PromptfooArtifactError("only Promptfoo JSON output version 3 is supported")
    rows = results.get("results")
    if rows is None:
        rows = results.get("outputs")
    if not isinstance(rows, list):
        raise PromptfooArtifactError("results.results must be an array")

    run = NormalizedRunResult(
        suite_name=suite_name,
        suite_version=suite_version,
        started_at=_timestamp_or_now(results.get("timestamp")),
    )
    for index, raw_row in enumerate(rows):
        run.case_results.append(_import_row(raw_row, index))

    _validate_stats_if_present(results.get("stats"), run.case_results)
    scores = [case.score for case in run.case_results if case.score is not None]
    failures = [case for case in run.case_results if case.status is RunStatus.FAIL]
    unknown = [case for case in run.case_results if case.status is RunStatus.UNKNOWN]
    if not run.case_results:
        status = RunStatus.UNKNOWN
        reason_codes = ["PROMPTFOO_NO_RESULTS"]
        score = None
    elif failures:
        status = RunStatus.FAIL
        reason_codes = ["PROMPTFOO_CASE_FAILURES"]
        score = sum(scores) / len(scores) if scores else 0.0
    elif unknown:
        status = RunStatus.WARN
        reason_codes = ["PROMPTFOO_CASES_UNKNOWN"]
        score = sum(scores) / len(scores) if scores else None
    else:
        status = RunStatus.PASS
        reason_codes = []
        score = sum(scores) / len(scores) if scores else 100.0

    run.dimension_scores = [
        DimensionScore(
            dimension=Dimension.QUALITY,
            score=round(score, 3) if score is not None else None,
            status=status,
            sample_count=len(run.case_results),
            reason_codes=reason_codes.copy(),
            metrics={
                "cases": len(run.case_results),
                "passed": sum(case.status is RunStatus.PASS for case in run.case_results),
                "failed": len(failures),
                "unknown": len(unknown),
                "source_format": "promptfoo-json-v3",
            },
        )
    ]
    run.finish(status, *reason_codes)
    return run


def _import_row(raw_row: Any, index: int) -> ProbeCaseResult:
    row = _mapping(raw_row, f"results.results[{index}]")
    success = row.get("success")
    if not isinstance(success, bool):
        raise PromptfooArtifactError(f"row {index} success must be boolean")
    raw_score = row.get("score")
    score: float | None
    if raw_score is None:
        score = None
    elif isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
        raise PromptfooArtifactError(f"row {index} score must be numeric or null")
    elif not 0 <= raw_score <= 1:
        raise PromptfooArtifactError(f"row {index} score must be between 0 and 1")
    else:
        score = float(raw_score) * 100

    error = row.get("error")
    if error is not None and not isinstance(error, str):
        raise PromptfooArtifactError(f"row {index} error must be a string")
    if error:
        status = RunStatus.FAIL
        reasons = ["PROMPTFOO_CASE_EXECUTION_ERROR"]
    elif success:
        status = RunStatus.PASS
        reasons = []
    else:
        status = RunStatus.FAIL
        reasons = ["PROMPTFOO_ASSERTION_FAILED"]

    response = row.get("response")
    response_mapping = response if isinstance(response, Mapping) else {}
    token_usage = response_mapping.get("tokenUsage")
    token_mapping = token_usage if isinstance(token_usage, Mapping) else {}
    grading = row.get("gradingResult")
    grading_mapping = grading if isinstance(grading, Mapping) else {}
    components = grading_mapping.get("componentResults")
    component_count = len(components) if isinstance(components, list) else 0
    evidence: dict[str, Any] = {
        "source": "promptfoo",
        "test_index": _optional_nonnegative_int(row.get("testIdx")),
        "prompt_index": _optional_nonnegative_int(row.get("promptIdx")),
        "assertion_count": component_count,
        "has_error": bool(error),
    }
    provider = row.get("provider")
    if isinstance(provider, str):
        evidence["provider"] = provider

    return ProbeCaseResult(
        case_id=_case_id(row, index),
        status=status,
        score=score,
        reason_codes=reasons,
        latency_ms=_optional_nonnegative_number(
            row.get("latencyMs", response_mapping.get("latencyMs"))
        ),
        provider_reported_input_tokens=_optional_nonnegative_int(
            token_mapping.get("prompt", token_mapping.get("input"))
        ),
        provider_reported_output_tokens=_optional_nonnegative_int(
            token_mapping.get("completion", token_mapping.get("output"))
        ),
        provider_reported_total_tokens=_optional_nonnegative_int(token_mapping.get("total")),
        evidence=evidence,
    )


def _case_id(row: Mapping[str, Any], index: int) -> str:
    test_index = _optional_nonnegative_int(row.get("testIdx"))
    prompt_index = _optional_nonnegative_int(row.get("promptIdx"))
    if test_index is not None and prompt_index is not None:
        return f"test-{test_index}-prompt-{prompt_index}"
    return f"row-{index}"


def _validate_stats_if_present(stats: Any, cases: list[ProbeCaseResult]) -> None:
    if stats is None:
        return
    mapping = _mapping(stats, "results.stats")
    counts: dict[str, int] = {}
    for field in ("successes", "failures", "errors"):
        value = mapping.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PromptfooArtifactError(f"results.stats.{field} must be non-negative")
        counts[field] = value
    if sum(counts.values()) != len(cases):
        raise PromptfooArtifactError("Promptfoo stats do not match result row count")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PromptfooArtifactError(f"{field} must be an object")
    return value


def _timestamp_or_now(value: Any) -> str:
    if not isinstance(value, str):
        return datetime.now(UTC).isoformat()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PromptfooArtifactError("results.timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise PromptfooArtifactError("results.timestamp must include a timezone")
    return parsed.isoformat()


def _optional_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _optional_nonnegative_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)
