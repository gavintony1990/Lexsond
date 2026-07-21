from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5


class MonitorScheduler:
    """Small bounded dispatcher for durable monitor-policy claims.

    The repository owns due-time and lease fencing. This thread performs no
    provider traffic itself; dispatch returns after handing a run to the normal
    ControlPlaneService executor or Temporal launcher.
    """

    def __init__(
        self,
        store: Any,
        dispatch: Callable[[Mapping[str, Any], str], str],
        *,
        enabled: bool = True,
        poll_seconds: float = 1.0,
        batch_size: int = 4,
        lease_seconds: float = 30.0,
        sample_retention_days: int = 30,
        incident_retention_days: int = 365,
        maintenance_interval_seconds: float = 3600.0,
        maintenance_max_batches: int = 100,
        maintenance_time_budget_seconds: float = 5.0,
    ) -> None:
        if (
            poll_seconds <= 0
            or not 1 <= batch_size <= 32
            or lease_seconds <= 0
            or not 1 <= sample_retention_days <= 3650
            or not sample_retention_days <= incident_retention_days <= 3650
            or maintenance_interval_seconds <= 0
            or not 1 <= maintenance_max_batches <= 1000
            or maintenance_time_budget_seconds <= 0
        ):
            raise ValueError("invalid monitor scheduler bounds")
        self._store = store
        self._dispatch = dispatch
        self._poll_seconds = poll_seconds
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._sample_retention_days = sample_retention_days
        self._incident_retention_days = incident_retention_days
        self._maintenance_interval_seconds = maintenance_interval_seconds
        self._maintenance_max_batches = maintenance_max_batches
        self._maintenance_time_budget_seconds = maintenance_time_budget_seconds
        self._maintenance_saturated = False
        self._next_maintenance_at = 0.0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        if enabled:
            self._thread = threading.Thread(
                target=self._run,
                name="lexsond-monitor-scheduler",
                daemon=True,
            )
            self._thread.start()
        else:
            self._stopped.set()

    def wake(self) -> None:
        self._wake.set()

    def close(self) -> bool:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=max(self._poll_seconds * 2, 2.0))
        return self._stopped.is_set()

    def wait_closed(self, timeout: float | None = None) -> bool:
        return self._stopped.wait(timeout)

    def run_once(self, *, now: str | None = None) -> int:
        observed_at = now or datetime.now(UTC).isoformat()
        claims = self._store.claim_due_monitor_policies(
            now=observed_at,
            limit=self._batch_size,
            lease_seconds=self._lease_seconds,
        )
        dispatched = 0
        for claim in claims:
            idempotency_key = str(
                uuid5(
                    NAMESPACE_URL,
                    f"lexsond:monitor:{claim['id']}:{claim['scheduled_for']}",
                )
            )
            try:
                run_id = self._dispatch(claim, idempotency_key)
                self._store.complete_monitor_policy_dispatch(
                    claim["id"],
                    lease_token=claim["lease_token"],
                    scheduled_for=claim["scheduled_for"],
                    run_id=run_id,
                )
                dispatched += 1
            except Exception:
                self._store.fail_monitor_policy_dispatch(
                    claim["id"],
                    lease_token=claim["lease_token"],
                    scheduled_for=claim["scheduled_for"],
                    failure_code="DISPATCH_ERROR",
                )
        return dispatched

    def run_maintenance_once(self, *, now: str | None = None) -> dict[str, int]:
        observed_at = datetime.fromisoformat(now) if now is not None else datetime.now(UTC)
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        observed_at = observed_at.astimezone(UTC)
        cutoffs = {
            "samples_before": (
                observed_at - timedelta(days=self._sample_retention_days)
            ).isoformat(),
            "incidents_before": (
                observed_at - timedelta(days=self._incident_retention_days)
            ).isoformat(),
        }
        batch_size = 1000
        totals = {"samples": 0, "incidents": 0}
        started = time.monotonic()
        self._maintenance_saturated = False
        for _ in range(self._maintenance_max_batches):
            removed = self._store.prune_monitoring_data(**cutoffs, limit=batch_size)
            totals["samples"] += int(removed.get("samples", 0))
            totals["incidents"] += int(removed.get("incidents", 0))
            full_batch = (
                int(removed.get("samples", 0)) >= batch_size
                or int(removed.get("incidents", 0)) >= batch_size
            )
            if not full_batch:
                return totals
            if time.monotonic() - started >= self._maintenance_time_budget_seconds:
                self._maintenance_saturated = True
                return totals
        self._maintenance_saturated = True
        return totals

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception:
                    pass
                if time.monotonic() >= self._next_maintenance_at:
                    try:
                        self.run_maintenance_once()
                    except Exception:
                        pass
                    retry_seconds = (
                        min(self._maintenance_interval_seconds, 60.0)
                        if self._maintenance_saturated
                        else self._maintenance_interval_seconds
                    )
                    self._next_maintenance_at = time.monotonic() + retry_seconds
                self._wake.wait(self._poll_seconds)
                self._wake.clear()
        finally:
            self._stopped.set()
