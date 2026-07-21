from __future__ import annotations

import unittest

from lexsond.monitoring.challenge import arithmetic_challenge
from lexsond.monitoring.scheduler import MonitorScheduler
from lexsond.monitoring.state import MonitorObservation, MonitorStatus, transition_state


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
    def test_maintenance_requires_the_persistent_store_contract(self) -> None:
        scheduler = MonitorScheduler(object(), lambda *_: "unused", enabled=False)

        with self.assertRaises(AttributeError):
            scheduler.run_maintenance_once(now="2026-07-22T00:00:00+00:00")

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


if __name__ == "__main__":
    unittest.main()
