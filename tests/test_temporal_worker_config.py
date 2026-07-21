from __future__ import annotations

import unittest

from lexsond.workflows.temporal_worker import (
    _validate_storage_args,
    build_parser,
)


class TemporalWorkerConfigTests(unittest.TestCase):
    def test_worker_requires_postgres_credential_bindings(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--evidence-root", "/tmp/evidence"])

        with self.assertRaises(SystemExit):
            _validate_storage_args(parser, args)

    def test_worker_accepts_secret_free_postgres_config(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--evidence-root",
                "/tmp/evidence",
                "--credential-bindings",
                "/tmp/bindings.json",
                "--postgres-dsn-env",
                "LEXSOND_POSTGRES_DSN",
            ]
        )

        _validate_storage_args(parser, args)

        self.assertFalse(hasattr(args, "storage_backend"))
        self.assertNotIn("password", vars(args))

    def test_worker_parser_rejects_removed_sqlite_options(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "--evidence-root",
                    "/tmp/evidence",
                    "--credential-bindings",
                    "/tmp/bindings.json",
                    "--sqlite-database",
                    "/tmp/probe.sqlite3",
                ]
            )


if __name__ == "__main__":
    unittest.main()
