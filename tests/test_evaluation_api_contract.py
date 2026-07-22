from __future__ import annotations

import asyncio
import json
import time
import unittest
from uuid import UUID, uuid4

from pydantic import ValidationError
from starlette.requests import Request

from lexsond.evaluations.compiler import DatasetValidationError
from lexsond.web.api_models import (
    EvaluationDatasetMetadata,
    EvaluationRunCreate,
    EvaluationRunPreview,
)
from lexsond.web.app import _read_evaluation_multipart
from lexsond.web.evaluation_service import (
    EvaluationService,
    EvaluationUnavailable,
    _native_messages,
)


class EvaluationApiContractTests(unittest.TestCase):
    def test_native_message_adapter_preserves_roles_and_adds_choices_once(self) -> None:
        messages = _native_messages(
            {
                "messages": [
                    {"role": "system", "content": "Follow the format."},
                    {"role": "user", "content": "Pick one."},
                    {"role": "assistant", "content": "Ready."},
                    {"role": "user", "content": "Final choice?"},
                ],
                "choices": ["cyan", "amber"],
            }
        )
        self.assertEqual([role for role, _ in messages], ["system", "user", "assistant", "user"])
        self.assertEqual(sum("A. cyan" in content for _, content in messages), 1)
    def test_run_request_is_bounded_credential_exclusive_and_unknown_cost_confirmed(self) -> None:
        valid = EvaluationRunCreate(
            dataset_revision_id=uuid4(),
            channel_id=uuid4(),
            catalog_snapshot_id=uuid4(),
            model_ids=[f"model-{index}" for index in range(10)],
            api_key="sk-temporary-sentinel",
            confirm_unknown_cost=True,
        )
        self.assertEqual(valid.concurrency, 2)
        self.assertEqual(valid.sample_count, 20)
        invalid = (
            {"model_ids": [f"m-{index}" for index in range(11)], "confirm_unknown_cost": True},
            {"model_ids": ["same", "same"], "confirm_unknown_cost": True},
            {"model_ids": ["one"], "concurrency": 3, "confirm_unknown_cost": True},
            {"model_ids": ["one"], "confirm_unknown_cost": False},
            {
                "model_ids": ["one"],
                "api_key": "sk-temporary-sentinel",
                "credential_profile_id": uuid4(),
                "confirm_unknown_cost": True,
            },
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                EvaluationRunCreate(
                    dataset_revision_id=uuid4(),
                    channel_id=uuid4(),
                    catalog_snapshot_id=uuid4(),
                    **changes,
                )

    def test_dataset_metadata_requires_rights_and_safe_https_sources(self) -> None:
        valid = dict(
            slug="private-smoke",
            name="Private smoke",
            description="",
            license_spdx="LicenseRef-Proprietary",
            license_url="https://example.invalid/license",
            source_url=None,
            distribution_policy="BUNDLED",
            default_scorer_id="exact_match",
            format="jsonl",
            rights_confirmed=True,
        )
        self.assertEqual(EvaluationDatasetMetadata(**valid).slug, "private-smoke")
        for changes in (
            {"rights_confirmed": False},
            {"source_url": "http://example.invalid/data"},
            {"source_url": "https://user:pass@example.invalid/data"},
            {"distribution_policy": "RESEARCH_ONLY"},
        ):
            value = {**valid, **changes}
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                EvaluationDatasetMetadata(**value)

    def test_dataset_metadata_create_and_patch_reject_recognizable_credentials(self) -> None:
        secret = "sk-evaluation-metadata-sentinel"
        base = dict(
            slug="private-smoke", name="Private smoke", description="",
            license_spdx="LicenseRef-Proprietary",
            license_url="https://example.invalid/license", source_url=None,
            distribution_policy="BUNDLED", default_scorer_id="exact_match",
            format="jsonl", rights_confirmed=True,
        )
        for field in ("name", "description", "license_spdx", "license_url", "source_url"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                EvaluationDatasetMetadata(**{**base, field: secret})
        with self.assertRaises(ValidationError):
            from lexsond.web.api_models import EvaluationDatasetPatch

            EvaluationDatasetPatch(version=1, description=f"Authorization: Bearer {secret}")

    def test_multipart_body_limit_is_checked_before_receive(self) -> None:
        receive_called = False

        async def receive():
            nonlocal receive_called
            receive_called = True
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/evaluation-datasets/validate-upload",
                "headers": [
                    (b"content-type", b"multipart/form-data; boundary=x"),
                    (b"content-length", str(11 * 1024 * 1024).encode()),
                ],
                "query_string": b"",
                "scheme": "http",
                "server": ("127.0.0.1", 80),
                "client": ("127.0.0.1", 1),
            },
            receive,
        )
        with self.assertRaises(DatasetValidationError) as raised:
            asyncio.run(_read_evaluation_multipart(request, require_metadata=False))
        self.assertEqual(raised.exception.reason_code, "FILE_TOO_LARGE")
        self.assertFalse(receive_called)

    def test_multipart_parser_accepts_one_file_and_never_executes_content(self) -> None:
        boundary = "lexsond-boundary"
        document = json.dumps(
            {
                "id": "one",
                "category": "basic",
                "language": "en",
                "input": {"messages": [{"role": "user", "content": "Return 1"}]},
                "reference": {"scorer": "exact_match", "answer": "1"},
                "metadata": {},
            }
        ).encode()
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"data.jsonl\"\r\n"
            "Content-Type: application/jsonl\r\n\r\n"
        ).encode() + document + f"\r\n--{boundary}--\r\n".encode()

        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/upload",
                "headers": [
                    (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
                    (b"content-length", str(len(body)).encode()),
                ],
                "query_string": b"",
                "scheme": "http",
                "server": ("127.0.0.1", 80),
                "client": ("127.0.0.1", 1),
            },
            receive,
        )
        parsed = asyncio.run(_read_evaluation_multipart(request, require_metadata=False))
        self.assertEqual(parsed["file"], document)

    def test_service_snapshot_and_background_arguments_do_not_persist_temporary_key(self) -> None:
        store = _EvaluationStore()
        submissions: list[tuple] = []
        service = EvaluationService(
            store=store,
            submit_background=lambda *args: submissions.append(args),
        )
        payload = EvaluationRunCreate(
            dataset_revision_id=UUID(store.revision_id),
            channel_id=UUID(store.channel_id),
            catalog_snapshot_id=UUID(store.snapshot_id),
            model_ids=["model-a"],
            sample_count=1,
            api_key="sk-evaluation-temporary-sentinel",
            confirm_unknown_cost=True,
        )
        run = service.start_run(
            payload,
            workspace_id=store.workspace_id,
            actor_user_id=store.user_id,
            idempotency_key=str(uuid4()),
            credential_fingerprint="f" * 64,
        )
        self.assertEqual(run["state"], "RUNNING")
        self.assertNotIn("sk-evaluation-temporary-sentinel", repr(store.created))
        self.assertNotIn("api_key", store.created["request_snapshot_json"])
        self.assertEqual(len(submissions), 1)
        self.assertNotIn("sk-evaluation-temporary-sentinel", repr(submissions[0]))

    def test_scheduling_failure_is_durable_and_does_not_echo_the_secret(self) -> None:
        store = _EvaluationStore()
        service = EvaluationService(
            store=store,
            submit_background=lambda *_args: (_ for _ in ()).throw(RuntimeError("executor closed")),
        )
        payload = EvaluationRunCreate(
            dataset_revision_id=UUID(store.revision_id),
            channel_id=UUID(store.channel_id),
            catalog_snapshot_id=UUID(store.snapshot_id),
            model_ids=["model-a"], sample_count=1,
            api_key="sk-evaluation-scheduling-sentinel",
            confirm_unknown_cost=True,
        )

        with self.assertRaises(EvaluationUnavailable) as raised:
            service.start_run(
                payload, workspace_id=store.workspace_id,
                actor_user_id=store.user_id, idempotency_key=str(uuid4()),
                credential_fingerprint="f" * 64,
            )

        self.assertEqual(store.failed[1], "EVALUATION_SCHEDULING_FAILURE")
        self.assertNotIn("sk-evaluation-scheduling-sentinel", str(raised.exception))

    def test_idempotency_race_replay_never_schedules_duplicate_billable_work(self) -> None:
        store = _EvaluationStore()
        durable_id = "70000000-0000-4000-8000-000000000001"
        store.race_replay_id = durable_id
        submissions: list[tuple] = []
        service = EvaluationService(
            store=store,
            submit_background=lambda *args: submissions.append(args),
        )
        payload = EvaluationRunCreate(
            dataset_revision_id=UUID(store.revision_id),
            channel_id=UUID(store.channel_id),
            catalog_snapshot_id=UUID(store.snapshot_id),
            model_ids=["model-a"],
            sample_count=1,
            api_key="sk-evaluation-race-sentinel",
            confirm_unknown_cost=True,
        )

        run = service.start_run(
            payload,
            workspace_id=store.workspace_id,
            actor_user_id=store.user_id,
            idempotency_key=str(uuid4()),
            credential_fingerprint="f" * 64,
        )

        self.assertEqual(run["id"], durable_id)
        self.assertEqual(submissions, [])

    def test_idempotent_replay_does_not_require_current_catalog_or_channel(self) -> None:
        store = _EvaluationStore()
        submissions: list[tuple] = []
        service = EvaluationService(
            store=store,
            submit_background=lambda *args: submissions.append(args),
        )
        payload = EvaluationRunCreate(
            dataset_revision_id=UUID(store.revision_id),
            channel_id=UUID(store.channel_id),
            catalog_snapshot_id=UUID(store.snapshot_id),
            model_ids=["model-a"], sample_count=1,
            api_key="sk-evaluation-replay-sentinel",
            confirm_unknown_cost=True,
        )
        key = str(uuid4())
        first = service.start_run(
            payload, workspace_id=store.workspace_id,
            actor_user_id=store.user_id, idempotency_key=key,
            credential_fingerprint="f" * 64,
        )
        store.replay = {
            **first,
            "request_snapshot": dict(store.created["request_snapshot_json"]),
        }
        store.context_unavailable = True

        replay = service.start_run(
            payload, workspace_id=store.workspace_id,
            actor_user_id=store.user_id, idempotency_key=key,
            credential_fingerprint="f" * 64,
        )

        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(len(submissions), 1)

    def test_service_startup_does_not_fail_other_workers_running_evaluations(self) -> None:
        store = _EvaluationStore()
        EvaluationService(store=store, submit_background=lambda *_args: None)
        self.assertFalse(store.orphan_failure_requested)

    def test_maintenance_eventually_recovers_a_lease_that_expires_after_restart(self) -> None:
        store = _EvaluationStore()
        service = EvaluationService(
            store=store,
            submit_background=lambda *_args: None,
            maintenance_interval_seconds=0.01,
        )
        try:
            deadline = time.monotonic() + 1.0
            while store.expired_scan_count < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(store.expired_scan_count, 2)
        finally:
            service.close()


class _EvaluationStore:
    workspace_id = "20000000-0000-4000-8000-000000000001"
    user_id = "10000000-0000-4000-8000-000000000001"
    revision_id = "30000000-0000-4000-8000-000000000001"
    channel_id = "40000000-0000-4000-8000-000000000001"
    snapshot_id = "50000000-0000-4000-8000-000000000001"

    def __init__(self) -> None:
        self.created = None
        self.failed = None
        self.race_replay_id = None
        self.replay = None
        self.context_unavailable = False
        self.orphan_failure_requested = False
        self.expired_scan_count = 0

    def ensure_system_catalog(self):
        return None

    def fail_orphaned_runs(self):
        self.orphan_failure_requested = True
        return 0

    def fail_expired_runs(self):
        self.expired_scan_count += 1
        return 0

    def resolve_run_context(self, **_kwargs):
        if self.context_unavailable:
            raise AssertionError("idempotent replay consulted mutable run context")
        from lexsond.evaluations.compiler import compile_document_items

        items = compile_document_items(
            [
                {
                    "id": "one",
                    "category": "basic",
                    "language": "en",
                    "input": {"messages": [{"role": "user", "content": "Return 1"}]},
                    "reference": {"scorer": "exact_match", "answer": "1"},
                    "metadata": {},
                }
            ]
        ).items
        return {
            "revision": {"dataset_id": "60000000-0000-4000-8000-000000000001"},
            "items": items,
            "target": {
                "id": self.channel_id,
                "base_url": "https://api.example.invalid/v1",
                "target_kind": "cloud",
                "provider_id": "openai",
                "protocol": "openai-compatible",
                "version": 1,
            },
            "model_source_id": "openai",
            "target_version": 1,
            "target_base_url_sha256": "a" * 64,
            "catalog_content_sha256": "b" * 64,
            "unknown_chat_capability_models": [],
        }

    def find_run_by_idempotency(self, *_args, **_kwargs):
        return self.replay

    def create_run(self, value, *, workspace_id):
        self.created = value
        if self.race_replay_id is not None:
            return {
                "id": self.race_replay_id,
                "workspace_id": workspace_id,
                "state": "RUNNING",
            }
        return {"id": value["evaluation_run_id"], "workspace_id": workspace_id, "state": "RUNNING"}

    def fail_run(self, run_id, failure_code, *, workspace_id, lease_id):
        self.failed = (run_id, failure_code, workspace_id)


if __name__ == "__main__":
    unittest.main()
