from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4


class RunnerStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    TARGET_FAILED = "TARGET_FAILED"
    RUNNER_FAILED = "RUNNER_FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class RunnerArtifact:
    uri: str
    sha256: str
    media_type: str

    def __post_init__(self) -> None:
        if not self.uri.strip() or not self.media_type.strip():
            raise ValueError("artifact uri and media_type must be non-empty")
        _validate_sha256(self.sha256, "artifact sha256")


@dataclass(frozen=True, slots=True)
class RunnerJob:
    runner_name: str
    runner_version: str
    endpoint_snapshot_id: str
    model: str
    suite_uri: str
    suite_sha256: str
    credential_handle: str = field(repr=False)
    timeout_seconds: float = 600
    max_requests: int = 100
    max_concurrency: int = 4
    max_cost_usd: float = 1
    job_id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        required = {
            "runner_name": self.runner_name,
            "runner_version": self.runner_version,
            "endpoint_snapshot_id": self.endpoint_snapshot_id,
            "model": self.model,
            "suite_uri": self.suite_uri,
            "suite_sha256": self.suite_sha256,
            "credential_handle": self.credential_handle,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(f"runner job fields must be non-empty: {missing}")
        if self.runner_name not in {"promptfoo", "evalscope", "aiperf", "guidellm"}:
            raise ValueError("runner_name is not supported")
        _validate_sha256(self.suite_sha256, "suite_sha256")
        if ":" not in self.credential_handle or self.credential_handle.lower().startswith("sk-"):
            raise ValueError("credential_handle must be an opaque secret reference")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not 1 <= self.max_requests <= 100_000:
            raise ValueError("max_requests must be between 1 and 100000")
        if not 1 <= self.max_concurrency <= self.max_requests:
            raise ValueError("max_concurrency must be between 1 and max_requests")
        if self.max_cost_usd <= 0:
            raise ValueError("max_cost_usd must be positive")


@dataclass(frozen=True, slots=True)
class RunnerOutcome:
    job_id: str
    runner_status: RunnerStatus
    runner_version: str
    result_schema_version: str
    exit_code: int | None
    artifacts: tuple[RunnerArtifact, ...] = ()
    error_code: str | None = None
    sanitized_stderr: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        if not self.job_id.strip() or not self.runner_version.strip():
            raise ValueError("job_id and runner_version must be non-empty")
        if not self.result_schema_version.strip():
            raise ValueError("result_schema_version must be non-empty")
        if self.runner_status in {RunnerStatus.SUCCEEDED, RunnerStatus.TARGET_FAILED}:
            if not self.artifacts:
                raise ValueError("successful or target-failed outcomes require artifacts")
        if self.sanitized_stderr is not None and len(self.sanitized_stderr) > 2000:
            raise ValueError("sanitized_stderr is limited to 2000 characters")
        _validate_timestamp(self.started_at, "started_at")
        _validate_timestamp(self.finished_at, "finished_at")


def _validate_sha256(value: str, field_name: str) -> None:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value.lower()
    ):
        raise ValueError(f"{field_name} must be a 64-character hexadecimal digest")


def _validate_timestamp(value: str, field_name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
