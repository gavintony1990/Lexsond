from __future__ import annotations

import hashlib
import math
from contextlib import AbstractContextManager, suppress
from datetime import datetime
from typing import Any, Mapping
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from ..workflows.canary import ConcurrentWorkflowUpdate
from ..workflows.contracts import (
    ActivityInvocation,
    ActivityOutcome,
    ActivityOutcomeStatus,
    CanaryWorkflowInput,
    FailureKind,
    WorkflowEvent,
)
from ..suite import compile_suite
from ..workflows.native_activities import EndpointSnapshot
from ..workflows.state import project_workflow_state
from .evidence import EvidenceManifest
from .runtime_contracts import (
    ActivityClaim,
    ActivityClaimDisposition,
    ActivityFailureRecord,
    CanaryRuntimeStoreIntegrityError,
    canonical_json_bytes,
    validate_lease_seconds,
    validate_sanitized_result,
)
from .journal_errors import WorkflowJournalCorruption, WorkflowJournalIntegrityError


class PostgresPool:
    """Owned synchronous psycopg pool with explicit startup validation."""

    def __init__(
        self,
        conninfo: str,
        *,
        min_size: int = 1,
        max_size: int = 8,
        timeout_seconds: float = 10.0,
        application_name: str = "lexsond-worker",
    ) -> None:
        if not isinstance(conninfo, str) or not conninfo.strip():
            raise ValueError("PostgreSQL conninfo must be non-empty")
        if not 1 <= min_size <= max_size <= 128:
            raise ValueError("pool sizes must satisfy 1 <= min_size <= max_size <= 128")
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 1 and 120")
        pool = ConnectionPool(
            conninfo=conninfo,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout_seconds,
            open=False,
            check=ConnectionPool.check_connection,
            kwargs={
                "autocommit": False,
                "row_factory": dict_row,
                "application_name": application_name,
            },
        )
        try:
            pool.open(wait=True, timeout=timeout_seconds)
        except BaseException:
            with suppress(Exception):
                pool.close()
            raise
        self._pool = pool

    def connection(self) -> AbstractContextManager[psycopg.Connection[dict[str, Any]]]:
        return self._pool.connection()

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> PostgresPool:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class PostgresWorkflowJournal:
    """PostgreSQL workflow journal and idempotent run initializer."""

    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def prepare_run(self, workflow_input: CanaryWorkflowInput) -> None:
        if not isinstance(workflow_input, CanaryWorkflowInput):
            raise ValueError("workflow_input must be a CanaryWorkflowInput")
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    "SELECT lexsond.create_workflow_run(%s, %s, %s)",
                    (
                        workflow_input.run_id,
                        workflow_input.content_hash(),
                        Jsonb(workflow_input.to_dict()),
                    ),
                )
        except psycopg.Error as exc:
            raise WorkflowJournalIntegrityError(
                "workflow run initialization violated the PostgreSQL contract"
            ) from exc

    def load(self, run_id: str) -> tuple[WorkflowEvent, ...]:
        _uuid(run_id, "run_id")
        with self._pool.connection() as connection:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
            )
            head = connection.execute(
                "SELECT last_sequence FROM lexsond.workflow_runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT sequence, event_id, event_json
                FROM lexsond.workflow_events
                WHERE run_id = %s
                ORDER BY sequence ASC
                """,
                (run_id,),
            ).fetchall()
        if head is None:
            if rows:
                raise WorkflowJournalCorruption("events exist without a workflow run")
            return ()
        if head["last_sequence"] != len(rows):
            raise WorkflowJournalCorruption(
                "workflow run head does not match the number of events"
            )
        return _decode_events(run_id, rows)

    def load_input(self, run_id: str) -> CanaryWorkflowInput | None:
        """Load and verify the durable Temporal launch outbox for recovery."""

        _uuid(run_id, "run_id")
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT workflow_input_sha256, workflow_input_json
                FROM lexsond.workflow_runs WHERE run_id = %s
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        workflow_input = CanaryWorkflowInput.from_dict(row["workflow_input_json"])
        if workflow_input.content_hash() != row["workflow_input_sha256"].strip():
            raise WorkflowJournalCorruption(
                "workflow input digest does not match stored JSON"
            )
        return workflow_input


    def append(self, event: WorkflowEvent, *, expected_sequence: int) -> None:
        if isinstance(expected_sequence, bool) or not isinstance(expected_sequence, int):
            raise ValueError("expected_sequence must be an integer")
        if expected_sequence < 0:
            raise ValueError("expected_sequence must be non-negative")
        if event.sequence != expected_sequence + 1:
            raise ConcurrentWorkflowUpdate(
                "event sequence does not follow the expected sequence"
            )
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                )
                run = connection.execute(
                    """
                    SELECT last_sequence, workflow_input_sha256,
                           workflow_input_json
                    FROM lexsond.workflow_runs
                    WHERE run_id = %s
                    """,
                    (event.run_id,),
                ).fetchone()
                if run is None:
                    raise WorkflowJournalIntegrityError(
                        "workflow run must be initialized before append"
                    )
                rows = connection.execute(
                    """
                    SELECT sequence, event_id, event_json
                    FROM lexsond.workflow_events
                    WHERE run_id = %s
                    ORDER BY sequence ASC
                    """,
                    (event.run_id,),
                ).fetchall()
                events = _decode_events(event.run_id, rows)
                if run["last_sequence"] != len(events):
                    raise WorkflowJournalCorruption(
                        "workflow run head does not match the number of events"
                    )
                if run["last_sequence"] != expected_sequence:
                    if (
                        1 <= event.sequence <= len(events)
                        and events[event.sequence - 1] == event
                    ):
                        return
                    raise ConcurrentWorkflowUpdate(
                        f"expected sequence {expected_sequence}, "
                        f"found {run['last_sequence']}"
                    )
                workflow_input = CanaryWorkflowInput.from_dict(
                    run["workflow_input_json"]
                )
                if (
                    workflow_input.content_hash()
                    != run["workflow_input_sha256"].strip()
                ):
                    raise WorkflowJournalCorruption(
                        "workflow input digest does not match stored JSON"
                    )
                project_workflow_state(
                    workflow_input,
                    (*events, event),
                )
                connection.execute(
                    """
                    SELECT lexsond.append_workflow_event(
                        %s, %s, %s
                    )
                    """,
                    (
                        event.run_id,
                        expected_sequence,
                        Jsonb(event.to_dict()),
                    ),
                )
        except psycopg.errors.SerializationFailure as exc:
            if self._is_exactly_stored(event):
                return
            raise ConcurrentWorkflowUpdate(
                "workflow journal compare-and-append conflict"
            ) from exc
        except (ConcurrentWorkflowUpdate, WorkflowJournalCorruption, WorkflowJournalIntegrityError):
            raise
        except (ValueError, psycopg.Error) as exc:
            raise WorkflowJournalIntegrityError(
                "workflow event violated the PostgreSQL journal contract"
            ) from exc

    def _is_exactly_stored(self, event: WorkflowEvent) -> bool:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM lexsond.workflow_events
                WHERE run_id = %s AND sequence = %s AND event_id = %s
                  AND event_json = %s
                """,
                (
                    event.run_id,
                    event.sequence,
                    event.event_id,
                    Jsonb(event.to_dict()),
                ),
            ).fetchone()
        return row is not None


class PostgresSnapshotWriter:
    """Persist immutable Web-control snapshots before a Temporal launch.

    Credential references are stored only in the dedicated endpoint column.
    They are deliberately excluded from the digested configuration JSON and
    therefore from the Temporal workflow input/history.
    """

    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def persist(
        self,
        *,
        endpoint_snapshot_id: str,
        provider_id: str,
        protocol: str,
        base_url: str,
        model: str,
        credential_ref: str,
        configuration: Mapping[str, Any],
        suite_uri: str,
        suite_document: Mapping[str, Any],
    ) -> tuple[str, str]:
        suite = compile_suite(suite_document)
        configuration_value = dict(configuration)
        forbidden = {
            "api_key",
            "authorization",
            "credential",
            "credential_handle",
            "credential_ref",
            "access_token",
            "refresh_token",
            "secret",
        }
        if forbidden.intersection(configuration_value):
            raise ValueError("endpoint configuration contains a forbidden secret field")
        configuration_sha256 = hashlib.sha256(
            canonical_json_bytes(configuration_value)
        ).hexdigest()
        suite_value = dict(suite_document)
        suite_sha256 = hashlib.sha256(canonical_json_bytes(suite_value)).hexdigest()
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO lexsond.endpoint_snapshots (
                        endpoint_snapshot_id, provider_id, protocol, base_url,
                        model, credential_ref, configuration_sha256,
                        configuration_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (endpoint_snapshot_id) DO NOTHING
                    """,
                    (
                        endpoint_snapshot_id,
                        provider_id,
                        protocol,
                        base_url,
                        model,
                        credential_ref,
                        configuration_sha256,
                        Jsonb(configuration_value),
                    ),
                )
                endpoint = connection.execute(
                    """
                    SELECT provider_id, protocol, base_url, model,
                           credential_ref, configuration_sha256,
                           configuration_json
                    FROM lexsond.endpoint_snapshots
                    WHERE endpoint_snapshot_id = %s
                    """,
                    (endpoint_snapshot_id,),
                ).fetchone()
                if endpoint is None or not _endpoint_snapshot_matches(
                    endpoint,
                    provider_id=provider_id,
                    protocol=protocol,
                    base_url=base_url,
                    model=model,
                    credential_ref=credential_ref,
                    configuration_sha256=configuration_sha256,
                    configuration=configuration_value,
                ):
                    raise WorkflowJournalIntegrityError(
                        "endpoint snapshot identifier conflicts with stored content"
                    )

                connection.execute(
                    """
                    INSERT INTO lexsond.probe_suite_snapshots (
                        suite_sha256, suite_name, suite_version, suite_uri,
                        suite_json
                    ) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (suite_sha256) DO NOTHING
                    """,
                    (
                        suite_sha256,
                        suite.name,
                        suite.version,
                        suite_uri,
                        Jsonb(suite_value),
                    ),
                )
                stored_suite = connection.execute(
                    """
                    SELECT suite_name, suite_version, suite_uri, suite_json
                    FROM lexsond.probe_suite_snapshots
                    WHERE suite_sha256 = %s
                    """,
                    (suite_sha256,),
                ).fetchone()
                if stored_suite is None or not _suite_snapshot_matches(
                    stored_suite,
                    suite_name=suite.name,
                    suite_version=suite.version,
                    suite_uri=suite_uri,
                    suite_document=suite_value,
                ):
                    raise WorkflowJournalIntegrityError(
                        "suite snapshot digest conflicts with stored content"
                    )
        except WorkflowJournalIntegrityError:
            raise
        except psycopg.Error as exc:
            raise WorkflowJournalIntegrityError(
                "PostgreSQL rejected a control-plane snapshot"
            ) from exc
        return configuration_sha256, suite_sha256


class PostgresEndpointSnapshotResolver:
    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def resolve(self, endpoint_snapshot_id: str) -> EndpointSnapshot:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT endpoint_snapshot_id, protocol, base_url, model,
                       credential_ref, configuration_sha256, configuration_json
                FROM lexsond.endpoint_snapshots
                WHERE endpoint_snapshot_id = %s
                """,
                (endpoint_snapshot_id,),
            ).fetchone()
        if row is None:
            raise LookupError("endpoint snapshot was not found")
        digest = hashlib.sha256(
            canonical_json_bytes(row["configuration_json"])
        ).hexdigest()
        if digest != row["configuration_sha256"].strip():
            raise LookupError("endpoint snapshot digest is invalid")
        try:
            return EndpointSnapshot(
                endpoint_snapshot_id=row["endpoint_snapshot_id"],
                protocol=row["protocol"],
                base_url=row["base_url"],
                model=row["model"],
                credential_handle=row["credential_ref"],
            )
        except ValueError as exc:
            raise LookupError("endpoint snapshot is invalid") from exc


class PostgresSuiteDocumentResolver:
    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def read(self, suite_uri: str) -> bytes:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT suite_sha256, suite_json
                FROM lexsond.probe_suite_snapshots
                WHERE suite_uri = %s
                """,
                (suite_uri,),
            ).fetchone()
        if row is None:
            raise LookupError("suite snapshot was not found")
        content = canonical_json_bytes(row["suite_json"])
        if hashlib.sha256(content).hexdigest() != row["suite_sha256"].strip():
            raise LookupError("suite snapshot digest is invalid")
        return content


class PostgresCanaryRuntimeStore:
    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def claim(
        self,
        invocation: ActivityInvocation,
        *,
        lease_seconds: float,
    ) -> ActivityClaim:
        lease_seconds = validate_lease_seconds(lease_seconds)
        proposed_token = str(uuid4())
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT * FROM lexsond.claim_activity_execution(
                        %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        invocation.idempotency_key,
                        invocation.run_id,
                        invocation.activity_name.value,
                        invocation.input_ref,
                        invocation.attempt,
                        proposed_token,
                        math.ceil(lease_seconds),
                    ),
                ).fetchone()
        except psycopg.Error as exc:
            raise _runtime_store_exception(
                "PostgreSQL rejected the Activity claim", exc
            ) from exc
        if row is None:
            raise CanaryRuntimeStoreIntegrityError(
                "PostgreSQL Activity claim returned no disposition"
            )
        try:
            disposition = ActivityClaimDisposition(row["disposition"])
            if disposition is ActivityClaimDisposition.ACQUIRED:
                return ActivityClaim(
                    disposition,
                    lease_token=str(row["returned_lease_token"]),
                )
            if disposition is ActivityClaimDisposition.COMPLETED:
                return ActivityClaim(
                    disposition,
                    outcome=ActivityOutcome(
                        ActivityOutcomeStatus(row["outcome_status"]),
                        row["result_ref"],
                    ),
                )
            if disposition is ActivityClaimDisposition.FAILED:
                return ActivityClaim(
                    disposition,
                    failure=ActivityFailureRecord(
                        row["error_code"],
                        kind=FailureKind(row["failure_kind"]),
                        retryable=row["retryable"],
                    ),
                )
            return ActivityClaim(
                ActivityClaimDisposition.BUSY,
                retry_after_seconds=row["retry_after_seconds"],
            )
        except (TypeError, ValueError) as exc:
            raise CanaryRuntimeStoreIntegrityError(
                "PostgreSQL returned an invalid Activity claim"
            ) from exc

    def complete(
        self,
        invocation: ActivityInvocation,
        *,
        lease_token: str,
        outcome: ActivityOutcome,
    ) -> None:
        _uuid(lease_token, "lease_token")
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    """
                    SELECT lexsond.complete_activity_execution(
                        %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        invocation.idempotency_key,
                        invocation.run_id,
                        invocation.activity_name.value,
                        invocation.input_ref,
                        invocation.attempt,
                        lease_token,
                        outcome.status.value,
                        outcome.result_ref,
                    ),
                )
        except psycopg.Error as exc:
            raise _runtime_store_exception(
                "PostgreSQL rejected Activity completion", exc
            ) from exc

    def renew(
        self,
        invocation: ActivityInvocation,
        *,
        lease_token: str,
        lease_seconds: float,
    ) -> None:
        _uuid(lease_token, "lease_token")
        lease_seconds = validate_lease_seconds(lease_seconds)
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    """
                    SELECT lexsond.renew_activity_execution(
                        %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        invocation.idempotency_key,
                        invocation.run_id,
                        invocation.activity_name.value,
                        invocation.input_ref,
                        invocation.attempt,
                        lease_token,
                        math.ceil(lease_seconds),
                    ),
                )
        except psycopg.Error as exc:
            raise _runtime_store_exception(
                "PostgreSQL rejected Activity lease renewal", exc
            ) from exc

    def fail(
        self,
        invocation: ActivityInvocation,
        *,
        lease_token: str,
        failure: ActivityFailureRecord,
    ) -> None:
        _uuid(lease_token, "lease_token")
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    """
                    SELECT lexsond.fail_activity_execution(
                        %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        invocation.idempotency_key,
                        invocation.run_id,
                        invocation.activity_name.value,
                        invocation.input_ref,
                        invocation.attempt,
                        lease_token,
                        failure.error_code,
                        failure.kind.value,
                        failure.retryable,
                    ),
                )
        except psycopg.Error as exc:
            raise _runtime_store_exception(
                "PostgreSQL rejected Activity failure", exc
            ) from exc

    def persist_result(
        self,
        *,
        run_id: str,
        result_ref: str,
        result: Mapping[str, Any],
    ) -> str:
        _uuid(run_id, "run_id")
        if not isinstance(result_ref, str) or not result_ref:
            raise ValueError("result_ref must be non-empty")
        validate_sanitized_result(run_id, result)
        digest = hashlib.sha256(canonical_json_bytes(result)).hexdigest()
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    "SELECT lexsond.persist_probe_result(%s, %s, %s, %s)",
                    (run_id, result_ref, digest, Jsonb(dict(result))),
                )
        except psycopg.Error as exc:
            raise _runtime_store_exception(
                "PostgreSQL rejected the immutable probe result", exc
            ) from exc
        return result_ref

    def load_result(self, run_id: str) -> dict[str, Any] | None:
        _uuid(run_id, "run_id")
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT result_sha256, normalized_result
                FROM lexsond.probe_results WHERE run_id = %s
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        value = row["normalized_result"]
        digest = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
        if digest != row["result_sha256"].strip():
            raise CanaryRuntimeStoreIntegrityError(
                "stored probe result digest mismatch"
            )
        validate_sanitized_result(run_id, value)
        return value


class PostgresEvidenceManifestRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def add(self, manifest: EvidenceManifest) -> None:
        if not isinstance(manifest, EvidenceManifest):
            raise ValueError("manifest must be an EvidenceManifest")
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO lexsond.evidence_objects (
                        evidence_id, run_id, evidence_kind, object_uri,
                        object_sha256, byte_size, media_type, redaction_status,
                        encrypted, retention_until, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (run_id, evidence_kind, object_sha256, object_uri)
                    DO NOTHING
                    """,
                    (
                        manifest.evidence_id,
                        manifest.run_id,
                        manifest.evidence_kind.value,
                        manifest.object_uri,
                        manifest.object_sha256,
                        manifest.byte_size,
                        manifest.media_type,
                        manifest.redaction_status.value,
                        manifest.encrypted,
                        manifest.retention_until,
                        manifest.created_at,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT run_id::TEXT, evidence_kind, object_uri,
                           object_sha256, byte_size, media_type,
                           redaction_status, encrypted, retention_until,
                           created_at, deleted_at
                    FROM lexsond.evidence_objects
                    WHERE run_id = %s AND evidence_kind = %s
                      AND object_sha256 = %s AND object_uri = %s
                    """,
                    (
                        manifest.run_id,
                        manifest.evidence_kind.value,
                        manifest.object_sha256,
                        manifest.object_uri,
                    ),
                ).fetchone()
                expected = (
                    manifest.run_id,
                    manifest.evidence_kind.value,
                    manifest.object_uri,
                    manifest.object_sha256,
                    manifest.byte_size,
                    manifest.media_type,
                    manifest.redaction_status.value,
                    manifest.encrypted,
                    (
                        datetime.fromisoformat(
                            manifest.retention_until.replace("Z", "+00:00")
                        )
                        if manifest.retention_until is not None
                        else None
                    ),
                    datetime.fromisoformat(
                        manifest.created_at.replace("Z", "+00:00")
                    ),
                    None,
                )
                actual = tuple(row.values()) if row is not None else None
                if actual != expected:
                    raise CanaryRuntimeStoreIntegrityError(
                        "evidence_id belongs to a conflicting manifest"
                    )
        except CanaryRuntimeStoreIntegrityError:
            raise
        except psycopg.Error as exc:
            raise CanaryRuntimeStoreIntegrityError(
                "PostgreSQL rejected the evidence manifest"
            ) from exc


def _decode_events(
    run_id: str, rows: list[dict[str, Any]]
) -> tuple[WorkflowEvent, ...]:
    events: list[WorkflowEvent] = []
    for expected_sequence, row in enumerate(rows, start=1):
        if row["sequence"] != expected_sequence:
            raise WorkflowJournalCorruption("stored event sequence is not contiguous")
        try:
            event = WorkflowEvent.from_dict(row["event_json"])
        except ValueError as exc:
            raise WorkflowJournalCorruption(
                f"stored workflow event {expected_sequence} is invalid"
            ) from exc
        if (
            event.run_id != run_id
            or event.sequence != row["sequence"]
            or event.event_id != str(row["event_id"])
        ):
            raise WorkflowJournalCorruption(
                "stored event columns do not match the event JSON"
            )
        events.append(event)
    return tuple(events)


def _endpoint_snapshot_matches(
    row: Mapping[str, Any],
    *,
    provider_id: str,
    protocol: str,
    base_url: str,
    model: str,
    credential_ref: str,
    configuration_sha256: str,
    configuration: Mapping[str, Any],
) -> bool:
    return (
        row["provider_id"] == provider_id
        and row["protocol"] == protocol
        and row["base_url"] == base_url
        and row["model"] == model
        and row["credential_ref"] == credential_ref
        and row["configuration_sha256"].strip() == configuration_sha256
        and row["configuration_json"] == configuration
    )


def _suite_snapshot_matches(
    row: Mapping[str, Any],
    *,
    suite_name: str,
    suite_version: str,
    suite_uri: str,
    suite_document: Mapping[str, Any],
) -> bool:
    return (
        row["suite_name"] == suite_name
        and row["suite_version"] == suite_version
        and row["suite_uri"] == suite_uri
        and row["suite_json"] == suite_document
    )


def _uuid(value: str, field_name: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc


def _runtime_store_exception(message: str, exc: psycopg.Error) -> RuntimeError:
    sqlstate = exc.sqlstate or ""
    if sqlstate[:2] in {"22", "23"} or sqlstate in {"44000", "55000"}:
        return CanaryRuntimeStoreIntegrityError(message)
    return RuntimeError(message)
