from __future__ import annotations

import json
import unittest
from concurrent.futures import Future
from pathlib import Path
from tempfile import TemporaryDirectory

from lexsond.models import NormalizedRunResult, RequestMeasurement, RunStatus
from lexsond.storage.redaction import sanitized_result_for_persistence
from lexsond.web.api_models import RunCreate, TargetCreate
from lexsond.web.control_service import ControlPlaneService
from lexsond.web.temporal_backend import build_temporal_launch_artifacts


class RecordingTemporalLauncher:
    available = True
    status = "READY"

    def __init__(self) -> None:
        self.launches: list[dict[str, object]] = []
        self.cancelled: list[str] = []

    def start(self, **values):
        self.launches.append(values)

    def cancel(self, run_id: str) -> bool:
        self.cancelled.append(run_id)
        return True

    def close(self) -> None:
        return None


class FlakyCancelTemporalLauncher(RecordingTemporalLauncher):
    def __init__(self) -> None:
        super().__init__()
        self.failures_remaining = 1

    def cancel(self, run_id: str) -> bool:
        self.cancelled.append(run_id)
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise TimeoutError("simulated cancel dispatch timeout")
        return True


class InlineExecutor:
    def submit(self, function, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, wait=True, cancel_futures=False):
        return None


class TemporalBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.suite = {
            "apiVersion": "probe.ai/v1alpha1",
            "kind": "ProbeSuite",
            "metadata": {"name": "single-chat", "version": "1"},
            "spec": {
                "layer": "L1",
                "protocol": "openai-chat",
                "request": {
                    "prompt": "Reply with exactly: PROBE_OK",
                    "stream": True,
                    "max_output_tokens": 32,
                },
                "sampling": {
                    "warmup": 0,
                    "requests": 1,
                    "concurrency": 1,
                    "timeout_seconds": 10,
                    "max_cost_usd": 0.1,
                },
                "assertions": [
                    {"type": "http_status", "equals": 200},
                    {"type": "output_nonempty"},
                ],
            },
        }

    def test_launch_input_has_one_attempt_and_no_credential_reference(self) -> None:
        target = {
            "id": "00000000-0000-4000-8000-000000000010",
            "provider_id": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "credential_ref": "vault://ai/deepseek",
        }
        launch = build_temporal_launch_artifacts(
            run_id="00000000-0000-4000-8000-000000000020",
            target=target,
            model="deepseek-chat",
            suite_document=self.suite,
            region="cn-east",
        )

        self.assertEqual(launch.workflow_input.retry_policy.max_attempts, 1)
        encoded_input = json.dumps(launch.workflow_input.to_dict())
        encoded_configuration = json.dumps(launch.endpoint_configuration)
        self.assertNotIn("credential_ref", encoded_input)
        self.assertNotIn("vault://", encoded_input)
        self.assertNotIn("credential_ref", encoded_configuration)
        self.assertNotIn("vault://", encoded_configuration)
        self.assertTrue(launch.workflow_input.suite_uri.startswith("https://"))

        rotated = build_temporal_launch_artifacts(
            run_id="00000000-0000-4000-8000-000000000021",
            target={**target, "credential_ref": "vault://ai/deepseek-rotated"},
            model="deepseek-chat",
            suite_document=self.suite,
            region="cn-east",
        )
        self.assertNotEqual(
            launch.workflow_input.endpoint_snapshot_id,
            rotated.workflow_input.endpoint_snapshot_id,
        )
        self.assertNotIn("vault://", rotated.workflow_input.endpoint_snapshot_id)

    def test_service_starts_temporal_without_using_plaintext_key_for_preflight(self) -> None:
        with TemporaryDirectory() as temporary:
            launcher = RecordingTemporalLauncher()
            service = ControlPlaneService(
                database_path=Path(temporary) / "control.sqlite3",
                default_suite_path=Path(__file__).parents[1]
                / "suites/canary/openai-compatible.json",
                temporal_launcher=launcher,
            )
            self.addCleanup(service.close)
            target = service.create_target(
                TargetCreate(
                    name="production custom",
                    target_kind="cloud",
                    provider_id=None,
                    base_url="https://models.example.invalid/v1",
                    default_model="chat-model",
                    credential_ref="vault://ai/production",
                )
            )

            run = service.start_run(
                RunCreate(
                    target_id=target["id"],
                    run_kind="component",
                    probe_type="chat",
                    execution_backend="temporal",
                    model="chat-model",
                    stream=True,
                    timeout_seconds=10,
                )
            )

            self.assertEqual(run["state"], "RUNNING")
            self.assertEqual(run["execution_backend"], "temporal")
            self.assertEqual(len(run["workflow"]["steps"]), 8)
            self.assertEqual(len(launcher.launches), 1)
            self.assertIn("on_events", launcher.launches[0])
            self.assertIn("on_terminal", launcher.launches[0])

    def test_temporal_terminal_callback_rejects_unsanitized_provider_output(self) -> None:
        with TemporaryDirectory() as temporary:
            database = Path(temporary) / "control.sqlite3"
            launcher = RecordingTemporalLauncher()
            service = ControlPlaneService(
                database_path=database,
                default_suite_path=Path(__file__).parents[1]
                / "suites/canary/openai-compatible.json",
                temporal_launcher=launcher,
            )
            self.addCleanup(service.close)
            target = service.create_target(
                TargetCreate(
                    name="terminal boundary",
                    target_kind="cloud",
                    provider_id=None,
                    base_url="https://models.example.invalid/v1",
                    default_model="chat-model",
                    credential_ref="vault://ai/production",
                )
            )
            run = service.start_run(
                RunCreate(
                    target_id=target["id"],
                    probe_type="chat",
                    execution_backend="temporal",
                    model="chat-model",
                )
            )
            raw = "provider-raw-secret-in-output"
            result = sanitized_result_for_persistence(
                NormalizedRunResult(
                    run_id=run["run_id"],
                    status=RunStatus.PASS,
                    measurements=[RequestMeasurement(output_text="safe")],
                )
            )
            result["measurements"][0]["output_text"] = raw

            launcher.launches[0]["on_terminal"](
                run["run_id"], "SUCCEEDED", result, None
            )

            stored = service.store.get_run(run["run_id"])
            self.assertEqual(stored["state"], "FAILED")
            self.assertEqual(stored["failure_code"], "TEMPORAL_RESULT_VALIDATION_ERROR")
            self.assertNotIn(raw.encode(), database.read_bytes())

    def test_temporal_cancel_intent_is_retried_before_terminal_state(self) -> None:
        with TemporaryDirectory() as temporary:
            launcher = FlakyCancelTemporalLauncher()
            service = ControlPlaneService(
                database_path=Path(temporary) / "control.sqlite3",
                default_suite_path=Path(__file__).parents[1]
                / "suites/canary/openai-compatible.json",
                temporal_launcher=launcher,
                executor=InlineExecutor(),
            )
            self.addCleanup(service.close)
            target = service.create_target(
                TargetCreate(
                    name="cancel retry",
                    target_kind="cloud",
                    provider_id=None,
                    base_url="https://models.example.invalid/v1",
                    default_model="chat-model",
                    credential_ref="vault://ai/production",
                )
            )
            run = service.start_run(
                RunCreate(
                    target_id=target["id"],
                    probe_type="chat",
                    execution_backend="temporal",
                    model="chat-model",
                )
            )

            cancelled = service.cancel_run(run["run_id"])

            self.assertEqual(cancelled["state"], "CANCELLED")
            self.assertIsNotNone(cancelled["cancel_requested_at"])
            self.assertEqual(launcher.cancelled, [run["run_id"], run["run_id"]])


if __name__ == "__main__":
    unittest.main()
