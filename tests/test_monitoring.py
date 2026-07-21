from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from lexsond.monitoring.state import MonitorObservation, MonitorStatus, transition_state
from lexsond.monitoring.scheduler import MonitorScheduler
from lexsond.monitoring.challenge import arithmetic_challenge
from lexsond.web.app import create_app
from lexsond.web.control_service import ControlPlaneService
from lexsond.web.control_store import ControlPlaneConflict, ControlPlaneStore


def _target(store: ControlPlaneStore, *, name: str = "Local relay") -> dict:
    return store.create_target(
        {
            "name": name,
            "target_kind": "local",
            "provider_id": "ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "default_model": "qwen3:8b",
            "credential_ref": None,
        }
    )


def _policy(store: ControlPlaneStore, target_id: str, *, name: str = "Local pulse") -> dict:
    return store.create_monitor_policy(
        {
            "name": name,
            "target_id": target_id,
            "run_kind": "component",
            "probe_type": "chat",
            "suite_revision_id": None,
            "execution_backend": "local",
            "model": "qwen3:8b",
            "stream": True,
            "timeout_seconds": 30.0,
            "interval_seconds": 300,
            "failure_threshold": 2,
            "recovery_threshold": 1,
            "enabled": True,
        }
    )


def _metadata(target: dict, policy_id: str) -> dict:
    return {
        "target_id": target["id"],
        "monitor_policy_id": policy_id,
        "suite_revision_id": None,
        "run_kind": "component",
        "execution_backend": "local",
        "base_url": target["base_url"],
        "model": target["default_model"],
        "target_kind": target["target_kind"],
        "provider_id": target["provider_id"],
        "run_mode": "single",
        "probe_type": "chat",
        "stream": True,
        "timeout_seconds": 30.0,
    }


def _result(status: str, *, error_class: str | None = None, e2e_ms: float = 120.0) -> dict:
    return {
        "schema_version": "probe.ai/result/v1alpha1",
        "run_id": str(uuid4()),
        "suite_name": "monitor",
        "suite_version": "1",
        "status": status,
        "reason_codes": [],
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "measurements": [
            {
                "request_id": str(uuid4()),
                "endpoint": "/chat/completions",
                "requested_model": "qwen3:8b",
                "response_model": "qwen3:8b",
                "streaming": True,
                "status_code": 200 if status == "PASS" else 500,
                "error_class": error_class,
                "error_message": None,
                "started_at": datetime.now(UTC).isoformat(),
                "connect_ms": 10.0,
                "response_headers_ms": 20.0,
                "ttfb_ms": 20.0,
                "ttft_ms": 40.0,
                "e2e_ms": e2e_ms,
                "itl_ms": 5.0,
                "output_tps": 30.0,
                "chunk_count": 2,
                "output_text": "",
                "finish_reason": "stop",
                "provider_reported_input_tokens": 4,
                "provider_reported_output_tokens": 3,
                "provider_reported_total_tokens": 7,
                "locally_estimated_input_tokens": 4,
                "locally_estimated_output_tokens": 3,
                "chunks": [],
                "evidence": {},
            }
        ],
        "case_results": [],
        "dimension_scores": [],
    }


class MonitorStateTests(unittest.TestCase):
    def test_consecutive_failures_and_recovery_emit_one_transition_each(self) -> None:
        current = None
        first = transition_state(
            current,
            MonitorObservation.FAIL,
            failure_threshold=2,
            recovery_threshold=1,
        )
        self.assertEqual(first.status, MonitorStatus.UNKNOWN)
        self.assertEqual(first.consecutive_failures, 1)
        self.assertIsNone(first.event_type)

        down = transition_state(
            first,
            MonitorObservation.FAIL,
            failure_threshold=2,
            recovery_threshold=1,
        )
        self.assertEqual(down.status, MonitorStatus.DOWN)
        self.assertEqual(down.event_type, "DOWN")

        still_down = transition_state(
            down,
            MonitorObservation.FAIL,
            failure_threshold=2,
            recovery_threshold=1,
        )
        self.assertEqual(still_down.status, MonitorStatus.DOWN)
        self.assertIsNone(still_down.event_type)

        recovered = transition_state(
            still_down,
            MonitorObservation.PASS,
            failure_threshold=2,
            recovery_threshold=1,
        )
        self.assertEqual(recovered.status, MonitorStatus.UP)
        self.assertEqual(recovered.event_type, "RECOVERED")

    def test_warn_is_degraded_and_cancelled_observation_does_not_change_state(self) -> None:
        degraded = transition_state(
            None,
            MonitorObservation.WARN,
            failure_threshold=2,
            recovery_threshold=1,
        )
        self.assertEqual(degraded.status, MonitorStatus.DEGRADED)
        self.assertEqual(degraded.event_type, "DEGRADED")
        unchanged = transition_state(
            degraded,
            MonitorObservation.UNKNOWN,
            failure_threshold=2,
            recovery_threshold=1,
        )
        self.assertEqual(unchanged.status, MonitorStatus.DEGRADED)
        self.assertIsNone(unchanged.event_type)


class MonitorChallengeTests(unittest.TestCase):
    def test_challenge_is_deterministic_rotating_and_never_contains_answer(self) -> None:
        values = [arithmetic_challenge(f"scheduled-slot-{index}") for index in range(200)]
        self.assertEqual(values[0], arithmetic_challenge("scheduled-slot-0"))
        self.assertGreater(len({value.prompt for value in values}), 150)
        for value in values:
            self.assertNotIn(value.expected_text, value.prompt)
            self.assertRegex(
                value.expected_text,
                r"^LEXSOND_RESULT=\d{2,3};NONCE=[0-9a-f]{32}$",
            )

    def test_challenge_nonce_does_not_collide_across_many_slots(self) -> None:
        values = {arithmetic_challenge(f"scheduled-slot-{index}").prompt for index in range(20_000)}
        self.assertEqual(len(values), 20_000)


class MonitorSchedulerTests(unittest.TestCase):
    def test_due_slot_has_deterministic_idempotency_and_fenced_completion(self) -> None:
        class Store:
            def __init__(self) -> None:
                self.claimed = False
                self.completed: list[dict] = []
                self.failed: list[dict] = []

            def claim_due_monitor_policies(self, **_):
                if self.claimed:
                    return []
                self.claimed = True
                return [
                    {
                        "id": "25f38bd3-26c3-4be5-96ea-9e18f2d87fd6",
                        "scheduled_for": "2026-07-21T12:00:00+00:00",
                        "lease_token": "lease-1",
                    }
                ]

            def complete_monitor_policy_dispatch(self, policy_id, **value):
                self.completed.append({"policy_id": policy_id, **value})

            def fail_monitor_policy_dispatch(self, policy_id, **value):
                self.failed.append({"policy_id": policy_id, **value})

        store = Store()
        dispatched: list[tuple[str, str]] = []

        def dispatch(policy, idempotency_key):
            dispatched.append((policy["id"], idempotency_key))
            return "9f1cb69f-adc6-48bb-8c64-13bac57b0e77"

        scheduler = MonitorScheduler(store, dispatch, enabled=False)
        self.addCleanup(scheduler.close)
        self.assertEqual(scheduler.run_once(now="2026-07-21T12:00:00+00:00"), 1)
        self.assertEqual(scheduler.run_once(now="2026-07-21T12:00:01+00:00"), 0)
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(len(dispatched[0][1]), 36)
        self.assertEqual(store.completed[0]["lease_token"], "lease-1")
        self.assertFalse(store.failed)

    def test_retention_uses_separate_sample_and_incident_windows(self) -> None:
        class Store:
            def __init__(self) -> None:
                self.call = None

            def prune_monitoring_data(self, **value):
                self.call = value
                return {"samples": 2, "incidents": 1}

        store = Store()
        scheduler = MonitorScheduler(
            store,
            lambda *_: "unused",
            enabled=False,
            sample_retention_days=30,
            incident_retention_days=365,
        )
        self.addCleanup(scheduler.close)
        removed = scheduler.run_maintenance_once(now="2026-07-21T12:00:00+00:00")
        self.assertEqual(removed, {"samples": 2, "incidents": 1})
        self.assertEqual(
            store.call["samples_before"], "2026-06-21T12:00:00+00:00"
        )
        self.assertEqual(
            store.call["incidents_before"], "2025-07-21T12:00:00+00:00"
        )

    def test_retention_drains_multiple_bounded_batches(self) -> None:
        class Store:
            def __init__(self) -> None:
                self.calls = 0

            def prune_monitoring_data(self, **_):
                self.calls += 1
                return (
                    {"samples": 1000, "incidents": 0}
                    if self.calls < 4
                    else {"samples": 17, "incidents": 2}
                )

        store = Store()
        scheduler = MonitorScheduler(store, lambda *_: "unused", enabled=False)
        self.addCleanup(scheduler.close)
        removed = scheduler.run_maintenance_once(now="2026-07-21T12:00:00+00:00")
        self.assertEqual(store.calls, 4)
        self.assertEqual(removed, {"samples": 3017, "incidents": 2})


class MonitorStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ControlPlaneStore(Path(self.temporary.name) / "control.sqlite3")
        self.target = _target(self.store)
        self.policy = _policy(self.store, self.target["id"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_policy_crud_claim_fencing_and_run_now(self) -> None:
        self.assertTrue(self.policy["enabled"])
        self.assertGreaterEqual(self.policy["schedule_offset_seconds"], 0)
        changed = self.store.update_monitor_policy(
            self.policy["id"],
            {"interval_seconds": 600},
            expected_version=self.policy["version"],
        )
        self.assertEqual(changed["interval_seconds"], 600)

        due_at = (datetime.now(UTC) + timedelta(days=365)).isoformat()
        claims = self.store.claim_due_monitor_policies(
            now=due_at,
            limit=4,
            lease_seconds=30,
        )
        self.assertEqual(len(claims), 1)
        claim = claims[0]
        with self.assertRaises(ControlPlaneConflict):
            self.store.complete_monitor_policy_dispatch(
                self.policy["id"],
                lease_token=str(uuid4()),
                scheduled_for=claim["scheduled_for"],
                run_id=str(uuid4()),
            )

        with self.assertRaisesRegex(ControlPlaneConflict, "already in progress"):
            self.store.update_monitor_policy(
                self.policy["id"],
                {"interval_seconds": 900},
                expected_version=changed["version"],
            )
        with self.assertRaisesRegex(ControlPlaneConflict, "already in progress"):
            self.store.archive_monitor_policy(self.policy["id"])
        with self.assertRaisesRegex(ControlPlaneConflict, "already in progress"):
            self.store.request_monitor_policy_run(self.policy["id"])
        self.store.fail_monitor_policy_dispatch(
            self.policy["id"],
            lease_token=claim["lease_token"],
            scheduled_for=claim["scheduled_for"],
            failure_code="TEST_RELEASE",
        )
        requested = self.store.request_monitor_policy_run(self.policy["id"])
        self.assertLessEqual(
            datetime.fromisoformat(requested["next_run_at"]),
            datetime.now(UTC) + timedelta(seconds=1),
        )
        archived = self.store.archive_monitor_policy(self.policy["id"])
        self.assertFalse(archived["enabled"])
        restored = self.store.restore_monitor_policy(self.policy["id"])
        self.assertIsNone(restored["archived_at"])

    def test_terminal_runs_project_idempotently_into_samples_state_and_incidents(self) -> None:
        def finish(status: str, *, error_class: str | None = None, e2e_ms: float = 120.0):
            run_id = str(uuid4())
            self.store.create_run(
                run_id,
                _metadata(self.target, self.policy["id"]),
                {"status": "RUNNING", "steps": []},
            )
            result = _result(status, error_class=error_class, e2e_ms=e2e_ms)
            result["run_id"] = run_id
            self.store.complete_run(run_id, result, {"status": status, "steps": []})
            return run_id, self.store.record_monitor_run(run_id)

        _, first = finish("FAIL", error_class="UPSTREAM_5XX")
        self.assertEqual(first["state"]["status"], "UNKNOWN")
        down_run, down = finish("FAIL", error_class="UPSTREAM_5XX")
        self.assertEqual(down["state"]["status"], "DOWN")
        self.assertEqual(down["incident"]["event_type"], "DOWN")

        replay = self.store.record_monitor_run(down_run)
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(self.store.list_monitor_incidents(limit=20)), 1)

        recovered_run, recovered = finish("PASS", e2e_ms=80.0)
        self.assertEqual(recovered["state"]["status"], "UP")
        self.assertEqual(recovered["incident"]["event_type"], "RECOVERED")
        overview = self.store.monitoring_overview(window="24h")
        self.assertEqual(overview["summary"]["policies"], 1)
        self.assertEqual(overview["summary"]["down"], 0)
        self.assertEqual(overview["summary"]["up"], 1)
        self.assertEqual(len(overview["timeline"]), 24)
        self.assertGreaterEqual(len(overview["policies"][0]["buckets"]), 1)
        self.assertEqual(overview["policies"][0]["sample_count"], 3)
        self.assertEqual(overview["policies"][0]["latest_error_class"], None)

        self.store.archive_run(recovered_run)
        with self.assertRaisesRegex(ControlPlaneConflict, "current monitor state"):
            self.store.purge_run(recovered_run)

    def test_target_with_policy_cannot_be_purged(self) -> None:
        self.store.archive_target(self.target["id"])
        with self.assertRaisesRegex(ControlPlaneConflict, "monitor policy"):
            self.store.purge_target(self.target["id"])

    def test_late_old_result_is_sampled_without_rewinding_current_state(self) -> None:
        old_run = str(uuid4())
        current_run = str(uuid4())
        for run_id, status in ((old_run, "FAIL"), (current_run, "PASS")):
            self.store.create_run(
                run_id,
                _metadata(self.target, self.policy["id"]),
                {"status": "RUNNING", "steps": []},
            )
            result = _result(status, error_class="UPSTREAM_5XX" if status == "FAIL" else None)
            result["run_id"] = run_id
            self.store.complete_run(run_id, result, {"status": status, "steps": []})
        with self.store._session() as connection:
            connection.execute(
                "UPDATE control_runs SET finished_at = ? WHERE run_id = ?",
                ("2020-01-01T00:00:00+00:00", old_run),
            )

        current = self.store.record_monitor_run(current_run)
        stale = self.store.record_monitor_run(old_run)

        self.assertEqual(current["state"]["status"], "UP")
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["state"]["status"], "UP")
        self.assertEqual(stale["state"]["last_run_id"], current_run)

    def test_retention_prunes_derived_history_but_preserves_current_state(self) -> None:
        run_id = str(uuid4())
        self.store.create_run(
            run_id,
            _metadata(self.target, self.policy["id"]),
            {"status": "RUNNING", "steps": []},
        )
        result = _result("PASS", e2e_ms=80.0)
        result["run_id"] = run_id
        self.store.complete_run(run_id, result, {"status": "PASS", "steps": []})
        projected = self.store.record_monitor_run(run_id)
        self.assertEqual(projected["state"]["status"], "UP")

        removed = self.store.prune_monitoring_data(
            samples_before="2999-01-01T00:00:00+00:00",
            incidents_before="2999-01-01T00:00:00+00:00",
        )
        self.assertEqual(removed["samples"], 1)
        overview = self.store.monitoring_overview(window="24h")
        self.assertEqual(overview["summary"]["samples"], 0)
        self.assertEqual(overview["policies"][0]["status"], "UP")


class MonitorApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        service = ControlPlaneService(
            database_path=root / "control.sqlite3",
            default_suite_path=Path(__file__).parents[1]
            / "suites/canary/openai-compatible.json",
            monitor_scheduler=False,
        )
        self.client = TestClient(create_app(service=service, frontend_path=root / "missing"))
        self.service = service
        response = self.client.post(
            "/api/v1/targets",
            json={
                "name": "Local API",
                "target_kind": "local",
                "provider_id": "ollama",
                "base_url": "http://127.0.0.1:11434/v1",
                "default_model": "qwen3:8b",
                "credential_ref": None,
            },
        )
        self.target_id = response.json()["data"]["id"]

    def tearDown(self) -> None:
        self.client.close()
        self.service.close()
        self.temporary.cleanup()

    def test_policy_api_crud_overview_and_secret_field_rejection(self) -> None:
        payload = {
            "name": "Five minute chat pulse",
            "target_id": self.target_id,
            "run_kind": "component",
            "probe_type": "chat",
            "suite_revision_id": None,
            "execution_backend": "local",
            "model": "qwen3:8b",
            "stream": True,
            "timeout_seconds": 30,
            "interval_seconds": 300,
            "failure_threshold": 2,
            "recovery_threshold": 1,
            "enabled": True,
        }
        rejected = self.client.post(
            "/api/v1/monitor-policies",
            json={**payload, "api_key": "sk-must-never-persist"},
        )
        self.assertEqual(rejected.status_code, 422)
        self.assertNotIn("sk-must-never-persist", rejected.text)

        created = self.client.post("/api/v1/monitor-policies", json=payload)
        self.assertEqual(created.status_code, 201, created.text)
        policy = created.json()["data"]
        listed = self.client.get("/api/v1/monitor-policies")
        self.assertEqual(listed.json()["data"][0]["id"], policy["id"])

        patched = self.client.patch(
            f"/api/v1/monitor-policies/{policy['id']}",
            json={"version": policy["version"], "enabled": False},
        )
        self.assertEqual(patched.status_code, 200)
        run_now = self.client.post(f"/api/v1/monitor-policies/{policy['id']}/run-now")
        self.assertEqual(run_now.status_code, 409)
        overview = self.client.get("/api/v1/monitoring/overview?window=24h")
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.json()["data"]["summary"]["policies"], 1)


if __name__ == "__main__":
    unittest.main()
