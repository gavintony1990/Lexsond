from __future__ import annotations

import json
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .models import NormalizedRunResult
from .probe import OpenAIChatProbe, ProbeConfig
from .scoring import ScoringPolicy, score_run


class SuiteValidationError(ValueError):
    pass


class SuiteExecutionError(RuntimeError):
    pass


class SuiteExecutionCancelled(SuiteExecutionError):
    pass


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


class ProbeLayer(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"
    L5 = "L5"
    L6 = "L6"


@dataclass(frozen=True, slots=True)
class RequestSpec:
    prompt: str
    stream: bool
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class SamplingSpec:
    warmup: int
    requests: int
    concurrency: int
    timeout_seconds: float
    max_cost_usd: float


@dataclass(frozen=True, slots=True)
class ProbeSuite:
    name: str
    version: str
    layer: ProbeLayer
    protocol: str
    request: RequestSpec
    sampling: SamplingSpec
    scoring_policy: ScoringPolicy


def load_suite_json(path: str | Path) -> ProbeSuite:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    return compile_suite(document)


def compile_suite(document: Mapping[str, Any]) -> ProbeSuite:
    """Compile an untrusted suite document into a bounded immutable specification."""

    if not isinstance(document, Mapping):
        raise SuiteValidationError("suite document must be an object")
    _reject_secret_fields(document)
    _require_exact_keys(document, {"apiVersion", "kind", "metadata", "spec"}, "suite")
    if document.get("apiVersion") != "probe.ai/v1alpha1":
        raise SuiteValidationError("apiVersion must be probe.ai/v1alpha1")
    if document.get("kind") != "ProbeSuite":
        raise SuiteValidationError("kind must be ProbeSuite")

    metadata = _mapping(document.get("metadata"), "metadata")
    _require_exact_keys(metadata, {"name", "version"}, "metadata")
    name = _nonempty_string(metadata.get("name"), "metadata.name")
    version = _nonempty_string(metadata.get("version"), "metadata.version")

    spec = _mapping(document.get("spec"), "spec")
    _require_exact_keys(
        spec,
        {"layer", "protocol", "request", "sampling", "assertions"},
        "spec",
    )
    try:
        layer = ProbeLayer(spec.get("layer"))
    except (ValueError, TypeError) as exc:
        raise SuiteValidationError("spec.layer must be L0 through L6") from exc
    protocol = _nonempty_string(spec.get("protocol"), "spec.protocol")
    if protocol != "openai-chat":
        raise SuiteValidationError("v1alpha1 only supports protocol=openai-chat")

    request_document = _mapping(spec.get("request"), "spec.request")
    _require_exact_keys(
        request_document,
        {"prompt", "stream", "max_output_tokens"},
        "spec.request",
    )
    request = RequestSpec(
        prompt=_nonempty_string(request_document.get("prompt"), "spec.request.prompt"),
        stream=_boolean(request_document.get("stream"), "spec.request.stream"),
        max_output_tokens=_bounded_int(
            request_document.get("max_output_tokens"),
            "spec.request.max_output_tokens",
            minimum=1,
            maximum=4096,
        ),
    )

    sampling_document = _mapping(spec.get("sampling"), "spec.sampling")
    _require_exact_keys(
        sampling_document,
        {"warmup", "requests", "concurrency", "timeout_seconds", "max_cost_usd"},
        "spec.sampling",
    )
    max_cost = sampling_document.get("max_cost_usd")
    if isinstance(max_cost, bool) or not isinstance(max_cost, (int, float)) or max_cost <= 0:
        raise SuiteValidationError("spec.sampling.max_cost_usd must be a positive number")
    sampling = SamplingSpec(
        warmup=_bounded_int(sampling_document.get("warmup"), "spec.sampling.warmup", 0, 20),
        requests=_bounded_int(
            sampling_document.get("requests"), "spec.sampling.requests", 1, 10_000
        ),
        concurrency=_bounded_int(
            sampling_document.get("concurrency"), "spec.sampling.concurrency", 1, 1_000
        ),
        timeout_seconds=_positive_number(
            sampling_document.get("timeout_seconds"), "spec.sampling.timeout_seconds"
        ),
        max_cost_usd=float(max_cost),
    )
    if sampling.concurrency > sampling.requests:
        raise SuiteValidationError("concurrency cannot exceed requests")
    if layer in {ProbeLayer.L0, ProbeLayer.L1, ProbeLayer.L2}:
        if sampling.requests > 100:
            raise SuiteValidationError("L0-L2 native canaries are limited to 100 requests")
        if sampling.concurrency > 10:
            raise SuiteValidationError("L0-L2 native canaries are limited to concurrency 10")

    assertions = spec.get("assertions")
    if not isinstance(assertions, list) or not assertions:
        raise SuiteValidationError("spec.assertions must be a non-empty array")
    scoring_policy = _compile_assertions(assertions, request.stream)
    return ProbeSuite(name, version, layer, protocol, request, sampling, scoring_policy)


def run_suite(
    suite: ProbeSuite,
    *,
    base_url: str,
    api_key: str | None,
    model: str,
    mock_mode: str | None = None,
    cancel_signal: CancellationSignal | None = None,
    probe_runner: Callable[[ProbeConfig], NormalizedRunResult] | None = None,
) -> NormalizedRunResult:
    if suite.layer in {ProbeLayer.L4, ProbeLayer.L5, ProbeLayer.L6}:
        raise SuiteExecutionError(
            f"{suite.layer} suites require a specialized approved workflow and "
            "cannot use run_suite"
        )

    def execute_one() -> NormalizedRunResult:
        _raise_if_cancelled(cancel_signal)
        config = ProbeConfig(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=suite.sampling.timeout_seconds,
            stream=suite.request.stream,
            prompt=suite.request.prompt,
            max_output_tokens=suite.request.max_output_tokens,
            mock_mode=mock_mode,
        )
        if probe_runner is not None:
            return probe_runner(config)
        return OpenAIChatProbe(config).run()

    for _ in range(suite.sampling.warmup):
        _raise_if_cancelled(cancel_signal)
        execute_one()

    _raise_if_cancelled(cancel_signal)
    executor = ThreadPoolExecutor(max_workers=suite.sampling.concurrency)
    futures = {
        executor.submit(execute_one): index
        for index in range(suite.sampling.requests)
    }
    partial_results: list[NormalizedRunResult | None] = [
        None
    ] * suite.sampling.requests
    pending = set(futures)
    try:
        while pending:
            _raise_if_cancelled(cancel_signal)
            completed, pending = wait(
                pending,
                timeout=0.1,
                return_when=FIRST_COMPLETED,
            )
            for future in completed:
                partial_results[futures[future]] = future.result()
    finally:
        if _is_cancelled(cancel_signal):
            for future in pending:
                future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
    completed_results = [
        partial for partial in partial_results if partial is not None
    ]
    if len(completed_results) != suite.sampling.requests:
        raise SuiteExecutionError("native suite did not produce every requested sample")
    result = NormalizedRunResult(suite_name=suite.name, suite_version=suite.version)
    result.measurements = [
        measurement
        for partial in completed_results
        for measurement in partial.measurements
    ]
    return score_run(result, suite.scoring_policy)


def _is_cancelled(cancel_signal: CancellationSignal | None) -> bool:
    return cancel_signal is not None and cancel_signal.is_set()


def _raise_if_cancelled(cancel_signal: CancellationSignal | None) -> None:
    if _is_cancelled(cancel_signal):
        raise SuiteExecutionCancelled("native suite execution was cancelled")


def _compile_assertions(assertions: list[Any], stream: bool) -> ScoringPolicy:
    policy: dict[str, Any] = {"require_nonempty_output": False}
    seen: set[str] = set()
    allowed = {
        "http_status",
        "success_rate",
        "output_nonempty",
        "exact_text",
        "sse_sequence_valid",
        "finish_reason_present",
        "pseudo_stream_absent",
        "ttft_ms",
        "e2e_ms",
    }
    for index, raw in enumerate(assertions):
        assertion = _mapping(raw, f"spec.assertions[{index}]")
        kind = _nonempty_string(assertion.get("type"), f"spec.assertions[{index}].type")
        if kind not in allowed:
            raise SuiteValidationError(f"unknown assertion type: {kind}")
        if kind in seen:
            raise SuiteValidationError(f"duplicate assertion type: {kind}")
        seen.add(kind)

        if kind == "http_status":
            _require_exact_keys(assertion, {"type", "equals"}, kind)
            policy["expected_http_status"] = _bounded_int(
                assertion.get("equals"), f"{kind}.equals", 100, 599
            )
        elif kind == "success_rate":
            _require_exact_keys(assertion, {"type", "gte"}, kind)
            policy["minimum_success_rate"] = _ratio(assertion.get("gte"), f"{kind}.gte")
        elif kind == "output_nonempty":
            _require_exact_keys(assertion, {"type"}, kind)
            policy["require_nonempty_output"] = True
        elif kind == "exact_text":
            _require_exact_keys(assertion, {"type", "equals"}, kind)
            policy["expected_text"] = _nonempty_string(
                assertion.get("equals"), f"{kind}.equals"
            )
        elif kind == "sse_sequence_valid":
            _require_exact_keys(assertion, {"type"}, kind)
            if not stream:
                raise SuiteValidationError(
                    "sse_sequence_valid requires request.stream=true"
                )
            policy["require_sse_done"] = True
        elif kind == "finish_reason_present":
            _require_exact_keys(assertion, {"type"}, kind)
            policy["require_finish_reason"] = True
        elif kind == "pseudo_stream_absent":
            _require_exact_keys(assertion, {"type"}, kind)
            if not stream:
                raise SuiteValidationError(
                    "pseudo_stream_absent requires request.stream=true"
                )
            policy["reject_pseudo_stream"] = True
        elif kind == "ttft_ms":
            _require_exact_keys(assertion, {"type", "p95_lte"}, kind)
            if not stream:
                raise SuiteValidationError("ttft_ms requires request.stream=true")
            policy["max_p95_ttft_ms"] = _positive_number(
                assertion.get("p95_lte"), f"{kind}.p95_lte"
            )
        elif kind == "e2e_ms":
            _require_exact_keys(assertion, {"type", "p95_lte"}, kind)
            policy["max_p95_e2e_ms"] = _positive_number(
                assertion.get("p95_lte"), f"{kind}.p95_lte"
            )
    return ScoringPolicy(**policy)


def _reject_secret_fields(value: Any, path: str = "suite") -> None:
    forbidden = {"api_key", "apikey", "authorization", "secret", "secret_ref"}
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in forbidden:
                raise SuiteValidationError(
                    f"secret material is forbidden in suite documents: {path}.{key}"
                )
            _reject_secret_fields(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_fields(item, f"{path}[{index}]")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SuiteValidationError(f"{field} must be an object")
    return value


def _require_exact_keys(document: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = set(document) - allowed
    missing = allowed - set(document)
    if unknown:
        raise SuiteValidationError(f"{field} has unknown fields: {sorted(unknown)}")
    if missing:
        raise SuiteValidationError(f"{field} is missing fields: {sorted(missing)}")


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SuiteValidationError(f"{field} must be a non-empty string")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise SuiteValidationError(f"{field} must be a boolean")
    return value


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise SuiteValidationError(
            f"{field} must be an integer between {minimum} and {maximum}"
        )
    return value


def _positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise SuiteValidationError(f"{field} must be a positive number")
    return float(value)


def _ratio(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= 1
    ):
        raise SuiteValidationError(f"{field} must be between 0 and 1")
    return float(value)
