from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Mapping

from ..models import SCHEMA_VERSION
from .contracts import RunnerArtifact, RunnerJob, RunnerOutcome, RunnerStatus


_SAFE_INHERITED_ENV = ("PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True, slots=True)
class RunnerExecutable:
    runner_name: str
    runner_version: str
    path: Path

    def __post_init__(self) -> None:
        if not self.runner_name or not self.runner_version:
            raise ValueError("runner executable name and version must be non-empty")
        if not self.path.is_absolute():
            raise ValueError("runner executable path must be absolute")


@dataclass(frozen=True, slots=True)
class RunnerProcessSpec:
    arguments: tuple[str, ...]
    artifact_files: tuple[str, ...]
    credential_env_var: str = "OPENAI_API_KEY"
    target_failure_exit_codes: tuple[int, ...] = (1,)

    def __post_init__(self) -> None:
        if not self.arguments or any(not isinstance(arg, str) for arg in self.arguments):
            raise ValueError("runner arguments must be a non-empty string tuple")
        if not self.artifact_files:
            raise ValueError("runner must declare at least one artifact file")
        if len(self.artifact_files) != len(set(self.artifact_files)):
            raise ValueError("runner artifact files must be unique")
        for name in self.artifact_files:
            path = PurePath(name)
            if path.is_absolute() or not name or ".." in path.parts:
                raise ValueError("artifact files must be relative and remain in artifact_dir")
        if not _ENV_NAME.fullmatch(self.credential_env_var):
            raise ValueError("credential_env_var must be an uppercase environment name")
        if any(isinstance(code, bool) or not isinstance(code, int) for code in self.target_failure_exit_codes):
            raise ValueError("target_failure_exit_codes must contain integers")


class RunnerProcessLauncher:
    """Bounded no-shell launcher for a version-pinned runner executable.

    This creates a process boundary and strict evidence contract. Production
    deployments must additionally place the process in a container/sandbox to
    enforce filesystem, network, CPU, and memory policy.
    """

    def __init__(
        self,
        executables: Mapping[str, RunnerExecutable],
        *,
        max_log_bytes: int = 1_000_000,
        max_artifact_bytes: int = 50_000_000,
        termination_grace_seconds: float = 1.0,
    ) -> None:
        if max_log_bytes <= 0 or max_artifact_bytes <= 0:
            raise ValueError("launcher byte limits must be positive")
        if termination_grace_seconds <= 0:
            raise ValueError("termination_grace_seconds must be positive")
        self._executables = dict(executables)
        self._max_log_bytes = max_log_bytes
        self._max_artifact_bytes = max_artifact_bytes
        self._termination_grace_seconds = termination_grace_seconds

    def run(
        self,
        job: RunnerJob,
        spec: RunnerProcessSpec,
        *,
        suite_path: Path,
        artifact_dir: Path,
        credential_value: str,
        cancel_event: threading.Event | None = None,
    ) -> RunnerOutcome:
        started_at = _now()
        executable = self._resolve_executable(job)
        suite_path = suite_path.resolve()
        artifact_dir = artifact_dir.resolve()
        self._validate_inputs(job, spec, suite_path, artifact_dir, credential_value)

        if cancel_event is not None and cancel_event.is_set():
            return self._outcome(
                job,
                RunnerStatus.CANCELLED,
                None,
                started_at,
                error_code="RUNNER_CANCELLED_BEFORE_START",
            )

        command = [
            str(executable.path),
            *(
                _expand_argument(
                    argument,
                    suite_path=suite_path,
                    artifact_dir=artifact_dir,
                    job=job,
                )
                for argument in spec.arguments
            ),
        ]
        if any(credential_value in argument for argument in command):
            raise ValueError("credential value must not appear in runner arguments")
        environment = _minimal_environment(job, spec, credential_value)

        try:
            process = subprocess.Popen(
                command,
                cwd=artifact_dir,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            return self._outcome(
                job,
                RunnerStatus.RUNNER_FAILED,
                None,
                started_at,
                error_code="RUNNER_START_FAILED",
                stderr=_sanitize(str(exc), credential_value),
            )

        stdout = _BoundedPipeBuffer(self._max_log_bytes)
        stderr = _BoundedPipeBuffer(self._max_log_bytes)
        stdout_thread = threading.Thread(
            target=_drain_pipe, args=(process.stdout, stdout), daemon=True
        )
        stderr_thread = threading.Thread(
            target=_drain_pipe, args=(process.stderr, stderr), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()

        deadline = time.monotonic() + job.timeout_seconds
        termination_reason: str | None = None
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                termination_reason = "RUNNER_CANCELLED"
                break
            if time.monotonic() >= deadline:
                termination_reason = "RUNNER_TIMEOUT"
                break
            time.sleep(0.02)

        if termination_reason is not None:
            _terminate_process_tree(process, self._termination_grace_seconds)
        exit_code = process.wait()
        stdout_thread.join(timeout=self._termination_grace_seconds)
        stderr_thread.join(timeout=self._termination_grace_seconds)
        stderr_text = _sanitize(stderr.text(), credential_value)

        if termination_reason is not None:
            return self._outcome(
                job,
                RunnerStatus.CANCELLED,
                exit_code,
                started_at,
                error_code=termination_reason,
                stderr=stderr_text,
            )

        try:
            artifacts = self._collect_artifacts(artifact_dir, spec.artifact_files)
        except _ArtifactCollectionError as exc:
            detail = _sanitize(f"{stderr_text}\n{exc}".strip(), credential_value)
            return self._outcome(
                job,
                RunnerStatus.RUNNER_FAILED,
                exit_code,
                started_at,
                error_code=exc.error_code,
                stderr=detail,
            )

        if exit_code == 0:
            status = RunnerStatus.SUCCEEDED
            error_code = None
        elif exit_code in spec.target_failure_exit_codes:
            status = RunnerStatus.TARGET_FAILED
            error_code = "TARGET_ASSERTION_FAILED"
        else:
            status = RunnerStatus.RUNNER_FAILED
            error_code = "RUNNER_EXIT_NONZERO"
            artifacts = ()
        return self._outcome(
            job,
            status,
            exit_code,
            started_at,
            artifacts=artifacts,
            error_code=error_code,
            stderr=stderr_text,
        )

    def _resolve_executable(self, job: RunnerJob) -> RunnerExecutable:
        executable = self._executables.get(job.runner_name)
        if executable is None or executable.runner_name != job.runner_name:
            raise ValueError(f"no executable is configured for runner {job.runner_name}")
        if executable.runner_version != job.runner_version:
            raise ValueError("runner job version does not match configured executable version")
        if not executable.path.is_file():
            raise ValueError("configured runner executable does not exist")
        return executable

    def _validate_inputs(
        self,
        job: RunnerJob,
        spec: RunnerProcessSpec,
        suite_path: Path,
        artifact_dir: Path,
        credential_value: str,
    ) -> None:
        if not credential_value:
            raise ValueError("credential value must not be empty")
        if not suite_path.is_file() or suite_path.is_symlink():
            raise ValueError("suite_path must be a regular non-symlink file")
        if _sha256(suite_path) != job.suite_sha256.lower():
            raise ValueError("suite file digest does not match RunnerJob")
        if not artifact_dir.is_dir() or artifact_dir.is_symlink():
            raise ValueError("artifact_dir must be a regular non-symlink directory")
        for name in spec.artifact_files:
            path = artifact_dir / name
            if path.exists() or path.is_symlink():
                raise ValueError("expected runner artifacts must not exist before launch")

    def _collect_artifacts(
        self, artifact_dir: Path, artifact_files: tuple[str, ...]
    ) -> tuple[RunnerArtifact, ...]:
        collected: list[RunnerArtifact] = []
        for name in artifact_files:
            path = artifact_dir / name
            if not path.exists():
                raise _ArtifactCollectionError("RUNNER_ARTIFACT_MISSING", f"missing artifact: {name}")
            if path.is_symlink() or not path.is_file():
                raise _ArtifactCollectionError("RUNNER_ARTIFACT_INVALID", f"invalid artifact: {name}")
            resolved = path.resolve()
            if artifact_dir not in resolved.parents:
                raise _ArtifactCollectionError("RUNNER_ARTIFACT_ESCAPED", f"artifact escaped directory: {name}")
            if resolved.stat().st_size > self._max_artifact_bytes:
                raise _ArtifactCollectionError("RUNNER_ARTIFACT_TOO_LARGE", f"artifact is too large: {name}")
            media_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
            collected.append(
                RunnerArtifact(
                    uri=resolved.as_uri(),
                    sha256=_sha256(resolved),
                    media_type=media_type,
                )
            )
        return tuple(collected)

    @staticmethod
    def _outcome(
        job: RunnerJob,
        status: RunnerStatus,
        exit_code: int | None,
        started_at: str,
        *,
        artifacts: tuple[RunnerArtifact, ...] = (),
        error_code: str | None = None,
        stderr: str | None = None,
    ) -> RunnerOutcome:
        return RunnerOutcome(
            job_id=job.job_id,
            runner_status=status,
            runner_version=job.runner_version,
            result_schema_version=SCHEMA_VERSION,
            exit_code=exit_code,
            artifacts=artifacts,
            error_code=error_code,
            sanitized_stderr=stderr[:2000] if stderr else None,
            started_at=started_at,
            finished_at=_now(),
        )


class _ArtifactCollectionError(Exception):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class _BoundedPipeBuffer:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._data = bytearray()
        self._truncated = False

    def append(self, chunk: bytes) -> None:
        remaining = self._limit - len(self._data)
        if remaining > 0:
            self._data.extend(chunk[:remaining])
        if len(chunk) > remaining:
            self._truncated = True

    def text(self) -> str:
        value = self._data.decode("utf-8", errors="replace")
        if self._truncated:
            value += "\n[TRUNCATED]"
        return value


def _drain_pipe(pipe: object, destination: _BoundedPipeBuffer) -> None:
    if pipe is None:
        return
    try:
        while True:
            chunk = pipe.read(65536)  # type: ignore[attr-defined]
            if not chunk:
                return
            destination.append(chunk)
    finally:
        pipe.close()  # type: ignore[attr-defined]


def _expand_argument(
    argument: str,
    *,
    suite_path: Path,
    artifact_dir: Path,
    job: RunnerJob,
) -> str:
    replacements = {
        "{suite_path}": str(suite_path),
        "{artifact_dir}": str(artifact_dir),
        "{model}": job.model,
        "{max_requests}": str(job.max_requests),
        "{max_concurrency}": str(job.max_concurrency),
        "{max_cost_usd}": str(job.max_cost_usd),
    }
    for placeholder, value in replacements.items():
        argument = argument.replace(placeholder, value)
    return argument


def _minimal_environment(
    job: RunnerJob, spec: RunnerProcessSpec, credential_value: str
) -> dict[str, str]:
    environment = {
        name: os.environ[name] for name in _SAFE_INHERITED_ENV if name in os.environ
    }
    environment.update(
        {
            spec.credential_env_var: credential_value,
            "PROBE_JOB_ID": job.job_id,
            "PROBE_MAX_REQUESTS": str(job.max_requests),
            "PROBE_MAX_CONCURRENCY": str(job.max_concurrency),
            "PROBE_MAX_COST_USD": str(job.max_cost_usd),
        }
    )
    return environment


def _terminate_process_tree(process: subprocess.Popen[bytes], grace_seconds: float) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=grace_seconds)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except OSError:
        pass


def _sanitize(value: str, credential_value: str) -> str:
    sanitized = value.replace(credential_value, "[REDACTED]")
    sanitized = re.sub(
        r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+",
        r"\1[REDACTED]",
        sanitized,
    )
    sanitized = re.sub(
        r"(?i)((?:api[_-]?key|token|secret)\s*[:=]\s*)[^\s,;]+",
        r"\1[REDACTED]",
        sanitized,
    )
    return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", sanitized)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()
