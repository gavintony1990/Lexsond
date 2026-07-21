from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

from ..monitoring.state import (
    MonitorObservation,
    MonitorState,
    MonitorStatus,
    transition_state,
)
from ..storage.redaction import redact_text, redact_value
from ..suite import compile_suite


class ControlPlaneError(RuntimeError):
    pass


class ControlPlaneNotFound(ControlPlaneError):
    pass


class ControlPlaneConflict(ControlPlaneError):
    pass


class ControlPlaneStore:
    """SQLite control plane for mutable resources and immutable run evidence.

    The existing workflow/runtime stores remain authoritative for production
    canaries. This index gives the Web API one safe, local representation of
    targets, suite revisions and both local and Temporal run references.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._backup_legacy_database()
        self._initialize()

    # Targets

    def create_target(self, value: Mapping[str, Any]) -> dict[str, Any]:
        now = _now()
        target_id = str(uuid4())
        try:
            with self._session() as connection:
                connection.execute(
                    """
                    INSERT INTO control_targets (
                        target_id, name, target_kind, provider_id, base_url,
                        default_model, credential_ref, version, created_at,
                        updated_at, archived_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)
                    """,
                    (
                        target_id,
                        value["name"],
                        value["target_kind"],
                        value.get("provider_id"),
                        value["base_url"],
                        value.get("default_model", ""),
                        value.get("credential_ref"),
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ControlPlaneConflict("target name already exists") from exc
        return self.get_target(target_id, include_archived=True)

    def get_target(
        self, target_id: str, *, include_archived: bool = False
    ) -> dict[str, Any]:
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM control_targets WHERE target_id = ?",
                (target_id,),
            ).fetchone()
        if row is None or (row["archived_at"] is not None and not include_archived):
            raise ControlPlaneNotFound("target was not found")
        return self._target(row)

    def list_targets(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        where = "" if include_archived else "WHERE archived_at IS NULL"
        with self._session() as connection:
            rows = connection.execute(
                f"SELECT * FROM control_targets {where} ORDER BY updated_at DESC, target_id",
            ).fetchall()
        return [self._target(row) for row in rows]

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
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown target fields: {', '.join(sorted(unknown))}")
        assignments = [f"{field} = ?" for field in changes]
        params = list(changes.values())
        assignments.extend(("version = version + 1", "updated_at = ?"))
        params.extend((_now(), target_id, expected_version))
        try:
            with self._session() as connection:
                cursor = connection.execute(
                    f"""
                    UPDATE control_targets SET {', '.join(assignments)}
                    WHERE target_id = ? AND version = ? AND archived_at IS NULL
                    """,
                    params,
                )
                if cursor.rowcount != 1:
                    self._raise_missing_or_version(connection, "control_targets", "target_id", target_id)
        except sqlite3.IntegrityError as exc:
            raise ControlPlaneConflict("target update conflicts with stored data") from exc
        return self.get_target(target_id, include_archived=True)

    def archive_target(self, target_id: str) -> dict[str, Any]:
        return self._archive_resource("control_targets", "target_id", target_id, self.get_target)

    def restore_target(self, target_id: str) -> dict[str, Any]:
        return self._restore_resource("control_targets", "target_id", target_id, self.get_target)

    def purge_target(self, target_id: str) -> None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT archived_at FROM control_targets WHERE target_id = ?",
                (target_id,),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("target was not found")
            if row["archived_at"] is None:
                raise ControlPlaneConflict("target must be archived before purge")
            referenced = connection.execute(
                "SELECT 1 FROM control_runs WHERE target_id = ? LIMIT 1",
                (target_id,),
            ).fetchone()
            if referenced is not None:
                raise ControlPlaneConflict("target is referenced by a run")
            agent_reference = connection.execute(
                "SELECT 1 FROM agent_sessions WHERE target_id = ? LIMIT 1",
                (target_id,),
            ).fetchone()
            if agent_reference is not None:
                raise ControlPlaneConflict("target is referenced by an Agent session")
            if connection.execute(
                "SELECT 1 FROM monitor_policies WHERE target_id = ? LIMIT 1",
                (target_id,),
            ).fetchone() is not None:
                raise ControlPlaneConflict("target is referenced by a monitor policy")
            connection.execute("DELETE FROM control_targets WHERE target_id = ?", (target_id,))

    # Suites and immutable revisions

    def create_suite(self, value: Mapping[str, Any]) -> dict[str, Any]:
        document = _validated_suite_document(value["document"])
        suite_id = str(uuid4())
        revision_id = str(uuid4())
        now = _now()
        encoded, digest = _document_payload(document)
        try:
            with self._session() as connection:
                connection.execute(
                    """
                    INSERT INTO control_suites (
                        suite_id, name, description, latest_revision_id,
                        version, created_at, updated_at, archived_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?, NULL)
                    """,
                    (
                        suite_id,
                        value["name"],
                        value.get("description", ""),
                        revision_id,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO control_suite_revisions (
                        revision_id, suite_id, revision, document_json,
                        document_sha256, created_at
                    ) VALUES (?, ?, 1, ?, ?, ?)
                    """,
                    (revision_id, suite_id, encoded, digest, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ControlPlaneConflict("suite name or revision already exists") from exc
        return self.get_suite(suite_id, include_archived=True)

    def get_suite(
        self, suite_id: str, *, include_archived: bool = False
    ) -> dict[str, Any]:
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM control_suites WHERE suite_id = ?",
                (suite_id,),
            ).fetchone()
            if row is None or (row["archived_at"] is not None and not include_archived):
                raise ControlPlaneNotFound("suite was not found")
            revision = connection.execute(
                "SELECT * FROM control_suite_revisions WHERE revision_id = ?",
                (row["latest_revision_id"],),
            ).fetchone()
        return self._suite(row, revision)

    def list_suites(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        where = "" if include_archived else "WHERE s.archived_at IS NULL"
        with self._session() as connection:
            rows = connection.execute(
                f"""
                SELECT s.*, r.revision, r.document_json, r.document_sha256,
                       r.created_at AS revision_created_at
                FROM control_suites s
                JOIN control_suite_revisions r ON r.revision_id = s.latest_revision_id
                {where}
                ORDER BY s.updated_at DESC, s.suite_id
                """
            ).fetchall()
        return [self._suite(row, row) for row in rows]

    def update_suite(
        self,
        suite_id: str,
        changes: Mapping[str, Any],
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        allowed = {"name", "description", "document"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown suite fields: {', '.join(sorted(unknown))}")
        if not changes:
            return self.get_suite(suite_id, include_archived=True)
        now = _now()
        try:
            with self._session() as connection:
                current = connection.execute(
                    "SELECT * FROM control_suites WHERE suite_id = ?",
                    (suite_id,),
                ).fetchone()
                if current is None:
                    raise ControlPlaneNotFound("suite was not found")
                if current["archived_at"] is not None:
                    raise ControlPlaneConflict("archived suite cannot be updated")
                if current["version"] != expected_version:
                    raise ControlPlaneConflict("resource version is stale")

                next_revision_id = current["latest_revision_id"]
                if "document" in changes:
                    document = _validated_suite_document(changes["document"])
                    encoded, digest = _document_payload(document)
                    last_revision = connection.execute(
                        "SELECT MAX(revision) FROM control_suite_revisions WHERE suite_id = ?",
                        (suite_id,),
                    ).fetchone()[0]
                    next_revision_id = str(uuid4())
                    connection.execute(
                        """
                        INSERT INTO control_suite_revisions (
                            revision_id, suite_id, revision, document_json,
                            document_sha256, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            next_revision_id,
                            suite_id,
                            int(last_revision) + 1,
                            encoded,
                            digest,
                            now,
                        ),
                    )
                cursor = connection.execute(
                    """
                    UPDATE control_suites
                    SET name = ?, description = ?, latest_revision_id = ?,
                        version = version + 1, updated_at = ?
                    WHERE suite_id = ? AND version = ?
                    """,
                    (
                        changes.get("name", current["name"]),
                        changes.get("description", current["description"]),
                        next_revision_id,
                        now,
                        suite_id,
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ControlPlaneConflict("resource version is stale")
        except sqlite3.IntegrityError as exc:
            raise ControlPlaneConflict("suite update conflicts with stored data") from exc
        return self.get_suite(suite_id, include_archived=True)

    def list_suite_revisions(self, suite_id: str) -> list[dict[str, Any]]:
        with self._session() as connection:
            if connection.execute(
                "SELECT 1 FROM control_suites WHERE suite_id = ?", (suite_id,)
            ).fetchone() is None:
                raise ControlPlaneNotFound("suite was not found")
            rows = connection.execute(
                """
                SELECT * FROM control_suite_revisions
                WHERE suite_id = ? ORDER BY revision DESC
                """,
                (suite_id,),
            ).fetchall()
        return [self._revision(row) for row in rows]

    def get_suite_revision(self, revision_id: str) -> dict[str, Any]:
        with self._session() as connection:
            row = connection.execute(
                """
                SELECT r.* FROM control_suite_revisions r
                JOIN control_suites s ON s.suite_id = r.suite_id
                WHERE r.revision_id = ? AND s.archived_at IS NULL
                """,
                (revision_id,),
            ).fetchone()
        if row is None:
            raise ControlPlaneNotFound("suite revision was not found")
        return self._revision(row)

    def archive_suite(self, suite_id: str) -> dict[str, Any]:
        return self._archive_resource("control_suites", "suite_id", suite_id, self.get_suite)

    def restore_suite(self, suite_id: str) -> dict[str, Any]:
        return self._restore_resource("control_suites", "suite_id", suite_id, self.get_suite)

    def purge_suite(self, suite_id: str) -> None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT archived_at FROM control_suites WHERE suite_id = ?",
                (suite_id,),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("suite was not found")
            if row["archived_at"] is None:
                raise ControlPlaneConflict("suite must be archived before purge")
            referenced = connection.execute(
                """
                SELECT 1 FROM control_runs r
                JOIN control_suite_revisions sr ON sr.revision_id = r.suite_revision_id
                WHERE sr.suite_id = ? LIMIT 1
                """,
                (suite_id,),
            ).fetchone()
            if referenced is not None:
                raise ControlPlaneConflict("suite is referenced by a run")
            if connection.execute(
                """
                SELECT 1 FROM monitor_policies p
                JOIN control_suite_revisions sr ON sr.revision_id = p.suite_revision_id
                WHERE sr.suite_id = ? LIMIT 1
                """,
                (suite_id,),
            ).fetchone() is not None:
                raise ControlPlaneConflict("suite is referenced by a monitor policy")
            connection.execute("DELETE FROM control_suite_revisions WHERE suite_id = ?", (suite_id,))
            connection.execute("DELETE FROM control_suites WHERE suite_id = ?", (suite_id,))

    # Agent sessions, checkpointer messages, and observable tool events

    def create_agent_session(self, value: Mapping[str, Any]) -> dict[str, Any]:
        _validate_agent_session_value(value)
        session_id = str(uuid4())
        now = _now()
        with self._session() as connection:
            target = connection.execute(
                "SELECT archived_at FROM control_targets WHERE target_id = ?",
                (value["target_id"],),
            ).fetchone()
            if target is None or target["archived_at"] is not None:
                raise ControlPlaneConflict("Agent target is missing or archived")
            connection.execute(
                """
                INSERT INTO agent_sessions (
                    session_id, title, target_id, target_version, base_url,
                    target_kind, provider_id, model, skill_id, version,
                    created_at, updated_at, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)
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
                    now,
                    now,
                ),
            )
        return self.get_agent_session(session_id, include_archived=True)

    def get_agent_session(
        self, session_id: str, *, include_archived: bool = False
    ) -> dict[str, Any]:
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM agent_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None or (row["archived_at"] is not None and not include_archived):
            raise ControlPlaneNotFound("Agent session was not found")
        return self._agent_session(row)

    def list_agent_sessions(
        self, *, include_archived: bool = False, limit: int = 100
    ) -> list[dict[str, Any]]:
        where = "" if include_archived else "WHERE archived_at IS NULL"
        with self._session() as connection:
            rows = connection.execute(
                f"SELECT * FROM agent_sessions {where} ORDER BY updated_at DESC, session_id DESC LIMIT ?",
                (min(max(int(limit), 1), 100),),
            ).fetchall()
        return [self._agent_session(row) for row in rows]

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
        assignments = [f"{field} = ?" for field in changes]
        params = [*changes.values(), _now(), session_id, expected_version]
        with self._session() as connection:
            cursor = connection.execute(
                f"""
                UPDATE agent_sessions
                SET {', '.join(assignments)}, version = version + 1, updated_at = ?
                WHERE session_id = ? AND version = ? AND archived_at IS NULL
                  AND (turn_lease_token IS NULL OR turn_lease_until <= ?)
                """,
                [*params, _now()],
            )
            if cursor.rowcount != 1:
                self._raise_missing_or_version(
                    connection, "agent_sessions", "session_id", session_id
                )
        return self.get_agent_session(session_id, include_archived=True)

    def archive_agent_session(self, session_id: str) -> dict[str, Any]:
        now = _now()
        with self._session() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_sessions SET archived_at = ?, updated_at = ?
                WHERE session_id = ? AND archived_at IS NULL
                  AND (turn_lease_token IS NULL OR turn_lease_until <= ?)
                """,
                (now, now, session_id, now),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT archived_at, turn_lease_token, turn_lease_until FROM agent_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise ControlPlaneNotFound("Agent session was not found")
                if row["archived_at"] is not None:
                    raise ControlPlaneConflict("Agent session is already archived")
                raise ControlPlaneConflict("Agent session has an active turn")
        return self.get_agent_session(session_id, include_archived=True)

    def restore_agent_session(self, session_id: str) -> dict[str, Any]:
        now = _now()
        with self._session() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_sessions SET archived_at = NULL, updated_at = ?
                WHERE session_id = ? AND archived_at IS NOT NULL
                  AND EXISTS (
                    SELECT 1 FROM control_targets t
                    WHERE t.target_id = agent_sessions.target_id
                      AND t.archived_at IS NULL
                  )
                """,
                (now, session_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    """
                    SELECT s.archived_at, t.archived_at AS target_archived_at
                    FROM agent_sessions s
                    JOIN control_targets t ON t.target_id = s.target_id
                    WHERE s.session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise ControlPlaneNotFound("Agent session was not found")
                if row["archived_at"] is None:
                    raise ControlPlaneConflict("Agent session is not archived")
                raise ControlPlaneConflict("Agent target must be restored first")
        return self.get_agent_session(session_id, include_archived=True)

    def purge_agent_session(self, session_id: str) -> None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT archived_at, turn_lease_token FROM agent_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("Agent session was not found")
            if row["archived_at"] is None:
                raise ControlPlaneConflict("Agent session must be archived before purge")
            connection.execute(
                "DELETE FROM agent_sessions WHERE session_id = ?", (session_id,)
            )

    def claim_agent_turn(self, session_id: str, *, lease_seconds: float) -> str:
        bounded = min(max(float(lease_seconds), 1.0), 600.0)
        now_value = datetime.now(UTC)
        now, expires_at, token = (
            now_value.isoformat(),
            (now_value + timedelta(seconds=bounded)).isoformat(),
            str(uuid4()),
        )
        with self._session() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_sessions SET turn_lease_token = ?, turn_lease_until = ?
                WHERE session_id = ? AND archived_at IS NULL
                  AND (turn_lease_token IS NULL OR turn_lease_until <= ?)
                """,
                (token, expires_at, session_id, now),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT archived_at FROM agent_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None or row["archived_at"] is not None:
                    raise ControlPlaneNotFound("Agent session was not found")
                raise ControlPlaneConflict("Agent session already has an active turn")
        return token

    def release_agent_turn(self, session_id: str, token: str) -> None:
        with self._session() as connection:
            connection.execute(
                """
                UPDATE agent_sessions SET turn_lease_token = NULL, turn_lease_until = NULL
                WHERE session_id = ? AND turn_lease_token = ?
                """,
                (session_id, token),
            )

    def renew_agent_turn(
        self, session_id: str, token: str, *, lease_seconds: float
    ) -> None:
        bounded = min(max(float(lease_seconds), 1.0), 600.0)
        expires_at = (datetime.now(UTC) + timedelta(seconds=bounded)).isoformat()
        with self._session() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_sessions SET turn_lease_until = ?
                WHERE session_id = ? AND turn_lease_token = ? AND archived_at IS NULL
                """,
                (expires_at, session_id, token),
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
        """Atomically scrub/archive any checkpoint that collides with a key."""

        now = _now()
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM agent_sessions WHERE session_id = ?", (session_id,)
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
                        SELECT 1 FROM agent_messages
                        WHERE session_id = ? AND (
                            instr(content, ?) > 0 OR instr(metadata_json, ?) > 0
                        )
                    ) OR EXISTS (
                        SELECT 1 FROM agent_events
                        WHERE session_id = ? AND (
                            instr(name, ?) > 0 OR instr(payload_json, ?) > 0
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
                UPDATE agent_sessions SET title = ?, base_url = ?, provider_id = ?,
                    model = ?, skill_id = ?, version = version + 1,
                    updated_at = ?, archived_at = COALESCE(archived_at, ?)
                WHERE session_id = ?
                """,
                (
                    safe["title"], safe["base_url"], safe["provider_id"],
                    safe["model"], safe["skill_id"], now, now, session_id,
                ),
            )
            for message in connection.execute(
                "SELECT sequence, content, metadata_json FROM agent_messages WHERE session_id = ?",
                (session_id,),
            ).fetchall():
                connection.execute(
                    "UPDATE agent_messages SET content = ?, metadata_json = ? WHERE session_id = ? AND sequence = ?",
                    (
                        redact_text(message["content"], sensitive_values=(sensitive_value,)),
                        _encode(redact_value(json.loads(message["metadata_json"]), sensitive_values=(sensitive_value,))),
                        session_id,
                        message["sequence"],
                    ),
                )
            for event in connection.execute(
                "SELECT sequence, name, payload_json FROM agent_events WHERE session_id = ?",
                (session_id,),
            ).fetchall():
                connection.execute(
                    "UPDATE agent_events SET name = ?, payload_json = ? WHERE session_id = ? AND sequence = ?",
                    (
                        redact_text(event["name"], sensitive_values=(sensitive_value,)),
                        _encode(redact_value(json.loads(event["payload_json"]), sensitive_values=(sensitive_value,))),
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
        now, message_id = _now(), str(uuid4())
        with self._session() as connection:
            session = connection.execute(
                "SELECT archived_at, turn_lease_token FROM agent_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise ControlPlaneNotFound("Agent session was not found")
            if session["archived_at"] is not None:
                raise ControlPlaneConflict("archived Agent session cannot accept messages")
            _require_agent_turn_token(session, turn_token)
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM agent_messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO agent_messages (
                    session_id, sequence, message_id, role, content,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    sequence,
                    message_id,
                    role,
                    content,
                    _encode(metadata_value),
                    now,
                ),
            )
            connection.execute(
                "UPDATE agent_sessions SET updated_at = ? WHERE session_id = ?",
                (now, session_id),
            )
        return {
            "message_id": message_id,
            "session_id": session_id,
            "sequence": sequence,
            "role": role,
            "content": content,
            "metadata": metadata_value,
            "created_at": now,
        }

    def list_agent_messages(
        self, session_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.get_agent_session(session_id, include_archived=True)
        bounded = min(max(int(limit), 1), 100)
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM agent_messages WHERE session_id = ?
                    ORDER BY sequence DESC LIMIT ?
                ) ORDER BY sequence
                """,
                (session_id, bounded),
            ).fetchall()
        return [self._agent_message(row) for row in rows]

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
        now, event_id = _now(), str(uuid4())
        with self._session() as connection:
            session = connection.execute(
                "SELECT archived_at, turn_lease_token FROM agent_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise ControlPlaneNotFound("Agent session was not found")
            if session["archived_at"] is not None:
                raise ControlPlaneConflict("archived Agent session cannot accept events")
            _require_agent_turn_token(session, turn_token)
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM agent_events WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            value = {
                "event_id": event_id,
                "session_id": session_id,
                "sequence": sequence,
                "event_type": event_type,
                "name": name,
                "status": status,
                "payload": payload_value,
                "occurred_at": now,
            }
            connection.execute(
                """
                INSERT INTO agent_events (
                    session_id, sequence, event_id, event_type, name, status,
                    payload_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    sequence,
                    event_id,
                    event_type,
                    name,
                    status,
                    _encode(value["payload"]),
                    now,
                ),
            )
        return value

    def list_agent_events(
        self, session_id: str, *, after_sequence: int = 0
    ) -> list[dict[str, Any]]:
        self.get_agent_session(session_id, include_archived=True)
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_events
                WHERE session_id = ? AND sequence > ? ORDER BY sequence
                """,
                (session_id, max(int(after_sequence), 0)),
            ).fetchall()
        return [self._agent_event(row) for row in rows]

    # Continuous monitoring policies and derived health state

    def create_monitor_policy(self, value: Mapping[str, Any]) -> dict[str, Any]:
        _validate_monitor_policy_value(value)
        policy_id = str(uuid4())
        now = datetime.now(UTC)
        interval = int(value["interval_seconds"])
        offset = _schedule_offset(policy_id, interval)
        next_run_at = (
            (now + timedelta(seconds=offset)).isoformat()
            if bool(value.get("enabled", True))
            else None
        )
        try:
            with self._session() as connection:
                _require_monitor_references(connection, value)
                connection.execute(
                    """
                    INSERT INTO monitor_policies (
                        policy_id, name, target_id, suite_revision_id, run_kind,
                        probe_type, execution_backend, model, streaming,
                        timeout_seconds, interval_seconds, failure_threshold,
                        recovery_threshold, schedule_offset_seconds, enabled,
                        version, next_run_at, last_run_at, last_run_id,
                        last_dispatch_failure_code, lease_token, lease_until,
                        created_at, updated_at, archived_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1,
                              ?, NULL, NULL, NULL, NULL, NULL, ?, ?, NULL)
                    """,
                    (
                        policy_id,
                        value["name"],
                        str(value["target_id"]),
                        _optional_text(value.get("suite_revision_id")),
                        value["run_kind"],
                        str(value.get("probe_type") or "chat"),
                        value["execution_backend"],
                        value["model"],
                        1 if value["stream"] else 0,
                        float(value["timeout_seconds"]),
                        interval,
                        int(value["failure_threshold"]),
                        int(value["recovery_threshold"]),
                        offset,
                        1 if value.get("enabled", True) else 0,
                        next_run_at,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ControlPlaneConflict("monitor policy conflicts with stored data") from exc
        return self.get_monitor_policy(policy_id, include_archived=True)

    def get_monitor_policy(
        self, policy_id: str, *, include_archived: bool = False
    ) -> dict[str, Any]:
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM monitor_policies WHERE policy_id = ?", (policy_id,)
            ).fetchone()
        if row is None or (row["archived_at"] is not None and not include_archived):
            raise ControlPlaneNotFound("monitor policy was not found")
        return self._monitor_policy(row)

    def list_monitor_policies(
        self, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        where = "" if include_archived else "WHERE archived_at IS NULL"
        with self._session() as connection:
            rows = connection.execute(
                f"SELECT * FROM monitor_policies {where} ORDER BY updated_at DESC, policy_id"
            ).fetchall()
        return [self._monitor_policy(row) for row in rows]

    def update_monitor_policy(
        self,
        policy_id: str,
        changes: Mapping[str, Any],
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        if not changes:
            return self.get_monitor_policy(policy_id, include_archived=True)
        allowed = {
            "name", "target_id", "suite_revision_id", "run_kind", "probe_type",
            "execution_backend", "model", "stream", "timeout_seconds",
            "interval_seconds", "failure_threshold", "recovery_threshold", "enabled",
        }
        if set(changes) - allowed:
            raise ValueError("monitor policy update contains unknown fields")
        current = self.get_monitor_policy(policy_id, include_archived=True)
        if current["archived_at"] is not None:
            raise ControlPlaneConflict("archived monitor policy cannot be updated")
        merged = {**current, **changes}
        _validate_monitor_policy_value(merged)
        interval = int(merged["interval_seconds"])
        offset = _schedule_offset(policy_id, interval)
        now = datetime.now(UTC)
        normalized = {
            field: (
                str(value)
                if field in {"target_id", "suite_revision_id", "probe_type"} and value is not None
                else (1 if bool(value) else 0)
                if field in {"stream", "enabled"}
                else value
            )
            for field, value in changes.items()
        }
        if "interval_seconds" in changes:
            normalized["schedule_offset_seconds"] = offset
        schedule_changed = bool({"enabled", "interval_seconds"} & set(changes))
        if schedule_changed:
            normalized["next_run_at"] = (
                (now + timedelta(seconds=offset)).isoformat()
                if bool(merged["enabled"])
                else None
            )
            normalized["lease_token"] = None
            normalized["lease_until"] = None
        assignments = [f"{field} = ?" for field in normalized]
        params = [*normalized.values(), now.isoformat(), policy_id, expected_version]
        try:
            with self._session() as connection:
                _require_monitor_references(connection, merged)
                cursor = connection.execute(
                    f"""
                    UPDATE monitor_policies
                    SET {', '.join(assignments)}, version = version + 1, updated_at = ?
                    WHERE policy_id = ? AND version = ? AND archived_at IS NULL
                      AND (lease_until IS NULL OR lease_until <= ?)
                    """,
                    (*params, now.isoformat()),
                )
                if cursor.rowcount != 1:
                    locked = connection.execute(
                        "SELECT lease_until FROM monitor_policies WHERE policy_id = ?",
                        (policy_id,),
                    ).fetchone()
                    if (
                        locked is not None
                        and locked["lease_until"] is not None
                        and locked["lease_until"] > now.isoformat()
                    ):
                        raise ControlPlaneConflict(
                            "monitor policy dispatch is already in progress"
                        )
                    self._raise_missing_or_version(
                        connection, "monitor_policies", "policy_id", policy_id
                    )
        except sqlite3.IntegrityError as exc:
            raise ControlPlaneConflict("monitor policy update conflicts with stored data") from exc
        return self.get_monitor_policy(policy_id, include_archived=True)

    def archive_monitor_policy(self, policy_id: str) -> dict[str, Any]:
        now = _now()
        with self._session() as connection:
            cursor = connection.execute(
                """
                UPDATE monitor_policies SET archived_at = ?, enabled = 0,
                    next_run_at = NULL, lease_token = NULL, lease_until = NULL,
                    version = version + 1, updated_at = ?
                WHERE policy_id = ? AND archived_at IS NULL
                  AND (lease_until IS NULL OR lease_until <= ?)
                """,
                (now, now, policy_id, now),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT lease_until FROM monitor_policies WHERE policy_id = ?", (policy_id,)
                ).fetchone()
                if row is None:
                    raise ControlPlaneNotFound("monitor policy was not found")
                if row["lease_until"] is not None and row["lease_until"] > now:
                    raise ControlPlaneConflict("monitor policy dispatch is already in progress")
                raise ControlPlaneConflict("monitor policy is already archived")
        return self.get_monitor_policy(policy_id, include_archived=True)

    def restore_monitor_policy(self, policy_id: str) -> dict[str, Any]:
        now = _now()
        with self._session() as connection:
            cursor = connection.execute(
                """
                UPDATE monitor_policies SET archived_at = NULL,
                    version = version + 1, updated_at = ?
                WHERE policy_id = ? AND archived_at IS NOT NULL
                """,
                (now, policy_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT 1 FROM monitor_policies WHERE policy_id = ?", (policy_id,)
                ).fetchone()
                if row is None:
                    raise ControlPlaneNotFound("monitor policy was not found")
                raise ControlPlaneConflict("monitor policy is not archived")
        return self.get_monitor_policy(policy_id, include_archived=True)

    def purge_monitor_policy(self, policy_id: str) -> None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT archived_at FROM monitor_policies WHERE policy_id = ?", (policy_id,)
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("monitor policy was not found")
            if row["archived_at"] is None:
                raise ControlPlaneConflict("monitor policy must be archived before purge")
            connection.execute(
                "UPDATE control_runs SET monitor_policy_id = NULL WHERE monitor_policy_id = ?",
                (policy_id,),
            )
            connection.execute("DELETE FROM monitor_policies WHERE policy_id = ?", (policy_id,))

    def request_monitor_policy_run(self, policy_id: str) -> dict[str, Any]:
        now = _now()
        with self._session() as connection:
            cursor = connection.execute(
                """
                UPDATE monitor_policies SET next_run_at = ?, updated_at = ?,
                    last_dispatch_failure_code = NULL
                WHERE policy_id = ? AND archived_at IS NULL AND enabled = 1
                  AND (lease_until IS NULL OR lease_until <= ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM control_runs run
                      WHERE run.run_id = monitor_policies.last_run_id
                        AND run.state = 'RUNNING'
                  )
                """,
                (now, now, policy_id, now),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    """
                    SELECT policy.enabled, policy.archived_at, policy.lease_until,
                           run.state AS last_run_state
                    FROM monitor_policies policy
                    LEFT JOIN control_runs run ON run.run_id = policy.last_run_id
                    WHERE policy.policy_id = ?
                    """,
                    (policy_id,),
                ).fetchone()
                if row is None or row["archived_at"] is not None:
                    raise ControlPlaneNotFound("monitor policy was not found")
                if row["lease_until"] is not None and row["lease_until"] > now:
                    raise ControlPlaneConflict("monitor policy dispatch is already in progress")
                if row["last_run_state"] == "RUNNING":
                    raise ControlPlaneConflict("monitor policy already has a running probe")
                raise ControlPlaneConflict("monitor policy is disabled")
        return self.get_monitor_policy(policy_id)

    def claim_due_monitor_policies(
        self, *, now: str, limit: int, lease_seconds: float
    ) -> list[dict[str, Any]]:
        bounded = min(max(int(limit), 1), 32)
        if lease_seconds <= 0 or lease_seconds > 300:
            raise ValueError("monitor policy lease_seconds is out of bounds")
        now_value = _parse_utc(now)
        lease_until = (now_value + timedelta(seconds=lease_seconds)).isoformat()
        claims: list[dict[str, Any]] = []
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT * FROM monitor_policies
                WHERE enabled = 1 AND archived_at IS NULL
                  AND next_run_at IS NOT NULL AND next_run_at <= ?
                  AND (lease_until IS NULL OR lease_until <= ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM control_runs run
                      WHERE run.run_id = monitor_policies.last_run_id
                        AND run.state = 'RUNNING'
                  )
                ORDER BY next_run_at, policy_id LIMIT ?
                """,
                (now_value.isoformat(), now_value.isoformat(), bounded),
            ).fetchall()
            for row in rows:
                token = str(uuid4())
                cursor = connection.execute(
                    """
                    UPDATE monitor_policies SET lease_token = ?, lease_until = ?
                    WHERE policy_id = ? AND enabled = 1 AND archived_at IS NULL
                      AND (lease_until IS NULL OR lease_until <= ?)
                    """,
                    (token, lease_until, row["policy_id"], now_value.isoformat()),
                )
                if cursor.rowcount != 1:
                    continue
                claim = self._monitor_policy(row)
                claim["lease_token"] = token
                claim["scheduled_for"] = row["next_run_at"]
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
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM monitor_policies WHERE policy_id = ?", (policy_id,)
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("monitor policy was not found")
            if row["lease_token"] != lease_token or row["next_run_at"] != scheduled_for:
                raise ControlPlaneConflict("monitor policy dispatch lease is stale")
            if run_id is not None:
                linked = connection.execute(
                    "SELECT monitor_policy_id FROM control_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if linked is None or linked["monitor_policy_id"] != policy_id:
                    raise ControlPlaneConflict("monitor run does not match its policy")
            next_run = _next_schedule(
                scheduled_for,
                interval_seconds=int(row["interval_seconds"]),
                now=now,
            )
            connection.execute(
                """
                UPDATE monitor_policies SET next_run_at = ?, last_run_at = ?,
                    last_run_id = COALESCE(?, last_run_id),
                    last_dispatch_failure_code = ?, lease_token = NULL,
                    lease_until = NULL, updated_at = ?
                WHERE policy_id = ? AND lease_token = ?
                """,
                (
                    next_run,
                    now.isoformat(),
                    run_id,
                    failure_code,
                    now.isoformat(),
                    policy_id,
                    lease_token,
                ),
            )

    def record_monitor_run(self, run_id: str) -> dict[str, Any] | None:
        now = _now()
        with self._session() as connection:
            run = connection.execute(
                "SELECT * FROM control_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise ControlPlaneNotFound("run was not found")
            policy_id = run["monitor_policy_id"]
            if policy_id is None:
                return None
            if run["state"] == "RUNNING":
                raise ControlPlaneConflict("running monitor run cannot be projected")
            existing = connection.execute(
                "SELECT * FROM monitor_samples WHERE run_id = ?", (run_id,)
            ).fetchone()
            if existing is not None:
                return {
                    "sample": self._monitor_sample(existing),
                    "state": self._monitor_state_row(
                        connection.execute(
                            "SELECT * FROM monitor_states WHERE policy_id = ?", (policy_id,)
                        ).fetchone()
                    ),
                    "incident": None,
                    "replayed": True,
                }
            policy = connection.execute(
                "SELECT * FROM monitor_policies WHERE policy_id = ?", (policy_id,)
            ).fetchone()
            if policy is None:
                return None
            observation = _monitor_observation(run)
            result = json.loads(run["result_json"]) if run["result_json"] else None
            e2e_ms, ttft_ms, error_class = _monitor_metrics(result, run["failure_code"])
            observed_at = run["finished_at"] or now
            sample_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO monitor_samples (
                    sample_id, policy_id, run_id, observed_at, observation,
                    error_class, p95_e2e_ms, p95_ttft_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sample_id, policy_id, run_id, observed_at, observation.value,
                    error_class, e2e_ms, ttft_ms, now,
                ),
            )
            state_row = connection.execute(
                "SELECT * FROM monitor_states WHERE policy_id = ?", (policy_id,)
            ).fetchone()
            if (
                state_row is not None
                and _parse_utc(observed_at) <= _parse_utc(state_row["last_observed_at"])
            ):
                sample = connection.execute(
                    "SELECT * FROM monitor_samples WHERE sample_id = ?", (sample_id,)
                ).fetchone()
                return {
                    "sample": self._monitor_sample(sample),
                    "state": self._monitor_state_row(state_row),
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
            connection.execute(
                """
                INSERT INTO monitor_states (
                    policy_id, status, consecutive_successes,
                    consecutive_failures, last_observation, last_run_id,
                    last_observed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    status = excluded.status,
                    consecutive_successes = excluded.consecutive_successes,
                    consecutive_failures = excluded.consecutive_failures,
                    last_observation = excluded.last_observation,
                    last_run_id = excluded.last_run_id,
                    last_observed_at = excluded.last_observed_at,
                    updated_at = excluded.updated_at
                """,
                (
                    policy_id, transitioned.status.value,
                    transitioned.consecutive_successes,
                    transitioned.consecutive_failures, observation.value,
                    run_id, observed_at, now,
                ),
            )
            incident = None
            if transitioned.event_type is not None:
                incident_id = str(uuid4())
                from_status = (previous or MonitorState(MonitorStatus.UNKNOWN)).status.value
                connection.execute(
                    """
                    INSERT INTO monitor_incident_events (
                        incident_id, policy_id, run_id, event_type,
                        from_status, to_status, error_class, observed_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        incident_id, policy_id, run_id, transitioned.event_type,
                        from_status, transitioned.status.value, error_class,
                        observed_at, now,
                    ),
                )
                incident = {
                    "id": incident_id,
                    "policy_id": policy_id,
                    "run_id": run_id,
                    "event_type": transitioned.event_type,
                    "from_status": from_status,
                    "to_status": transitioned.status.value,
                    "error_class": error_class,
                    "observed_at": observed_at,
                }
            sample = connection.execute(
                "SELECT * FROM monitor_samples WHERE sample_id = ?", (sample_id,)
            ).fetchone()
            state = connection.execute(
                "SELECT * FROM monitor_states WHERE policy_id = ?", (policy_id,)
            ).fetchone()
        return {
            "sample": self._monitor_sample(sample),
            "state": self._monitor_state_row(state),
            "incident": incident,
            "replayed": False,
        }

    def list_monitor_incidents(
        self, *, policy_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        bounded = min(max(int(limit), 1), 500)
        where, params = ("", []) if policy_id is None else ("WHERE policy_id = ?", [policy_id])
        with self._session() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM monitor_incident_events {where}
                ORDER BY observed_at DESC, incident_id DESC LIMIT ?
                """,
                (*params, bounded),
            ).fetchall()
        return [self._monitor_incident(row) for row in rows]

    def monitoring_overview(
        self, *, window: str = "24h", include_archived: bool = False
    ) -> dict[str, Any]:
        window_seconds, bucket_seconds = _monitor_window(window)
        now = datetime.now(UTC)
        cutoff = (now - timedelta(seconds=window_seconds)).isoformat()
        policy_where = "" if include_archived else "WHERE p.archived_at IS NULL"
        with self._session() as connection:
            policies = connection.execute(
                f"""
                SELECT p.*, s.status, s.consecutive_successes,
                       s.consecutive_failures, s.last_observation,
                       s.last_run_id AS state_last_run_id,
                       s.last_observed_at
                FROM monitor_policies p
                LEFT JOIN monitor_states s ON s.policy_id = p.policy_id
                {policy_where}
                ORDER BY p.name COLLATE NOCASE, p.policy_id
                """
            ).fetchall()
            samples = connection.execute(
                """
                SELECT * FROM monitor_samples
                WHERE observed_at >= ? ORDER BY observed_at, sample_id
                """,
                (cutoff,),
            ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for sample in samples:
            grouped.setdefault(sample["policy_id"], []).append(sample)
        rows: list[dict[str, Any]] = []
        status_counts = {status.value.lower(): 0 for status in MonitorStatus}
        for policy in policies:
            status_value = policy["status"] or MonitorStatus.UNKNOWN.value
            status_counts[status_value.lower()] += 1
            policy_samples = grouped.get(policy["policy_id"], [])
            buckets = _aggregate_monitor_buckets(policy_samples, bucket_seconds)
            latest = policy_samples[-1] if policy_samples else None
            item = self._monitor_policy(policy)
            item.update(
                {
                    "status": status_value,
                    "consecutive_successes": int(policy["consecutive_successes"] or 0),
                    "consecutive_failures": int(policy["consecutive_failures"] or 0),
                    "last_observation": policy["last_observation"],
                    "last_observed_at": policy["last_observed_at"],
                    "latest_error_class": latest["error_class"] if latest else None,
                    "sample_count": len(policy_samples),
                    "buckets": buckets,
                }
            )
            rows.append(item)
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
            "policies": rows,
        }

    def prune_monitoring_data(
        self,
        *,
        samples_before: str,
        incidents_before: str,
        limit: int = 1000,
    ) -> dict[str, int]:
        """Delete bounded batches of derived monitoring history.

        Current health is retained in ``monitor_states``. Run records remain
        governed by the explicit archive/purge lifecycle and are not touched.
        """

        sample_cutoff = _parse_utc(samples_before).isoformat()
        incident_cutoff = _parse_utc(incidents_before).isoformat()
        bounded = min(max(int(limit), 1), 10_000)
        with self._session() as connection:
            sample_cursor = connection.execute(
                """
                DELETE FROM monitor_samples WHERE sample_id IN (
                    SELECT sample_id FROM monitor_samples
                    WHERE observed_at < ? ORDER BY observed_at LIMIT ?
                )
                """,
                (sample_cutoff, bounded),
            )
            incident_cursor = connection.execute(
                """
                DELETE FROM monitor_incident_events WHERE incident_id IN (
                    SELECT incident_id FROM monitor_incident_events
                    WHERE observed_at < ? ORDER BY observed_at LIMIT ?
                )
                """,
                (incident_cutoff, bounded),
            )
        return {
            "samples": max(sample_cursor.rowcount, 0),
            "incidents": max(incident_cursor.rowcount, 0),
        }

    # Unified run index and sanitized events

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
        now = _now()
        try:
            with self._session() as connection:
                target_id = metadata.get("target_id")
                if target_id is not None:
                    target = connection.execute(
                        "SELECT archived_at FROM control_targets WHERE target_id = ?",
                        (target_id,),
                    ).fetchone()
                    if target is None or target["archived_at"] is not None:
                        raise ControlPlaneConflict("run target is missing or archived")
                revision_id = metadata.get("suite_revision_id")
                if revision_id is not None:
                    revision = connection.execute(
                        """
                        SELECT s.archived_at FROM control_suite_revisions r
                        JOIN control_suites s ON s.suite_id = r.suite_id
                        WHERE r.revision_id = ?
                        """,
                        (revision_id,),
                    ).fetchone()
                    if revision is None or revision["archived_at"] is not None:
                        raise ControlPlaneConflict("run suite revision is missing or archived")
                policy_id = metadata.get("monitor_policy_id")
                if policy_id is not None:
                    policy = connection.execute(
                        "SELECT * FROM monitor_policies WHERE policy_id = ?",
                        (policy_id,),
                    ).fetchone()
                    if policy is None or policy["archived_at"] is not None:
                        raise ControlPlaneConflict("monitor policy is missing or archived")
                    expected = (
                        policy["target_id"],
                        policy["suite_revision_id"],
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
                INSERT INTO control_runs (
                    run_id, idempotency_key, request_sha256,
                    target_id, suite_revision_id, monitor_policy_id, run_kind,
                    execution_backend, state, result_status, created_at,
                    finished_at, base_url, model, target_kind, provider_id,
                    run_mode, probe_type, streaming, timeout_seconds,
                    result_json, failure_code, workflow_json, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING', NULL, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL)
                """,
                (
                    run_id,
                    idempotency_key,
                    request_sha256,
                    metadata.get("target_id"),
                    metadata.get("suite_revision_id"),
                    metadata.get("monitor_policy_id"),
                    metadata.get("run_kind", "component"),
                    metadata.get("execution_backend", "local"),
                    now,
                    metadata["base_url"],
                    metadata["model"],
                    metadata["target_kind"],
                    metadata.get("provider_id"),
                    metadata["run_mode"],
                    metadata["probe_type"],
                    1 if metadata["stream"] else 0,
                    float(metadata["timeout_seconds"]),
                    _encode(workflow),
                ),
                )
                self._append_event(
                    connection,
                    run_id,
                    event_type="RUN_STARTED",
                    phase="binding",
                    status="RUNNING",
                    occurred_at=now,
                )
        except sqlite3.IntegrityError as exc:
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
        with self._session() as connection:
            row = connection.execute(
                "SELECT run_id, request_sha256 FROM control_runs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_sha256:
            raise ControlPlaneConflict("idempotency key belongs to a different run request")
        return self.get_run(row["run_id"], include_archived=True)

    def get_run(self, run_id: str, *, include_archived: bool = False) -> dict[str, Any]:
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM control_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None or (row["archived_at"] is not None and not include_archived):
            raise ControlPlaneNotFound("run was not found")
        return self._run(row, include_result=True)

    def list_runs(
        self, *, include_archived: bool = False, limit: int = 50
    ) -> list[dict[str, Any]]:
        bounded = min(max(int(limit), 1), 100)
        where = "" if include_archived else "WHERE archived_at IS NULL"
        with self._session() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM control_runs {where}
                ORDER BY created_at DESC, run_id DESC LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        return [self._run(row, include_result=False) for row in rows]

    def list_temporal_runs_for_recovery(self) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT * FROM control_runs
                WHERE state = 'RUNNING' AND execution_backend = 'temporal'
                ORDER BY created_at, run_id
                """
            ).fetchall()
        return [self._run(row, include_result=False) for row in rows]

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
        now = _now()
        with self._session() as connection:
            if source_event_id is not None and connection.execute(
                "SELECT 1 FROM control_run_events WHERE run_id = ? AND source_event_id = ?",
                (run_id, source_event_id),
            ).fetchone() is not None:
                return
            cursor = connection.execute(
                """
                UPDATE control_runs SET workflow_json = ?
                WHERE run_id = ? AND state = 'RUNNING'
                """,
                (_encode(workflow), run_id),
            )
            if cursor.rowcount != 1:
                raise ControlPlaneConflict("run workflow is no longer mutable")
            self._append_event(
                connection, run_id, event_type, phase, status, now,
                source_event_id=source_event_id,
            )

    def complete_run(
        self,
        run_id: str,
        result: Mapping[str, Any],
        workflow: Mapping[str, Any],
    ) -> None:
        now = _now()
        payload = _encode(result)
        with self._session() as connection:
            cursor = connection.execute(
                """
                UPDATE control_runs
                SET state = 'COMPLETED', result_status = ?, finished_at = ?,
                    result_json = ?, failure_code = NULL, workflow_json = ?
                WHERE run_id = ? AND state = 'RUNNING'
                """,
                (result["status"], now, payload, _encode(workflow), run_id),
            )
            if cursor.rowcount != 1:
                raise ControlPlaneConflict("run is no longer running")
            self._append_event(connection, run_id, "RUN_COMPLETED", "complete", result["status"], now)

    def fail_run(
        self, run_id: str, failure_code: str, workflow: Mapping[str, Any]
    ) -> None:
        now = _now()
        with self._session() as connection:
            cursor = connection.execute(
                """
                UPDATE control_runs
                SET state = 'FAILED', result_status = 'FAIL', finished_at = ?,
                    failure_code = ?, result_json = NULL, workflow_json = ?
                WHERE run_id = ? AND state = 'RUNNING'
                """,
                (now, failure_code, _encode(workflow), run_id),
            )
            if cursor.rowcount != 1:
                raise ControlPlaneConflict("run is no longer running")
            self._append_event(connection, run_id, "RUN_FAILED", "complete", "FAIL", now)

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        now = _now()
        with self._session() as connection:
            cursor = connection.execute(
                """
                UPDATE control_runs
                SET state = 'CANCELLED', result_status = 'UNKNOWN',
                    finished_at = ?, failure_code = 'CANCEL_REQUESTED'
                WHERE run_id = ? AND state = 'RUNNING'
                """,
                (now, run_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT state FROM control_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    raise ControlPlaneNotFound("run was not found")
                raise ControlPlaneConflict("only a running run can be cancelled")
            self._append_event(connection, run_id, "RUN_CANCELLED", "complete", "CANCELLED", now)
        return self.get_run(run_id)

    def request_cancel_run(self, run_id: str) -> dict[str, Any]:
        now = _now()
        with self._session() as connection:
            row = connection.execute(
                "SELECT state, cancel_requested_at FROM control_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("run was not found")
            if row["state"] != "RUNNING":
                raise ControlPlaneConflict("only a running run can be cancelled")
            if row["cancel_requested_at"] is None:
                connection.execute(
                    "UPDATE control_runs SET cancel_requested_at = ? WHERE run_id = ?",
                    (now, run_id),
                )
                self._append_event(
                    connection,
                    run_id,
                    "RUN_CANCEL_REQUESTED",
                    "complete",
                    "CANCEL_REQUESTED",
                    now,
                )
        return self.get_run(run_id, include_archived=True)

    def archive_run(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id, include_archived=True)
        if run["state"] == "RUNNING":
            raise ControlPlaneConflict("running run cannot be archived")
        return self._archive_resource("control_runs", "run_id", run_id, self.get_run)

    def restore_run(self, run_id: str) -> dict[str, Any]:
        return self._restore_resource("control_runs", "run_id", run_id, self.get_run)

    def purge_run(self, run_id: str) -> None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT state, archived_at FROM control_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("run was not found")
            if row["state"] == "RUNNING" or row["archived_at"] is None:
                raise ControlPlaneConflict("terminal run must be archived before purge")
            if connection.execute(
                "SELECT 1 FROM monitor_states WHERE last_run_id = ?", (run_id,)
            ).fetchone() is not None or connection.execute(
                "SELECT 1 FROM monitor_policies WHERE last_run_id = ?", (run_id,)
            ).fetchone() is not None:
                raise ControlPlaneConflict(
                    "current monitor state run cannot be purged until a newer run is recorded"
                )
            connection.execute("DELETE FROM control_run_events WHERE run_id = ?", (run_id,))
            connection.execute("DELETE FROM control_runs WHERE run_id = ?", (run_id,))

    def append_run_event(
        self,
        run_id: str,
        *,
        event_type: str,
        phase: str,
        status: str,
    ) -> dict[str, Any]:
        with self._session() as connection:
            return self._append_event(
                connection, run_id, event_type, phase, status, _now()
            )

    def list_run_events(self, run_id: str, *, after_sequence: int = 0) -> list[dict[str, Any]]:
        with self._session() as connection:
            if connection.execute(
                "SELECT 1 FROM control_runs WHERE run_id = ?", (run_id,)
            ).fetchone() is None:
                raise ControlPlaneNotFound("run was not found")
            rows = connection.execute(
                """
                SELECT event_json FROM control_run_events
                WHERE run_id = ? AND sequence > ? ORDER BY sequence
                """,
                (run_id, max(int(after_sequence), 0)),
            ).fetchall()
        return [json.loads(row["event_json"]) for row in rows]

    # Internal

    def _initialize(self) -> None:
        with self._session() as connection:
            connection.executescript(_SCHEMA)
            event_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(control_run_events)")
            }
            if "source_event_id" not in event_columns:
                connection.execute(
                    "ALTER TABLE control_run_events ADD COLUMN source_event_id TEXT"
                )
            run_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(control_runs)")
            }
            if "idempotency_key" not in run_columns:
                connection.execute(
                    "ALTER TABLE control_runs ADD COLUMN idempotency_key TEXT"
                )
            if "request_sha256" not in run_columns:
                connection.execute(
                    "ALTER TABLE control_runs ADD COLUMN request_sha256 TEXT"
                )
            if "cancel_requested_at" not in run_columns:
                connection.execute(
                    "ALTER TABLE control_runs ADD COLUMN cancel_requested_at TEXT"
                )
            if "monitor_policy_id" not in run_columns:
                connection.execute(
                    "ALTER TABLE control_runs ADD COLUMN monitor_policy_id TEXT"
                )
            agent_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(agent_sessions)")
            }
            if "turn_lease_token" not in agent_columns:
                connection.execute(
                    "ALTER TABLE agent_sessions ADD COLUMN turn_lease_token TEXT"
                )
            if "turn_lease_until" not in agent_columns:
                connection.execute(
                    "ALTER TABLE agent_sessions ADD COLUMN turn_lease_until TEXT"
                )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_agent_turn_lease_insert
                BEFORE INSERT ON agent_sessions
                WHEN (NEW.turn_lease_token IS NULL) != (NEW.turn_lease_until IS NULL)
                BEGIN
                    SELECT RAISE(ABORT, 'Agent turn lease fields must be paired');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_agent_turn_lease_update
                BEFORE UPDATE OF turn_lease_token, turn_lease_until ON agent_sessions
                WHEN (NEW.turn_lease_token IS NULL) != (NEW.turn_lease_until IS NULL)
                BEGIN
                    SELECT RAISE(ABORT, 'Agent turn lease fields must be paired');
                END
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_control_run_event_source
                ON control_run_events(run_id, source_event_id)
                WHERE source_event_id IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_control_run_idempotency
                ON control_runs(idempotency_key)
                WHERE idempotency_key IS NOT NULL
                """
            )
            for version in (1, 2, 3, 4, 5, 6, 7, 8):
                connection.execute(
                    "INSERT OR IGNORE INTO control_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, _now()),
                )
            self._import_legacy_runs(connection)

    def _backup_legacy_database(self) -> None:
        if not self.database_path.is_file():
            return
        backup = self.database_path.with_suffix(self.database_path.suffix + ".pre-control-plane.bak")
        if backup.exists():
            return
        try:
            with sqlite3.connect(self.database_path) as connection:
                legacy = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_probe_runs'"
                ).fetchone()
                migrated = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='control_schema_migrations'"
                ).fetchone()
        except sqlite3.DatabaseError:
            return
        if legacy is not None and migrated is None:
            shutil.copy2(self.database_path, backup)

    @staticmethod
    def _import_legacy_runs(connection: sqlite3.Connection) -> None:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_probe_runs'"
        ).fetchone()
        if exists is None:
            return
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(web_probe_runs)")
        }
        required = {
            "run_id", "state", "result_status", "created_at", "finished_at",
            "base_url", "model", "target_kind", "provider_id", "run_mode",
            "probe_type", "streaming", "timeout_seconds", "result_json",
            "failure_code", "workflow_json",
        }
        if not required.issubset(columns):
            return
        connection.execute(
            """
            INSERT OR IGNORE INTO control_runs (
                run_id, target_id, suite_revision_id, run_kind,
                execution_backend, state, result_status, created_at, finished_at,
                base_url, model, target_kind, provider_id, run_mode, probe_type,
                streaming, timeout_seconds, result_json, failure_code,
                workflow_json, archived_at
            )
            SELECT run_id, NULL, NULL,
                   CASE WHEN run_mode = 'canary' THEN 'suite' ELSE 'component' END,
                   'local', state, result_status, created_at, finished_at,
                   base_url, model, target_kind, provider_id, run_mode, probe_type,
                   streaming, timeout_seconds, result_json, failure_code,
                   workflow_json, NULL
            FROM web_probe_runs
            """
        )
        rows = connection.execute(
            """
            SELECT run_id, created_at, state FROM control_runs r
            WHERE NOT EXISTS (
                SELECT 1 FROM control_run_events e WHERE e.run_id = r.run_id
            )
            """
        ).fetchall()
        for row in rows:
            ControlPlaneStore._append_event(
                connection,
                row["run_id"],
                "LEGACY_RUN_IMPORTED",
                "complete" if row["state"] != "RUNNING" else "binding",
                row["state"],
                row["created_at"],
            )

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        phase: str,
        status: str,
        occurred_at: str,
        *,
        source_event_id: str | None = None,
    ) -> dict[str, Any]:
        if connection.execute(
            "SELECT 1 FROM control_runs WHERE run_id = ?", (run_id,)
        ).fetchone() is None:
            raise ControlPlaneNotFound("run was not found")
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM control_run_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
        event = {
            "event_id": str(uuid4()),
            "run_id": run_id,
            "sequence": sequence,
            "event_type": event_type,
            "phase": phase,
            "status": status,
            "occurred_at": occurred_at,
        }
        if source_event_id is not None:
            event["source_event_id"] = source_event_id
        connection.execute(
            """
            INSERT INTO control_run_events (
                run_id, sequence, event_id, event_type, phase, status,
                occurred_at, source_event_id, event_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                sequence,
                event["event_id"],
                event_type,
                phase,
                status,
                occurred_at,
                source_event_id,
                _encode(event),
            ),
        )
        return event

    def _archive_resource(self, table: str, id_column: str, resource_id: str, getter: Any) -> dict[str, Any]:
        now = _now()
        with self._session() as connection:
            cursor = connection.execute(
                f"UPDATE {table} SET archived_at = ?, updated_at = ? WHERE {id_column} = ? AND archived_at IS NULL"
                if table != "control_runs"
                else f"UPDATE {table} SET archived_at = ? WHERE {id_column} = ? AND archived_at IS NULL",
                (now, now, resource_id) if table != "control_runs" else (now, resource_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    f"SELECT 1 FROM {table} WHERE {id_column} = ?", (resource_id,)
                ).fetchone()
                if row is None:
                    raise ControlPlaneNotFound("resource was not found")
                raise ControlPlaneConflict("resource is already archived")
        return getter(resource_id, include_archived=True)

    def _restore_resource(self, table: str, id_column: str, resource_id: str, getter: Any) -> dict[str, Any]:
        now = _now()
        with self._session() as connection:
            cursor = connection.execute(
                f"UPDATE {table} SET archived_at = NULL, updated_at = ? WHERE {id_column} = ? AND archived_at IS NOT NULL"
                if table != "control_runs"
                else f"UPDATE {table} SET archived_at = NULL WHERE {id_column} = ? AND archived_at IS NOT NULL",
                (now, resource_id) if table != "control_runs" else (resource_id,),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    f"SELECT 1 FROM {table} WHERE {id_column} = ?", (resource_id,)
                ).fetchone()
                if row is None:
                    raise ControlPlaneNotFound("resource was not found")
                raise ControlPlaneConflict("resource is not archived")
        return getter(resource_id, include_archived=True)

    @staticmethod
    def _raise_missing_or_version(
        connection: sqlite3.Connection, table: str, id_column: str, resource_id: str
    ) -> None:
        row = connection.execute(
            f"SELECT archived_at FROM {table} WHERE {id_column} = ?", (resource_id,)
        ).fetchone()
        if row is None:
            raise ControlPlaneNotFound("resource was not found")
        if row["archived_at"] is not None:
            raise ControlPlaneConflict("archived resource cannot be updated")
        raise ControlPlaneConflict("resource version is stale")

    @staticmethod
    def _target(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["target_id"],
            "name": row["name"],
            "target_kind": row["target_kind"],
            "provider_id": row["provider_id"],
            "base_url": row["base_url"],
            "default_model": row["default_model"],
            "credential_ref": row["credential_ref"],
            "credential_ref_configured": row["credential_ref"] is not None,
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "archived_at": row["archived_at"],
        }

    @staticmethod
    def _revision(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["revision_id"],
            "suite_id": row["suite_id"],
            "revision": row["revision"],
            "document": json.loads(row["document_json"]),
            "sha256": row["document_sha256"],
            "created_at": row["created_at"],
        }

    @classmethod
    def _suite(cls, row: sqlite3.Row, revision: sqlite3.Row) -> dict[str, Any]:
        revision_value = (
            cls._revision(revision)
            if "revision_id" in revision.keys()
            else {
                "id": row["latest_revision_id"],
                "suite_id": row["suite_id"],
                "revision": revision["revision"],
                "document": json.loads(revision["document_json"]),
                "sha256": revision["document_sha256"],
                "created_at": revision["revision_created_at"],
            }
        )
        return {
            "id": row["suite_id"],
            "name": row["name"],
            "description": row["description"],
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "archived_at": row["archived_at"],
            "latest_revision": revision_value,
        }

    @staticmethod
    def _agent_session(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "session_id": row["session_id"],
            "title": row["title"],
            "target_id": row["target_id"],
            "target_version": row["target_version"],
            "base_url": row["base_url"],
            "target_kind": row["target_kind"],
            "provider_id": row["provider_id"],
            "model": row["model"],
            "skill_id": row["skill_id"],
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "archived_at": row["archived_at"],
        }

    @staticmethod
    def _agent_message(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "message_id": row["message_id"],
            "session_id": row["session_id"],
            "sequence": row["sequence"],
            "role": row["role"],
            "content": row["content"],
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _agent_event(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "session_id": row["session_id"],
            "sequence": row["sequence"],
            "event_type": row["event_type"],
            "name": row["name"],
            "status": row["status"],
            "payload": json.loads(row["payload_json"]),
            "occurred_at": row["occurred_at"],
        }

    @staticmethod
    def _run(row: sqlite3.Row, *, include_result: bool) -> dict[str, Any]:
        value = {
            "run_id": row["run_id"],
            "target_id": row["target_id"],
            "suite_revision_id": row["suite_revision_id"],
            "monitor_policy_id": row["monitor_policy_id"],
            "run_kind": row["run_kind"],
            "execution_backend": row["execution_backend"],
            "state": row["state"],
            "result_status": row["result_status"],
            "created_at": row["created_at"],
            "finished_at": row["finished_at"],
            "archived_at": row["archived_at"],
            "failure_code": row["failure_code"],
            "cancel_requested_at": row["cancel_requested_at"],
            "config": {
                "base_url": row["base_url"],
                "model": row["model"],
                "target_kind": row["target_kind"],
                "provider_id": row["provider_id"],
                "run_mode": row["run_mode"],
                "probe_type": row["probe_type"],
                "stream": bool(row["streaming"]),
                "timeout_seconds": row["timeout_seconds"],
            },
            "workflow": json.loads(row["workflow_json"]) if row["workflow_json"] else None,
        }
        if include_result:
            value["result"] = json.loads(row["result_json"]) if row["result_json"] else None
        return value

    @staticmethod
    def _monitor_policy(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": row["policy_id"],
            "name": row["name"],
            "target_id": row["target_id"],
            "suite_revision_id": row["suite_revision_id"],
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
            "next_run_at": row["next_run_at"],
            "last_run_at": row["last_run_at"],
            "last_run_id": row["last_run_id"],
            "last_dispatch_failure_code": row["last_dispatch_failure_code"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "archived_at": row["archived_at"],
        }

    @staticmethod
    def _monitor_sample(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": row["sample_id"],
            "policy_id": row["policy_id"],
            "run_id": row["run_id"],
            "observed_at": row["observed_at"],
            "observation": row["observation"],
            "error_class": row["error_class"],
            "p95_e2e_ms": row["p95_e2e_ms"],
            "p95_ttft_ms": row["p95_ttft_ms"],
        }

    @staticmethod
    def _monitor_state_row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "policy_id": row["policy_id"],
            "status": row["status"],
            "consecutive_successes": int(row["consecutive_successes"]),
            "consecutive_failures": int(row["consecutive_failures"]),
            "last_observation": row["last_observation"],
            "last_run_id": row["last_run_id"],
            "last_observed_at": row["last_observed_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _monitor_incident(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": row["incident_id"],
            "policy_id": row["policy_id"],
            "run_id": row["run_id"],
            "event_type": row["event_type"],
            "from_status": row["from_status"],
            "to_status": row["to_status"],
            "error_class": row["error_class"],
            "observed_at": row["observed_at"],
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _session(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _validated_suite_document(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("suite document must be an object")
    document = json.loads(_encode(value))
    compile_suite(document)
    return document


def _validate_agent_session_value(value: Mapping[str, Any]) -> None:
    title = value.get("title")
    model = value.get("model")
    base_url = value.get("base_url")
    skill_id = value.get("skill_id")
    if not isinstance(title, str) or not 1 <= len(title) <= 120:
        raise ValueError("Agent session title is invalid")
    if redact_text(title) != title:
        raise ValueError("Agent session title contains a credential")
    if not isinstance(model, str) or not 1 <= len(model) <= 256:
        raise ValueError("Agent session model is invalid")
    if redact_text(model) != model:
        raise ValueError("Agent session model contains a credential")
    if (
        not isinstance(base_url, str)
        or not base_url
        or "?" in base_url
        or "#" in base_url
        or redact_text(base_url) != base_url
    ):
        raise ValueError("Agent session base_url is invalid")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Agent session base_url is invalid")
    if (
        not isinstance(skill_id, str)
        or not 1 <= len(skill_id) <= 64
        or skill_id[0] not in "abcdefghijklmnopqrstuvwxyz0123456789"
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in skill_id)
    ):
        raise ValueError("Agent session skill_id is invalid")


def _validate_agent_session_changes(changes: Mapping[str, Any]) -> None:
    if "title" in changes:
        title = changes["title"]
        if not isinstance(title, str) or not 1 <= len(title) <= 120 or redact_text(title) != title:
            raise ValueError("Agent session title is invalid or contains a credential")
    if "skill_id" in changes:
        _validate_agent_session_value(
            {
                "title": "valid",
                "model": "valid-model",
                "base_url": "https://example.invalid/v1",
                "skill_id": changes["skill_id"],
            }
        )


def _validate_agent_event_fields(event_type: str, name: str, status: str) -> None:
    if (
        not isinstance(event_type, str)
        or not 3 <= len(event_type) <= 128
        or event_type[0] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in event_type)
    ):
        raise ValueError("Agent event_type is invalid")
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 128
        or redact_text(name) != name
    ):
        raise ValueError("Agent event name is invalid or contains a credential")
    if status not in {"RUNNING", "PASS", "WARN", "FAIL"}:
        raise ValueError("Agent event status is invalid")


def _contains_forbidden_agent_key(value: Any) -> bool:
    forbidden = {
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "credential_handle",
        "credential_ref",
        "access_token",
        "refresh_token",
        "secret",
        "secret_ref",
    }
    if isinstance(value, Mapping):
        return any(
            str(key).lower() in forbidden or _contains_forbidden_agent_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_agent_key(item) for item in value)
    return False


def _require_agent_turn_token(row: Mapping[str, Any], turn_token: str | None) -> None:
    raw_token = row["turn_lease_token"]
    active_token = str(raw_token) if raw_token is not None else None
    if active_token != turn_token:
        raise ControlPlaneConflict("Agent turn fencing token is stale")


def _document_payload(value: Mapping[str, Any]) -> tuple[str, str]:
    encoded = _encode(value)
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _encode(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _parse_utc(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("monitor timestamps must include a timezone")
    return parsed.astimezone(UTC)


def _schedule_offset(policy_id: str, interval_seconds: int) -> int:
    spread = min(max(int(interval_seconds), 1), 60)
    return int(hashlib.sha256(policy_id.encode("utf-8")).hexdigest()[:8], 16) % spread


def _next_schedule(scheduled_for: str, *, interval_seconds: int, now: datetime) -> str:
    scheduled = _parse_utc(scheduled_for)
    interval = timedelta(seconds=interval_seconds)
    candidate = scheduled + interval
    if candidate <= now:
        missed = int((now - candidate).total_seconds() // interval_seconds) + 1
        candidate += interval * missed
    return candidate.isoformat()


def _validate_monitor_policy_value(value: Mapping[str, Any]) -> None:
    required = {
        "name", "target_id", "run_kind", "execution_backend", "model", "stream",
        "timeout_seconds", "interval_seconds", "failure_threshold",
        "recovery_threshold", "enabled",
    }
    missing = required - set(value)
    if missing:
        raise ValueError(f"monitor policy is missing fields: {', '.join(sorted(missing))}")
    if value["run_kind"] not in {"component", "suite"}:
        raise ValueError("invalid monitor policy run_kind")
    revision_id = value.get("suite_revision_id")
    if (value["run_kind"] == "suite") != (revision_id is not None):
        raise ValueError("suite monitor policy must reference exactly one suite revision")
    if value["execution_backend"] not in {"local", "temporal"}:
        raise ValueError("invalid monitor policy execution_backend")
    if not isinstance(value["name"], str) or not value["name"].strip():
        raise ValueError("monitor policy name is required")
    if not isinstance(value["model"], str) or not value["model"].strip():
        raise ValueError("monitor policy model is required")
    for text in (value["name"], value["model"]):
        if redact_text(text) != text:
            raise ValueError("monitor policy durable fields contain a credential")
    if not 60 <= int(value["interval_seconds"]) <= 2_592_000:
        raise ValueError("monitor policy interval is out of bounds")
    if not 1 <= int(value["failure_threshold"]) <= 10:
        raise ValueError("monitor failure threshold is out of bounds")
    if not 1 <= int(value["recovery_threshold"]) <= 10:
        raise ValueError("monitor recovery threshold is out of bounds")
    if not 0 < float(value["timeout_seconds"]) <= 300:
        raise ValueError("monitor timeout is out of bounds")


def _require_monitor_references(
    connection: sqlite3.Connection, value: Mapping[str, Any]
) -> None:
    target = connection.execute(
        "SELECT archived_at FROM control_targets WHERE target_id = ?",
        (str(value["target_id"]),),
    ).fetchone()
    if target is None or target["archived_at"] is not None:
        raise ControlPlaneConflict("monitor target is missing or archived")
    revision_id = value.get("suite_revision_id")
    if revision_id is None:
        return
    revision = connection.execute(
        """
        SELECT s.archived_at FROM control_suite_revisions r
        JOIN control_suites s ON s.suite_id = r.suite_id
        WHERE r.revision_id = ?
        """,
        (str(revision_id),),
    ).fetchone()
    if revision is None or revision["archived_at"] is not None:
        raise ControlPlaneConflict("monitor suite revision is missing or archived")


def _monitor_observation(run: Mapping[str, Any]) -> MonitorObservation:
    if run["state"] == "CANCELLED":
        return MonitorObservation.UNKNOWN
    if run["state"] == "FAILED":
        return MonitorObservation.FAIL
    status = run["result_status"] or "UNKNOWN"
    try:
        return MonitorObservation(status)
    except ValueError:
        return MonitorObservation.UNKNOWN


def _monitor_metrics(
    result: Mapping[str, Any] | None, failure_code: str | None
) -> tuple[float | None, float | None, str | None]:
    if not isinstance(result, Mapping):
        return None, None, failure_code
    measurements = result.get("measurements")
    if not isinstance(measurements, list):
        return None, None, failure_code
    e2e: list[float] = []
    ttft: list[float] = []
    error_class = failure_code
    for measurement in measurements:
        if not isinstance(measurement, Mapping):
            continue
        if isinstance(measurement.get("e2e_ms"), (int, float)):
            e2e.append(float(measurement["e2e_ms"]))
        if isinstance(measurement.get("ttft_ms"), (int, float)):
            ttft.append(float(measurement["ttft_ms"]))
        candidate = measurement.get("error_class")
        if error_class is None and isinstance(candidate, str) and len(candidate) <= 128:
            error_class = candidate
    return _percentile(e2e), _percentile(ttft), error_class


def _percentile(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, ((95 * len(ordered) + 99) // 100) - 1)
    return round(ordered[index], 3)


def _monitor_window(window: str) -> tuple[int, int]:
    windows = {
        "90m": (5_400, 300),
        "24h": (86_400, 3_600),
        "7d": (604_800, 21_600),
        "30d": (2_592_000, 86_400),
    }
    try:
        return windows[window]
    except KeyError as exc:
        raise ValueError("monitoring window must be one of 90m, 24h, 7d, 30d") from exc


def _aggregate_monitor_buckets(
    samples: list[Mapping[str, Any]], bucket_seconds: int
) -> list[dict[str, Any]]:
    grouped: dict[int, list[Mapping[str, Any]]] = {}
    for sample in samples:
        stamp = int(_parse_utc(sample["observed_at"]).timestamp())
        grouped.setdefault(stamp - (stamp % bucket_seconds), []).append(sample)
    buckets: list[dict[str, Any]] = []
    for stamp, rows in sorted(grouped.items()):
        counts = {name: 0 for name in ("pass", "warn", "fail", "unknown")}
        e2e = []
        ttft = []
        for row in rows:
            counts[str(row["observation"]).lower()] += 1
            if row["p95_e2e_ms"] is not None:
                e2e.append(float(row["p95_e2e_ms"]))
            if row["p95_ttft_ms"] is not None:
                ttft.append(float(row["p95_ttft_ms"]))
        total = len(rows)
        buckets.append(
            {
                "started_at": datetime.fromtimestamp(stamp, UTC).isoformat(),
                "total": total,
                **counts,
                "pass_rate": round(counts["pass"] / total * 100, 1),
                "p95_e2e_ms": _percentile(e2e),
                "p95_ttft_ms": _percentile(ttft),
            }
        )
    return buckets


def _monitor_timeline(
    now: datetime, window_seconds: int, bucket_seconds: int
) -> list[str]:
    end = int(now.timestamp())
    end -= end % bucket_seconds
    count = max(window_seconds // bucket_seconds, 1)
    start = end - (count - 1) * bucket_seconds
    return [
        datetime.fromtimestamp(start + index * bucket_seconds, UTC).isoformat()
        for index in range(count)
    ]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS control_schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS control_targets (
    target_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('local', 'cloud')),
    provider_id TEXT,
    base_url TEXT NOT NULL,
    default_model TEXT NOT NULL DEFAULT '',
    credential_ref TEXT,
    version INTEGER NOT NULL CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    target_id TEXT NOT NULL REFERENCES control_targets(target_id) ON DELETE RESTRICT,
    target_version INTEGER NOT NULL CHECK (target_version >= 1),
    base_url TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('local', 'cloud')),
    provider_id TEXT,
    model TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    turn_lease_token TEXT,
    turn_lease_until TEXT,
    CHECK ((turn_lease_token IS NULL) = (turn_lease_until IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_agent_sessions_updated
ON agent_sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_target
ON agent_sessions(target_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_messages (
    session_id TEXT NOT NULL REFERENCES agent_sessions(session_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    message_id TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, sequence)
);

CREATE TABLE IF NOT EXISTS agent_events (
    session_id TEXT NOT NULL REFERENCES agent_sessions(session_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    occurred_at TEXT NOT NULL,
    PRIMARY KEY (session_id, sequence)
);

CREATE TABLE IF NOT EXISTS control_suites (
    suite_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    latest_revision_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT
);

CREATE TABLE IF NOT EXISTS control_suite_revisions (
    revision_id TEXT PRIMARY KEY,
    suite_id TEXT NOT NULL REFERENCES control_suites(suite_id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    document_json TEXT NOT NULL CHECK (json_valid(document_json)),
    document_sha256 TEXT NOT NULL CHECK (length(document_sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (suite_id, revision),
    UNIQUE (suite_id, document_sha256)
);

CREATE TABLE IF NOT EXISTS monitor_policies (
    policy_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    target_id TEXT NOT NULL REFERENCES control_targets(target_id) ON DELETE RESTRICT,
    suite_revision_id TEXT REFERENCES control_suite_revisions(revision_id) ON DELETE RESTRICT,
    run_kind TEXT NOT NULL CHECK (run_kind IN ('component', 'suite')),
    probe_type TEXT NOT NULL,
    execution_backend TEXT NOT NULL CHECK (execution_backend IN ('local', 'temporal')),
    model TEXT NOT NULL,
    streaming INTEGER NOT NULL CHECK (streaming IN (0, 1)),
    timeout_seconds REAL NOT NULL CHECK (timeout_seconds > 0),
    interval_seconds INTEGER NOT NULL CHECK (interval_seconds BETWEEN 60 AND 2592000),
    failure_threshold INTEGER NOT NULL CHECK (failure_threshold BETWEEN 1 AND 10),
    recovery_threshold INTEGER NOT NULL CHECK (recovery_threshold BETWEEN 1 AND 10),
    schedule_offset_seconds INTEGER NOT NULL CHECK (schedule_offset_seconds BETWEEN 0 AND 59),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    version INTEGER NOT NULL CHECK (version >= 1),
    next_run_at TEXT,
    last_run_at TEXT,
    last_run_id TEXT,
    last_dispatch_failure_code TEXT,
    lease_token TEXT,
    lease_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    CHECK ((lease_token IS NULL) = (lease_until IS NULL)),
    CHECK ((run_kind = 'suite') = (suite_revision_id IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_monitor_policies_due
ON monitor_policies(enabled, next_run_at)
WHERE archived_at IS NULL;

CREATE TABLE IF NOT EXISTS control_runs (
    run_id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    request_sha256 TEXT CHECK (request_sha256 IS NULL OR length(request_sha256) = 64),
    target_id TEXT REFERENCES control_targets(target_id) ON DELETE RESTRICT,
    suite_revision_id TEXT REFERENCES control_suite_revisions(revision_id) ON DELETE RESTRICT,
    monitor_policy_id TEXT REFERENCES monitor_policies(policy_id) ON DELETE SET NULL,
    run_kind TEXT NOT NULL CHECK (run_kind IN ('component', 'suite')),
    execution_backend TEXT NOT NULL CHECK (execution_backend IN ('local', 'temporal')),
    state TEXT NOT NULL CHECK (state IN ('RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')),
    result_status TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    provider_id TEXT,
    run_mode TEXT NOT NULL CHECK (run_mode IN ('single', 'canary')),
    probe_type TEXT NOT NULL,
    streaming INTEGER NOT NULL CHECK (streaming IN (0, 1)),
    timeout_seconds REAL NOT NULL,
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    failure_code TEXT,
    cancel_requested_at TEXT,
    workflow_json TEXT CHECK (workflow_json IS NULL OR json_valid(workflow_json)),
    archived_at TEXT,
    CHECK ((idempotency_key IS NULL) = (request_sha256 IS NULL)),
    CHECK (result_json IS NULL OR state = 'COMPLETED')
);

CREATE INDEX IF NOT EXISTS idx_control_runs_created
ON control_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_control_runs_target
ON control_runs(target_id, created_at DESC);

CREATE TABLE IF NOT EXISTS control_run_events (
    run_id TEXT NOT NULL REFERENCES control_runs(run_id) ON DELETE RESTRICT,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    source_event_id TEXT,
    event_json TEXT NOT NULL CHECK (json_valid(event_json)),
    PRIMARY KEY (run_id, sequence)
);

CREATE TABLE IF NOT EXISTS monitor_states (
    policy_id TEXT PRIMARY KEY REFERENCES monitor_policies(policy_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('UNKNOWN', 'UP', 'DEGRADED', 'DOWN')),
    consecutive_successes INTEGER NOT NULL CHECK (consecutive_successes >= 0),
    consecutive_failures INTEGER NOT NULL CHECK (consecutive_failures >= 0),
    last_observation TEXT NOT NULL CHECK (last_observation IN ('PASS', 'WARN', 'FAIL', 'UNKNOWN')),
    last_run_id TEXT NOT NULL REFERENCES control_runs(run_id) ON DELETE RESTRICT,
    last_observed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitor_samples (
    sample_id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL REFERENCES monitor_policies(policy_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL UNIQUE REFERENCES control_runs(run_id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    observation TEXT NOT NULL CHECK (observation IN ('PASS', 'WARN', 'FAIL', 'UNKNOWN')),
    error_class TEXT,
    p95_e2e_ms REAL,
    p95_ttft_ms REAL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_monitor_samples_policy_observed
ON monitor_samples(policy_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS monitor_incident_events (
    incident_id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL REFERENCES monitor_policies(policy_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES control_runs(run_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (event_type IN ('DOWN', 'DEGRADED', 'RECOVERED')),
    from_status TEXT NOT NULL CHECK (from_status IN ('UNKNOWN', 'UP', 'DEGRADED', 'DOWN')),
    to_status TEXT NOT NULL CHECK (to_status IN ('UNKNOWN', 'UP', 'DEGRADED', 'DOWN')),
    error_class TEXT,
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (policy_id, run_id, event_type)
);

CREATE INDEX IF NOT EXISTS idx_monitor_incidents_observed
ON monitor_incident_events(observed_at DESC);
"""
