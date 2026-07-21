from __future__ import annotations

import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from lexsond.workflows.temporal_start import build_parser, build_workflow_input


class TemporalStartCommandTests(unittest.TestCase):
    def test_builds_immutable_input_from_exact_suite_bytes(self) -> None:
        with TemporaryDirectory() as directory:
            source = (
                Path(__file__).parents[1]
                / "suites"
                / "canary"
                / "openai-compatible.json"
            )
            suite_path = Path(directory) / "canary.json"
            suite_bytes = source.read_bytes()
            suite_path.write_bytes(suite_bytes)
            args = build_parser().parse_args(
                [
                    "--endpoint-snapshot-id",
                    "endpoint-v1",
                    "--suite-file",
                    str(suite_path),
                    "--region",
                    "local-test",
                ]
            )

            value = build_workflow_input(args)

            self.assertEqual(value.suite_name, "openai-compatible-canary")
            self.assertEqual(value.suite_version, "0.1.0")
            self.assertEqual(
                value.suite_sha256, hashlib.sha256(suite_bytes).hexdigest()
            )
            self.assertEqual(value.suite_uri, suite_path.as_uri())
            self.assertNotIn("credential", value.to_dict())

    def test_rejects_symlinked_suite_file(self) -> None:
        with TemporaryDirectory() as directory:
            source = (
                Path(__file__).parents[1]
                / "suites"
                / "canary"
                / "openai-compatible.json"
            )
            link = Path(directory) / "suite-link.json"
            link.symlink_to(source)
            args = build_parser().parse_args(
                [
                    "--endpoint-snapshot-id",
                    "endpoint-v1",
                    "--suite-file",
                    str(link),
                    "--region",
                    "local-test",
                ]
            )

            with self.assertRaisesRegex(ValueError, "non-symlink"):
                build_workflow_input(args)

    def test_builds_production_input_from_immutable_suite_reference(self) -> None:
        args = build_parser().parse_args(
            [
                "--endpoint-snapshot-id",
                "endpoint-v1",
                "--suite-uri",
                "s3://probe-suites/canary-a1.json",
                "--suite-name",
                "production-canary",
                "--suite-version",
                "2026.07.20",
                "--suite-sha256",
                "a" * 64,
                "--region",
                "cn-east-1",
            ]
        )

        value = build_workflow_input(args)

        self.assertEqual(value.suite_uri, "s3://probe-suites/canary-a1.json")
        self.assertEqual(value.suite_name, "production-canary")
        self.assertEqual(value.suite_sha256, "a" * 64)

    def test_remote_suite_reference_requires_identity_and_digest(self) -> None:
        args = build_parser().parse_args(
            [
                "--endpoint-snapshot-id",
                "endpoint-v1",
                "--suite-uri",
                "s3://probe-suites/canary.json",
                "--region",
                "test",
            ]
        )

        with self.assertRaisesRegex(ValueError, "requires"):
            build_workflow_input(args)

    def test_remote_suite_digest_is_canonicalized_to_lowercase(self) -> None:
        args = build_parser().parse_args(
            [
                "--endpoint-snapshot-id",
                "endpoint-v1",
                "--suite-uri",
                "s3://probe-suites/canary.json",
                "--suite-name",
                "canary",
                "--suite-version",
                "1",
                "--suite-sha256",
                "A" * 64,
                "--region",
                "test",
            ]
        )

        self.assertEqual(build_workflow_input(args).suite_sha256, "a" * 64)


if __name__ == "__main__":
    unittest.main()
