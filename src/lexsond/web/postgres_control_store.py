from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from ..storage.postgres import PostgresPool
from ..monitoring.state import MonitorState, MonitorStatus, transition_state
from ..storage.redaction import redact_text, redact_value
from ..storage.runtime_contracts import validate_sanitized_result
from ..suite import compile_suite
from .control_contracts import (
    ControlPlaneConflict,
    ControlPlaneNotFound,
    _contains_forbidden_agent_key,
    _aggregate_monitor_buckets,
    _monitor_metrics,
    _monitor_observation,
    _monitor_timeline,
    _monitor_window,
    _next_schedule,
    _parse_utc,
    _schedule_offset,
    _validate_monitor_policy_value,
    _require_agent_turn_token,
    _validate_agent_event_fields,
    _validate_agent_session_changes,
    _validate_agent_session_value,
)


LEGACY_WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"


class PostgresControlPlaneStore:
    """PostgreSQL implementation of the mutable Web repository contract."""

    def __init__(self, pool: PostgresPool, *, owns_pool: bool = False) -> None:
        self._pool = pool
        self._owns_pool = owns_pool

    @classmethod
    def from_dsn(cls, dsn: str) -> PostgresControlPlaneStore:
        return cls(
            PostgresPool(dsn, application_name="lexsond-control-store"),
            owns_pool=True,
        )

    def close(self) -> None:
        if self._owns_pool:
            self._pool.close()

    def for_workspace(self, workspace_id: str) -> WorkspaceControlPlaneStore:
        """Bind every user-facing repository operation to one workspace."""

        try:
            normalized = str(UUID(str(workspace_id)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("workspace_id must be a UUID") from exc
        return WorkspaceControlPlaneStore(self, normalized)

    def authentication_store(self) -> Any:
        from .postgres_auth_store import PostgresAuthStore

        return PostgresAuthStore(self._pool)

    def evaluation_store(self) -> Any:
        from .postgres_evaluation_store import PostgresEvaluationStore

        return PostgresEvaluationStore(self._pool)

    # Credential profiles (metadata only; secret material lives in a vault)

    def find_credential_profile_by_idempotency(
        self,
        idempotency_key: str,
        request_sha256: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
    ) -> dict[str, Any] | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM lexsond.credential_profiles
                WHERE workspace_id = %s AND idempotency_key = %s
                """,
                (workspace_id, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_sha256:
            raise ControlPlaneConflict(
                "idempotency key was already used for a different credential profile"
            )
        return _credential_profile(row)

    def create_credential_profile(
        self,
        value: Mapping[str, Any],
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
    ) -> dict[str, Any]:
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO lexsond.credential_profiles (
                        credential_id, workspace_id, label, provider_id,
                        storage_backend, secret_locator, masked_suffix,
                        fingerprint, idempotency_key, request_sha256
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        value["credential_id"],
                        workspace_id,
                        value["label"],
                        value["provider_id"],
                        value["storage_backend"],
                        value["secret_locator"],
                        value["masked_suffix"],
                        value["fingerprint"],
                        value["idempotency_key"],
                        value["request_sha256"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO lexsond.credential_audit_events (
                        event_id, workspace_id, credential_id, actor_user_id,
                        action, outcome
                    ) VALUES (%s, %s, %s, %s, 'CREATE', 'SUCCESS')
                    """,
                    (
                        str(uuid4()),
                        workspace_id,
                        value["credential_id"],
                        value["actor_user_id"],
                    ),
                )
        except psycopg.errors.UniqueViolation as exc:
            replay = self.find_credential_profile_by_idempotency(
                value["idempotency_key"],
                value["request_sha256"],
                workspace_id=workspace_id,
            )
            if replay is not None:
                return replay
            raise ControlPlaneConflict(
                "credential label or fingerprint already exists"
            ) from exc
        return self.get_credential_profile(
            value["credential_id"],
            workspace_id=workspace_id,
            include_archived=True,
        )

    def get_credential_profile(
        self,
        credential_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM lexsond.credential_profiles
                WHERE credential_id = %s AND workspace_id = %s
                """,
                (credential_id, workspace_id),
            ).fetchone()
        if row is None or (row["archived_at"] is not None and not include_archived):
            raise ControlPlaneNotFound("credential profile was not found")
        return _credential_profile(row)

    def list_credential_profiles(
        self,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        archived = "" if include_archived else "AND archived_at IS NULL"
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM lexsond.credential_profiles
                WHERE workspace_id = %s {archived}
                ORDER BY updated_at DESC, credential_id
                """,
                (workspace_id,),
            ).fetchall()
        return [_credential_profile(row) for row in rows]

    def get_credential_locator(
        self,
        credential_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
    ) -> UUID:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT secret_locator FROM lexsond.credential_profiles
                WHERE credential_id = %s AND workspace_id = %s
                  AND archived_at IS NULL
                """,
                (credential_id, workspace_id),
            ).fetchone()
        if row is None:
            raise ControlPlaneNotFound("credential profile was not found")
        return UUID(str(row["secret_locator"]))

    def update_credential_profile(
        self,
        credential_id: str,
        changes: Mapping[str, Any],
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        expected_version: int,
        audit_action: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        allowed = {
            "label",
            "fingerprint",
            "masked_suffix",
            "status",
            "last_verified_at",
            "last_used_at",
            "archived_at",
        }
        if not changes or not set(changes).issubset(allowed):
            raise ValueError("credential update contains unknown fields")
        if audit_action not in {"RENAME", "REPLACE", "VERIFY", "ARCHIVE", "DELETE_SECRET"}:
            raise ValueError("credential audit action is invalid")
        normalized = {
            key: (datetime.now(UTC) if key == "archived_at" and value == "NOW" else value)
            for key, value in changes.items()
        }
        assignments = ", ".join(f"{key} = %s" for key in normalized)
        params = [*normalized.values(), credential_id, workspace_id, expected_version]
        try:
            with self._pool.connection() as connection:
                cursor = connection.execute(
                    f"""
                    UPDATE lexsond.credential_profiles
                    SET {assignments}, version = version + 1,
                        updated_at = clock_timestamp()
                    WHERE credential_id = %s AND workspace_id = %s
                      AND version = %s AND archived_at IS NULL
                    """,
                    params,
                )
                if cursor.rowcount != 1:
                    exists = connection.execute(
                        """
                        SELECT 1 FROM lexsond.credential_profiles
                        WHERE credential_id = %s AND workspace_id = %s
                        """,
                        (credential_id, workspace_id),
                    ).fetchone()
                    if exists is None:
                        raise ControlPlaneNotFound("credential profile was not found")
                    raise ControlPlaneConflict("credential profile version is stale")
                connection.execute(
                    """
                    INSERT INTO lexsond.credential_audit_events (
                        event_id, workspace_id, credential_id, actor_user_id,
                        action, outcome
                    ) VALUES (%s, %s, %s, %s, %s, 'SUCCESS')
                    """,
                    (
                        str(uuid4()),
                        workspace_id,
                        credential_id,
                        actor_user_id,
                        audit_action,
                    ),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise ControlPlaneConflict(
                "credential label or fingerprint already exists"
            ) from exc
        return self.get_credential_profile(
            credential_id, workspace_id=workspace_id, include_archived=True
        )

    # Catalog snapshots and bounded multi-model batches

    def create_model_catalog_snapshot(
        self,
        value: Mapping[str, Any],
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
    ) -> dict[str, Any]:
        snapshot_id = str(value.get("snapshot_id") or uuid4())
        models = list(value["models"])
        content_sha256 = hashlib.sha256(
            json.dumps(
                models, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()
        with self._pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO lexsond.model_catalog_snapshots (
                    snapshot_id, workspace_id, target_id,
                    credential_profile_id, target_version, provider_id,
                    credential_fingerprint, credential_version,
                    target_base_url, target_kind, protocol,
                    models_json, model_count, content_sha256, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    clock_timestamp() + INTERVAL '15 minutes')
                """,
                (
                    snapshot_id,
                    workspace_id,
                    value["target_id"],
                    value.get("credential_profile_id"),
                    value["target_version"],
                    value.get("provider_id"),
                    value.get("credential_fingerprint"),
                    value.get("credential_version"),
                    value["target_base_url"],
                    value["target_kind"],
                    value["protocol"],
                    Jsonb(models),
                    len(models),
                    content_sha256,
                ),
            )
        return self.get_model_catalog_snapshot(
            snapshot_id, workspace_id=workspace_id
        )

    def get_model_catalog_snapshot(
        self,
        snapshot_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT *, expires_at <= clock_timestamp() AS expired
                FROM lexsond.model_catalog_snapshots
                WHERE snapshot_id = %s AND workspace_id = %s
                """,
                (snapshot_id, workspace_id),
            ).fetchone()
        if row is None:
            raise ControlPlaneNotFound("model catalog snapshot was not found")
        return _model_catalog_snapshot(row)

    def find_probe_batch_by_idempotency(
        self,
        idempotency_key: str,
        request_sha256: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
    ) -> dict[str, Any] | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT batch_id, request_sha256 FROM lexsond.probe_batches
                WHERE workspace_id = %s AND idempotency_key = %s
                """,
                (workspace_id, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        if row["request_sha256"].strip() != request_sha256:
            raise ControlPlaneConflict(
                "idempotency key was already used for another probe batch"
            )
        return self.get_probe_batch(str(row["batch_id"]), workspace_id=workspace_id)

    def create_probe_batch(
        self,
        value: Mapping[str, Any],
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
    ) -> dict[str, Any]:
        replay = self.find_probe_batch_by_idempotency(
            value["idempotency_key"],
            value["request_sha256"],
            workspace_id=workspace_id,
        )
        if replay is not None:
            return replay
        batch_id = str(value["batch_id"])
        model_ids = list(value["model_ids"])
        catalog_only = value["mode"] == "catalog_only"
        try:
            with self._pool.connection() as connection:
                snapshot = connection.execute(
                    """
                    SELECT * FROM lexsond.model_catalog_snapshots
                    WHERE snapshot_id = %s AND workspace_id = %s
                    FOR SHARE
                    """,
                    (value["catalog_snapshot_id"], workspace_id),
                ).fetchone()
                if snapshot is None:
                    raise ControlPlaneNotFound("model catalog snapshot was not found")
                if str(snapshot["target_id"]) != str(value["target_id"]):
                    raise ControlPlaneConflict(
                        "catalog snapshot belongs to another channel"
                    )
                snapshot_profile = (
                    str(snapshot["credential_profile_id"])
                    if snapshot["credential_profile_id"] is not None
                    else None
                )
                requested_profile = (
                    str(value["credential_profile_id"])
                    if value.get("credential_profile_id") is not None
                    else None
                )
                if snapshot_profile != requested_profile:
                    raise ControlPlaneConflict(
                        "batch credential does not match the catalog snapshot"
                    )
                if snapshot["expires_at"] <= datetime.now(UTC):
                    raise ControlPlaneConflict("model catalog snapshot is stale")
                visible_ids = {
                    str(entry.get("id"))
                    for entry in snapshot["models_json"]
                    if isinstance(entry, Mapping) and entry.get("id") is not None
                }
                if not set(model_ids).issubset(visible_ids):
                    raise ControlPlaneConflict(
                        "batch models must come from the visible catalog snapshot"
                    )
                initial_state = "COMPLETED" if catalog_only else "RUNNING"
                connection.execute(
                    """
                    INSERT INTO lexsond.probe_batches (
                        batch_id, workspace_id, target_id,
                        credential_profile_id, catalog_snapshot_id,
                        suite_revision_id, mode, state, model_count,
                        max_concurrency, max_output_tokens, timeout_seconds,
                        confirm_unknown_cost, idempotency_key, request_sha256,
                        finished_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s,
                        CASE WHEN %s THEN clock_timestamp() ELSE NULL END)
                    """,
                    (
                        batch_id,
                        workspace_id,
                        value["target_id"],
                        value.get("credential_profile_id"),
                        value["catalog_snapshot_id"],
                        value.get("suite_revision_id"),
                        value["mode"],
                        initial_state,
                        len(model_ids),
                        value["max_concurrency"],
                        value["max_output_tokens"],
                        value["timeout_seconds"],
                        value["confirm_unknown_cost"],
                        value["idempotency_key"],
                        value["request_sha256"],
                        catalog_only,
                    ),
                )
                for ordinal, model_id in enumerate(model_ids, start=1):
                    item_id = str(uuid4())
                    connection.execute(
                        """
                        INSERT INTO lexsond.probe_batch_items (
                            item_id, workspace_id, batch_id, ordinal,
                            model_id, state, started_at, finished_at
                        ) VALUES (%s, %s, %s, %s, %s, %s,
                            CASE WHEN %s THEN clock_timestamp() ELSE NULL END,
                            CASE WHEN %s THEN clock_timestamp() ELSE NULL END)
                        """,
                        (
                            item_id, workspace_id, batch_id, ordinal, model_id,
                            "COMPLETED" if catalog_only else "PENDING",
                            catalog_only, catalog_only,
                        ),
                    )
                    _append_probe_batch_event(
                        connection,
                        workspace_id=workspace_id,
                        batch_id=batch_id,
                        event_type=(
                            "CATALOG_MODEL_CONFIRMED"
                            if catalog_only
                            else "MODEL_QUEUED"
                        ),
                        state="COMPLETED" if catalog_only else "PENDING",
                        item_id=item_id,
                        model_id=model_id,
                    )
        except psycopg.errors.UniqueViolation as exc:
            replay = self.find_probe_batch_by_idempotency(
                value["idempotency_key"],
                value["request_sha256"],
                workspace_id=workspace_id,
            )
            if replay is not None:
                return replay
            raise ControlPlaneConflict("probe batch already exists") from exc
        except psycopg.errors.ForeignKeyViolation as exc:
            raise ControlPlaneConflict(
                "batch resources must belong to the current workspace"
            ) from exc
        return self.get_probe_batch(batch_id, workspace_id=workspace_id)

    def get_probe_batch(
        self,
        batch_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM lexsond.probe_batches
                WHERE batch_id = %s AND workspace_id = %s
                """,
                (batch_id, workspace_id),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("probe batch was not found")
            items = connection.execute(
                """
                SELECT * FROM lexsond.probe_batch_items
                WHERE batch_id = %s AND workspace_id = %s
                ORDER BY ordinal
                """,
                (batch_id, workspace_id),
            ).fetchall()
        return _probe_batch(row, items)

    def list_probe_batches(
        self,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        bounded = min(max(int(limit), 1), 100)
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM lexsond.probe_batches
                WHERE workspace_id = %s
                ORDER BY created_at DESC, batch_id DESC LIMIT %s
                """,
                (workspace_id, bounded),
            ).fetchall()
        return [
            self.get_probe_batch(str(row["batch_id"]), workspace_id=workspace_id)
            for row in rows
        ]

    def start_probe_batch_item(
        self,
        batch_id: str,
        item_id: str,
        run_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
    ) -> None:
        with self._pool.connection() as connection:
            batch = connection.execute(
                """
                SELECT state, cancel_requested_at FROM lexsond.probe_batches
                WHERE batch_id = %s AND workspace_id = %s FOR UPDATE
                """,
                (batch_id, workspace_id),
            ).fetchone()
            if batch is None:
                raise ControlPlaneNotFound("probe batch was not found")
            if batch["state"] != "RUNNING" or batch["cancel_requested_at"] is not None:
                raise ControlPlaneConflict("probe batch no longer accepts work")
            cursor = connection.execute(
                """
                UPDATE lexsond.probe_batch_items
                SET state = 'RUNNING', run_id = %s,
                    started_at = clock_timestamp()
                WHERE item_id = %s AND batch_id = %s AND workspace_id = %s
                  AND state = 'PENDING'
                """,
                (run_id, item_id, batch_id, workspace_id),
            )
            if cursor.rowcount != 1:
                raise ControlPlaneConflict("probe batch item is not pending")
            item = connection.execute(
                "SELECT model_id FROM lexsond.probe_batch_items WHERE item_id = %s",
                (item_id,),
            ).fetchone()
            _append_probe_batch_event(
                connection,
                workspace_id=workspace_id,
                batch_id=batch_id,
                event_type="MODEL_STARTED",
                state="RUNNING",
                item_id=item_id,
                model_id=item["model_id"],
            )

    def finish_probe_batch_item(
        self,
        batch_id: str,
        item_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        state: str,
        failure_code: str | None = None,
    ) -> None:
        if state not in {"COMPLETED", "FAILED", "CANCELLED", "SKIPPED"}:
            raise ValueError("probe batch item terminal state is invalid")
        with self._pool.connection() as connection:
            connection.execute(
                "SELECT 1 FROM lexsond.probe_batches WHERE batch_id = %s AND workspace_id = %s FOR UPDATE",
                (batch_id, workspace_id),
            )
            cursor = connection.execute(
                """
                UPDATE lexsond.probe_batch_items
                SET state = %s, failure_code = %s,
                    started_at = COALESCE(started_at, clock_timestamp()),
                    finished_at = clock_timestamp()
                WHERE item_id = %s AND batch_id = %s AND workspace_id = %s
                  AND state IN ('PENDING', 'RUNNING')
                RETURNING model_id
                """,
                (state, failure_code, item_id, batch_id, workspace_id),
            )
            item = cursor.fetchone()
            if item is None:
                raise ControlPlaneConflict("probe batch item is already terminal")
            _append_probe_batch_event(
                connection,
                workspace_id=workspace_id,
                batch_id=batch_id,
                event_type=f"MODEL_{state}",
                state=state,
                item_id=item_id,
                model_id=item["model_id"],
            )

    def request_probe_batch_cancel(
        self,
        batch_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                UPDATE lexsond.probe_batches
                SET cancel_requested_at = COALESCE(
                    cancel_requested_at, clock_timestamp()
                )
                WHERE batch_id = %s AND workspace_id = %s AND state = 'RUNNING'
                RETURNING batch_id
                """,
                (batch_id, workspace_id),
            ).fetchone()
            if row is None:
                existing = connection.execute(
                    "SELECT 1 FROM lexsond.probe_batches WHERE batch_id = %s AND workspace_id = %s",
                    (batch_id, workspace_id),
                ).fetchone()
                if existing is None:
                    raise ControlPlaneNotFound("probe batch was not found")
        return self.get_probe_batch(batch_id, workspace_id=workspace_id)

    def finalize_probe_batch(
        self,
        batch_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
    ) -> dict[str, Any]:
        already_terminal = False
        with self._pool.connection() as connection:
            batch = connection.execute(
                """
                SELECT * FROM lexsond.probe_batches
                WHERE batch_id = %s AND workspace_id = %s FOR UPDATE
                """,
                (batch_id, workspace_id),
            ).fetchone()
            if batch is None:
                raise ControlPlaneNotFound("probe batch was not found")
            if batch["state"] != "RUNNING":
                already_terminal = True
            counts = [] if already_terminal else connection.execute(
                """
                SELECT state, COUNT(*) AS count
                FROM lexsond.probe_batch_items
                WHERE batch_id = %s AND workspace_id = %s GROUP BY state
                """,
                (batch_id, workspace_id),
            ).fetchall()
            if already_terminal:
                pass
            else:
                by_state = {row["state"]: int(row["count"]) for row in counts}
                if by_state.get("PENDING", 0) or by_state.get("RUNNING", 0):
                    raise ControlPlaneConflict("probe batch still has active items")
                completed = by_state.get("COMPLETED", 0)
                failed = by_state.get("FAILED", 0)
                cancelled = by_state.get("CANCELLED", 0) + by_state.get("SKIPPED", 0)
                if batch["cancel_requested_at"] is not None or cancelled:
                    state = "CANCELLED"
                elif completed and failed:
                    state = "PARTIAL"
                elif completed:
                    state = "COMPLETED"
                else:
                    state = "FAILED"
                connection.execute(
                    """
                    UPDATE lexsond.probe_batches
                    SET state = %s, finished_at = clock_timestamp()
                    WHERE batch_id = %s AND workspace_id = %s
                    """,
                    (state, batch_id, workspace_id),
                )
                _append_probe_batch_event(
                    connection,
                    workspace_id=workspace_id,
                    batch_id=batch_id,
                    event_type="BATCH_FINISHED",
                    state=state,
                )
        return self.get_probe_batch(batch_id, workspace_id=workspace_id)

    def list_probe_batch_events(
        self,
        batch_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        with self._pool.connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM lexsond.probe_batches WHERE batch_id = %s AND workspace_id = %s",
                (batch_id, workspace_id),
            ).fetchone()
            if exists is None:
                raise ControlPlaneNotFound("probe batch was not found")
            rows = connection.execute(
                """
                SELECT * FROM lexsond.probe_batch_events
                WHERE batch_id = %s AND workspace_id = %s AND sequence > %s
                ORDER BY sequence LIMIT %s
                """,
                (batch_id, workspace_id, max(after_sequence, 0), min(max(limit, 1), 500)),
            ).fetchall()
        return [_probe_batch_event(row) for row in rows]

    # Targets

    # Partner onboarding

    def find_partner_application_by_idempotency(
        self,
        idempotency_key: str,
        request_sha256: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
    ) -> dict[str, Any] | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM lexsond.partner_applications
                WHERE workspace_id = %s AND idempotency_key = %s
                """,
                (workspace_id, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_sha256:
            raise ControlPlaneConflict(
                "idempotency key was already used for another partner application"
            )
        return _partner_application(row)

    def create_partner_application(
        self,
        value: Mapping[str, Any],
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
    ) -> dict[str, Any]:
        replay = self.find_partner_application_by_idempotency(
            value["idempotency_key"],
            value["request_sha256"],
            workspace_id=workspace_id,
        )
        if replay is not None:
            return replay
        application_id = str(uuid4())
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO lexsond.partner_applications (
                        application_id, workspace_id, site_name, website_url,
                        terms_url, privacy_url, contact_email, api_base_url,
                        protocol, region, model_claims, pricing_notes,
                        source_evidence_url, monitoring_credential_id,
                        idempotency_key, request_sha256
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        application_id,
                        workspace_id,
                        value["site_name"],
                        value["website_url"],
                        value["terms_url"],
                        value["privacy_url"],
                        value["contact_email"],
                        value["api_base_url"],
                        value["protocol"],
                        value["region"],
                        Jsonb(value["model_claims"]),
                        value["pricing_notes"],
                        value["source_evidence_url"],
                        value.get("monitoring_credential_id"),
                        value["idempotency_key"],
                        value["request_sha256"],
                    ),
                )
        except psycopg.errors.UniqueViolation as exc:
            replay = self.find_partner_application_by_idempotency(
                value["idempotency_key"],
                value["request_sha256"],
                workspace_id=workspace_id,
            )
            if replay is not None:
                return replay
            raise ControlPlaneConflict("partner application already exists") from exc
        except psycopg.errors.ForeignKeyViolation as exc:
            raise ControlPlaneConflict(
                "monitoring credential must belong to the current workspace"
            ) from exc
        return self.get_partner_application(
            application_id, workspace_id=workspace_id
        )

    def get_partner_application(
        self,
        application_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM lexsond.partner_applications
                WHERE application_id = %s AND workspace_id = %s
                """,
                (application_id, workspace_id),
            ).fetchone()
        if row is None:
            raise ControlPlaneNotFound("partner application was not found")
        return _partner_application(row)

    def list_partner_applications(
        self,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("partner application limit is out of bounds")
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM lexsond.partner_applications
                WHERE workspace_id = %s
                ORDER BY updated_at DESC, application_id
                LIMIT %s
                """,
                (workspace_id, limit),
            ).fetchall()
        return [_partner_application(row) for row in rows]

    def update_partner_application(
        self,
        application_id: str,
        changes: Mapping[str, Any],
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        expected_version: int,
    ) -> dict[str, Any]:
        allowed = {
            "site_name", "website_url", "terms_url", "privacy_url",
            "contact_email", "api_base_url", "protocol", "region",
            "model_claims", "pricing_notes", "source_evidence_url",
            "monitoring_credential_id",
        }
        if not changes or not set(changes).issubset(allowed):
            raise ValueError("partner application update contains unknown fields")
        normalized = {
            key: Jsonb(value) if key == "model_claims" else value
            for key, value in changes.items()
        }
        assignments = ", ".join(f"{key} = %s" for key in normalized)
        with self._pool.connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE lexsond.partner_applications
                SET {assignments}, version = version + 1,
                    updated_at = clock_timestamp()
                WHERE application_id = %s AND workspace_id = %s
                  AND version = %s AND status = 'DRAFT'
                """,
                (*normalized.values(), application_id, workspace_id, expected_version),
            )
            if cursor.rowcount != 1:
                existing = connection.execute(
                    """
                    SELECT status, version FROM lexsond.partner_applications
                    WHERE application_id = %s AND workspace_id = %s
                    """,
                    (application_id, workspace_id),
                ).fetchone()
                if existing is None:
                    raise ControlPlaneNotFound("partner application was not found")
                if existing["status"] != "DRAFT":
                    raise ControlPlaneConflict(
                        "submitted partner applications are immutable"
                    )
                raise ControlPlaneConflict("partner application version is stale")
        return self.get_partner_application(
            application_id, workspace_id=workspace_id
        )

    def submit_partner_application(
        self,
        application_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        expected_version: int,
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM lexsond.partner_applications
                WHERE application_id = %s AND workspace_id = %s
                FOR UPDATE
                """,
                (application_id, workspace_id),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("partner application was not found")
            if row["status"] != "DRAFT":
                raise ControlPlaneConflict("partner application is already submitted")
            if row["version"] != expected_version:
                raise ControlPlaneConflict("partner application version is stale")
            snapshot = {
                key: row[key]
                for key in (
                    "site_name", "website_url", "terms_url", "privacy_url",
                    "contact_email", "api_base_url", "protocol", "region",
                    "pricing_notes", "source_evidence_url",
                )
            }
            snapshot["model_claims"] = list(row["model_claims"])
            snapshot_sha256 = hashlib.sha256(
                json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            connection.execute(
                """
                INSERT INTO lexsond.partner_application_revisions (
                    revision_id, workspace_id, application_id, revision,
                    snapshot_sha256, snapshot_json
                ) VALUES (%s, %s, %s, 1, %s, %s)
                """,
                (
                    str(uuid4()), workspace_id, application_id,
                    snapshot_sha256, Jsonb(snapshot),
                ),
            )
            connection.execute(
                """
                UPDATE lexsond.partner_applications
                SET status = 'SUBMITTED', submitted_at = clock_timestamp(),
                    updated_at = clock_timestamp(), version = version + 1
                WHERE application_id = %s AND workspace_id = %s
                """,
                (application_id, workspace_id),
            )
        return self.get_partner_application(
            application_id, workspace_id=workspace_id
        )

    def create_target(
        self,
        value: Mapping[str, Any],
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
    ) -> dict[str, Any]:
        target_id = str(uuid4())
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO lexsond.targets (
                        target_id, workspace_id, name, target_kind, provider_id,
                        base_url, default_model, credential_ref
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        target_id,
                        workspace_id,
                        value["name"],
                        value["target_kind"],
                        value.get("provider_id"),
                        value["base_url"],
                        value.get("default_model", ""),
                        value.get("credential_ref"),
                    ),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise ControlPlaneConflict("target name already exists") from exc
        return self.get_target(
            target_id, workspace_id=workspace_id, include_archived=True
        )

    def get_target(
        self,
        target_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM lexsond.targets WHERE target_id = %s AND workspace_id = %s",
                (target_id, workspace_id),
            ).fetchone()
        if row is None or (row["archived_at"] is not None and not include_archived):
            raise ControlPlaneNotFound("target was not found")
        return _target(row)

    def list_targets(
        self,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        archived = "" if include_archived else "AND archived_at IS NULL"
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM lexsond.targets WHERE workspace_id = %s {archived} ORDER BY updated_at DESC, target_id",
                (workspace_id,),
            ).fetchall()
        return [_target(row) for row in rows]

    def update_target(
        self,
        target_id: str,
        changes: Mapping[str, Any],
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        expected_version: int,
    ) -> dict[str, Any]:
        if not changes:
            return self.get_target(
                target_id, workspace_id=workspace_id, include_archived=True
            )
        allowed = {
            "name",
            "target_kind",
            "provider_id",
            "base_url",
            "default_model",
            "credential_ref",
        }
        if set(changes) - allowed:
            raise ValueError("target update contains unknown fields")
        assignments = [f"{field} = %s" for field in changes]
        params = [*changes.values(), target_id, workspace_id, expected_version]
        try:
            with self._pool.connection() as connection:
                cursor = connection.execute(
                    f"""
                    UPDATE lexsond.targets
                    SET {', '.join(assignments)}, version = version + 1,
                        updated_at = clock_timestamp()
                    WHERE target_id = %s AND workspace_id = %s
                      AND version = %s AND archived_at IS NULL
                    """,
                    params,
                )
                if cursor.rowcount != 1:
                    _raise_target_update_conflict(connection, target_id, workspace_id)
        except psycopg.errors.UniqueViolation as exc:
            raise ControlPlaneConflict("target update conflicts with stored data") from exc
        return self.get_target(
            target_id, workspace_id=workspace_id, include_archived=True
        )

    def archive_target(
        self, target_id: str, *, workspace_id: str = LEGACY_WORKSPACE_ID
    ) -> dict[str, Any]:
        self._archive("targets", "target_id", target_id, workspace_id=workspace_id)
        return self.get_target(
            target_id, workspace_id=workspace_id, include_archived=True
        )

    def restore_target(
        self, target_id: str, *, workspace_id: str = LEGACY_WORKSPACE_ID
    ) -> dict[str, Any]:
        self._restore("targets", "target_id", target_id, workspace_id=workspace_id)
        return self.get_target(
            target_id, workspace_id=workspace_id, include_archived=True
        )

    def purge_target(
        self, target_id: str, *, workspace_id: str = LEGACY_WORKSPACE_ID
    ) -> None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT archived_at FROM lexsond.targets WHERE target_id = %s AND workspace_id = %s",
                (target_id, workspace_id),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("target was not found")
            if row["archived_at"] is None:
                raise ControlPlaneConflict("target must be archived before purge")
            if connection.execute(
                "SELECT 1 FROM lexsond.probe_runs WHERE target_id = %s AND workspace_id = %s LIMIT 1",
                (target_id, workspace_id),
            ).fetchone():
                raise ControlPlaneConflict("target is referenced by a run")
            if connection.execute(
                "SELECT 1 FROM lexsond.agent_sessions WHERE target_id = %s AND workspace_id = %s LIMIT 1",
                (target_id, workspace_id),
            ).fetchone():
                raise ControlPlaneConflict("target is referenced by an Agent session")
            if connection.execute(
                "SELECT 1 FROM lexsond.monitor_policies WHERE target_id = %s AND workspace_id = %s LIMIT 1",
                (target_id, workspace_id),
            ).fetchone():
                raise ControlPlaneConflict("target is referenced by a monitor policy")
            connection.execute(
                "DELETE FROM lexsond.targets WHERE target_id = %s AND workspace_id = %s",
                (target_id, workspace_id),
            )

    # Suites and immutable revisions

    def create_suite(
        self,
        value: Mapping[str, Any],
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
    ) -> dict[str, Any]:
        document = _suite_document(value["document"])
        encoded, digest = _document_payload(document)
        suite_id, revision_id = str(uuid4()), str(uuid4())
        try:
            with self._pool.connection() as connection:
                connection.execute("SET CONSTRAINTS ALL DEFERRED")
                connection.execute(
                    """
                    INSERT INTO lexsond.suites (
                        suite_id, workspace_id, name, description, latest_revision_id
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        suite_id,
                        workspace_id,
                        value["name"],
                        value.get("description", ""),
                        revision_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO lexsond.suite_revisions (
                        revision_id, workspace_id, suite_id, revision,
                        document_sha256, document_json
                    ) VALUES (%s, %s, %s, 1, %s, %s)
                    """,
                    (revision_id, workspace_id, suite_id, digest, Jsonb(document)),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise ControlPlaneConflict("suite name or revision already exists") from exc
        return self.get_suite(
            suite_id, workspace_id=workspace_id, include_archived=True
        )

    def get_suite(
        self,
        suite_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT s.*, r.revision_id, r.revision, r.document_sha256,
                       r.document_json, r.created_at AS revision_created_at
                FROM lexsond.suites s
                JOIN lexsond.suite_revisions r
                  ON r.revision_id = s.latest_revision_id
                WHERE s.suite_id = %s AND s.workspace_id = %s
                """,
                (suite_id, workspace_id),
            ).fetchone()
        if row is None or (row["archived_at"] is not None and not include_archived):
            raise ControlPlaneNotFound("suite was not found")
        return _suite(row)

    def list_suites(
        self,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        archived = "" if include_archived else "AND s.archived_at IS NULL"
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT s.*, r.revision_id, r.revision, r.document_sha256,
                       r.document_json, r.created_at AS revision_created_at
                FROM lexsond.suites s
                JOIN lexsond.suite_revisions r
                  ON r.revision_id = s.latest_revision_id
                WHERE s.workspace_id = %s {archived}
                ORDER BY s.updated_at DESC, s.suite_id
                """,
                (workspace_id,),
            ).fetchall()
        return [_suite(row) for row in rows]

    def update_suite(
        self,
        suite_id: str,
        changes: Mapping[str, Any],
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        expected_version: int,
    ) -> dict[str, Any]:
        if set(changes) - {"name", "description", "document"}:
            raise ValueError("suite update contains unknown fields")
        if not changes:
            return self.get_suite(
                suite_id, workspace_id=workspace_id, include_archived=True
            )
        try:
            with self._pool.connection() as connection:
                current = connection.execute(
                    "SELECT * FROM lexsond.suites WHERE suite_id = %s AND workspace_id = %s FOR UPDATE",
                    (suite_id, workspace_id),
                ).fetchone()
                if current is None:
                    raise ControlPlaneNotFound("suite was not found")
                if current["archived_at"] is not None:
                    raise ControlPlaneConflict("archived suite cannot be updated")
                if current["version"] != expected_version:
                    raise ControlPlaneConflict("resource version is stale")
                revision_id = current["latest_revision_id"]
                if "document" in changes:
                    document = _suite_document(changes["document"])
                    _, digest = _document_payload(document)
                    revision = connection.execute(
                        "SELECT COALESCE(MAX(revision), 0) + 1 AS value FROM lexsond.suite_revisions WHERE suite_id = %s AND workspace_id = %s",
                        (suite_id, workspace_id),
                    ).fetchone()["value"]
                    revision_id = str(uuid4())
                    connection.execute(
                        """
                        INSERT INTO lexsond.suite_revisions (
                            revision_id, workspace_id, suite_id, revision,
                            document_sha256, document_json
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            revision_id,
                            workspace_id,
                            suite_id,
                            revision,
                            digest,
                            Jsonb(document),
                        ),
                    )
                connection.execute(
                    """
                    UPDATE lexsond.suites
                    SET name = %s, description = %s, latest_revision_id = %s,
                        version = version + 1, updated_at = clock_timestamp()
                    WHERE suite_id = %s AND workspace_id = %s
                    """,
                    (
                        changes.get("name", current["name"]),
                        changes.get("description", current["description"]),
                        revision_id,
                        suite_id,
                        workspace_id,
                    ),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise ControlPlaneConflict("suite update conflicts with stored data") from exc
        return self.get_suite(
            suite_id, workspace_id=workspace_id, include_archived=True
        )

    def list_suite_revisions(
        self, suite_id: str, *, workspace_id: str = LEGACY_WORKSPACE_ID
    ) -> list[dict[str, Any]]:
        with self._pool.connection() as connection:
            if not connection.execute(
                "SELECT 1 FROM lexsond.suites WHERE suite_id = %s AND workspace_id = %s",
                (suite_id, workspace_id),
            ).fetchone():
                raise ControlPlaneNotFound("suite was not found")
            rows = connection.execute(
                "SELECT * FROM lexsond.suite_revisions WHERE suite_id = %s AND workspace_id = %s ORDER BY revision DESC",
                (suite_id, workspace_id),
            ).fetchall()
        return [_revision(row) for row in rows]

    def get_suite_revision(
        self, revision_id: str, *, workspace_id: str = LEGACY_WORKSPACE_ID
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT r.* FROM lexsond.suite_revisions r
                JOIN lexsond.suites s ON s.suite_id = r.suite_id
                WHERE r.revision_id = %s AND r.workspace_id = %s
                  AND s.archived_at IS NULL
                """,
                (revision_id, workspace_id),
            ).fetchone()
        if row is None:
            raise ControlPlaneNotFound("suite revision was not found")
        return _revision(row)

    def archive_suite(
        self, suite_id: str, *, workspace_id: str = LEGACY_WORKSPACE_ID
    ) -> dict[str, Any]:
        self._archive("suites", "suite_id", suite_id, workspace_id=workspace_id)
        return self.get_suite(
            suite_id, workspace_id=workspace_id, include_archived=True
        )

    def restore_suite(
        self, suite_id: str, *, workspace_id: str = LEGACY_WORKSPACE_ID
    ) -> dict[str, Any]:
        self._restore("suites", "suite_id", suite_id, workspace_id=workspace_id)
        return self.get_suite(
            suite_id, workspace_id=workspace_id, include_archived=True
        )

    def purge_suite(
        self, suite_id: str, *, workspace_id: str = LEGACY_WORKSPACE_ID
    ) -> None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT archived_at FROM lexsond.suites WHERE suite_id = %s AND workspace_id = %s",
                (suite_id, workspace_id),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("suite was not found")
            if row["archived_at"] is None:
                raise ControlPlaneConflict("suite must be archived before purge")
            if connection.execute(
                """
                SELECT 1 FROM lexsond.probe_runs r
                JOIN lexsond.suite_revisions sr
                  ON sr.revision_id = r.suite_revision_id
                WHERE sr.suite_id = %s AND r.workspace_id = %s LIMIT 1
                """,
                (suite_id, workspace_id),
            ).fetchone():
                raise ControlPlaneConflict("suite is referenced by a run")
            if connection.execute(
                """
                SELECT 1 FROM lexsond.monitor_policies p
                JOIN lexsond.suite_revisions sr
                  ON sr.revision_id = p.suite_revision_id
                WHERE sr.suite_id = %s AND p.workspace_id = %s LIMIT 1
                """,
                (suite_id, workspace_id),
            ).fetchone():
                raise ControlPlaneConflict("suite is referenced by a monitor policy")
            connection.execute(
                "DELETE FROM lexsond.suite_revisions WHERE suite_id = %s AND workspace_id = %s",
                (suite_id, workspace_id),
            )
            connection.execute(
                "DELETE FROM lexsond.suites WHERE suite_id = %s AND workspace_id = %s",
                (suite_id, workspace_id),
            )

    # Agent sessions, checkpointer messages, and observable tool events

    def create_agent_session(
        self,
        value: Mapping[str, Any],
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
    ) -> dict[str, Any]:
        _validate_agent_session_value(value)
        if redact_text(str(value["title"])) != value["title"]:
            raise ValueError("Agent session title contains a credential")
        if redact_text(str(value["model"])) != value["model"]:
            raise ValueError("Agent session model contains a credential")
        session_id = str(uuid4())
        with self._pool.connection() as connection:
            target = connection.execute(
                "SELECT archived_at FROM lexsond.targets WHERE target_id = %s AND workspace_id = %s FOR SHARE",
                (value["target_id"], workspace_id),
            ).fetchone()
            if target is None or target["archived_at"] is not None:
                raise ControlPlaneConflict("Agent target is missing or archived")
            connection.execute(
                """
                INSERT INTO lexsond.agent_sessions (
                    session_id, workspace_id, title, target_id, target_version,
                    base_url, target_kind, provider_id, model, skill_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    session_id,
                    workspace_id,
                    value["title"],
                    value["target_id"],
                    value["target_version"],
                    value["base_url"],
                    value["target_kind"],
                    value.get("provider_id"),
                    value["model"],
                    value["skill_id"],
                ),
            )
        return self.get_agent_session(
            session_id, workspace_id=workspace_id, include_archived=True
        )

    def get_agent_session(
        self,
        session_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM lexsond.agent_sessions WHERE session_id = %s AND workspace_id = %s",
                (session_id, workspace_id),
            ).fetchone()
        if row is None or (row["archived_at"] is not None and not include_archived):
            raise ControlPlaneNotFound("Agent session was not found")
        return _agent_session(row)

    def list_agent_sessions(
        self,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        include_archived: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        archived = "" if include_archived else "AND archived_at IS NULL"
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM lexsond.agent_sessions WHERE workspace_id = %s {archived} ORDER BY updated_at DESC, session_id DESC LIMIT %s",
                (workspace_id, min(max(int(limit), 1), 100)),
            ).fetchall()
        return [_agent_session(row) for row in rows]

    def update_agent_session(
        self,
        session_id: str,
        changes: Mapping[str, Any],
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        expected_version: int,
    ) -> dict[str, Any]:
        if set(changes) - {"title", "skill_id"}:
            raise ValueError("Agent session update contains unknown fields")
        if not changes:
            return self.get_agent_session(
                session_id, workspace_id=workspace_id, include_archived=True
            )
        _validate_agent_session_changes(changes)
        assignments = [f"{field} = %s" for field in changes]
        params = [*changes.values(), session_id, workspace_id, expected_version]
        with self._pool.connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE lexsond.agent_sessions
                SET {', '.join(assignments)}, version = version + 1,
                    updated_at = clock_timestamp()
                WHERE session_id = %s AND workspace_id = %s
                  AND version = %s AND archived_at IS NULL
                  AND (turn_lease_token IS NULL OR turn_lease_until <= clock_timestamp())
                """,
                params,
            )
            if cursor.rowcount != 1:
                _raise_agent_update_conflict(connection, session_id, workspace_id)
        return self.get_agent_session(
            session_id, workspace_id=workspace_id, include_archived=True
        )

    def archive_agent_session(
        self, session_id: str, *, workspace_id: str = LEGACY_WORKSPACE_ID
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE lexsond.agent_sessions
                SET archived_at = clock_timestamp(), updated_at = clock_timestamp()
                WHERE session_id = %s AND workspace_id = %s AND archived_at IS NULL
                  AND (turn_lease_token IS NULL OR turn_lease_until <= clock_timestamp())
                """,
                (session_id, workspace_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT archived_at, turn_lease_token FROM lexsond.agent_sessions WHERE session_id = %s AND workspace_id = %s",
                    (session_id, workspace_id),
                ).fetchone()
                if row is None:
                    raise ControlPlaneNotFound("Agent session was not found")
                if row["archived_at"] is not None:
                    raise ControlPlaneConflict("Agent session is already archived")
                raise ControlPlaneConflict("Agent session has an active turn")
        return self.get_agent_session(
            session_id, workspace_id=workspace_id, include_archived=True
        )

    def restore_agent_session(
        self, session_id: str, *, workspace_id: str = LEGACY_WORKSPACE_ID
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT s.archived_at, t.archived_at AS target_archived_at
                FROM lexsond.agent_sessions s
                JOIN lexsond.targets t ON t.target_id = s.target_id
                WHERE s.session_id = %s AND s.workspace_id = %s
                FOR UPDATE OF s FOR SHARE OF t
                """,
                (session_id, workspace_id),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("Agent session was not found")
            if row["archived_at"] is None:
                raise ControlPlaneConflict("Agent session is not archived")
            if row["target_archived_at"] is not None:
                raise ControlPlaneConflict("Agent target must be restored first")
            connection.execute(
                """
                UPDATE lexsond.agent_sessions
                SET archived_at = NULL, updated_at = clock_timestamp()
                WHERE session_id = %s AND workspace_id = %s
                """,
                (session_id, workspace_id),
            )
        return self.get_agent_session(
            session_id, workspace_id=workspace_id, include_archived=True
        )

    def purge_agent_session(
        self, session_id: str, *, workspace_id: str = LEGACY_WORKSPACE_ID
    ) -> None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "DELETE FROM lexsond.agent_sessions WHERE session_id = %s AND workspace_id = %s AND archived_at IS NOT NULL RETURNING session_id",
                (session_id, workspace_id),
            ).fetchone()
            if row is None:
                exists = connection.execute(
                    "SELECT 1 FROM lexsond.agent_sessions WHERE session_id = %s AND workspace_id = %s",
                    (session_id, workspace_id),
                ).fetchone()
                if exists is None:
                    raise ControlPlaneNotFound("Agent session was not found")
                raise ControlPlaneConflict("Agent session must be archived before purge")

    def claim_agent_turn(
        self,
        session_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        lease_seconds: float,
    ) -> str:
        bounded = min(max(float(lease_seconds), 1.0), 600.0)
        token = str(uuid4())
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE lexsond.agent_sessions
                SET turn_lease_token = %s,
                    turn_lease_until = clock_timestamp() + (%s * INTERVAL '1 second')
                WHERE session_id = %s AND workspace_id = %s AND archived_at IS NULL
                  AND (turn_lease_token IS NULL OR turn_lease_until <= clock_timestamp())
                """,
                (token, bounded, session_id, workspace_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT archived_at FROM lexsond.agent_sessions WHERE session_id = %s AND workspace_id = %s",
                    (session_id, workspace_id),
                ).fetchone()
                if row is None or row["archived_at"] is not None:
                    raise ControlPlaneNotFound("Agent session was not found")
                raise ControlPlaneConflict("Agent session already has an active turn")
        return token

    def release_agent_turn(
        self,
        session_id: str,
        token: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
    ) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """
                UPDATE lexsond.agent_sessions
                SET turn_lease_token = NULL, turn_lease_until = NULL
                WHERE session_id = %s AND workspace_id = %s AND turn_lease_token = %s
                """,
                (session_id, workspace_id, token),
            )

    def renew_agent_turn(
        self,
        session_id: str,
        token: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        lease_seconds: float,
    ) -> None:
        bounded = min(max(float(lease_seconds), 1.0), 600.0)
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE lexsond.agent_sessions
                SET turn_lease_until = clock_timestamp() + (%s * INTERVAL '1 second')
                WHERE session_id = %s AND workspace_id = %s AND turn_lease_token = %s
                  AND archived_at IS NULL
                """,
                (bounded, session_id, workspace_id, token),
            )
            if cursor.rowcount != 1:
                raise ControlPlaneConflict("Agent turn lease was lost")

    def quarantine_agent_session_credential(
        self,
        session_id: str,
        sensitive_value: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        turn_token: str | None = None,
    ) -> bool:
        now_safe = "[REDACTED]"
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM lexsond.agent_sessions WHERE session_id = %s AND workspace_id = %s FOR UPDATE",
                (session_id, workspace_id),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("Agent session was not found")
            _require_agent_turn_token(row, turn_token)
            snapshot_fields = ("title", "base_url", "provider_id", "model", "skill_id")
            collision = any(
                sensitive_value in row[field]
                for field in snapshot_fields
                if row[field] is not None
            )
            if not collision:
                collision = connection.execute(
                    """
                    SELECT 1
                    WHERE EXISTS (
                        SELECT 1 FROM lexsond.agent_messages
                        WHERE session_id = %s AND (
                            position(%s IN content) > 0
                            OR position(%s IN metadata_json::TEXT) > 0
                        )
                    ) OR EXISTS (
                        SELECT 1 FROM lexsond.agent_events
                        WHERE session_id = %s AND (
                            position(%s IN name) > 0
                            OR position(%s IN payload_json::TEXT) > 0
                        )
                    )
                    """,
                    (
                        session_id, sensitive_value, sensitive_value,
                        session_id, sensitive_value, sensitive_value,
                    ),
                ).fetchone() is not None
            if not collision:
                return False
            safe = {
                field: redact_text(row[field], sensitive_values=(sensitive_value,))
                if row[field] is not None
                else None
                for field in ("title", "base_url", "provider_id", "model", "skill_id")
            }
            if safe["skill_id"] != row["skill_id"]:
                safe["skill_id"] = "credential-quarantine"
            connection.execute(
                """
                UPDATE lexsond.agent_sessions SET title = %s, base_url = %s,
                    provider_id = %s, model = %s, skill_id = %s,
                    version = version + 1, updated_at = clock_timestamp(),
                    archived_at = COALESCE(archived_at, clock_timestamp())
                WHERE session_id = %s
                """,
                (
                    safe["title"], safe["base_url"], safe["provider_id"],
                    safe["model"], safe["skill_id"], session_id,
                ),
            )
            messages = connection.execute(
                "SELECT sequence, content, metadata_json FROM lexsond.agent_messages WHERE session_id = %s",
                (session_id,),
            ).fetchall()
            for message in messages:
                connection.execute(
                    """
                    UPDATE lexsond.agent_messages SET content = %s, metadata_json = %s
                    WHERE session_id = %s AND sequence = %s
                    """,
                    (
                        redact_text(message["content"], sensitive_values=(sensitive_value,)),
                        Jsonb(redact_value(dict(message["metadata_json"]), sensitive_values=(sensitive_value,))),
                        session_id,
                        message["sequence"],
                    ),
                )
            events = connection.execute(
                "SELECT sequence, name, payload_json FROM lexsond.agent_events WHERE session_id = %s",
                (session_id,),
            ).fetchall()
            for event in events:
                connection.execute(
                    """
                    UPDATE lexsond.agent_events SET name = %s, payload_json = %s
                    WHERE session_id = %s AND sequence = %s
                    """,
                    (
                        redact_text(event["name"], sensitive_values=(sensitive_value,)) or now_safe,
                        Jsonb(redact_value(dict(event["payload_json"]), sensitive_values=(sensitive_value,))),
                        session_id,
                        event["sequence"],
                    ),
                )
        return True

    def append_agent_message(
        self,
        session_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        role: str,
        content: str,
        metadata: Mapping[str, Any] | None = None,
        turn_token: str | None = None,
    ) -> dict[str, Any]:
        if role not in {"user", "assistant"}:
            raise ValueError("Agent message role must be user or assistant")
        if len(content) > 12_000:
            raise ValueError("Agent message content exceeds the safe limit")
        if redact_text(content) != content:
            raise ValueError("Agent message contains an unredacted credential")
        metadata_value = dict(metadata or {})
        if _contains_forbidden_agent_key(metadata_value):
            raise ValueError("Agent message metadata contains a forbidden secret key")
        if redact_value(metadata_value) != metadata_value:
            raise ValueError("Agent message metadata contains an unredacted credential")
        message_id = str(uuid4())
        with self._pool.connection() as connection:
            session = connection.execute(
                "SELECT archived_at, turn_lease_token FROM lexsond.agent_sessions WHERE session_id = %s AND workspace_id = %s FOR UPDATE",
                (session_id, workspace_id),
            ).fetchone()
            if session is None:
                raise ControlPlaneNotFound("Agent session was not found")
            if session["archived_at"] is not None:
                raise ControlPlaneConflict("archived Agent session cannot accept messages")
            _require_agent_turn_token(session, turn_token)
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM lexsond.agent_messages WHERE session_id = %s",
                (session_id,),
            ).fetchone()["value"]
            row = connection.execute(
                """
                INSERT INTO lexsond.agent_messages (
                    session_id, sequence, message_id, role, content, metadata_json
                ) VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING created_at
                """,
                (
                    session_id,
                    sequence,
                    message_id,
                    role,
                    content,
                    Jsonb(metadata_value),
                ),
            ).fetchone()
            connection.execute(
                "UPDATE lexsond.agent_sessions SET updated_at = clock_timestamp() WHERE session_id = %s",
                (session_id,),
            )
        return {
            "message_id": message_id,
            "session_id": session_id,
            "sequence": sequence,
            "role": role,
            "content": content,
            "metadata": metadata_value,
            "created_at": _time(row["created_at"]),
        }

    def list_agent_messages(
        self,
        session_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.get_agent_session(
            session_id, workspace_id=workspace_id, include_archived=True
        )
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM lexsond.agent_messages
                    WHERE session_id = %s ORDER BY sequence DESC LIMIT %s
                ) AS recent ORDER BY sequence
                """,
                (session_id, min(max(int(limit), 1), 100)),
            ).fetchall()
        return [_agent_message(row) for row in rows]

    def append_agent_event(
        self,
        session_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        event_type: str,
        name: str,
        status: str,
        payload: Mapping[str, Any] | None = None,
        turn_token: str | None = None,
    ) -> dict[str, Any]:
        _validate_agent_event_fields(event_type, name, status)
        payload_value = dict(payload or {})
        if _contains_forbidden_agent_key(payload_value):
            raise ValueError("Agent event payload contains a forbidden secret key")
        if redact_value(payload_value) != payload_value:
            raise ValueError("Agent event contains an unredacted credential")
        event_id = str(uuid4())
        with self._pool.connection() as connection:
            session = connection.execute(
                "SELECT archived_at, turn_lease_token FROM lexsond.agent_sessions WHERE session_id = %s AND workspace_id = %s FOR UPDATE",
                (session_id, workspace_id),
            ).fetchone()
            if session is None:
                raise ControlPlaneNotFound("Agent session was not found")
            if session["archived_at"] is not None:
                raise ControlPlaneConflict("archived Agent session cannot accept events")
            _require_agent_turn_token(session, turn_token)
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM lexsond.agent_events WHERE session_id = %s",
                (session_id,),
            ).fetchone()["value"]
            row = connection.execute(
                """
                INSERT INTO lexsond.agent_events (
                    session_id, sequence, event_id, event_type, name, status,
                    payload_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING occurred_at
                """,
                (
                    session_id,
                    sequence,
                    event_id,
                    event_type,
                    name,
                    status,
                    Jsonb(payload_value),
                ),
            ).fetchone()
        return {
            "event_id": event_id,
            "session_id": session_id,
            "sequence": sequence,
            "event_type": event_type,
            "name": name,
            "status": status,
            "payload": payload_value,
            "occurred_at": _time(row["occurred_at"]),
        }

    def list_agent_events(
        self,
        session_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        after_sequence: int = 0,
    ) -> list[dict[str, Any]]:
        self.get_agent_session(
            session_id, workspace_id=workspace_id, include_archived=True
        )
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM lexsond.agent_events
                WHERE session_id = %s AND sequence > %s ORDER BY sequence
                """,
                (session_id, max(int(after_sequence), 0)),
            ).fetchall()
        return [_agent_event(row) for row in rows]

    # Continuous monitoring policies and derived health state

    def create_monitor_policy(
        self,
        value: Mapping[str, Any],
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
    ) -> dict[str, Any]:
        _validate_monitor_policy_value(value)
        policy_id = str(uuid4())
        interval = int(value["interval_seconds"])
        offset = _schedule_offset(policy_id, interval)
        next_run_at = (
            datetime.now(UTC) + timedelta(seconds=offset)
            if bool(value.get("enabled", True))
            else None
        )
        try:
            with self._pool.connection() as connection:
                _require_monitor_references(connection, value, workspace_id)
                connection.execute(
                    """
                    INSERT INTO lexsond.monitor_policies (
                        policy_id, workspace_id, name, target_id, suite_revision_id, run_kind,
                        probe_type, execution_backend, model, streaming,
                        timeout_seconds, interval_seconds, failure_threshold,
                        recovery_threshold, schedule_offset_seconds, enabled,
                        next_run_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        policy_id,
                        workspace_id,
                        value["name"],
                        str(value["target_id"]),
                        str(value["suite_revision_id"])
                        if value.get("suite_revision_id") is not None
                        else None,
                        value["run_kind"],
                        str(value.get("probe_type") or "chat"),
                        value["execution_backend"],
                        value["model"],
                        bool(value["stream"]),
                        float(value["timeout_seconds"]),
                        interval,
                        int(value["failure_threshold"]),
                        int(value["recovery_threshold"]),
                        offset,
                        bool(value.get("enabled", True)),
                        next_run_at,
                    ),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise ControlPlaneConflict("monitor policy conflicts with stored data") from exc
        return self.get_monitor_policy(
            policy_id, workspace_id=workspace_id, include_archived=True
        )

    def get_monitor_policy(
        self,
        policy_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM lexsond.monitor_policies WHERE policy_id = %s AND workspace_id = %s",
                (policy_id, workspace_id),
            ).fetchone()
        if row is None or (row["archived_at"] is not None and not include_archived):
            raise ControlPlaneNotFound("monitor policy was not found")
        return _monitor_policy(row)

    def list_monitor_policies(
        self,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        archived = "" if include_archived else "AND archived_at IS NULL"
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM lexsond.monitor_policies
                WHERE workspace_id = %s {archived}
                ORDER BY updated_at DESC, policy_id
                """,
                (workspace_id,),
            ).fetchall()
        return [_monitor_policy(row) for row in rows]

    def update_monitor_policy(
        self,
        policy_id: str,
        changes: Mapping[str, Any],
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        expected_version: int,
    ) -> dict[str, Any]:
        if not changes:
            return self.get_monitor_policy(
                policy_id, workspace_id=workspace_id, include_archived=True
            )
        allowed = {
            "name", "target_id", "suite_revision_id", "run_kind", "probe_type",
            "execution_backend", "model", "stream", "timeout_seconds",
            "interval_seconds", "failure_threshold", "recovery_threshold", "enabled",
        }
        if set(changes) - allowed:
            raise ValueError("monitor policy update contains unknown fields")
        current = self.get_monitor_policy(
            policy_id, workspace_id=workspace_id, include_archived=True
        )
        if current["archived_at"] is not None:
            raise ControlPlaneConflict("archived monitor policy cannot be updated")
        merged = {**current, **changes}
        _validate_monitor_policy_value(merged)
        interval = int(merged["interval_seconds"])
        offset = _schedule_offset(policy_id, interval)
        normalized = {
            ("streaming" if field == "stream" else field): (
                str(value)
                if field in {"target_id", "suite_revision_id", "probe_type"}
                and value is not None
                else value
            )
            for field, value in changes.items()
        }
        if "interval_seconds" in changes:
            normalized["schedule_offset_seconds"] = offset
        if {"enabled", "interval_seconds"} & set(changes):
            normalized["next_run_at"] = (
                datetime.now(UTC) + timedelta(seconds=offset)
                if bool(merged["enabled"])
                else None
            )
            normalized["lease_token"] = None
            normalized["lease_until"] = None
        assignments = [f"{field} = %s" for field in normalized]
        try:
            with self._pool.connection() as connection:
                _require_monitor_references(connection, merged, workspace_id)
                cursor = connection.execute(
                    f"""
                    UPDATE lexsond.monitor_policies
                    SET {', '.join(assignments)}, version = version + 1,
                        updated_at = clock_timestamp()
                    WHERE policy_id = %s AND workspace_id = %s
                      AND version = %s AND archived_at IS NULL
                      AND (lease_until IS NULL OR lease_until <= clock_timestamp())
                    """,
                    (*normalized.values(), policy_id, workspace_id, expected_version),
                )
                if cursor.rowcount != 1:
                    locked = connection.execute(
                        "SELECT lease_until FROM lexsond.monitor_policies WHERE policy_id = %s AND workspace_id = %s",
                        (policy_id, workspace_id),
                    ).fetchone()
                    if (
                        locked is not None
                        and locked["lease_until"] is not None
                        and locked["lease_until"] > datetime.now(UTC)
                    ):
                        raise ControlPlaneConflict(
                            "monitor policy dispatch is already in progress"
                        )
                    _raise_monitor_update_conflict(connection, policy_id, workspace_id)
        except psycopg.errors.UniqueViolation as exc:
            raise ControlPlaneConflict("monitor policy update conflicts with stored data") from exc
        return self.get_monitor_policy(
            policy_id, workspace_id=workspace_id, include_archived=True
        )

    def archive_monitor_policy(
        self, policy_id: str, *, workspace_id: str = LEGACY_WORKSPACE_ID
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE lexsond.monitor_policies
                SET archived_at = clock_timestamp(), enabled = FALSE,
                    next_run_at = NULL, lease_token = NULL, lease_until = NULL,
                    version = version + 1, updated_at = clock_timestamp()
                WHERE policy_id = %s AND workspace_id = %s AND archived_at IS NULL
                  AND (lease_until IS NULL OR lease_until <= clock_timestamp())
                """,
                (policy_id, workspace_id),
            )
            if cursor.rowcount != 1:
                locked = connection.execute(
                    "SELECT lease_until FROM lexsond.monitor_policies WHERE policy_id = %s AND workspace_id = %s",
                    (policy_id, workspace_id),
                ).fetchone()
                if (
                    locked is not None
                    and locked["lease_until"] is not None
                    and locked["lease_until"] > datetime.now(UTC)
                ):
                    raise ControlPlaneConflict("monitor policy dispatch is already in progress")
                _raise_monitor_update_conflict(connection, policy_id, workspace_id)
        return self.get_monitor_policy(
            policy_id, workspace_id=workspace_id, include_archived=True
        )

    def restore_monitor_policy(
        self, policy_id: str, *, workspace_id: str = LEGACY_WORKSPACE_ID
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE lexsond.monitor_policies
                SET archived_at = NULL, version = version + 1,
                    updated_at = clock_timestamp()
                WHERE policy_id = %s AND workspace_id = %s AND archived_at IS NOT NULL
                """,
                (policy_id, workspace_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT archived_at FROM lexsond.monitor_policies WHERE policy_id = %s AND workspace_id = %s",
                    (policy_id, workspace_id),
                ).fetchone()
                if row is None:
                    raise ControlPlaneNotFound("monitor policy was not found")
                raise ControlPlaneConflict("monitor policy is not archived")
        return self.get_monitor_policy(
            policy_id, workspace_id=workspace_id, include_archived=True
        )

    def purge_monitor_policy(
        self, policy_id: str, *, workspace_id: str = LEGACY_WORKSPACE_ID
    ) -> None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT archived_at FROM lexsond.monitor_policies WHERE policy_id = %s AND workspace_id = %s FOR UPDATE",
                (policy_id, workspace_id),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("monitor policy was not found")
            if row["archived_at"] is None:
                raise ControlPlaneConflict("monitor policy must be archived before purge")
            connection.execute(
                "UPDATE lexsond.probe_runs SET monitor_policy_id = NULL WHERE monitor_policy_id = %s AND workspace_id = %s",
                (policy_id, workspace_id),
            )
            connection.execute(
                "DELETE FROM lexsond.monitor_policies WHERE policy_id = %s AND workspace_id = %s",
                (policy_id, workspace_id),
            )

    def request_monitor_policy_run(
        self, policy_id: str, *, workspace_id: str = LEGACY_WORKSPACE_ID
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE lexsond.monitor_policies
                SET next_run_at = clock_timestamp(), updated_at = clock_timestamp(),
                    last_dispatch_failure_code = NULL
                WHERE policy_id = %s AND workspace_id = %s
                  AND archived_at IS NULL AND enabled
                  AND (lease_until IS NULL OR lease_until <= clock_timestamp())
                  AND NOT EXISTS (
                      SELECT 1 FROM lexsond.probe_runs run
                      WHERE run.run_id = monitor_policies.last_run_id
                        AND run.state = 'RUNNING'
                  )
                """,
                (policy_id, workspace_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    """
                    SELECT policy.enabled, policy.archived_at, policy.lease_until,
                           run.state AS last_run_state
                    FROM lexsond.monitor_policies policy
                    LEFT JOIN lexsond.probe_runs run ON run.run_id = policy.last_run_id
                    WHERE policy.policy_id = %s AND policy.workspace_id = %s
                    """,
                    (policy_id, workspace_id),
                ).fetchone()
                if row is None or row["archived_at"] is not None:
                    raise ControlPlaneNotFound("monitor policy was not found")
                if row["lease_until"] is not None and row["lease_until"] > datetime.now(UTC):
                    raise ControlPlaneConflict("monitor policy dispatch is already in progress")
                if row["last_run_state"] == "RUNNING":
                    raise ControlPlaneConflict("monitor policy already has a running probe")
                raise ControlPlaneConflict("monitor policy is disabled")
        return self.get_monitor_policy(policy_id, workspace_id=workspace_id)

    def claim_due_monitor_policies(
        self, *, now: str, limit: int, lease_seconds: float
    ) -> list[dict[str, Any]]:
        bounded = min(max(int(limit), 1), 32)
        if lease_seconds <= 0 or lease_seconds > 300:
            raise ValueError("monitor policy lease_seconds is out of bounds")
        observed = datetime.fromisoformat(now)
        if observed.tzinfo is None:
            raise ValueError("monitor timestamps must include a timezone")
        claims: list[dict[str, Any]] = []
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM lexsond.monitor_policies
                WHERE enabled AND archived_at IS NULL
                  AND next_run_at IS NOT NULL AND next_run_at <= %s
                  AND (lease_until IS NULL OR lease_until <= %s)
                  AND NOT EXISTS (
                      SELECT 1 FROM lexsond.probe_runs run
                      WHERE run.run_id = monitor_policies.last_run_id
                        AND run.state = 'RUNNING'
                  )
                ORDER BY next_run_at, policy_id
                FOR UPDATE SKIP LOCKED LIMIT %s
                """,
                (observed, observed, bounded),
            ).fetchall()
            for row in rows:
                token = str(uuid4())
                connection.execute(
                    """
                    UPDATE lexsond.monitor_policies
                    SET lease_token = %s, lease_until = %s
                    WHERE policy_id = %s
                    """,
                    (token, observed + timedelta(seconds=lease_seconds), row["policy_id"]),
                )
                claim = _monitor_policy(row)
                claim["lease_token"] = token
                claim["scheduled_for"] = _time(row["next_run_at"])
                claims.append(claim)
        return claims

    def complete_monitor_policy_dispatch(
        self,
        policy_id: str,
        *,
        lease_token: str,
        scheduled_for: str,
        run_id: str,
    ) -> None:
        self._finish_monitor_policy_dispatch(
            policy_id,
            lease_token=lease_token,
            scheduled_for=scheduled_for,
            run_id=run_id,
            failure_code=None,
        )

    def fail_monitor_policy_dispatch(
        self,
        policy_id: str,
        *,
        lease_token: str,
        scheduled_for: str,
        failure_code: str,
    ) -> None:
        if not failure_code or len(failure_code) > 128 or not failure_code.replace("_", "").isalnum():
            raise ValueError("invalid monitor dispatch failure code")
        self._finish_monitor_policy_dispatch(
            policy_id,
            lease_token=lease_token,
            scheduled_for=scheduled_for,
            run_id=None,
            failure_code=failure_code,
        )

    def _finish_monitor_policy_dispatch(
        self,
        policy_id: str,
        *,
        lease_token: str,
        scheduled_for: str,
        run_id: str | None,
        failure_code: str | None,
    ) -> None:
        now = datetime.now(UTC)
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM lexsond.monitor_policies WHERE policy_id = %s FOR UPDATE",
                (policy_id,),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("monitor policy was not found")
            if str(row["lease_token"]) != lease_token or _time(row["next_run_at"]) != scheduled_for:
                raise ControlPlaneConflict("monitor policy dispatch lease is stale")
            if run_id is not None:
                linked = connection.execute(
                    "SELECT monitor_policy_id FROM lexsond.probe_runs WHERE run_id = %s",
                    (run_id,),
                ).fetchone()
                if linked is None or str(linked["monitor_policy_id"]) != policy_id:
                    raise ControlPlaneConflict("monitor run does not match its policy")
            connection.execute(
                """
                UPDATE lexsond.monitor_policies
                SET next_run_at = %s, last_run_at = %s,
                    last_run_id = COALESCE(%s, last_run_id),
                    last_dispatch_failure_code = %s,
                    lease_token = NULL, lease_until = NULL, updated_at = %s
                WHERE policy_id = %s AND lease_token = %s
                """,
                (
                    _next_schedule(
                        scheduled_for,
                        interval_seconds=int(row["interval_seconds"]),
                        now=now,
                    ),
                    now,
                    run_id,
                    failure_code,
                    now,
                    policy_id,
                    lease_token,
                ),
            )

    def record_monitor_run(self, run_id: str) -> dict[str, Any] | None:
        with self._pool.connection() as connection:
            identity = connection.execute(
                "SELECT monitor_policy_id FROM lexsond.probe_runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            if identity is None:
                raise ControlPlaneNotFound("run was not found")
            policy_id = identity["monitor_policy_id"]
            if policy_id is None:
                return None
            policy_id = str(policy_id)
            policy = connection.execute(
                "SELECT * FROM lexsond.monitor_policies WHERE policy_id = %s FOR UPDATE",
                (policy_id,),
            ).fetchone()
            if policy is None:
                return None
            run = connection.execute(
                "SELECT * FROM lexsond.probe_runs WHERE run_id = %s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if run is None or str(run["monitor_policy_id"]) != policy_id:
                return None
            if run["state"] == "RUNNING":
                raise ControlPlaneConflict("running monitor run cannot be projected")
            existing = connection.execute(
                "SELECT * FROM lexsond.monitor_samples WHERE run_id = %s", (run_id,)
            ).fetchone()
            if existing is not None:
                state = connection.execute(
                    "SELECT * FROM lexsond.monitor_states WHERE policy_id = %s",
                    (policy_id,),
                ).fetchone()
                return {
                    "sample": _monitor_sample(existing),
                    "state": _monitor_state(state),
                    "incident": None,
                    "replayed": True,
                }
            observation = _monitor_observation(run)
            result = dict(run["result_json"]) if run["result_json"] is not None else None
            e2e_ms, ttft_ms, error_class = _monitor_metrics(result, run["failure_code"])
            observed_at = run["finished_at"] or datetime.now(UTC)
            sample_id = str(uuid4())
            sample = connection.execute(
                """
                INSERT INTO lexsond.monitor_samples (
                    sample_id, policy_id, run_id, observed_at, observation,
                    error_class, p95_e2e_ms, p95_ttft_ms
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    sample_id, policy_id, run_id, observed_at, observation.value,
                    error_class, e2e_ms, ttft_ms,
                ),
            ).fetchone()
            state_row = connection.execute(
                "SELECT * FROM lexsond.monitor_states WHERE policy_id = %s FOR UPDATE",
                (policy_id,),
            ).fetchone()
            if (
                state_row is not None
                and observed_at <= state_row["last_observed_at"]
            ):
                return {
                    "sample": _monitor_sample(sample),
                    "state": _monitor_state(state_row),
                    "incident": None,
                    "replayed": False,
                    "stale": True,
                }
            previous = (
                MonitorState(
                    MonitorStatus(state_row["status"]),
                    int(state_row["consecutive_successes"]),
                    int(state_row["consecutive_failures"]),
                )
                if state_row is not None
                else None
            )
            transitioned = transition_state(
                previous,
                observation,
                failure_threshold=int(policy["failure_threshold"]),
                recovery_threshold=int(policy["recovery_threshold"]),
            )
            state = connection.execute(
                """
                INSERT INTO lexsond.monitor_states (
                    policy_id, status, consecutive_successes,
                    consecutive_failures, last_observation, last_run_id,
                    last_observed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(policy_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    consecutive_successes = EXCLUDED.consecutive_successes,
                    consecutive_failures = EXCLUDED.consecutive_failures,
                    last_observation = EXCLUDED.last_observation,
                    last_run_id = EXCLUDED.last_run_id,
                    last_observed_at = EXCLUDED.last_observed_at,
                    updated_at = clock_timestamp()
                RETURNING *
                """,
                (
                    policy_id, transitioned.status.value,
                    transitioned.consecutive_successes,
                    transitioned.consecutive_failures, observation.value,
                    run_id, observed_at,
                ),
            ).fetchone()
            incident = None
            if transitioned.event_type is not None:
                from_status = (previous or MonitorState(MonitorStatus.UNKNOWN)).status.value
                incident_row = connection.execute(
                    """
                    INSERT INTO lexsond.monitor_incident_events (
                        incident_id, policy_id, run_id, event_type,
                        from_status, to_status, error_class, observed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        str(uuid4()), policy_id, run_id, transitioned.event_type,
                        from_status, transitioned.status.value, error_class, observed_at,
                    ),
                ).fetchone()
                incident = _monitor_incident(incident_row)
        return {
            "sample": _monitor_sample(sample),
            "state": _monitor_state(state),
            "incident": incident,
            "replayed": False,
        }

    def list_monitor_incidents(
        self,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        policy_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        policy_filter = "" if policy_id is None else "AND event.policy_id = %s"
        params = [] if policy_id is None else [policy_id]
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT event.* FROM lexsond.monitor_incident_events event
                JOIN lexsond.monitor_policies policy
                  ON policy.policy_id = event.policy_id
                WHERE policy.workspace_id = %s {policy_filter}
                ORDER BY event.observed_at DESC, event.incident_id DESC LIMIT %s
                """,
                (workspace_id, *params, min(max(int(limit), 1), 500)),
            ).fetchall()
        return [_monitor_incident(row) for row in rows]

    def monitoring_overview(
        self,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        window: str = "24h",
        include_archived: bool = False,
    ) -> dict[str, Any]:
        window_seconds, bucket_seconds = _monitor_window(window)
        now = datetime.now(UTC)
        archived = "" if include_archived else "AND p.archived_at IS NULL"
        with self._pool.connection() as connection:
            policies = connection.execute(
                f"""
                SELECT p.*, s.status, s.consecutive_successes,
                       s.consecutive_failures, s.last_observation,
                       s.last_observed_at
                FROM lexsond.monitor_policies p
                LEFT JOIN lexsond.monitor_states s ON s.policy_id = p.policy_id
                WHERE p.workspace_id = %s {archived}
                ORDER BY p.name, p.policy_id
                """,
                (workspace_id,),
            ).fetchall()
            samples = connection.execute(
                """
                SELECT sample.* FROM lexsond.monitor_samples sample
                JOIN lexsond.monitor_policies policy
                  ON policy.policy_id = sample.policy_id
                WHERE policy.workspace_id = %s AND sample.observed_at >= %s
                ORDER BY sample.observed_at, sample.sample_id
                """,
                (workspace_id, now - timedelta(seconds=window_seconds)),
            ).fetchall()
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for sample in samples:
            grouped.setdefault(str(sample["policy_id"]), []).append(sample)
        status_counts = {name: 0 for name in ("unknown", "up", "degraded", "down")}
        values: list[dict[str, Any]] = []
        for policy in policies:
            policy_id = str(policy["policy_id"])
            status_value = policy["status"] or "UNKNOWN"
            status_counts[status_value.lower()] += 1
            policy_samples = grouped.get(policy_id, [])
            latest = policy_samples[-1] if policy_samples else None
            value = _monitor_policy(policy)
            value.update(
                {
                    "status": status_value,
                    "consecutive_successes": int(policy["consecutive_successes"] or 0),
                    "consecutive_failures": int(policy["consecutive_failures"] or 0),
                    "last_observation": policy["last_observation"],
                    "last_observed_at": _time(policy["last_observed_at"]),
                    "latest_error_class": latest["error_class"] if latest else None,
                    "sample_count": len(policy_samples),
                    "buckets": _aggregate_monitor_buckets(policy_samples, bucket_seconds),
                }
            )
            values.append(value)
        return {
            "window": window,
            "window_seconds": window_seconds,
            "bucket_seconds": bucket_seconds,
            "generated_at": now.isoformat(),
            "timeline": _monitor_timeline(now, window_seconds, bucket_seconds),
            "summary": {
                "policies": len(policies),
                **status_counts,
                "samples": len(samples),
            },
            "policies": values,
        }

    def prune_monitoring_data(
        self,
        *,
        samples_before: str,
        incidents_before: str,
        limit: int = 1000,
    ) -> dict[str, int]:
        sample_cutoff = _parse_utc(samples_before)
        incident_cutoff = _parse_utc(incidents_before)
        bounded = min(max(int(limit), 1), 10_000)
        with self._pool.connection() as connection:
            sample_cursor = connection.execute(
                """
                WITH doomed AS (
                    SELECT sample_id FROM lexsond.monitor_samples
                    WHERE observed_at < %s ORDER BY observed_at LIMIT %s
                )
                DELETE FROM lexsond.monitor_samples sample
                USING doomed WHERE sample.sample_id = doomed.sample_id
                """,
                (sample_cutoff, bounded),
            )
            incident_cursor = connection.execute(
                """
                WITH doomed AS (
                    SELECT incident_id FROM lexsond.monitor_incident_events
                    WHERE observed_at < %s ORDER BY observed_at LIMIT %s
                )
                DELETE FROM lexsond.monitor_incident_events incident
                USING doomed WHERE incident.incident_id = doomed.incident_id
                """,
                (incident_cutoff, bounded),
            )
        return {
            "samples": max(sample_cursor.rowcount, 0),
            "incidents": max(incident_cursor.rowcount, 0),
        }

    # Runs and safe event stream

    def create_run(
        self,
        run_id: str,
        metadata: Mapping[str, Any],
        workflow: Mapping[str, Any],
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        idempotency_key: str | None = None,
        request_sha256: str | None = None,
    ) -> dict[str, Any]:
        existing = self._get_idempotent_run(
            idempotency_key, request_sha256, workspace_id=workspace_id
        )
        if existing is not None:
            return existing
        try:
            with self._pool.connection() as connection:
                target_id = metadata.get("target_id")
                if target_id is not None:
                    target = connection.execute(
                        "SELECT archived_at FROM lexsond.targets WHERE target_id = %s AND workspace_id = %s FOR SHARE",
                        (target_id, workspace_id),
                    ).fetchone()
                    if target is None or target["archived_at"] is not None:
                        raise ControlPlaneConflict("run target is missing or archived")
                revision_id = metadata.get("suite_revision_id")
                if revision_id is not None:
                    revision = connection.execute(
                        """
                        SELECT s.archived_at FROM lexsond.suite_revisions r
                        JOIN lexsond.suites s ON s.suite_id = r.suite_id
                        WHERE r.revision_id = %s AND r.workspace_id = %s
                        FOR SHARE OF r, s
                        """,
                        (revision_id, workspace_id),
                    ).fetchone()
                    if revision is None or revision["archived_at"] is not None:
                        raise ControlPlaneConflict("run suite revision is missing or archived")
                policy_id = metadata.get("monitor_policy_id")
                if policy_id is not None:
                    policy = connection.execute(
                        "SELECT * FROM lexsond.monitor_policies WHERE policy_id = %s AND workspace_id = %s FOR SHARE",
                        (policy_id, workspace_id),
                    ).fetchone()
                    if policy is None or policy["archived_at"] is not None:
                        raise ControlPlaneConflict("monitor policy is missing or archived")
                    expected = (
                        str(policy["target_id"]),
                        str(policy["suite_revision_id"])
                        if policy["suite_revision_id"]
                        else None,
                        policy["run_kind"],
                        policy["execution_backend"],
                        policy["model"],
                        policy["probe_type"],
                        bool(policy["streaming"]),
                        float(policy["timeout_seconds"]),
                    )
                    actual = (
                        metadata.get("target_id"),
                        metadata.get("suite_revision_id"),
                        metadata.get("run_kind", "component"),
                        metadata.get("execution_backend", "local"),
                        metadata["model"],
                        metadata["probe_type"],
                        bool(metadata["stream"]),
                        float(metadata["timeout_seconds"]),
                    )
                    if expected != actual:
                        raise ControlPlaneConflict("monitor run does not match its policy snapshot")
                connection.execute(
                    """
                    INSERT INTO lexsond.probe_runs (
                        run_id, workspace_id, idempotency_key, request_sha256,
                        target_id, suite_revision_id, monitor_policy_id, run_kind,
                        execution_backend, state, base_url, model, target_kind,
                        provider_id, run_mode, probe_type, streaming,
                        timeout_seconds, max_output_tokens,
                        credential_profile_id, workflow_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'RUNNING', %s, %s, %s,
                              %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        workspace_id,
                        idempotency_key,
                        request_sha256,
                        metadata.get("target_id"),
                        metadata.get("suite_revision_id"),
                        metadata.get("monitor_policy_id"),
                        metadata.get("run_kind", "component"),
                        metadata.get("execution_backend", "local"),
                        metadata["base_url"],
                        metadata["model"],
                        metadata["target_kind"],
                        metadata.get("provider_id"),
                        metadata["run_mode"],
                        metadata["probe_type"],
                        bool(metadata["stream"]),
                        float(metadata["timeout_seconds"]),
                        int(metadata.get("max_output_tokens", 64)),
                        metadata.get("credential_profile_id"),
                        Jsonb(dict(workflow)),
                    ),
                )
                self._append_event(
                    connection, run_id, "RUN_STARTED", "binding", "RUNNING"
                )
        except psycopg.errors.UniqueViolation as exc:
            existing = self._get_idempotent_run(
                idempotency_key, request_sha256, workspace_id=workspace_id
            )
            if existing is not None:
                return existing
            raise ControlPlaneConflict("run creation conflicts with stored data") from exc
        return self.get_run(run_id, workspace_id=workspace_id)

    def _get_idempotent_run(
        self,
        idempotency_key: str | None,
        request_sha256: str | None,
        *,
        workspace_id: str,
    ) -> dict[str, Any] | None:
        if idempotency_key is None:
            if request_sha256 is not None:
                raise ValueError("request_sha256 requires idempotency_key")
            return None
        if request_sha256 is None:
            raise ValueError("idempotency_key requires request_sha256")
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT run_id, request_sha256 FROM lexsond.probe_runs WHERE idempotency_key = %s AND workspace_id = %s",
                (idempotency_key, workspace_id),
            ).fetchone()
        if row is None:
            return None
        if row["request_sha256"].strip() != request_sha256:
            raise ControlPlaneConflict("idempotency key belongs to a different run request")
        return self.get_run(
            str(row["run_id"]), workspace_id=workspace_id, include_archived=True
        )

    def get_run(
        self,
        run_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM lexsond.probe_runs WHERE run_id = %s AND workspace_id = %s",
                (run_id, workspace_id),
            ).fetchone()
        if row is None or (row["archived_at"] is not None and not include_archived):
            raise ControlPlaneNotFound("run was not found")
        return _run(row, include_result=True)

    def get_run_system(
        self, run_id: str, *, include_archived: bool = False
    ) -> dict[str, Any]:
        """Internal worker lookup for a globally unique run identifier."""

        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM lexsond.probe_runs WHERE run_id = %s", (run_id,)
            ).fetchone()
        if row is None or (row["archived_at"] is not None and not include_archived):
            raise ControlPlaneNotFound("run was not found")
        return _run(row, include_result=True)

    def list_runs(
        self,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        include_archived: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        archived = "" if include_archived else "AND archived_at IS NULL"
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM lexsond.probe_runs WHERE workspace_id = %s {archived} ORDER BY created_at DESC, run_id DESC LIMIT %s",
                (workspace_id, min(max(int(limit), 1), 100)),
            ).fetchall()
        return [_run(row, include_result=False) for row in rows]

    def list_temporal_runs_for_recovery(self) -> list[dict[str, Any]]:
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM lexsond.probe_runs
                WHERE state = 'RUNNING' AND execution_backend = 'temporal'
                ORDER BY created_at, run_id
                """
            ).fetchall()
        return [_run(row, include_result=False) for row in rows]

    def update_run_workflow(
        self,
        run_id: str,
        workflow: Mapping[str, Any],
        *,
        event_type: str,
        phase: str,
        status: str,
        source_event_id: str | None = None,
    ) -> None:
        with self._pool.connection() as connection:
            if source_event_id is not None and connection.execute(
                "SELECT 1 FROM lexsond.probe_run_events WHERE run_id = %s AND source_event_id = %s",
                (run_id, source_event_id),
            ).fetchone() is not None:
                return
            cursor = connection.execute(
                "UPDATE lexsond.probe_runs SET workflow_json = %s WHERE run_id = %s AND state = 'RUNNING'",
                (Jsonb(dict(workflow)), run_id),
            )
            if cursor.rowcount != 1:
                raise ControlPlaneConflict("run workflow is no longer mutable")
            self._append_event(
                connection, run_id, event_type, phase, status,
                source_event_id=source_event_id,
            )

    def complete_run(
        self, run_id: str, result: Mapping[str, Any], workflow: Mapping[str, Any]
    ) -> None:
        validate_sanitized_result(run_id, result)
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE lexsond.probe_runs
                SET state = 'COMPLETED', result_status = %s,
                    finished_at = clock_timestamp(), result_json = %s,
                    failure_code = NULL, workflow_json = %s
                WHERE run_id = %s AND state = 'RUNNING'
                """,
                (result["status"], Jsonb(dict(result)), Jsonb(dict(workflow)), run_id),
            )
            if cursor.rowcount != 1:
                raise ControlPlaneConflict("run is no longer running")
            self._append_event(connection, run_id, "RUN_COMPLETED", "complete", str(result["status"]))

    def fail_run(
        self, run_id: str, failure_code: str, workflow: Mapping[str, Any]
    ) -> None:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE lexsond.probe_runs
                SET state = 'FAILED', result_status = 'FAIL',
                    finished_at = clock_timestamp(), failure_code = %s,
                    result_json = NULL, workflow_json = %s
                WHERE run_id = %s AND state = 'RUNNING'
                """,
                (failure_code, Jsonb(dict(workflow)), run_id),
            )
            if cursor.rowcount != 1:
                raise ControlPlaneConflict("run is no longer running")
            self._append_event(connection, run_id, "RUN_FAILED", "complete", "FAIL")

    def cancel_run(
        self, run_id: str, *, workspace_id: str = LEGACY_WORKSPACE_ID
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE lexsond.probe_runs
                SET state = 'CANCELLED', result_status = 'UNKNOWN',
                    finished_at = clock_timestamp(), failure_code = 'CANCEL_REQUESTED'
                WHERE run_id = %s AND workspace_id = %s AND state = 'RUNNING'
                """,
                (run_id, workspace_id),
            )
            if cursor.rowcount != 1:
                if not connection.execute(
                    "SELECT 1 FROM lexsond.probe_runs WHERE run_id = %s AND workspace_id = %s",
                    (run_id, workspace_id),
                ).fetchone():
                    raise ControlPlaneNotFound("run was not found")
                raise ControlPlaneConflict("only a running run can be cancelled")
            self._append_event(connection, run_id, "RUN_CANCELLED", "complete", "CANCELLED")
        return self.get_run(run_id, workspace_id=workspace_id)

    def cancel_run_system(self, run_id: str) -> dict[str, Any]:
        """Internal terminal projection used after a trusted worker callback."""

        workspace_id = self.get_run_system(run_id, include_archived=True)[
            "workspace_id"
        ]
        return self.cancel_run(run_id, workspace_id=workspace_id)

    def request_cancel_run(
        self, run_id: str, *, workspace_id: str = LEGACY_WORKSPACE_ID
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT state, cancel_requested_at FROM lexsond.probe_runs WHERE run_id = %s AND workspace_id = %s FOR UPDATE",
                (run_id, workspace_id),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("run was not found")
            if row["state"] != "RUNNING":
                raise ControlPlaneConflict("only a running run can be cancelled")
            if row["cancel_requested_at"] is None:
                connection.execute(
                    "UPDATE lexsond.probe_runs SET cancel_requested_at = clock_timestamp() WHERE run_id = %s AND workspace_id = %s",
                    (run_id, workspace_id),
                )
                self._append_event(
                    connection,
                    run_id,
                    "RUN_CANCEL_REQUESTED",
                    "complete",
                    "CANCEL_REQUESTED",
                )
        return self.get_run(
            run_id, workspace_id=workspace_id, include_archived=True
        )

    def archive_run(
        self, run_id: str, *, workspace_id: str = LEGACY_WORKSPACE_ID
    ) -> dict[str, Any]:
        run = self.get_run(
            run_id, workspace_id=workspace_id, include_archived=True
        )
        if run["state"] == "RUNNING":
            raise ControlPlaneConflict("running run cannot be archived")
        self._archive(
            "probe_runs",
            "run_id",
            run_id,
            workspace_id=workspace_id,
            has_updated_at=False,
        )
        return self.get_run(
            run_id, workspace_id=workspace_id, include_archived=True
        )

    def restore_run(
        self, run_id: str, *, workspace_id: str = LEGACY_WORKSPACE_ID
    ) -> dict[str, Any]:
        self._restore(
            "probe_runs",
            "run_id",
            run_id,
            workspace_id=workspace_id,
            has_updated_at=False,
        )
        return self.get_run(
            run_id, workspace_id=workspace_id, include_archived=True
        )

    def purge_run(
        self, run_id: str, *, workspace_id: str = LEGACY_WORKSPACE_ID
    ) -> None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT state, archived_at FROM lexsond.probe_runs WHERE run_id = %s AND workspace_id = %s",
                (run_id, workspace_id),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("run was not found")
            if row["state"] == "RUNNING" or row["archived_at"] is None:
                raise ControlPlaneConflict("terminal run must be archived before purge")
            if connection.execute(
                "SELECT 1 FROM lexsond.monitor_states state JOIN lexsond.monitor_policies policy ON policy.policy_id = state.policy_id WHERE state.last_run_id = %s AND policy.workspace_id = %s",
                (run_id, workspace_id),
            ).fetchone() is not None or connection.execute(
                "SELECT 1 FROM lexsond.monitor_policies WHERE last_run_id = %s AND workspace_id = %s",
                (run_id, workspace_id),
            ).fetchone() is not None:
                raise ControlPlaneConflict(
                    "current monitor state run cannot be purged until a newer run is recorded"
                )
            connection.execute(
                "DELETE FROM lexsond.probe_runs WHERE run_id = %s AND workspace_id = %s",
                (run_id, workspace_id),
            )

    def append_run_event(
        self, run_id: str, *, event_type: str, phase: str, status: str
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            return self._append_event(connection, run_id, event_type, phase, status)

    def list_run_events(
        self,
        run_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        after_sequence: int = 0,
    ) -> list[dict[str, Any]]:
        with self._pool.connection() as connection:
            if not connection.execute(
                "SELECT 1 FROM lexsond.probe_runs WHERE run_id = %s AND workspace_id = %s",
                (run_id, workspace_id),
            ).fetchone():
                raise ControlPlaneNotFound("run was not found")
            rows = connection.execute(
                """
                SELECT event_json FROM lexsond.probe_run_events
                WHERE run_id = %s AND sequence > %s ORDER BY sequence
                """,
                (run_id, max(int(after_sequence), 0)),
            ).fetchall()
        return [dict(row["event_json"]) for row in rows]

    def _archive(
        self,
        table: str,
        id_column: str,
        resource_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        has_updated_at: bool = True,
    ) -> None:
        updated = ", updated_at = clock_timestamp()" if has_updated_at else ""
        with self._pool.connection() as connection:
            cursor = connection.execute(
                f"UPDATE lexsond.{table} SET archived_at = clock_timestamp(){updated} WHERE {id_column} = %s AND workspace_id = %s AND archived_at IS NULL",
                (resource_id, workspace_id),
            )
            if cursor.rowcount != 1:
                _raise_archive_conflict(
                    connection,
                    table,
                    id_column,
                    resource_id,
                    workspace_id=workspace_id,
                    archived=True,
                )

    def _restore(
        self,
        table: str,
        id_column: str,
        resource_id: str,
        *,
        workspace_id: str = LEGACY_WORKSPACE_ID,
        has_updated_at: bool = True,
    ) -> None:
        updated = ", updated_at = clock_timestamp()" if has_updated_at else ""
        with self._pool.connection() as connection:
            cursor = connection.execute(
                f"UPDATE lexsond.{table} SET archived_at = NULL{updated} WHERE {id_column} = %s AND workspace_id = %s AND archived_at IS NOT NULL",
                (resource_id, workspace_id),
            )
            if cursor.rowcount != 1:
                _raise_archive_conflict(
                    connection,
                    table,
                    id_column,
                    resource_id,
                    workspace_id=workspace_id,
                    archived=False,
                )

    @staticmethod
    def _append_event(
        connection: Any,
        run_id: str,
        event_type: str,
        phase: str,
        status: str,
        *,
        source_event_id: str | None = None,
    ) -> dict[str, Any]:
        connection.execute(
            "SELECT 1 FROM lexsond.probe_runs WHERE run_id = %s FOR UPDATE",
            (run_id,),
        )
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM lexsond.probe_run_events WHERE run_id = %s",
            (run_id,),
        ).fetchone()["value"]
        event = {
            "event_id": str(uuid4()),
            "run_id": run_id,
            "sequence": sequence,
            "event_type": event_type,
            "phase": phase,
            "status": status,
            "occurred_at": datetime.now().astimezone().isoformat(),
        }
        if source_event_id is not None:
            event["source_event_id"] = source_event_id
        connection.execute(
            """
            INSERT INTO lexsond.probe_run_events (
                run_id, sequence, event_id, event_type, phase, status,
                occurred_at, source_event_id, event_json
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                sequence,
                event["event_id"],
                event_type,
                phase,
                status,
                event["occurred_at"],
                source_event_id,
                Jsonb(event),
            ),
        )
        return event


class WorkspaceControlPlaneStore:
    """Capability object that cannot perform an unscoped user-resource query."""

    _SCOPED_METHODS = frozenset(
        {
            "create_target",
            "get_target",
            "list_targets",
            "update_target",
            "archive_target",
            "restore_target",
            "purge_target",
            "create_suite",
            "get_suite",
            "list_suites",
            "update_suite",
            "list_suite_revisions",
            "get_suite_revision",
            "archive_suite",
            "restore_suite",
            "purge_suite",
            "create_agent_session",
            "get_agent_session",
            "list_agent_sessions",
            "update_agent_session",
            "archive_agent_session",
            "restore_agent_session",
            "purge_agent_session",
            "claim_agent_turn",
            "release_agent_turn",
            "renew_agent_turn",
            "quarantine_agent_session_credential",
            "append_agent_message",
            "list_agent_messages",
            "append_agent_event",
            "list_agent_events",
            "create_monitor_policy",
            "get_monitor_policy",
            "list_monitor_policies",
            "update_monitor_policy",
            "archive_monitor_policy",
            "restore_monitor_policy",
            "purge_monitor_policy",
            "request_monitor_policy_run",
            "list_monitor_incidents",
            "monitoring_overview",
            "create_run",
            "get_run",
            "list_runs",
            "cancel_run",
            "request_cancel_run",
            "archive_run",
            "restore_run",
            "purge_run",
            "list_run_events",
            "get_credential_profile",
            "list_credential_profiles",
            "create_model_catalog_snapshot",
            "get_model_catalog_snapshot",
            "create_probe_batch",
            "find_probe_batch_by_idempotency",
            "get_probe_batch",
            "list_probe_batches",
            "start_probe_batch_item",
            "finish_probe_batch_item",
            "request_probe_batch_cancel",
            "finalize_probe_batch",
            "list_probe_batch_events",
            "create_partner_application",
            "get_partner_application",
            "list_partner_applications",
            "update_partner_application",
            "submit_partner_application",
        }
    )

    def __init__(self, store: PostgresControlPlaneStore, workspace_id: str) -> None:
        self._store = store
        self.workspace_id = workspace_id

    def __getattr__(self, name: str) -> Any:
        if name not in self._SCOPED_METHODS:
            raise AttributeError(
                f"{type(self).__name__} does not expose unscoped operation {name!r}"
            )
        operation = getattr(self._store, name)

        def scoped(*args: Any, **kwargs: Any) -> Any:
            if "workspace_id" in kwargs:
                raise TypeError("workspace_id is already bound")
            return operation(*args, workspace_id=self.workspace_id, **kwargs)

        return scoped


def _target(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["target_id"]),
        "workspace_id": str(row["workspace_id"]),
        "name": row["name"],
        "target_kind": row["target_kind"],
        "provider_id": row["provider_id"],
        "target_base_url": row.get("target_base_url"),
        "target_kind": row.get("target_kind"),
        "protocol": row.get("protocol"),
        "base_url": row["base_url"],
        "default_model": row["default_model"],
        "credential_ref": row["credential_ref"],
        "credential_ref_configured": row["credential_ref"] is not None,
        "version": row["version"],
        "created_at": _time(row["created_at"]),
        "updated_at": _time(row["updated_at"]),
        "archived_at": _time(row["archived_at"]),
    }


def _credential_profile(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["credential_id"]),
        "workspace_id": str(row["workspace_id"]),
        "label": row["label"],
        "provider_id": row["provider_id"],
        "storage_backend": row["storage_backend"],
        "masked_suffix": row["masked_suffix"],
        "status": row["status"],
        "version": row["version"],
        "last_verified_at": _time(row["last_verified_at"]),
        "last_used_at": _time(row["last_used_at"]),
        "created_at": _time(row["created_at"]),
        "updated_at": _time(row["updated_at"]),
        "archived_at": _time(row["archived_at"]),
    }


def _model_catalog_snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "snapshot_id": str(row["snapshot_id"]),
        "workspace_id": str(row["workspace_id"]),
        "target_id": str(row["target_id"]),
        "credential_profile_id": (
            str(row["credential_profile_id"])
            if row["credential_profile_id"] is not None
            else None
        ),
        "credential_version": row.get("credential_version"),
        "target_version": row["target_version"],
        "provider_id": row["provider_id"],
        "models": list(row["models_json"]),
        "model_count": row["model_count"],
        "status": "STALE" if row.get("expired") else row["status"],
        "content_sha256": row["content_sha256"].strip(),
        "fetched_at": _time(row["fetched_at"]),
        "expires_at": _time(row["expires_at"]),
    }


def _probe_batch(row: Mapping[str, Any], items: list[Mapping[str, Any]]) -> dict[str, Any]:
    public_items = [_probe_batch_item(item) for item in items]
    counts: dict[str, int] = {}
    for item in public_items:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    return {
        "batch_id": str(row["batch_id"]),
        "workspace_id": str(row["workspace_id"]),
        "target_id": str(row["target_id"]),
        "credential_profile_id": (
            str(row["credential_profile_id"])
            if row["credential_profile_id"] is not None
            else None
        ),
        "catalog_snapshot_id": str(row["catalog_snapshot_id"]),
        "suite_revision_id": (
            str(row["suite_revision_id"])
            if row["suite_revision_id"] is not None
            else None
        ),
        "mode": row["mode"],
        "state": row["state"],
        "model_count": row["model_count"],
        "max_concurrency": row["max_concurrency"],
        "max_output_tokens": row["max_output_tokens"],
        "timeout_seconds": float(row["timeout_seconds"]),
        "confirm_unknown_cost": bool(row["confirm_unknown_cost"]),
        "cancel_requested_at": _time(row["cancel_requested_at"]),
        "created_at": _time(row["created_at"]),
        "finished_at": _time(row["finished_at"]),
        "counts": counts,
        "items": public_items,
    }


def _probe_batch_item(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "item_id": str(row["item_id"]),
        "ordinal": row["ordinal"],
        "model_id": row["model_id"],
        "state": row["state"],
        "run_id": str(row["run_id"]) if row["run_id"] is not None else None,
        "failure_code": row["failure_code"],
        "started_at": _time(row["started_at"]),
        "finished_at": _time(row["finished_at"]),
    }


def _probe_batch_event(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(row["event_id"]),
        "batch_id": str(row["batch_id"]),
        "sequence": row["sequence"],
        "event_type": row["event_type"],
        "item_id": str(row["item_id"]) if row["item_id"] is not None else None,
        "model_id": row["model_id"],
        "state": row["state"],
        "occurred_at": _time(row["occurred_at"]),
    }


def _append_probe_batch_event(
    connection: Any,
    *,
    workspace_id: str,
    batch_id: str,
    event_type: str,
    state: str,
    item_id: str | None = None,
    model_id: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO lexsond.probe_batch_events (
            batch_id, workspace_id, sequence, event_id, event_type,
            item_id, model_id, state
        ) SELECT %s, %s, COALESCE(MAX(sequence), 0) + 1, %s, %s, %s, %s, %s
          FROM lexsond.probe_batch_events WHERE batch_id = %s
        """,
        (
            batch_id, workspace_id, str(uuid4()), event_type,
            item_id, model_id, state, batch_id,
        ),
    )


def _partner_application(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["application_id"]),
        "workspace_id": str(row["workspace_id"]),
        "site_name": row["site_name"],
        "website_url": row["website_url"],
        "terms_url": row["terms_url"],
        "privacy_url": row["privacy_url"],
        "contact_email": row["contact_email"],
        "api_base_url": row["api_base_url"],
        "protocol": row["protocol"],
        "region": row["region"],
        "model_claims": list(row["model_claims"]),
        "pricing_notes": row["pricing_notes"],
        "source_evidence_url": row["source_evidence_url"],
        "monitoring_credential_id": (
            str(row["monitoring_credential_id"])
            if row["monitoring_credential_id"] is not None
            else None
        ),
        "status": row["status"],
        "version": row["version"],
        "created_at": _time(row["created_at"]),
        "updated_at": _time(row["updated_at"]),
        "submitted_at": _time(row["submitted_at"]),
    }


def _revision(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["revision_id"]),
        "workspace_id": str(row["workspace_id"]),
        "suite_id": str(row["suite_id"]),
        "revision": row["revision"],
        "document": dict(row["document_json"]),
        "sha256": row["document_sha256"].strip(),
        "created_at": _time(row.get("revision_created_at", row["created_at"])),
    }


def _suite(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["suite_id"]),
        "workspace_id": str(row["workspace_id"]),
        "name": row["name"],
        "description": row["description"],
        "version": row["version"],
        "created_at": _time(row["created_at"]),
        "updated_at": _time(row["updated_at"]),
        "archived_at": _time(row["archived_at"]),
        "latest_revision": _revision(row),
    }


def _agent_session(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "session_id": str(row["session_id"]),
        "workspace_id": str(row["workspace_id"]),
        "title": row["title"],
        "target_id": str(row["target_id"]),
        "target_version": row["target_version"],
        "base_url": row["base_url"],
        "target_kind": row["target_kind"],
        "provider_id": row["provider_id"],
        "model": row["model"],
        "skill_id": row["skill_id"],
        "version": row["version"],
        "created_at": _time(row["created_at"]),
        "updated_at": _time(row["updated_at"]),
        "archived_at": _time(row["archived_at"]),
    }


def _agent_message(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "message_id": str(row["message_id"]),
        "session_id": str(row["session_id"]),
        "sequence": row["sequence"],
        "role": row["role"],
        "content": row["content"],
        "metadata": dict(row["metadata_json"]),
        "created_at": _time(row["created_at"]),
    }


def _agent_event(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(row["event_id"]),
        "session_id": str(row["session_id"]),
        "sequence": row["sequence"],
        "event_type": row["event_type"],
        "name": row["name"],
        "status": row["status"],
        "payload": dict(row["payload_json"]),
        "occurred_at": _time(row["occurred_at"]),
    }


def _run(row: Mapping[str, Any], *, include_result: bool) -> dict[str, Any]:
    value = {
        "run_id": str(row["run_id"]),
        "workspace_id": str(row["workspace_id"]),
        "target_id": str(row["target_id"]) if row["target_id"] else None,
        "suite_revision_id": (
            str(row["suite_revision_id"]) if row["suite_revision_id"] else None
        ),
        "monitor_policy_id": (
            str(row["monitor_policy_id"]) if row.get("monitor_policy_id") else None
        ),
        "run_kind": row["run_kind"],
        "execution_backend": row["execution_backend"],
        "state": row["state"],
        "result_status": row["result_status"],
        "created_at": _time(row["created_at"]),
        "finished_at": _time(row["finished_at"]),
        "archived_at": _time(row["archived_at"]),
        "failure_code": row["failure_code"],
        "cancel_requested_at": _time(row["cancel_requested_at"]),
        "config": {
            "base_url": row["base_url"],
            "model": row["model"],
            "target_kind": row["target_kind"],
            "provider_id": row["provider_id"],
            "run_mode": row["run_mode"],
            "probe_type": row["probe_type"],
            "stream": row["streaming"],
            "timeout_seconds": row["timeout_seconds"],
            "max_output_tokens": row.get("max_output_tokens", 64),
        },
        "workflow": dict(row["workflow_json"]),
    }
    if include_result:
        value["result"] = dict(row["result_json"]) if row["result_json"] else None
    return value


def _monitor_policy(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["policy_id"]),
        "workspace_id": str(row["workspace_id"]),
        "name": row["name"],
        "target_id": str(row["target_id"]),
        "suite_revision_id": (
            str(row["suite_revision_id"]) if row["suite_revision_id"] else None
        ),
        "run_kind": row["run_kind"],
        "probe_type": row["probe_type"],
        "execution_backend": row["execution_backend"],
        "model": row["model"],
        "stream": bool(row["streaming"]),
        "timeout_seconds": float(row["timeout_seconds"]),
        "interval_seconds": int(row["interval_seconds"]),
        "failure_threshold": int(row["failure_threshold"]),
        "recovery_threshold": int(row["recovery_threshold"]),
        "schedule_offset_seconds": int(row["schedule_offset_seconds"]),
        "enabled": bool(row["enabled"]),
        "version": int(row["version"]),
        "next_run_at": _time(row["next_run_at"]),
        "last_run_at": _time(row["last_run_at"]),
        "last_run_id": str(row["last_run_id"]) if row["last_run_id"] else None,
        "last_dispatch_failure_code": row["last_dispatch_failure_code"],
        "created_at": _time(row["created_at"]),
        "updated_at": _time(row["updated_at"]),
        "archived_at": _time(row["archived_at"]),
    }


def _monitor_sample(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["sample_id"]),
        "policy_id": str(row["policy_id"]),
        "run_id": str(row["run_id"]),
        "observed_at": _time(row["observed_at"]),
        "observation": row["observation"],
        "error_class": row["error_class"],
        "p95_e2e_ms": row["p95_e2e_ms"],
        "p95_ttft_ms": row["p95_ttft_ms"],
    }


def _monitor_state(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "policy_id": str(row["policy_id"]),
        "status": row["status"],
        "consecutive_successes": int(row["consecutive_successes"]),
        "consecutive_failures": int(row["consecutive_failures"]),
        "last_observation": row["last_observation"],
        "last_run_id": str(row["last_run_id"]),
        "last_observed_at": _time(row["last_observed_at"]),
        "updated_at": _time(row["updated_at"]),
    }


def _monitor_incident(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["incident_id"]),
        "policy_id": str(row["policy_id"]),
        "run_id": str(row["run_id"]),
        "event_type": row["event_type"],
        "from_status": row["from_status"],
        "to_status": row["to_status"],
        "error_class": row["error_class"],
        "observed_at": _time(row["observed_at"]),
    }


def _require_monitor_references(
    connection: Any, value: Mapping[str, Any], workspace_id: str
) -> None:
    target = connection.execute(
        "SELECT archived_at FROM lexsond.targets WHERE target_id = %s AND workspace_id = %s FOR SHARE",
        (str(value["target_id"]), workspace_id),
    ).fetchone()
    if target is None or target["archived_at"] is not None:
        raise ControlPlaneConflict("monitor target is missing or archived")
    revision_id = value.get("suite_revision_id")
    if revision_id is None:
        return
    revision = connection.execute(
        """
        SELECT s.archived_at FROM lexsond.suite_revisions r
        JOIN lexsond.suites s ON s.suite_id = r.suite_id
        WHERE r.revision_id = %s AND r.workspace_id = %s FOR SHARE OF r, s
        """,
        (str(revision_id), workspace_id),
    ).fetchone()
    if revision is None or revision["archived_at"] is not None:
        raise ControlPlaneConflict("monitor suite revision is missing or archived")


def _suite_document(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("suite document must be an object")
    document = json.loads(json.dumps(value, ensure_ascii=False))
    compile_suite(document)
    return document


def _document_payload(value: Mapping[str, Any]) -> tuple[bytes, str]:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return encoded, hashlib.sha256(encoded).hexdigest()


def _time(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _raise_target_update_conflict(
    connection: Any, target_id: str, workspace_id: str
) -> None:
    row = connection.execute(
        "SELECT archived_at FROM lexsond.targets WHERE target_id = %s AND workspace_id = %s",
        (target_id, workspace_id),
    ).fetchone()
    if row is None:
        raise ControlPlaneNotFound("target was not found")
    if row["archived_at"] is not None:
        raise ControlPlaneConflict("archived target cannot be updated")
    raise ControlPlaneConflict("resource version is stale")


def _raise_monitor_update_conflict(
    connection: Any, policy_id: str, workspace_id: str
) -> None:
    row = connection.execute(
        "SELECT archived_at FROM lexsond.monitor_policies WHERE policy_id = %s AND workspace_id = %s",
        (policy_id, workspace_id),
    ).fetchone()
    if row is None:
        raise ControlPlaneNotFound("monitor policy was not found")
    if row["archived_at"] is not None:
        raise ControlPlaneConflict("archived monitor policy cannot be updated")
    raise ControlPlaneConflict("monitor policy version is stale")


def _raise_agent_update_conflict(
    connection: Any, session_id: str, workspace_id: str
) -> None:
    row = connection.execute(
        "SELECT archived_at FROM lexsond.agent_sessions WHERE session_id = %s AND workspace_id = %s",
        (session_id, workspace_id),
    ).fetchone()
    if row is None:
        raise ControlPlaneNotFound("Agent session was not found")
    if row["archived_at"] is not None:
        raise ControlPlaneConflict("archived Agent session cannot be updated")
    raise ControlPlaneConflict("resource version is stale")


def _raise_archive_conflict(
    connection: Any,
    table: str,
    id_column: str,
    resource_id: str,
    *,
    workspace_id: str,
    archived: bool,
) -> None:
    if not connection.execute(
        f"SELECT 1 FROM lexsond.{table} WHERE {id_column} = %s AND workspace_id = %s",
        (resource_id, workspace_id),
    ).fetchone():
        raise ControlPlaneNotFound("resource was not found")
    raise ControlPlaneConflict(
        "resource is already archived" if archived else "resource is not archived"
    )
