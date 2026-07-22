from __future__ import annotations

import unittest

from lexsond.models import RequestMeasurement
from lexsond.probe import _capture_retry_after


class ProbeRetryAfterTests(unittest.TestCase):
    def test_numeric_retry_after_is_bounded_capacity_evidence(self) -> None:
        measurement = RequestMeasurement()
        _capture_retry_after(_Response("3.5"), measurement)  # type: ignore[arg-type]
        self.assertEqual(measurement.evidence, {"retry_after_seconds": 3.5})

    def test_invalid_oversized_or_unbounded_header_is_not_retained(self) -> None:
        for value in ("not-a-date", "9" * 129, "999999999999"):
            with self.subTest(value=value[:20]):
                measurement = RequestMeasurement()
                _capture_retry_after(_Response(value), measurement)  # type: ignore[arg-type]
                self.assertEqual(measurement.evidence, {})


class _Response:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def getheader(self, name: str) -> str | None:
        assert name == "Retry-After"
        return self.value


if __name__ == "__main__":
    unittest.main()
