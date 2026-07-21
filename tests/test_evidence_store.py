from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from lexsond.storage import (
    EvidenceKind,
    EvidenceStoreIntegrityError,
    FileEvidenceStore,
    RedactionStatus,
    sanitized_result_for_persistence,
)
from lexsond.models import (
    ChunkMeasurement,
    NormalizedRunResult,
    RequestMeasurement,
    RunStatus,
)


class FileEvidenceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        self.store = FileEvidenceStore(self.root, max_object_bytes=10_000)
        self.run_id = str(uuid4())
        self.created_at = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
        self.retention_until = (self.created_at + timedelta(days=1)).isoformat()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_stores_canonical_sanitized_json_and_deduplicates_content(self) -> None:
        value = {"status": "PASS", "metrics": {"ttft_ms": 123.4}}
        first = self.store.put_json(
            run_id=self.run_id,
            evidence_kind=EvidenceKind.NORMALIZED_RESULT,
            value=value,
            redaction_status=RedactionStatus.SANITIZED,
            created_at=self.created_at.isoformat(),
        )
        second = self.store.put_json(
            run_id=self.run_id,
            evidence_kind=EvidenceKind.NORMALIZED_RESULT,
            value={"metrics": {"ttft_ms": 123.4}, "status": "PASS"},
            redaction_status=RedactionStatus.SANITIZED,
            created_at=self.created_at.isoformat(),
        )
        self.assertEqual(first.object_uri, second.object_uri)
        self.assertEqual(first.object_sha256, second.object_sha256)
        self.assertNotEqual(first.evidence_id, second.evidence_id)
        content = self.store.read_bytes(first)
        self.assertEqual(json.loads(content), value)
        self.assertEqual(self.store.read_json_ref(first.object_uri), value)
        mode = os.stat(Path(first.object_uri.removeprefix("file://"))).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_rejects_secret_fields_recursively(self) -> None:
        with self.assertRaisesRegex(ValueError, "forbidden secret field"):
            self.store.put_json(
                run_id=self.run_id,
                evidence_kind=EvidenceKind.NORMALIZED_RESULT,
                value={"metadata": {"authorization": "Bearer must-not-persist"}},
                redaction_status=RedactionStatus.SANITIZED,
                created_at=self.created_at.isoformat(),
            )

    def test_sensitive_evidence_requires_retention(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires retention_until"):
            self.store.put_bytes(
                run_id=self.run_id,
                evidence_kind=EvidenceKind.RUNNER_LOG,
                content=b"sanitized log",
                media_type="text/plain",
                redaction_status=RedactionStatus.SANITIZED,
                created_at=self.created_at.isoformat(),
            )

    def test_local_store_rejects_unencrypted_raw_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot accept unencrypted"):
            self.store.put_bytes(
                run_id=self.run_id,
                evidence_kind=EvidenceKind.RUNNER_REVIEW,
                content=b"raw review",
                media_type="application/x-ndjson",
                redaction_status=RedactionStatus.RAW_RESTRICTED,
                retention_until=self.retention_until,
                created_at=self.created_at.isoformat(),
            )

    def test_tampering_is_detected_on_read(self) -> None:
        manifest = self.store.put_bytes(
            run_id=self.run_id,
            evidence_kind=EvidenceKind.RUNNER_REPORT,
            content=b"trusted aggregate",
            media_type="application/json",
            redaction_status=RedactionStatus.NOT_REQUIRED,
            created_at=self.created_at.isoformat(),
        )
        path = Path(manifest.object_uri.removeprefix("file://"))
        path.write_bytes(b"tampered aggregate")
        with self.assertRaises(EvidenceStoreIntegrityError):
            self.store.read_bytes(manifest)

    def test_tampering_is_detected_when_reading_object_reference(self) -> None:
        manifest = self.store.put_json(
            run_id=self.run_id,
            evidence_kind=EvidenceKind.NORMALIZED_RESULT,
            value={"status": "PASS"},
            redaction_status=RedactionStatus.SANITIZED,
            created_at=self.created_at.isoformat(),
        )
        path = Path(manifest.object_uri.removeprefix("file://"))
        path.write_text('{"status":"FAIL"}', encoding="utf-8")

        with self.assertRaises(EvidenceStoreIntegrityError):
            self.store.read_json_ref(manifest.object_uri)

    def test_object_size_limit_is_enforced_before_write(self) -> None:
        limited_store = FileEvidenceStore(self.root, max_object_bytes=1024)
        with self.assertRaisesRegex(ValueError, "max_object_bytes"):
            limited_store.put_bytes(
                run_id=self.run_id,
                evidence_kind=EvidenceKind.RUNNER_REPORT,
                content=b"x" * 1025,
                media_type="application/json",
                redaction_status=RedactionStatus.NOT_REQUIRED,
                created_at=self.created_at.isoformat(),
            )
        objects = list((self.root / "objects").rglob("*"))
        self.assertEqual(objects, [])

    def test_normalized_result_is_sanitized_before_durable_storage(self) -> None:
        result = NormalizedRunResult(run_id=self.run_id)
        result.measurements.append(
            RequestMeasurement(
                endpoint="https://relay.example/v1",
                requested_model="model",
                output_text="private model response",
                error_message="upstream echoed sk-sensitive-example-123456",
            )
        )
        result.finish(RunStatus.FAIL, "UPSTREAM_ERROR")
        sanitized = sanitized_result_for_persistence(result)
        serialized = json.dumps(sanitized)
        self.assertNotIn("private model response", serialized)
        self.assertNotIn("sk-sensitive-example-123456", serialized)
        measurement = sanitized["measurements"][0]
        self.assertEqual(measurement["output_text"], "")
        self.assertIsNone(measurement["error_message"])
        self.assertEqual(measurement["evidence"]["output_text_chars"], 22)
        self.assertEqual(result.measurements[0].output_text, "private model response")

        manifest = self.store.put_json(
            run_id=self.run_id,
            evidence_kind=EvidenceKind.NORMALIZED_RESULT,
            value=sanitized,
            redaction_status=RedactionStatus.SANITIZED,
            created_at=self.created_at.isoformat(),
        )
        self.assertEqual(json.loads(self.store.read_bytes(manifest)), sanitized)

    def test_upstream_metadata_cannot_echo_runtime_secret_into_storage(self) -> None:
        secret = "gsk_runtime-secret-never-persist"
        result = NormalizedRunResult(run_id=self.run_id)
        result.measurements.append(
            RequestMeasurement(
                endpoint="https://relay.example/v1",
                requested_model="requested-model",
                response_model=f"model-{secret}",
                finish_reason=secret,
                provider_reported_input_tokens=secret,  # type: ignore[arg-type]
                provider_reported_output_tokens=secret,  # type: ignore[arg-type]
                provider_reported_total_tokens=secret,  # type: ignore[arg-type]
                chunks=[
                    ChunkMeasurement(
                        sequence=0,
                        received_after_ms=1,
                        event_type=f"event-{secret}",
                        content_chars=0,
                        finish_reason=secret,
                    )
                ],
                evidence={"content_type": f"text/plain; reflected={secret}"},
            )
        )

        sanitized = sanitized_result_for_persistence(
            result,
            sensitive_values=(secret,),
        )
        serialized = json.dumps(sanitized)

        self.assertNotIn(secret, serialized)
        measurement = sanitized["measurements"][0]
        self.assertEqual(measurement["response_model"], "model-[REDACTED]")
        self.assertEqual(measurement["finish_reason"], "[REDACTED]")
        self.assertEqual(
            measurement["provider_reported_total_tokens"],
            "[REDACTED]",
        )
        self.assertEqual(
            measurement["evidence"]["content_type"],
            "text/plain; reflected=[REDACTED]",
        )


if __name__ == "__main__":
    unittest.main()
