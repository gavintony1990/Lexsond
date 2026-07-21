from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from ..storage.postgres import PostgresPool
from ..storage.redaction import redact_text, redact_value
from ..suite import compile_suite
from .control_store import (
    ControlPlaneConflict,
    ControlPlaneNotFound,
    _contains_forbidden_agent_key,
    _require_agent_turn_token,
    _validate_agent_event_fields,
    _validate_agent_session_changes,
    _validate_agent_session_value,
)


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

    # Targets

    def create_target(self, value: Mapping[str, Any]) -> dict[str, Any]:
        target_id = str(uuid4())
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO lexsond.targets (
                        target_id, name, target_kind, provider_id, base_url,
                        default_model, credential_ref
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        target_id,
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
        return self.get_target(target_id, include_archived=True)

    def get_target(
        self, target_id: str, *, include_archived: bool = False
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM lexsond.targets WHERE target_id = %s",
                (target_id,),
            ).fetchone()
        if row is None or (row["archived_at"] is not None and not include_archived):
            raise ControlPlaneNotFound("target was not found")
        return _target(row)

    def list_targets(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        where = "" if include_archived else "WHERE archived_at IS NULL"
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM lexsond.targets {where} ORDER BY updated_at DESC, target_id"
            ).fetchall()
        return [_target(row) for row in rows]

    def update_target(
        self,
        target_id: str,
        changes: Mapping[str, Any],
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        if not changes:
            return self.get_target(target_id, include_archived=True)
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
        params = [*changes.values(), target_id, expected_version]
        try:
            with self._pool.connection() as connection:
                cursor = connection.execute(
                    f"""
                    UPDATE lexsond.targets
                    SET {', '.join(assignments)}, version = version + 1,
                        updated_at = clock_timestamp()
                    WHERE target_id = %s AND version = %s AND archived_at IS NULL
                    """,
                    params,
                )
                if cursor.rowcount != 1:
                    _raise_target_update_conflict(connection, target_id)
        except psycopg.errors.UniqueViolation as exc:
            raise ControlPlaneConflict("target update conflicts with stored data") from exc
        return self.get_target(target_id, include_archived=True)

    def archive_target(self, target_id: str) -> dict[str, Any]:
        self._archive("targets", "target_id", target_id)
        return self.get_target(target_id, include_archived=True)

    def restore_target(self, target_id: str) -> dict[str, Any]:
        self._restore("targets", "target_id", target_id)
        return self.get_target(target_id, include_archived=True)

    def purge_target(self, target_id: str) -> None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT archived_at FROM lexsond.targets WHERE target_id = %s",
                (target_id,),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("target was not found")
            if row["archived_at"] is None:
                raise ControlPlaneConflict("target must be archived before purge")
            if connection.execute(
                "SELECT 1 FROM lexsond.probe_runs WHERE target_id = %s LIMIT 1",
                (target_id,),
            ).fetchone():
                raise ControlPlaneConflict("target is referenced by a run")
            if connection.execute(
                "SELECT 1 FROM lexsond.agent_sessions WHERE target_id = %s LIMIT 1",
                (target_id,),
            ).fetchone():
                raise ControlPlaneConflict("target is referenced by an Agent session")
            connection.execute(
                "DELETE FROM lexsond.targets WHERE target_id = %s", (target_id,)
            )

    # Suites and immutable revisions

    def create_suite(self, value: Mapping[str, Any]) -> dict[str, Any]:
        document = _suite_document(value["document"])
        encoded, digest = _document_payload(document)
        suite_id, revision_id = str(uuid4()), str(uuid4())
        try:
            with self._pool.connection() as connection:
                connection.execute("SET CONSTRAINTS ALL DEFERRED")
                connection.execute(
                    """
                    INSERT INTO lexsond.suites (
                        suite_id, name, description, latest_revision_id
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (suite_id, value["name"], value.get("description", ""), revision_id),
                )
                connection.execute(
                    """
                    INSERT INTO lexsond.suite_revisions (
                        revision_id, suite_id, revision, document_sha256,
                        document_json
                    ) VALUES (%s, %s, 1, %s, %s)
                    """,
                    (revision_id, suite_id, digest, Jsonb(document)),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise ControlPlaneConflict("suite name or revision already exists") from exc
        return self.get_suite(suite_id, include_archived=True)

    def get_suite(
        self, suite_id: str, *, include_archived: bool = False
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT s.*, r.revision_id, r.revision, r.document_sha256,
                       r.document_json, r.created_at AS revision_created_at
                FROM lexsond.suites s
                JOIN lexsond.suite_revisions r
                  ON r.revision_id = s.latest_revision_id
                WHERE s.suite_id = %s
                """,
                (suite_id,),
            ).fetchone()
        if row is None or (row["archived_at"] is not None and not include_archived):
            raise ControlPlaneNotFound("suite was not found")
        return _suite(row)

    def list_suites(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        where = "" if include_archived else "WHERE s.archived_at IS NULL"
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT s.*, r.revision_id, r.revision, r.document_sha256,
                       r.document_json, r.created_at AS revision_created_at
                FROM lexsond.suites s
                JOIN lexsond.suite_revisions r
                  ON r.revision_id = s.latest_revision_id
                {where}
                ORDER BY s.updated_at DESC, s.suite_id
                """
            ).fetchall()
        return [_suite(row) for row in rows]

    def update_suite(
        self,
        suite_id: str,
        changes: Mapping[str, Any],
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        if set(changes) - {"name", "description", "document"}:
            raise ValueError("suite update contains unknown fields")
        if not changes:
            return self.get_suite(suite_id, include_archived=True)
        try:
            with self._pool.connection() as connection:
                current = connection.execute(
                    "SELECT * FROM lexsond.suites WHERE suite_id = %s FOR UPDATE",
                    (suite_id,),
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
                        "SELECT COALESCE(MAX(revision), 0) + 1 AS value FROM lexsond.suite_revisions WHERE suite_id = %s",
                        (suite_id,),
                    ).fetchone()["value"]
                    revision_id = str(uuid4())
                    connection.execute(
                        """
                        INSERT INTO lexsond.suite_revisions (
                            revision_id, suite_id, revision, document_sha256,
                            document_json
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        (revision_id, suite_id, revision, digest, Jsonb(document)),
                    )
                connection.execute(
                    """
                    UPDATE lexsond.suites
                    SET name = %s, description = %s, latest_revision_id = %s,
                        version = version + 1, updated_at = clock_timestamp()
                    WHERE suite_id = %s
                    """,
                    (
                        changes.get("name", current["name"]),
                        changes.get("description", current["description"]),
                        revision_id,
                        suite_id,
                    ),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise ControlPlaneConflict("suite update conflicts with stored data") from exc
        return self.get_suite(suite_id, include_archived=True)

    def list_suite_revisions(self, suite_id: str) -> list[dict[str, Any]]:
        with self._pool.connection() as connection:
            if not connection.execute(
                "SELECT 1 FROM lexsond.suites WHERE suite_id = %s", (suite_id,)
            ).fetchone():
                raise ControlPlaneNotFound("suite was not found")
            rows = connection.execute(
                "SELECT * FROM lexsond.suite_revisions WHERE suite_id = %s ORDER BY revision DESC",
                (suite_id,),
            ).fetchall()
        return [_revision(row) for row in rows]

    def get_suite_revision(self, revision_id: str) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT r.* FROM lexsond.suite_revisions r
                JOIN lexsond.suites s ON s.suite_id = r.suite_id
                WHERE r.revision_id = %s AND s.archived_at IS NULL
                """,
                (revision_id,),
            ).fetchone()
        if row is None:
            raise ControlPlaneNotFound("suite revision was not found")
        return _revision(row)

    def archive_suite(self, suite_id: str) -> dict[str, Any]:
        self._archive("suites", "suite_id", suite_id)
        return self.get_suite(suite_id, include_archived=True)

    def restore_suite(self, suite_id: str) -> dict[str, Any]:
        self._restore("suites", "suite_id", suite_id)
        return self.get_suite(suite_id, include_archived=True)

    def purge_suite(self, suite_id: str) -> None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT archived_at FROM lexsond.suites WHERE suite_id = %s",
                (suite_id,),
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
                WHERE sr.suite_id = %s LIMIT 1
                """,
                (suite_id,),
            ).fetchone():
                raise ControlPlaneConflict("suite is referenced by a run")
            connection.execute(
                "DELETE FROM lexsond.suite_revisions WHERE suite_id = %s",
                (suite_id,),
            )
            connection.execute(
                "DELETE FROM lexsond.suites WHERE suite_id = %s", (suite_id,)
            )

    # Agent sessions, checkpointer messages, and observable tool events

    def create_agent_session(self, value: Mapping[str, Any]) -> dict[str, Any]:
        _validate_agent_session_value(value)
        if redact_text(str(value["title"])) != value["title"]:
            raise ValueError("Agent session title contains a credential")
        if redact_text(str(value["model"])) != value["model"]:
            raise ValueError("Agent session model contains a credential")
        session_id = str(uuid4())
        with self._pool.connection() as connection:
            target = connection.execute(
                "SELECT archived_at FROM lexsond.targets WHERE target_id = %s FOR SHARE",
                (value["target_id"],),
            ).fetchone()
            if target is None or target["archived_at"] is not None:
                raise ControlPlaneConflict("Agent target is missing or archived")
            connection.execute(
                """
                INSERT INTO lexsond.agent_sessions (
                    session_id, title, target_id, target_version, base_url,
                    target_kind, provider_id, model, skill_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    session_id,
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
        return self.get_agent_session(session_id, include_archived=True)

    def get_agent_session(
        self, session_id: str, *, include_archived: bool = False
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM lexsond.agent_sessions WHERE session_id = %s",
                (session_id,),
            ).fetchone()
        if row is None or (row["archived_at"] is not None and not include_archived):
            raise ControlPlaneNotFound("Agent session was not found")
        return _agent_session(row)

    def list_agent_sessions(
        self, *, include_archived: bool = False, limit: int = 100
    ) -> list[dict[str, Any]]:
        where = "" if include_archived else "WHERE archived_at IS NULL"
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM lexsond.agent_sessions {where} ORDER BY updated_at DESC, session_id DESC LIMIT %s",
                (min(max(int(limit), 1), 100),),
            ).fetchall()
        return [_agent_session(row) for row in rows]

    def update_agent_session(
        self,
        session_id: str,
        changes: Mapping[str, Any],
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        if set(changes) - {"title", "skill_id"}:
            raise ValueError("Agent session update contains unknown fields")
        if not changes:
            return self.get_agent_session(session_id, include_archived=True)
        _validate_agent_session_changes(changes)
        assignments = [f"{field} = %s" for field in changes]
        params = [*changes.values(), session_id, expected_version]
        with self._pool.connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE lexsond.agent_sessions
                SET {', '.join(assignments)}, version = version + 1,
                    updated_at = clock_timestamp()
                WHERE session_id = %s AND version = %s AND archived_at IS NULL
                  AND (turn_lease_token IS NULL OR turn_lease_until <= clock_timestamp())
                """,
                params,
            )
            if cursor.rowcount != 1:
                _raise_agent_update_conflict(connection, session_id)
        return self.get_agent_session(session_id, include_archived=True)

    def archive_agent_session(self, session_id: str) -> dict[str, Any]:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE lexsond.agent_sessions
                SET archived_at = clock_timestamp(), updated_at = clock_timestamp()
                WHERE session_id = %s AND archived_at IS NULL
                  AND (turn_lease_token IS NULL OR turn_lease_until <= clock_timestamp())
                """,
                (session_id,),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT archived_at, turn_lease_token FROM lexsond.agent_sessions WHERE session_id = %s",
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise ControlPlaneNotFound("Agent session was not found")
                if row["archived_at"] is not None:
                    raise ControlPlaneConflict("Agent session is already archived")
                raise ControlPlaneConflict("Agent session has an active turn")
        return self.get_agent_session(session_id, include_archived=True)

    def restore_agent_session(self, session_id: str) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT s.archived_at, t.archived_at AS target_archived_at
                FROM lexsond.agent_sessions s
                JOIN lexsond.targets t ON t.target_id = s.target_id
                WHERE s.session_id = %s
                FOR UPDATE OF s FOR SHARE OF t
                """,
                (session_id,),
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
                WHERE session_id = %s
                """,
                (session_id,),
            )
        return self.get_agent_session(session_id, include_archived=True)

    def purge_agent_session(self, session_id: str) -> None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "DELETE FROM lexsond.agent_sessions WHERE session_id = %s AND archived_at IS NOT NULL RETURNING session_id",
                (session_id,),
            ).fetchone()
            if row is None:
                exists = connection.execute(
                    "SELECT 1 FROM lexsond.agent_sessions WHERE session_id = %s",
                    (session_id,),
                ).fetchone()
                if exists is None:
                    raise ControlPlaneNotFound("Agent session was not found")
                raise ControlPlaneConflict("Agent session must be archived before purge")

    def claim_agent_turn(self, session_id: str, *, lease_seconds: float) -> str:
        bounded = min(max(float(lease_seconds), 1.0), 600.0)
        token = str(uuid4())
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE lexsond.agent_sessions
                SET turn_lease_token = %s,
                    turn_lease_until = clock_timestamp() + (%s * INTERVAL '1 second')
                WHERE session_id = %s AND archived_at IS NULL
                  AND (turn_lease_token IS NULL OR turn_lease_until <= clock_timestamp())
                """,
                (token, bounded, session_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT archived_at FROM lexsond.agent_sessions WHERE session_id = %s",
                    (session_id,),
                ).fetchone()
                if row is None or row["archived_at"] is not None:
                    raise ControlPlaneNotFound("Agent session was not found")
                raise ControlPlaneConflict("Agent session already has an active turn")
        return token

    def release_agent_turn(self, session_id: str, token: str) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """
                UPDATE lexsond.agent_sessions
                SET turn_lease_token = NULL, turn_lease_until = NULL
                WHERE session_id = %s AND turn_lease_token = %s
                """,
                (session_id, token),
            )

    def renew_agent_turn(
        self, session_id: str, token: str, *, lease_seconds: float
    ) -> None:
        bounded = min(max(float(lease_seconds), 1.0), 600.0)
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE lexsond.agent_sessions
                SET turn_lease_until = clock_timestamp() + (%s * INTERVAL '1 second')
                WHERE session_id = %s AND turn_lease_token = %s
                  AND archived_at IS NULL
                """,
                (bounded, session_id, token),
            )
            if cursor.rowcount != 1:
                raise ControlPlaneConflict("Agent turn lease was lost")

    def quarantine_agent_session_credential(
        self,
        session_id: str,
        sensitive_value: str,
        *,
        turn_token: str | None = None,
    ) -> bool:
        now_safe = "[REDACTED]"
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM lexsond.agent_sessions WHERE session_id = %s FOR UPDATE",
                (session_id,),
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
                "SELECT archived_at, turn_lease_token FROM lexsond.agent_sessions WHERE session_id = %s FOR UPDATE",
                (session_id,),
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
        self, session_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.get_agent_session(session_id, include_archived=True)
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
                "SELECT archived_at, turn_lease_token FROM lexsond.agent_sessions WHERE session_id = %s FOR UPDATE",
                (session_id,),
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
        self, session_id: str, *, after_sequence: int = 0
    ) -> list[dict[str, Any]]:
        self.get_agent_session(session_id, include_archived=True)
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM lexsond.agent_events
                WHERE session_id = %s AND sequence > %s ORDER BY sequence
                """,
                (session_id, max(int(after_sequence), 0)),
            ).fetchall()
        return [_agent_event(row) for row in rows]

    # Runs and safe event stream

    def create_run(
        self,
        run_id: str,
        metadata: Mapping[str, Any],
        workflow: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        request_sha256: str | None = None,
    ) -> dict[str, Any]:
        existing = self._get_idempotent_run(idempotency_key, request_sha256)
        if existing is not None:
            return existing
        try:
            with self._pool.connection() as connection:
                target_id = metadata.get("target_id")
                if target_id is not None:
                    target = connection.execute(
                        "SELECT archived_at FROM lexsond.targets WHERE target_id = %s FOR SHARE",
                        (target_id,),
                    ).fetchone()
                    if target is None or target["archived_at"] is not None:
                        raise ControlPlaneConflict("run target is missing or archived")
                revision_id = metadata.get("suite_revision_id")
                if revision_id is not None:
                    revision = connection.execute(
                        """
                        SELECT s.archived_at FROM lexsond.suite_revisions r
                        JOIN lexsond.suites s ON s.suite_id = r.suite_id
                        WHERE r.revision_id = %s
                        FOR SHARE OF r, s
                        """,
                        (revision_id,),
                    ).fetchone()
                    if revision is None or revision["archived_at"] is not None:
                        raise ControlPlaneConflict("run suite revision is missing or archived")
                connection.execute(
                    """
                    INSERT INTO lexsond.probe_runs (
                        run_id, idempotency_key, request_sha256,
                        target_id, suite_revision_id, run_kind,
                        execution_backend, state, base_url, model, target_kind,
                        provider_id, run_mode, probe_type, streaming,
                        timeout_seconds, workflow_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'RUNNING', %s, %s, %s,
                              %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        idempotency_key,
                        request_sha256,
                        metadata.get("target_id"),
                        metadata.get("suite_revision_id"),
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
                        Jsonb(dict(workflow)),
                    ),
                )
                self._append_event(
                    connection, run_id, "RUN_STARTED", "binding", "RUNNING"
                )
        except psycopg.errors.UniqueViolation as exc:
            existing = self._get_idempotent_run(idempotency_key, request_sha256)
            if existing is not None:
                return existing
            raise ControlPlaneConflict("run creation conflicts with stored data") from exc
        return self.get_run(run_id)

    def _get_idempotent_run(
        self, idempotency_key: str | None, request_sha256: str | None
    ) -> dict[str, Any] | None:
        if idempotency_key is None:
            if request_sha256 is not None:
                raise ValueError("request_sha256 requires idempotency_key")
            return None
        if request_sha256 is None:
            raise ValueError("idempotency_key requires request_sha256")
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT run_id, request_sha256 FROM lexsond.probe_runs WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        if row["request_sha256"].strip() != request_sha256:
            raise ControlPlaneConflict("idempotency key belongs to a different run request")
        return self.get_run(str(row["run_id"]), include_archived=True)

    def get_run(self, run_id: str, *, include_archived: bool = False) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM lexsond.probe_runs WHERE run_id = %s", (run_id,)
            ).fetchone()
        if row is None or (row["archived_at"] is not None and not include_archived):
            raise ControlPlaneNotFound("run was not found")
        return _run(row, include_result=True)

    def list_runs(
        self, *, include_archived: bool = False, limit: int = 50
    ) -> list[dict[str, Any]]:
        where = "" if include_archived else "WHERE archived_at IS NULL"
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM lexsond.probe_runs {where} ORDER BY created_at DESC, run_id DESC LIMIT %s",
                (min(max(int(limit), 1), 100),),
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

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE lexsond.probe_runs
                SET state = 'CANCELLED', result_status = 'UNKNOWN',
                    finished_at = clock_timestamp(), failure_code = 'CANCEL_REQUESTED'
                WHERE run_id = %s AND state = 'RUNNING'
                """,
                (run_id,),
            )
            if cursor.rowcount != 1:
                if not connection.execute(
                    "SELECT 1 FROM lexsond.probe_runs WHERE run_id = %s", (run_id,)
                ).fetchone():
                    raise ControlPlaneNotFound("run was not found")
                raise ControlPlaneConflict("only a running run can be cancelled")
            self._append_event(connection, run_id, "RUN_CANCELLED", "complete", "CANCELLED")
        return self.get_run(run_id)

    def request_cancel_run(self, run_id: str) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT state, cancel_requested_at FROM lexsond.probe_runs WHERE run_id = %s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("run was not found")
            if row["state"] != "RUNNING":
                raise ControlPlaneConflict("only a running run can be cancelled")
            if row["cancel_requested_at"] is None:
                connection.execute(
                    "UPDATE lexsond.probe_runs SET cancel_requested_at = clock_timestamp() WHERE run_id = %s",
                    (run_id,),
                )
                self._append_event(
                    connection,
                    run_id,
                    "RUN_CANCEL_REQUESTED",
                    "complete",
                    "CANCEL_REQUESTED",
                )
        return self.get_run(run_id, include_archived=True)

    def archive_run(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id, include_archived=True)
        if run["state"] == "RUNNING":
            raise ControlPlaneConflict("running run cannot be archived")
        self._archive("probe_runs", "run_id", run_id, has_updated_at=False)
        return self.get_run(run_id, include_archived=True)

    def restore_run(self, run_id: str) -> dict[str, Any]:
        self._restore("probe_runs", "run_id", run_id, has_updated_at=False)
        return self.get_run(run_id, include_archived=True)

    def purge_run(self, run_id: str) -> None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT state, archived_at FROM lexsond.probe_runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("run was not found")
            if row["state"] == "RUNNING" or row["archived_at"] is None:
                raise ControlPlaneConflict("terminal run must be archived before purge")
            connection.execute(
                "DELETE FROM lexsond.probe_runs WHERE run_id = %s", (run_id,)
            )

    def append_run_event(
        self, run_id: str, *, event_type: str, phase: str, status: str
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            return self._append_event(connection, run_id, event_type, phase, status)

    def list_run_events(
        self, run_id: str, *, after_sequence: int = 0
    ) -> list[dict[str, Any]]:
        with self._pool.connection() as connection:
            if not connection.execute(
                "SELECT 1 FROM lexsond.probe_runs WHERE run_id = %s", (run_id,)
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
        has_updated_at: bool = True,
    ) -> None:
        updated = ", updated_at = clock_timestamp()" if has_updated_at else ""
        with self._pool.connection() as connection:
            cursor = connection.execute(
                f"UPDATE lexsond.{table} SET archived_at = clock_timestamp(){updated} WHERE {id_column} = %s AND archived_at IS NULL",
                (resource_id,),
            )
            if cursor.rowcount != 1:
                _raise_archive_conflict(connection, table, id_column, resource_id, archived=True)

    def _restore(
        self,
        table: str,
        id_column: str,
        resource_id: str,
        *,
        has_updated_at: bool = True,
    ) -> None:
        updated = ", updated_at = clock_timestamp()" if has_updated_at else ""
        with self._pool.connection() as connection:
            cursor = connection.execute(
                f"UPDATE lexsond.{table} SET archived_at = NULL{updated} WHERE {id_column} = %s AND archived_at IS NOT NULL",
                (resource_id,),
            )
            if cursor.rowcount != 1:
                _raise_archive_conflict(connection, table, id_column, resource_id, archived=False)

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


def _target(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["target_id"]),
        "name": row["name"],
        "target_kind": row["target_kind"],
        "provider_id": row["provider_id"],
        "base_url": row["base_url"],
        "default_model": row["default_model"],
        "credential_ref": row["credential_ref"],
        "credential_ref_configured": row["credential_ref"] is not None,
        "version": row["version"],
        "created_at": _time(row["created_at"]),
        "updated_at": _time(row["updated_at"]),
        "archived_at": _time(row["archived_at"]),
    }


def _revision(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["revision_id"]),
        "suite_id": str(row["suite_id"]),
        "revision": row["revision"],
        "document": dict(row["document_json"]),
        "sha256": row["document_sha256"].strip(),
        "created_at": _time(row.get("revision_created_at", row["created_at"])),
    }


def _suite(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["suite_id"]),
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
        "target_id": str(row["target_id"]) if row["target_id"] else None,
        "suite_revision_id": (
            str(row["suite_revision_id"]) if row["suite_revision_id"] else None
        ),
        "run_kind": row["run_kind"],
        "execution_backend": row["execution_backend"],
        "state": row["state"],
        "result_status": row["result_status"],
        "created_at": _time(row["created_at"]),
        "finished_at": _time(row["finished_at"]),
        "archived_at": _time(row["archived_at"]),
        "failure_code": row["failure_code"],
        "cancel_requested_at": row["cancel_requested_at"],
        "config": {
            "base_url": row["base_url"],
            "model": row["model"],
            "target_kind": row["target_kind"],
            "provider_id": row["provider_id"],
            "run_mode": row["run_mode"],
            "probe_type": row["probe_type"],
            "stream": row["streaming"],
            "timeout_seconds": row["timeout_seconds"],
        },
        "workflow": dict(row["workflow_json"]),
    }
    if include_result:
        value["result"] = dict(row["result_json"]) if row["result_json"] else None
    return value


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


def _raise_target_update_conflict(connection: Any, target_id: str) -> None:
    row = connection.execute(
        "SELECT archived_at FROM lexsond.targets WHERE target_id = %s", (target_id,)
    ).fetchone()
    if row is None:
        raise ControlPlaneNotFound("target was not found")
    if row["archived_at"] is not None:
        raise ControlPlaneConflict("archived target cannot be updated")
    raise ControlPlaneConflict("resource version is stale")


def _raise_agent_update_conflict(connection: Any, session_id: str) -> None:
    row = connection.execute(
        "SELECT archived_at FROM lexsond.agent_sessions WHERE session_id = %s",
        (session_id,),
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
    archived: bool,
) -> None:
    if not connection.execute(
        f"SELECT 1 FROM lexsond.{table} WHERE {id_column} = %s", (resource_id,)
    ).fetchone():
        raise ControlPlaneNotFound("resource was not found")
    raise ControlPlaneConflict(
        "resource is already archived" if archived else "resource is not archived"
    )
