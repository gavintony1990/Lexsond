from __future__ import annotations

import unittest

from lexsond.workflows.temporal_worker import (
    _validate_storage_args,
    build_parser,
)


class TemporalWorkerConfigTests(unittest.TestCase):
    def test_sqlite_backend_requires_all_local_sources(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--evidence-root", "/tmp/evidence"])

        with self.assertRaises(SystemExit):
            _validate_storage_args(parser, args)

    def test_postgres_backend_accepts_secret_free_control_plane_config(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--storage-backend",
                "postgres",
                "--evidence-root",
                "/tmp/evidence",
                "--credential-bindings",
                "/tmp/bindings.json",
                "--postgres-dsn-env",
                "LEXSOND_POSTGRES_DSN",
            ]
        )

        _validate_storage_args(parser, args)

        self.assertEqual(args.storage_backend, "postgres")
        self.assertNotIn("password", vars(args))

    def test_postgres_backend_rejects_local_snapshot_mix(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--storage-backend",
                "postgres",
                "--endpoint-snapshots",
                "/tmp/endpoints.json",
                "--evidence-root",
                "/tmp/evidence",
                "--credential-bindings",
                "/tmp/bindings.json",
            ]
        )

        with self.assertRaises(SystemExit):
            _validate_storage_args(parser, args)


if __name__ == "__main__":
    unittest.main()
