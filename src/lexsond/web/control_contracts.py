from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping
from urllib.parse import urlsplit

from ..monitoring.state import MonitorObservation
from ..storage.redaction import redact_text


class ControlPlaneError(RuntimeError):
    pass


class ControlPlaneNotFound(ControlPlaneError):
    pass


class ControlPlaneConflict(ControlPlaneError):
    pass


_REQUIRED_POSTGRES_STORE_METHODS = (
    "close",
    "list_temporal_runs_for_recovery",
    "list_monitor_policies",
    "claim_due_monitor_policies",
    "complete_monitor_policy_dispatch",
    "fail_monitor_policy_dispatch",
    "prune_monitoring_data",
    "record_monitor_run",
)


def _require_postgres_store_contract(store: Any) -> None:
    if store is None:
        raise ValueError("PostgreSQL control store is required")
    missing = tuple(
        name
        for name in _REQUIRED_POSTGRES_STORE_METHODS
        if not callable(getattr(store, name, None))
    )
    if missing:
        raise ValueError(
            "PostgreSQL control store is missing required methods: "
            + ", ".join(missing)
        )


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
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for character in skill_id
        )
    ):
        raise ValueError("Agent session skill_id is invalid")


def _validate_agent_session_changes(changes: Mapping[str, Any]) -> None:
    if "title" in changes:
        title = changes["title"]
        if (
            not isinstance(title, str)
            or not 1 <= len(title) <= 120
            or redact_text(title) != title
        ):
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
        or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
            for character in event_type
        )
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
        "name",
        "target_id",
        "run_kind",
        "execution_backend",
        "model",
        "stream",
        "timeout_seconds",
        "interval_seconds",
        "failure_threshold",
        "recovery_threshold",
        "enabled",
    }
    missing = required - set(value)
    if missing:
        raise ValueError(
            f"monitor policy is missing fields: {', '.join(sorted(missing))}"
        )
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
        raise ValueError(
            "monitoring window must be one of 90m, 24h, 7d, 30d"
        ) from exc


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
        e2e: list[float] = []
        ttft: list[float] = []
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
