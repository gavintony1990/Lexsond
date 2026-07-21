from __future__ import annotations

import argparse
import asyncio
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the local AI Lexsond Temporal worker"
    )
    parser.add_argument("--temporal-target", default="127.0.0.1:7233")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--task-queue", default="lexsond-canary-local")
    parser.add_argument(
        "--storage-backend",
        choices=("sqlite", "postgres"),
        default="sqlite",
    )
    parser.add_argument("--endpoint-snapshots", type=Path)
    parser.add_argument("--suite-root", type=Path)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--sqlite-database", type=Path)
    parser.add_argument("--credential-bindings", type=Path)
    parser.add_argument("--postgres-dsn-env", default="LEXSOND_POSTGRES_DSN")
    parser.add_argument("--postgres-pool-min", type=int, default=1)
    parser.add_argument("--postgres-pool-max", type=int, default=8)
    parser.add_argument("--activity-threads", type=int, default=8)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not 1 <= args.activity_threads <= 128:
        parser.error("--activity-threads must be between 1 and 128")
    for field_name in ("namespace", "task_queue", "temporal_target"):
        if not getattr(args, field_name).strip():
            parser.error(f"--{field_name.replace('_', '-')} must be non-empty")
    _validate_storage_args(parser, args)
    try:
        asyncio.run(_run_worker(args))
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("temporalio"):
            parser.error("Temporal support requires: pip install -e '.[temporal]'")
        if "PostgreSQL support requires" in str(exc):
            parser.error(str(exc))
        raise


async def _run_worker(args: argparse.Namespace) -> None:
    from temporalio.client import Client
    from temporalio.worker import Worker

    from ..storage import (
        FileEvidenceStore,
        SqliteCanaryRuntimeStore,
        SqliteWorkflowJournal,
    )
    from .native_activities import (
        CredentialReferenceEnvironmentSecretResolver,
        EnvironmentSecretResolver,
        FileSuiteDocumentResolver,
        JsonEndpointSnapshotResolver,
        NativeCanaryActivities,
    )
    from .temporal_activities import (
        TemporalCanaryStepActivity,
        TemporalJournalActivities,
    )
    from .temporal_workflow import TemporalCanaryWorkflow

    evidence_root = _absolute(args.evidence_root)
    evidence_store = FileEvidenceStore(evidence_root)
    postgres_pool = None
    manifest_repository = None
    run_initializer = None
    if args.storage_backend == "sqlite":
        endpoint_path = _absolute(args.endpoint_snapshots)
        suite_root = _absolute(args.suite_root)
        sqlite_database = _absolute(args.sqlite_database)
        if not sqlite_database.parent.is_dir():
            raise ValueError("SQLite database parent directory must exist")
        journal = SqliteWorkflowJournal(sqlite_database)
        runtime_store = SqliteCanaryRuntimeStore(sqlite_database)
        endpoint_resolver = JsonEndpointSnapshotResolver.from_file(endpoint_path)
        suite_resolver = FileSuiteDocumentResolver(suite_root)
        secret_resolver = EnvironmentSecretResolver()
    else:
        try:
            from ..storage.postgres import (
                PostgresCanaryRuntimeStore,
                PostgresEndpointSnapshotResolver,
                PostgresEvidenceManifestRepository,
                PostgresPool,
                PostgresSuiteDocumentResolver,
                PostgresWorkflowJournal,
            )
        except ModuleNotFoundError as exc:
            if exc.name and (
                exc.name.startswith("psycopg") or exc.name == "psycopg_pool"
            ):
                raise ModuleNotFoundError(
                    "PostgreSQL support requires: pip install -e '.[production]'"
                ) from exc
            raise
        dsn = os.environ.get(args.postgres_dsn_env)
        if not dsn:
            raise ValueError(
                f"PostgreSQL DSN environment variable {args.postgres_dsn_env} is empty"
            )
        secret_resolver = CredentialReferenceEnvironmentSecretResolver.from_file(
            _absolute(args.credential_bindings)
        )
        postgres_pool = PostgresPool(
            dsn,
            min_size=args.postgres_pool_min,
            max_size=args.postgres_pool_max,
        )
        journal = PostgresWorkflowJournal(postgres_pool)
        runtime_store = PostgresCanaryRuntimeStore(postgres_pool)
        endpoint_resolver = PostgresEndpointSnapshotResolver(postgres_pool)
        suite_resolver = PostgresSuiteDocumentResolver(postgres_pool)
        manifest_repository = PostgresEvidenceManifestRepository(postgres_pool)
        run_initializer = journal

    native_activities = NativeCanaryActivities(
        endpoint_resolver=endpoint_resolver,
        suite_resolver=suite_resolver,
        secret_resolver=secret_resolver,
        evidence_store=evidence_store,
        runtime_store=runtime_store,
        evidence_manifest_repository=manifest_repository,
    )
    journal_activities = TemporalJournalActivities(
        journal,
        run_initializer=run_initializer,
    )
    step_activity = TemporalCanaryStepActivity(native_activities)
    try:
        client = await Client.connect(args.temporal_target, namespace=args.namespace)
    except BaseException:
        if postgres_pool is not None:
            postgres_pool.close()
        raise

    try:
        with ThreadPoolExecutor(max_workers=args.activity_threads) as executor:
            worker = Worker(
                client,
                task_queue=args.task_queue,
                workflows=[TemporalCanaryWorkflow],
                activities=[
                    journal_activities.load_history,
                    journal_activities.append_event,
                    step_activity.execute_step,
                ],
                activity_executor=executor,
            )
            async with worker:
                await asyncio.Event().wait()
    finally:
        if postgres_pool is not None:
            postgres_pool.close()


def _absolute(path: Path) -> Path:
    return path if path.is_absolute() else Path.cwd() / path


def _validate_storage_args(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    if args.storage_backend == "sqlite":
        missing = [
            option
            for option, value in (
                ("--endpoint-snapshots", args.endpoint_snapshots),
                ("--suite-root", args.suite_root),
                ("--sqlite-database", args.sqlite_database),
            )
            if value is None
        ]
        if missing:
            parser.error(f"sqlite backend requires {', '.join(missing)}")
        if args.credential_bindings is not None:
            parser.error("--credential-bindings is only valid for postgres")
        return
    if args.credential_bindings is None:
        parser.error("postgres backend requires --credential-bindings")
    if any(
        value is not None
        for value in (
            args.endpoint_snapshots,
            args.suite_root,
            args.sqlite_database,
        )
    ):
        parser.error(
            "postgres backend reads snapshots from PostgreSQL; do not pass local snapshot options"
        )
    if re.fullmatch(r"LEXSOND_[A-Z0-9_]{1,96}", args.postgres_dsn_env) is None:
        parser.error("--postgres-dsn-env must use the LEXSOND_* namespace")
    if not 1 <= args.postgres_pool_min <= args.postgres_pool_max <= 128:
        parser.error("PostgreSQL pool sizes must satisfy 1 <= min <= max <= 128")


if __name__ == "__main__":
    main()
