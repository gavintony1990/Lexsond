from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MonitorObservation(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class MonitorStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    UP = "UP"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"


@dataclass(frozen=True, slots=True)
class MonitorState:
    status: MonitorStatus
    consecutive_successes: int = 0
    consecutive_failures: int = 0
    event_type: str | None = None


def transition_state(
    current: MonitorState | None,
    observation: MonitorObservation,
    *,
    failure_threshold: int,
    recovery_threshold: int,
) -> MonitorState:
    """Apply one terminal observation to a stable monitor state.

    UNKNOWN observations (for example an operator cancellation) are samples but
    deliberately do not open or close an incident. FAIL uses a consecutive
    threshold to avoid alert flapping, while WARN immediately exposes degraded
    service without calling it unavailable.
    """

    if failure_threshold < 1 or recovery_threshold < 1:
        raise ValueError("monitor transition thresholds must be positive")
    previous = current or MonitorState(MonitorStatus.UNKNOWN)
    if observation is MonitorObservation.UNKNOWN:
        return MonitorState(
            status=previous.status,
            consecutive_successes=previous.consecutive_successes,
            consecutive_failures=previous.consecutive_failures,
        )
    if observation is MonitorObservation.WARN:
        return MonitorState(
            status=MonitorStatus.DEGRADED,
            consecutive_successes=0,
            consecutive_failures=0,
            event_type=(
                "DEGRADED" if previous.status is not MonitorStatus.DEGRADED else None
            ),
        )
    if observation is MonitorObservation.FAIL:
        failures = previous.consecutive_failures + 1
        status = (
            MonitorStatus.DOWN
            if failures >= failure_threshold
            else previous.status
        )
        return MonitorState(
            status=status,
            consecutive_successes=0,
            consecutive_failures=failures,
            event_type=(
                "DOWN"
                if status is MonitorStatus.DOWN
                and previous.status is not MonitorStatus.DOWN
                else None
            ),
        )

    successes = previous.consecutive_successes + 1
    if previous.status in {MonitorStatus.DOWN, MonitorStatus.DEGRADED}:
        if successes < recovery_threshold:
            return MonitorState(
                status=previous.status,
                consecutive_successes=successes,
                consecutive_failures=0,
            )
        return MonitorState(
            status=MonitorStatus.UP,
            consecutive_successes=successes,
            consecutive_failures=0,
            event_type="RECOVERED",
        )
    return MonitorState(
        status=(
            MonitorStatus.UP
            if successes >= recovery_threshold
            else previous.status
        ),
        consecutive_successes=successes,
        consecutive_failures=0,
    )
