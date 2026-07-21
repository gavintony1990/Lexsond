from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4

from ..workflows.contracts import (
    ActivityInvocation,
    ActivityOutcome,
    ActivityOutcomeStatus,
    FailureKind,
)
from .runtime_contracts import (
    ActivityClaim,
    ActivityClaimDisposition,
    ActivityFailureRecord,
    CanaryRuntimeStoreIntegrityError,
    canonical_json_bytes,
    validate_lease_seconds,
    validate_sanitized_result,
)


class SqliteCanaryRuntimeStore:
    """Local durable idempotency and final-result store for Canary Activities.

    This mirrors the lease, terminal replay, and immutable-result semantics of
    the production PostgreSQL adapter.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        busy_timeout_ms: int = 5000,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(database_path, Path):
            raise ValueError("database_path must be a pathlib.Path")
        if database_path.exists() and database_path.is_dir():
            raise ValueError("database_path must not be a directory")
        if not database_path.parent.is_dir():
            raise ValueError("database_path parent directory must exist")
        if not 1 <= busy_timeout_ms <= 60_000:
            raise ValueError("busy_timeout_ms must be between 1 and 60000")
        self._database_path = database_path
        self._busy_timeout_ms = busy_timeout_ms
        self._clock = clock or time.time
        self._initialize()

    def claim(
        self,
        invocation: ActivityInvocation,
        *,
        lease_seconds: float,
    ) -> ActivityClaim:
        lease_seconds = validate_lease_seconds(lease_seconds)
        now = self._clock()
        lease_token = str(uuid4())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT run_id, activity_name, input_ref, status, attempt,
                       lease_token, lease_expires_at, outcome_status, result_ref,
                       error_code, failure_kind, retryable
                FROM canary_activity_executions
                WHERE idempotency_key = ?
                """,
                (invocation.idempotency_key,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO canary_activity_executions (
                        idempotency_key, run_id, activity_name, input_ref,
                        status, attempt, lease_token, lease_expires_at
                    ) VALUES (?, ?, ?, ?, 'LEASED', ?, ?, ?)
                    """,
                    (
                        invocation.idempotency_key,
                        invocation.run_id,
                        invocation.activity_name.value,
                        invocation.input_ref,
                        invocation.attempt,
                        lease_token,
                        now + lease_seconds,
                    ),
                )
                connection.commit()
                return ActivityClaim(
                    ActivityClaimDisposition.ACQUIRED,
                    lease_token=lease_token,
                )

            self._validate_identity(invocation, existing)
            status, stored_attempt = existing[3], existing[4]
            if invocation.attempt < stored_attempt:
                raise CanaryRuntimeStoreIntegrityError(
                    "Activity attempt moved backwards"
                )
            if status == "SUCCEEDED":
                outcome = self._outcome_from_row(existing)
                connection.commit()
                return ActivityClaim(
                    ActivityClaimDisposition.COMPLETED,
                    outcome=outcome,
                )
            if status == "FAILED" and invocation.attempt == stored_attempt:
                failure = self._failure_from_row(existing)
                connection.commit()
                return ActivityClaim(
                    ActivityClaimDisposition.FAILED,
                    failure=failure,
                )
            if status == "LEASED" and existing[6] > now:
                connection.commit()
                return ActivityClaim(
                    ActivityClaimDisposition.BUSY,
                    retry_after_seconds=max(existing[6] - now, 0.001),
                )
            if status not in {"LEASED", "FAILED"}:
                raise CanaryRuntimeStoreIntegrityError(
                    "stored Activity execution status is invalid"
                )
            if status == "FAILED" and invocation.attempt == stored_attempt:
                raise CanaryRuntimeStoreIntegrityError(
                    "failed Activity attempt cannot be reacquired"
                )
            connection.execute(
                """
                UPDATE canary_activity_executions
                SET status = 'LEASED', attempt = ?, lease_token = ?,
                    lease_expires_at = ?, outcome_status = NULL,
                    result_ref = NULL, error_code = NULL, failure_kind = NULL,
                    retryable = NULL, updated_at = ?
                WHERE idempotency_key = ?
                """,
                (
                    invocation.attempt,
                    lease_token,
                    now + lease_seconds,
                    now,
                    invocation.idempotency_key,
                ),
            )
            connection.commit()
            return ActivityClaim(
                ActivityClaimDisposition.ACQUIRED,
                lease_token=lease_token,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def renew(
        self,
        invocation: ActivityInvocation,
        *,
        lease_token: str,
        lease_seconds: float,
    ) -> None:
        self._validate_lease_token(lease_token)
        lease_seconds = validate_lease_seconds(lease_seconds)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._execution_row(connection, invocation.idempotency_key)
            if existing is None:
                raise CanaryRuntimeStoreIntegrityError(
                    "Activity execution does not exist"
                )
            self._validate_identity(invocation, existing)
            if (
                existing[3] != "LEASED"
                or existing[4] != invocation.attempt
                or existing[5] != lease_token
            ):
                raise CanaryRuntimeStoreIntegrityError(
                    "Activity lease no longer belongs to this execution"
                )
            now = self._clock()
            connection.execute(
                """
                UPDATE canary_activity_executions
                SET lease_expires_at = ?, updated_at = ?
                WHERE idempotency_key = ?
                """,
                (now + lease_seconds, now, invocation.idempotency_key),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete(
        self,
        invocation: ActivityInvocation,
        *,
        lease_token: str,
        outcome: ActivityOutcome,
    ) -> None:
        if not isinstance(outcome, ActivityOutcome):
            raise ValueError("outcome must be an ActivityOutcome")
        self._validate_lease_token(lease_token)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._execution_row(connection, invocation.idempotency_key)
            if existing is None:
                raise CanaryRuntimeStoreIntegrityError(
                    "Activity execution does not exist"
                )
            self._validate_identity(invocation, existing)
            if existing[3] == "SUCCEEDED":
                if self._outcome_from_row(existing) != outcome:
                    raise CanaryRuntimeStoreIntegrityError(
                        "idempotency key has a conflicting completed outcome"
                    )
                connection.commit()
                return
            if (
                existing[3] != "LEASED"
                or existing[4] != invocation.attempt
                or existing[5] != lease_token
            ):
                raise CanaryRuntimeStoreIntegrityError(
                    "Activity lease no longer belongs to this execution"
                )
            connection.execute(
                """
                UPDATE canary_activity_executions
                SET status = 'SUCCEEDED', lease_token = NULL,
                    lease_expires_at = NULL, outcome_status = ?, result_ref = ?,
                    error_code = NULL, failure_kind = NULL, retryable = NULL,
                    updated_at = ?
                WHERE idempotency_key = ?
                """,
                (
                    outcome.status.value,
                    outcome.result_ref,
                    self._clock(),
                    invocation.idempotency_key,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fail(
        self,
        invocation: ActivityInvocation,
        *,
        lease_token: str,
        failure: ActivityFailureRecord,
    ) -> None:
        if not isinstance(failure, ActivityFailureRecord):
            raise ValueError("failure must be an ActivityFailureRecord")
        self._validate_lease_token(lease_token)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._execution_row(connection, invocation.idempotency_key)
            if existing is None:
                raise CanaryRuntimeStoreIntegrityError(
                    "Activity execution does not exist"
                )
            self._validate_identity(invocation, existing)
            if existing[3] == "FAILED" and existing[4] == invocation.attempt:
                if self._failure_from_row(existing) != failure:
                    raise CanaryRuntimeStoreIntegrityError(
                        "idempotency key has a conflicting failed outcome"
                    )
                connection.commit()
                return
            if (
                existing[3] != "LEASED"
                or existing[4] != invocation.attempt
                or existing[5] != lease_token
            ):
                raise CanaryRuntimeStoreIntegrityError(
                    "Activity lease no longer belongs to this execution"
                )
            connection.execute(
                """
                UPDATE canary_activity_executions
                SET status = 'FAILED', lease_token = NULL,
                    lease_expires_at = NULL, outcome_status = NULL,
                    result_ref = NULL, error_code = ?, failure_kind = ?,
                    retryable = ?, updated_at = ?
                WHERE idempotency_key = ?
                """,
                (
                    failure.error_code,
                    failure.kind.value,
                    int(failure.retryable),
                    self._clock(),
                    invocation.idempotency_key,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def persist_result(
        self,
        *,
        run_id: str,
        result_ref: str,
        result: Mapping[str, Any],
    ) -> str:
        if not isinstance(result_ref, str) or not result_ref:
            raise ValueError("result_ref must be non-empty")
        if not isinstance(result, Mapping):
            raise ValueError("result must be an object")
        validate_sanitized_result(run_id, result)
        result_json = canonical_json_bytes(result).decode("utf-8")
        digest = hashlib.sha256(result_json.encode("utf-8")).hexdigest()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT result_ref, result_sha256, result_json
                FROM canary_probe_results
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            expected = (result_ref, digest, result_json)
            if existing is not None:
                if existing != expected:
                    raise CanaryRuntimeStoreIntegrityError(
                        "probe result is immutable for a workflow run"
                    )
                connection.commit()
                return result_ref
            connection.execute(
                """
                INSERT INTO canary_probe_results (
                    run_id, result_ref, result_sha256, result_json
                ) VALUES (?, ?, ?, ?)
                """,
                (run_id, result_ref, digest, result_json),
            )
            connection.commit()
            return result_ref
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def load_result(self, run_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT result_sha256, result_json
                FROM canary_probe_results
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        digest, result_json = row
        if hashlib.sha256(result_json.encode("utf-8")).hexdigest() != digest:
            raise CanaryRuntimeStoreIntegrityError("stored probe result digest mismatch")
        try:
            value = json.loads(result_json)
        except json.JSONDecodeError as exc:
            raise CanaryRuntimeStoreIntegrityError(
                "stored probe result is invalid JSON"
            ) from exc
        validate_sanitized_result(run_id, value)
        return value

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            legacy = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'canary_activity_outcomes'
                """
            ).fetchone()
            if legacy is not None:
                raise CanaryRuntimeStoreIntegrityError(
                    "legacy local Activity cache has no safe lease migration; "
                    "create a new local-development database"
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS canary_runtime_schema_metadata (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL CHECK (schema_version = 2)
                );

                INSERT OR IGNORE INTO canary_runtime_schema_metadata (
                    singleton, schema_version
                ) VALUES (1, 2);

                CREATE TABLE IF NOT EXISTS canary_activity_executions (
                    idempotency_key TEXT PRIMARY KEY NOT NULL,
                    run_id TEXT NOT NULL,
                    activity_name TEXT NOT NULL,
                    input_ref TEXT,
                    status TEXT NOT NULL
                        CHECK (status IN ('LEASED', 'SUCCEEDED', 'FAILED')),
                    attempt INTEGER NOT NULL CHECK (attempt >= 1),
                    lease_token TEXT,
                    lease_expires_at REAL,
                    outcome_status TEXT
                        CHECK (outcome_status IN ('SUCCEEDED', 'TARGET_FAILED')),
                    result_ref TEXT,
                    error_code TEXT,
                    failure_kind TEXT
                        CHECK (failure_kind IN (
                            'CONFIGURATION', 'POLICY', 'INFRASTRUCTURE', 'RUNNER'
                        )),
                    retryable INTEGER CHECK (retryable IN (0, 1)),
                    created_at REAL NOT NULL DEFAULT (unixepoch('subsec')),
                    updated_at REAL NOT NULL DEFAULT (unixepoch('subsec')),
                    CHECK (
                        (status = 'LEASED' AND lease_token IS NOT NULL
                            AND lease_expires_at IS NOT NULL
                            AND outcome_status IS NULL AND result_ref IS NULL
                            AND error_code IS NULL AND failure_kind IS NULL
                            AND retryable IS NULL)
                        OR (status = 'SUCCEEDED' AND lease_token IS NULL
                            AND lease_expires_at IS NULL
                            AND outcome_status IS NOT NULL AND result_ref IS NOT NULL
                            AND error_code IS NULL AND failure_kind IS NULL
                            AND retryable IS NULL)
                        OR (status = 'FAILED' AND lease_token IS NULL
                            AND lease_expires_at IS NULL
                            AND outcome_status IS NULL AND result_ref IS NULL
                            AND error_code IS NOT NULL AND failure_kind IS NOT NULL
                            AND retryable IS NOT NULL)
                    )
                );

                CREATE INDEX IF NOT EXISTS idx_canary_activity_lease_expiry
                    ON canary_activity_executions (lease_expires_at)
                    WHERE status = 'LEASED';

                CREATE TABLE IF NOT EXISTS canary_probe_results (
                    run_id TEXT PRIMARY KEY NOT NULL,
                    result_ref TEXT NOT NULL,
                    result_sha256 TEXT NOT NULL
                        CHECK (length(result_sha256) = 64),
                    result_json TEXT NOT NULL CHECK (json_valid(result_json))
                );
                """
            )
            version = connection.execute(
                """
                SELECT schema_version FROM canary_runtime_schema_metadata
                WHERE singleton = 1
                """
            ).fetchone()
            if version != (2,):
                raise CanaryRuntimeStoreIntegrityError(
                    "unsupported local Canary runtime schema version"
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=self._busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @staticmethod
    def _execution_row(
        connection: sqlite3.Connection, idempotency_key: str
    ) -> tuple[Any, ...] | None:
        return connection.execute(
            """
            SELECT run_id, activity_name, input_ref, status, attempt,
                   lease_token, lease_expires_at, outcome_status, result_ref,
                   error_code, failure_kind, retryable
            FROM canary_activity_executions
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()

    @staticmethod
    def _validate_identity(
        invocation: ActivityInvocation, row: tuple[Any, ...]
    ) -> None:
        identity = row[:3]
        expected = (
            invocation.run_id,
            invocation.activity_name.value,
            invocation.input_ref,
        )
        if identity != expected:
            raise CanaryRuntimeStoreIntegrityError(
                "idempotency key belongs to a different Activity invocation"
            )

    @staticmethod
    def _outcome_from_row(row: tuple[Any, ...]) -> ActivityOutcome:
        try:
            return ActivityOutcome(ActivityOutcomeStatus(row[7]), row[8])
        except (TypeError, ValueError) as exc:
            raise CanaryRuntimeStoreIntegrityError(
                "stored Activity outcome is invalid"
            ) from exc

    @staticmethod
    def _failure_from_row(row: tuple[Any, ...]) -> ActivityFailureRecord:
        try:
            return ActivityFailureRecord(
                error_code=row[9],
                kind=FailureKind(row[10]),
                retryable=bool(row[11]),
            )
        except (TypeError, ValueError) as exc:
            raise CanaryRuntimeStoreIntegrityError(
                "stored Activity failure is invalid"
            ) from exc

    @staticmethod
    def _validate_lease_token(value: str) -> None:
        try:
            UUID(value)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("lease_token must be a UUID") from exc
