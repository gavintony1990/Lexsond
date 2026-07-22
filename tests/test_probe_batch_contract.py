from __future__ import annotations

import unittest
from pathlib import Path
import threading
from types import MethodType
from uuid import uuid4

from pydantic import ValidationError

from lexsond.web.api_models import ProbeBatchCreate
from lexsond.web.control_service import ControlPlaneService


MIGRATIONS = Path(__file__).parents[1] / "migrations"


class ProbeBatchContractTests(unittest.TestCase):
    def test_batch_is_bounded_unique_and_credential_exclusive(self) -> None:
        value = ProbeBatchCreate(
            target_id=uuid4(),
            catalog_snapshot_id=uuid4(),
            mode="smoke",
            model_ids=[f"model-{index}" for index in range(10)],
            credential_profile_id=uuid4(),
        )
        self.assertEqual(value.max_concurrency, 2)
        self.assertEqual(value.max_output_tokens, 8)

        invalid_values = (
            {"model_ids": [f"model-{index}" for index in range(11)]},
            {"model_ids": ["duplicate", "duplicate"]},
            {"model_ids": ["one"], "max_concurrency": 3},
            {
                "model_ids": ["one"],
                "api_key": "sk-temporary",
                "credential_profile_id": uuid4(),
            },
        )
        for changes in invalid_values:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ProbeBatchCreate(
                    target_id=uuid4(),
                    catalog_snapshot_id=uuid4(),
                    mode="smoke",
                    **changes,
                )

    def test_quality_suite_requires_an_immutable_suite_revision(self) -> None:
        with self.assertRaises(ValidationError):
            ProbeBatchCreate(
                target_id=uuid4(),
                catalog_snapshot_id=uuid4(),
                mode="quality_suite",
                model_ids=["model-one"],
            )
        with self.assertRaises(ValidationError):
            ProbeBatchCreate(
                target_id=uuid4(),
                catalog_snapshot_id=uuid4(),
                mode="smoke",
                model_ids=["model-one"],
                suite_revision_id=uuid4(),
            )

    def test_migration_persists_only_sanitized_snapshots_batches_items_and_events(self) -> None:
        up = (MIGRATIONS / "0010_probe_batches.sql").read_text(encoding="utf-8")
        down = (MIGRATIONS / "0010_probe_batches.down.sql").read_text(encoding="utf-8")
        for table in (
            "model_catalog_snapshots",
            "probe_batches",
            "probe_batch_items",
            "probe_batch_events",
        ):
            self.assertIn(f"CREATE TABLE lexsond.{table}", up)
            self.assertIn(f"DROP TABLE IF EXISTS lexsond.{table}", down)
        self.assertIn("contains_forbidden_secret_key(models_json)", up)
        self.assertIn("contains_recognizable_secret_value(models_json)", up)
        self.assertIn("CHECK (model_count BETWEEN 1 AND 10)", up)
        self.assertIn("CHECK (max_concurrency BETWEEN 1 AND 2)", up)
        self.assertNotIn("api_key TEXT", up.lower())
        self.assertNotIn("authorization TEXT", up.lower())

    def test_dispatcher_runs_each_model_once_with_concurrency_never_above_two(self) -> None:
        store = _BatchStore(10)
        service = ControlPlaneService.__new__(ControlPlaneService)
        service.store = store
        service._closing = threading.Event()
        seen: list[tuple[str, str]] = []

        def start_run(instance, model, **kwargs):
            del instance
            secret = kwargs["api_key_override"].get_secret_value()
            seen.append((model.model, secret))
            return {"run_id": f"00000000-0000-4000-8000-{len(seen):012d}"}

        service.start_run = MethodType(start_run, service)
        service.cancel_run = MethodType(lambda *_args, **_kwargs: None, service)

        service._execute_probe_batch(
            _BATCH_ID,
            _WORKSPACE_ID,
            "sk-batch-dispatch-sentinel",
        )

        self.assertEqual([model for model, _secret in seen], [f"model-{i}" for i in range(10)])
        self.assertEqual(store.max_active, 2)
        self.assertEqual(store.batch["state"], "COMPLETED")
        self.assertNotIn("sk-batch-dispatch-sentinel", repr(store.batch))

    def test_catalog_only_batch_finalizes_without_dispatching_generation(self) -> None:
        store = _CatalogOnlyStore()
        service = ControlPlaneService.__new__(ControlPlaneService)
        service.store = store
        service._submit_background = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("catalog_only must not dispatch generation")
        )
        model = ProbeBatchCreate(
            target_id=store.target_id,
            catalog_snapshot_id=store.snapshot_id,
            mode="catalog_only",
            model_ids=["visible-model"],
        )

        result = ControlPlaneService.start_probe_batch.__wrapped__(
            service,
            model,
            workspace_id=_WORKSPACE_ID,
            idempotency_key=str(uuid4()),
        )

        self.assertEqual(result["state"], "COMPLETED")
        self.assertTrue(store.finalized)


_WORKSPACE_ID = "20000000-0000-4000-8000-000000000001"
_BATCH_ID = "50000000-0000-4000-8000-000000000001"


class _BatchStore:
    def __init__(self, count: int) -> None:
        self.batch = {
            "batch_id": _BATCH_ID,
            "workspace_id": _WORKSPACE_ID,
            "target_id": "30000000-0000-4000-8000-000000000001",
            "credential_profile_id": "40000000-0000-4000-8000-000000000001",
            "suite_revision_id": None,
            "mode": "smoke",
            "state": "RUNNING",
            "max_concurrency": 2,
            "max_output_tokens": 8,
            "timeout_seconds": 30.0,
            "cancel_requested_at": None,
            "items": [
                {
                    "item_id": f"60000000-0000-4000-8000-{index:012d}",
                    "model_id": f"model-{index}",
                    "state": "PENDING",
                    "run_id": None,
                }
                for index in range(count)
            ],
        }
        self.active = 0
        self.max_active = 0
        self.runs: dict[str, dict] = {}

    def for_workspace(self, workspace_id):
        if workspace_id != _WORKSPACE_ID:
            raise AssertionError("wrong workspace")
        return self

    def get_probe_batch(self, batch_id):
        if batch_id != _BATCH_ID:
            raise AssertionError("wrong batch")
        return self.batch

    def start_probe_batch_item(self, batch_id, item_id, run_id):
        del batch_id
        item = next(value for value in self.batch["items"] if value["item_id"] == item_id)
        item.update(state="RUNNING", run_id=run_id)
        self.runs[run_id] = {"state": "COMPLETED", "result_status": "PASS", "failure_code": None}
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.active > 2:
            raise AssertionError("batch concurrency exceeded two")

    def get_run(self, run_id, include_archived=False):
        del include_archived
        return self.runs[run_id]

    def finish_probe_batch_item(self, batch_id, item_id, *, state, failure_code):
        del batch_id, failure_code
        item = next(value for value in self.batch["items"] if value["item_id"] == item_id)
        if item["state"] == "RUNNING":
            self.active -= 1
        item["state"] = state

    def finalize_probe_batch(self, batch_id):
        del batch_id
        self.batch["state"] = "COMPLETED"
        return self.batch


class _CatalogOnlyStore:
    def __init__(self) -> None:
        from uuid import UUID

        self.target_id = UUID("30000000-0000-4000-8000-000000000001")
        self.snapshot_id = UUID("50000000-0000-4000-8000-000000000001")
        self.finalized = False

    def for_workspace(self, workspace_id):
        if workspace_id != _WORKSPACE_ID:
            raise AssertionError("wrong workspace")
        return self

    def get_target(self, target_id):
        return {
            "id": target_id,
            "workspace_id": _WORKSPACE_ID,
            "target_kind": "cloud",
            "version": 1,
        }

    def get_model_catalog_snapshot(self, snapshot_id):
        return {
            "snapshot_id": snapshot_id,
            "target_id": str(self.target_id),
            "credential_profile_id": None,
            "target_version": 1,
            "status": "FRESH",
        }

    def find_probe_batch_by_idempotency(self, *_args):
        return None

    def create_probe_batch(self, value):
        return {**value, "state": "RUNNING"}

    def finalize_probe_batch(self, batch_id):
        self.finalized = True
        return {"batch_id": batch_id, "state": "COMPLETED"}


if __name__ == "__main__":
    unittest.main()
