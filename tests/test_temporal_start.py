from __future__ import annotations

import unittest

from lexsond.workflows.temporal_start import build_parser, build_workflow_input


class TemporalStartCommandTests(unittest.TestCase):
    def test_parser_rejects_removed_local_suite_file_option(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                [
                    "--endpoint-snapshot-id",
                    "endpoint-v1",
                    "--suite-file",
                    "/tmp/canary.json",
                    "--region",
                    "local-test",
                ]
            )

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

    def test_postgres_suite_reference_requires_identity_and_digest(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                [
                    "--endpoint-snapshot-id",
                    "endpoint-v1",
                    "--suite-uri",
                    "s3://probe-suites/canary.json",
                    "--region",
                    "test",
                ]
            )

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
