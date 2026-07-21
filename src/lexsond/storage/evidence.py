from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4


class EvidenceKind(StrEnum):
    NORMALIZED_RESULT = "NORMALIZED_RESULT"
    REQUEST_TIMELINE = "REQUEST_TIMELINE"
    RUNNER_REPORT = "RUNNER_REPORT"
    RUNNER_REVIEW = "RUNNER_REVIEW"
    RUNNER_LOG = "RUNNER_LOG"
    BILLING_SNAPSHOT = "BILLING_SNAPSHOT"


class RedactionStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    SANITIZED = "SANITIZED"
    RAW_RESTRICTED = "RAW_RESTRICTED"


class EvidenceStoreIntegrityError(RuntimeError):
    pass


_RETENTION_REQUIRED = {
    EvidenceKind.REQUEST_TIMELINE,
    EvidenceKind.RUNNER_REVIEW,
    EvidenceKind.RUNNER_LOG,
}
_FORBIDDEN_JSON_KEYS = {
    "api_key",
    "authorization",
    "credential_handle",
    "access_token",
    "refresh_token",
    "secret",
}


@dataclass(frozen=True, slots=True)
class EvidenceManifest:
    evidence_id: str
    run_id: str
    evidence_kind: EvidenceKind
    object_uri: str
    object_sha256: str
    byte_size: int
    media_type: str
    redaction_status: RedactionStatus
    encrypted: bool
    retention_until: str | None
    created_at: str
    schema_version: str = "probe.ai/evidence-manifest/v1alpha1"

    def __post_init__(self) -> None:
        _uuid(self.evidence_id, "evidence_id")
        _uuid(self.run_id, "run_id")
        if not isinstance(self.evidence_kind, EvidenceKind):
            raise ValueError("evidence_kind has an invalid enum value")
        if not isinstance(self.redaction_status, RedactionStatus):
            raise ValueError("redaction_status has an invalid enum value")
        if not isinstance(self.object_uri, str):
            raise ValueError("object_uri must be a string")
        parsed = urlsplit(self.object_uri)
        if parsed.scheme not in {"file", "s3", "https"}:
            raise ValueError("object_uri must use file, s3, or https")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("object_uri must not contain credentials, query, or fragment")
        _sha256_digest(self.object_sha256, "object_sha256")
        if isinstance(self.byte_size, bool) or not isinstance(self.byte_size, int) or self.byte_size < 0:
            raise ValueError("byte_size must be a non-negative integer")
        if not isinstance(self.media_type, str) or not self.media_type.strip():
            raise ValueError("media_type must be non-empty")
        if not isinstance(self.encrypted, bool):
            raise ValueError("encrypted must be boolean")
        created = _timestamp(self.created_at, "created_at")
        retention = (
            _timestamp(self.retention_until, "retention_until")
            if self.retention_until is not None
            else None
        )
        if retention is not None and retention <= created:
            raise ValueError("retention_until must be later than created_at")
        if self.evidence_kind in _RETENTION_REQUIRED and retention is None:
            raise ValueError("this evidence kind requires retention_until")
        if self.redaction_status is RedactionStatus.RAW_RESTRICTED:
            if not self.encrypted or retention is None:
                raise ValueError("RAW_RESTRICTED evidence requires encryption and retention")
        if (
            self.evidence_kind is EvidenceKind.NORMALIZED_RESULT
            and self.redaction_status is RedactionStatus.NOT_REQUIRED
        ):
            raise ValueError("normalized results must be sanitized or access-restricted")
        if self.schema_version != "probe.ai/evidence-manifest/v1alpha1":
            raise ValueError("unsupported evidence manifest schema_version")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_kind"] = self.evidence_kind.value
        value["redaction_status"] = self.redaction_status.value
        return value


class EvidenceStore(Protocol):
    def put_json(
        self,
        *,
        run_id: str,
        evidence_kind: EvidenceKind,
        value: Mapping[str, Any],
        redaction_status: RedactionStatus,
        retention_until: str | None = None,
        created_at: str | None = None,
    ) -> EvidenceManifest: ...

    def read_json_ref(self, object_uri: str) -> dict[str, Any]: ...


class EvidenceManifestRepository(Protocol):
    def add(self, manifest: EvidenceManifest) -> None: ...


class FileEvidenceStore:
    """Content-addressed local store for sanitized development evidence."""

    def __init__(self, root: Path, *, max_object_bytes: int = 50_000_000) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("evidence root must be an absolute pathlib.Path")
        if not root.is_dir() or root.is_symlink():
            raise ValueError("evidence root must be an existing non-symlink directory")
        if (
            isinstance(max_object_bytes, bool)
            or not isinstance(max_object_bytes, int)
            or max_object_bytes <= 0
        ):
            raise ValueError("max_object_bytes must be positive")
        self._root = root.resolve()
        self._objects = self._root / "objects"
        self._objects.mkdir(mode=0o700, exist_ok=True)
        if self._objects.is_symlink():
            raise ValueError("evidence objects directory must not be a symlink")
        self._max_object_bytes = max_object_bytes

    def put_json(
        self,
        *,
        run_id: str,
        evidence_kind: EvidenceKind,
        value: Mapping[str, Any],
        redaction_status: RedactionStatus,
        retention_until: str | None = None,
        created_at: str | None = None,
    ) -> EvidenceManifest:
        if not isinstance(value, Mapping):
            raise ValueError("JSON evidence must be an object")
        _assert_no_secret_keys(value)
        content = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return self.put_bytes(
            run_id=run_id,
            evidence_kind=evidence_kind,
            content=content,
            media_type="application/json",
            redaction_status=redaction_status,
            retention_until=retention_until,
            created_at=created_at,
        )

    def put_bytes(
        self,
        *,
        run_id: str,
        evidence_kind: EvidenceKind,
        content: bytes,
        media_type: str,
        redaction_status: RedactionStatus,
        retention_until: str | None = None,
        created_at: str | None = None,
    ) -> EvidenceManifest:
        if not isinstance(content, bytes):
            raise ValueError("evidence content must be bytes")
        if not isinstance(evidence_kind, EvidenceKind):
            raise ValueError("evidence_kind has an invalid enum value")
        if not isinstance(redaction_status, RedactionStatus):
            raise ValueError("redaction_status has an invalid enum value")
        if len(content) > self._max_object_bytes:
            raise ValueError("evidence object exceeds max_object_bytes")
        if redaction_status is RedactionStatus.RAW_RESTRICTED:
            raise ValueError("the local file store cannot accept unencrypted RAW_RESTRICTED evidence")
        digest = hashlib.sha256(content).hexdigest()
        directory = self._objects / digest[:2]
        destination = directory / digest
        created = created_at or datetime.now(UTC).isoformat()
        manifest = EvidenceManifest(
            evidence_id=str(uuid4()),
            run_id=run_id,
            evidence_kind=evidence_kind,
            object_uri=destination.as_uri(),
            object_sha256=digest,
            byte_size=len(content),
            media_type=media_type,
            redaction_status=redaction_status,
            encrypted=False,
            retention_until=retention_until,
            created_at=created,
        )
        directory.mkdir(mode=0o700, exist_ok=True)
        if (
            self._objects.is_symlink()
            or self._objects.resolve() != self._root / "objects"
            or directory.is_symlink()
            or directory.resolve().parent != self._objects.resolve()
        ):
            raise EvidenceStoreIntegrityError("evidence object directory escaped store root")
        self._write_immutable(destination, content, digest)
        return manifest

    def read_bytes(self, manifest: EvidenceManifest) -> bytes:
        parsed = urlsplit(manifest.object_uri)
        if parsed.scheme != "file":
            raise ValueError("FileEvidenceStore can only read file manifests")
        raw_path = Path(unquote(parsed.path))
        if raw_path.is_symlink():
            raise EvidenceStoreIntegrityError("evidence object must not be a symlink")
        path = raw_path.resolve()
        if self._root not in path.parents or not path.is_file():
            raise EvidenceStoreIntegrityError("evidence object escaped or is not a regular file")
        if path.stat().st_size != manifest.byte_size:
            raise EvidenceStoreIntegrityError("evidence object size does not match manifest")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != manifest.object_sha256:
            raise EvidenceStoreIntegrityError("evidence object digest does not match manifest")
        return content

    def read_json_ref(self, object_uri: str) -> dict[str, Any]:
        """Read and verify a content-addressed JSON reference from this store.

        Workflow events intentionally carry only immutable object references,
        not full manifests. The SHA-256 is encoded in the object filename, so
        this path still verifies containment, shape, size, and content digest.
        """

        if not isinstance(object_uri, str):
            raise ValueError("object_uri must be a string")
        parsed = urlsplit(object_uri)
        if (
            parsed.scheme != "file"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("object_uri must be a non-credential file URI")
        raw_path = Path(unquote(parsed.path))
        if raw_path.is_symlink():
            raise EvidenceStoreIntegrityError("evidence object must not be a symlink")
        path = raw_path.resolve()
        if self._root not in path.parents or not path.is_file():
            raise EvidenceStoreIntegrityError(
                "evidence object escaped or is not a regular file"
            )
        if path.parent.parent != self._objects or path.parent.name != path.name[:2]:
            raise EvidenceStoreIntegrityError(
                "evidence object does not follow the content-addressed layout"
            )
        expected_digest = path.name
        _sha256_digest(expected_digest, "object URI digest")
        if path.stat().st_size > self._max_object_bytes:
            raise EvidenceStoreIntegrityError("evidence object exceeds max_object_bytes")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_digest:
            raise EvidenceStoreIntegrityError("evidence object digest does not match URI")
        try:
            value = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise EvidenceStoreIntegrityError("evidence object is not valid JSON") from exc
        if not isinstance(value, dict):
            raise EvidenceStoreIntegrityError("JSON evidence object must be an object")
        _assert_no_secret_keys(value)
        return value

    @staticmethod
    def _write_immutable(destination: Path, content: bytes, digest: str) -> None:
        try:
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            existing = destination.read_bytes()
            if len(existing) != len(content) or hashlib.sha256(existing).hexdigest() != digest:
                raise EvidenceStoreIntegrityError(
                    "content-addressed object exists with unexpected content"
                )
            return
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
            raise


def _assert_no_secret_keys(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"JSON object key at {path} must be a string")
            if key.lower() in _FORBIDDEN_JSON_KEYS:
                raise ValueError(f"forbidden secret field in JSON evidence: {path}.{key}")
            _assert_no_secret_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_secret_keys(item, f"{path}[{index}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"non-finite JSON number at {path}")


def _uuid(value: str, field: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _sha256_digest(value: str, field: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value.lower()
    ):
        raise ValueError(f"{field} must be a SHA-256 digest")


def _timestamp(value: str, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be ISO-8601")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed
