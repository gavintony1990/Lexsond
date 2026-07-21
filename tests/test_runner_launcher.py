from __future__ import annotations

import hashlib
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from lexsond.runners import (
    RunnerExecutable,
    RunnerJob,
    RunnerProcessLauncher,
    RunnerProcessSpec,
    RunnerStatus,
)


class RunnerProcessLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.suite_path = self.root / "suite.json"
        self.suite_path.write_text('{"suite":"bounded"}', encoding="utf-8")
        self.artifact_dir = self.root / "artifacts"
        self.artifact_dir.mkdir()
        digest = hashlib.sha256(self.suite_path.read_bytes()).hexdigest()
        self.job = RunnerJob(
            runner_name="promptfoo",
            runner_version="test-runtime-1",
            endpoint_snapshot_id="endpoint-v1",
            model="relay-model",
            suite_uri=self.suite_path.as_uri(),
            suite_sha256=digest,
            credential_handle="vault:test-key",
            timeout_seconds=1,
        )
        executable = RunnerExecutable(
            runner_name="promptfoo",
            runner_version="test-runtime-1",
            path=Path(sys.executable).resolve(),
        )
        self.launcher = RunnerProcessLauncher({"promptfoo": executable})

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_success_collects_hashed_artifact_and_redacts_secret(self) -> None:
        secret = "sk-test-secret-123456"
        code = (
            "import os,pathlib,sys; "
            "assert os.environ['OPENAI_API_KEY']; "
            "pathlib.Path('result.json').write_text('ok'); "
            "sys.stderr.write('api_key=' + os.environ['OPENAI_API_KEY'])"
        )
        outcome = self.launcher.run(
            self.job,
            RunnerProcessSpec(arguments=("-c", code), artifact_files=("result.json",)),
            suite_path=self.suite_path,
            artifact_dir=self.artifact_dir,
            credential_value=secret,
        )
        self.assertEqual(outcome.runner_status, RunnerStatus.SUCCEEDED)
        self.assertEqual(len(outcome.artifacts), 1)
        self.assertEqual(outcome.artifacts[0].sha256, hashlib.sha256(b"ok").hexdigest())
        self.assertNotIn(secret, outcome.sanitized_stderr or "")
        self.assertIn("[REDACTED]", outcome.sanitized_stderr or "")
        self.assertNotIn(secret, repr(self.job))

    def test_nonzero_with_artifact_is_target_failure(self) -> None:
        code = "import pathlib,sys; pathlib.Path('result.json').write_text('{}'); sys.exit(1)"
        outcome = self.launcher.run(
            self.job,
            RunnerProcessSpec(arguments=("-c", code), artifact_files=("result.json",)),
            suite_path=self.suite_path,
            artifact_dir=self.artifact_dir,
            credential_value="opaque-test-value",
        )
        self.assertEqual(outcome.runner_status, RunnerStatus.TARGET_FAILED)
        self.assertEqual(outcome.error_code, "TARGET_ASSERTION_FAILED")
        self.assertEqual(len(outcome.artifacts), 1)

    def test_timeout_terminates_process_tree(self) -> None:
        timed_job = RunnerJob(
            runner_name=self.job.runner_name,
            runner_version=self.job.runner_version,
            endpoint_snapshot_id=self.job.endpoint_snapshot_id,
            model=self.job.model,
            suite_uri=self.job.suite_uri,
            suite_sha256=self.job.suite_sha256,
            credential_handle="vault:test-key",
            timeout_seconds=0.05,
        )
        outcome = self.launcher.run(
            timed_job,
            RunnerProcessSpec(
                arguments=("-c", "import time; time.sleep(5)"),
                artifact_files=("result.json",),
            ),
            suite_path=self.suite_path,
            artifact_dir=self.artifact_dir,
            credential_value="opaque-test-value",
        )
        self.assertEqual(outcome.runner_status, RunnerStatus.CANCELLED)
        self.assertEqual(outcome.error_code, "RUNNER_TIMEOUT")

    def test_pre_cancel_does_not_start_runner(self) -> None:
        cancelled = threading.Event()
        cancelled.set()
        outcome = self.launcher.run(
            self.job,
            RunnerProcessSpec(
                arguments=("-c", "raise RuntimeError('must not run')"),
                artifact_files=("result.json",),
            ),
            suite_path=self.suite_path,
            artifact_dir=self.artifact_dir,
            credential_value="opaque-test-value",
            cancel_event=cancelled,
        )
        self.assertEqual(outcome.runner_status, RunnerStatus.CANCELLED)
        self.assertIsNone(outcome.exit_code)

    def test_rejects_digest_mismatch_and_secret_in_arguments(self) -> None:
        self.suite_path.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "digest"):
            self.launcher.run(
                self.job,
                RunnerProcessSpec(arguments=("-c", "pass"), artifact_files=("result.json",)),
                suite_path=self.suite_path,
                artifact_dir=self.artifact_dir,
                credential_value="opaque-test-value",
            )

        self.suite_path.write_text('{"suite":"bounded"}', encoding="utf-8")
        secret = "opaque-test-value"
        with self.assertRaisesRegex(ValueError, "must not appear"):
            self.launcher.run(
                self.job,
                RunnerProcessSpec(arguments=("-c", secret), artifact_files=("result.json",)),
                suite_path=self.suite_path,
                artifact_dir=self.artifact_dir,
                credential_value=secret,
            )

    def test_missing_artifact_is_runner_failure(self) -> None:
        outcome = self.launcher.run(
            self.job,
            RunnerProcessSpec(arguments=("-c", "pass"), artifact_files=("result.json",)),
            suite_path=self.suite_path,
            artifact_dir=self.artifact_dir,
            credential_value="opaque-test-value",
        )
        self.assertEqual(outcome.runner_status, RunnerStatus.RUNNER_FAILED)
        self.assertEqual(outcome.error_code, "RUNNER_ARTIFACT_MISSING")


if __name__ == "__main__":
    unittest.main()
