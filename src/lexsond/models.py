from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


SCHEMA_VERSION = "probe.ai/result/v1alpha1"


class RunStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class Dimension(StrEnum):
    AVAILABILITY = "availability"
    PROTOCOL = "protocol"
    PERFORMANCE = "performance"
    QUALITY = "quality"


class ErrorClass(StrEnum):
    AUTHENTICATION = "AUTHENTICATION"
    PAYMENT_REQUIRED = "PAYMENT_REQUIRED"
    AUTHORIZATION = "AUTHORIZATION"
    RATE_LIMIT = "RATE_LIMIT"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    UPSTREAM_5XX = "UPSTREAM_5XX"
    TIMEOUT = "TIMEOUT"
    NETWORK = "NETWORK"
    PROTOCOL = "PROTOCOL"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ChunkMeasurement:
    sequence: int
    received_after_ms: float
    event_type: str | None
    content_chars: int
    reasoning_chars: int = 0
    has_usage: bool = False
    finish_reason: str | None = None


@dataclass(slots=True)
class RequestMeasurement:
    request_id: str = field(default_factory=lambda: str(uuid4()))
    endpoint: str = ""
    requested_model: str = ""
    response_model: str | None = None
    streaming: bool = True
    status_code: int | None = None
    error_class: ErrorClass | None = None
    error_message: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    connect_ms: float | None = None
    response_headers_ms: float | None = None
    ttfb_ms: float | None = None
    ttft_ms: float | None = None
    e2e_ms: float | None = None
    itl_ms: float | None = None
    output_tps: float | None = None
    chunk_count: int = 0
    output_text: str = ""
    finish_reason: str | None = None
    provider_reported_input_tokens: int | None = None
    provider_reported_output_tokens: int | None = None
    provider_reported_total_tokens: int | None = None
    locally_estimated_input_tokens: int | None = None
    locally_estimated_output_tokens: int | None = None
    chunks: list[ChunkMeasurement] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProbeCaseResult:
    case_id: str
    status: RunStatus
    score: float | None
    reason_codes: list[str] = field(default_factory=list)
    latency_ms: float | None = None
    provider_reported_input_tokens: int | None = None
    provider_reported_output_tokens: int | None = None
    provider_reported_total_tokens: int | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DimensionScore:
    dimension: Dimension
    score: float | None
    status: RunStatus
    sample_count: int
    reason_codes: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    confidence_interval: dict[str, float] | None = None


@dataclass(slots=True)
class NormalizedRunResult:
    run_id: str = field(default_factory=lambda: str(uuid4()))
    schema_version: str = SCHEMA_VERSION
    suite_name: str = "openai-chat-canary"
    suite_version: str = "0.1.0"
    status: RunStatus = RunStatus.UNKNOWN
    reason_codes: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str | None = None
    measurements: list[RequestMeasurement] = field(default_factory=list)
    case_results: list[ProbeCaseResult] = field(default_factory=list)
    dimension_scores: list[DimensionScore] = field(default_factory=list)

    def finish(self, status: RunStatus, *reason_codes: str) -> None:
        self.status = status
        self.reason_codes.extend(reason_codes)
        self.finished_at = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return _enum_values(asdict(self))


def _enum_values(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {key: _enum_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_enum_values(item) for item in value]
    return value
