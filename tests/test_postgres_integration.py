from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4


RUN_POSTGRES_TESTS = os.environ.get("RUN_POSTGRES_TESTS") == "1"
POSTGRES_BIN = Path(
    os.environ.get("POSTGRES_BIN", "/opt/homebrew/opt/postgresql@16/bin")
)
PROJECT_ROOT = Path(__file__).parents[1]


@unittest.skipUnless(RUN_POSTGRES_TESTS, "set RUN_POSTGRES_TESTS=1")
class PostgresIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not all((POSTGRES_BIN / name).is_file() for name in ("initdb", "pg_ctl", "psql")):
            raise unittest.SkipTest("PostgreSQL 16 binaries are unavailable")
        try:
            import psycopg  # noqa: F401
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest("psycopg PostgreSQL extra is unavailable") from exc

        cls.temporary = TemporaryDirectory(prefix="lexsond-postgres-")
        cls.root = Path(cls.temporary.name)
        cls.data = cls.root / "data"
        cls.port = _available_port()
        _run(
            POSTGRES_BIN / "initdb",
            "-D",
            cls.data,
            "--no-locale",
            "--encoding=UTF8",
            "--auth=trust",
        )
        _run(
            POSTGRES_BIN / "pg_ctl",
            "-D",
            cls.data,
            "-l",
            cls.root / "postgres.log",
            "-o",
            f"-F -k {cls.root} -h '' -p {cls.port}",
            "-w",
            "start",
        )
        cls.dsn = f"host={cls.root} port={cls.port} dbname=postgres"
        try:
            cls._psql_file("0001_core.sql")
            cls._psql_file("0002_access.sql")
            cls._psql_file("0003_control_plane.sql")
            cls._psql_file("0004_agent_control_plane.sql")
            cls._psql_file("0005_continuous_monitoring.sql")
            from lexsond.storage.postgres import PostgresPool

            cls.pool = PostgresPool(cls.dsn, min_size=1, max_size=8)
        except Exception:
            cls._stop_cluster()
            cls.temporary.cleanup()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "pool"):
            cls.pool.close()
        cls._stop_cluster()
        cls.temporary.cleanup()

    @classmethod
    def _stop_cluster(cls) -> None:
        if hasattr(cls, "data") and (cls.data / "postmaster.pid").exists():
            subprocess.run(
                [
                    str(POSTGRES_BIN / "pg_ctl"),
                    "-D",
                    str(cls.data),
                    "-m",
                    "fast",
                    "-w",
                    "stop",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

    @classmethod
    def _psql_file(cls, migration: str, *, database: str = "postgres") -> None:
        _run(
            POSTGRES_BIN / "psql",
            "-h",
            cls.root,
            "-p",
            str(cls.port),
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            PROJECT_ROOT / "migrations" / migration,
        )

    def test_snapshot_resolvers_and_run_initialization_are_digest_bound(self) -> None:
        from lexsond.storage.postgres import (
            PostgresEndpointSnapshotResolver,
            PostgresSuiteDocumentResolver,
            PostgresWorkflowJournal,
        )

        workflow_input, suite_document = self._seed_run()
        endpoint = PostgresEndpointSnapshotResolver(self.pool).resolve(
            workflow_input.endpoint_snapshot_id
        )
        suite_bytes = PostgresSuiteDocumentResolver(self.pool).read(
            workflow_input.suite_uri
        )
        journal = PostgresWorkflowJournal(self.pool)
        journal.prepare_run(workflow_input)
        journal.prepare_run(workflow_input)

        self.assertEqual(endpoint.model, "gpt-test")
        self.assertEqual(endpoint.credential_handle, "vault://ai-probe/test-key")
        self.assertEqual(json.loads(suite_bytes), suite_document)
        self.assertEqual(journal.load(workflow_input.run_id), ())

        changed = workflow_input.to_dict()
        changed["region"] = "another-region"
        from lexsond.workflows import CanaryWorkflowInput

        with self.assertRaises(Exception):
            journal.prepare_run(CanaryWorkflowInput.from_dict(changed))

    def test_postgres_control_store_supports_crud_and_run_lifecycle(self) -> None:
        from lexsond.web.postgres_control_store import PostgresControlPlaneStore

        store = PostgresControlPlaneStore(self.pool)
        suffix = uuid4().hex[:10]
        target = store.create_target(
            {
                "name": f"control-{suffix}",
                "target_kind": "local",
                "provider_id": None,
                "base_url": "http://127.0.0.1:8000/v1",
                "default_model": "mock",
                "credential_ref": None,
            }
        )
        suite = store.create_suite(
            {
                "name": f"suite-{suffix}",
                "description": "bounded",
                "document": {
                    "apiVersion": "probe.ai/v1alpha1",
                    "kind": "ProbeSuite",
                    "metadata": {"name": f"suite-{suffix}", "version": "1"},
                    "spec": {
                        "layer": "L1",
                        "protocol": "openai-chat",
                        "request": {
                            "prompt": "Reply with exactly: PROBE_OK",
                            "stream": False,
                            "max_output_tokens": 32,
                        },
                        "sampling": {
                            "warmup": 0,
                            "requests": 1,
                            "concurrency": 1,
                            "timeout_seconds": 5,
                            "max_cost_usd": 0.1,
                        },
                        "assertions": [
                            {"type": "http_status", "equals": 200},
                            {"type": "output_nonempty"},
                        ],
                    },
                },
            }
        )
        run_id = str(uuid4())
        run = store.create_run(
            run_id,
            {
                "target_id": target["id"],
                "suite_revision_id": None,
                "run_kind": "component",
                "execution_backend": "local",
                "base_url": target["base_url"],
                "model": "mock",
                "target_kind": "local",
                "provider_id": None,
                "run_mode": "single",
                "probe_type": "chat",
                "stream": False,
                "timeout_seconds": 5,
            },
            {"schema_version": "test", "status": "RUNNING"},
        )
        self.assertEqual(run["state"], "RUNNING")
        self.assertEqual(store.cancel_run(run_id)["state"], "CANCELLED")
        store.archive_run(run_id)
        store.purge_run(run_id)
        store.archive_target(target["id"])
        store.purge_target(target["id"])
        store.archive_suite(suite["id"])
        store.purge_suite(suite["id"])

    def test_postgres_monitoring_claim_projection_and_retention(self) -> None:
        from datetime import UTC, datetime, timedelta

        from lexsond.web.control_store import ControlPlaneConflict
        from lexsond.web.postgres_control_store import PostgresControlPlaneStore

        store = PostgresControlPlaneStore(self.pool)
        suffix = uuid4().hex[:10]
        target = store.create_target(
            {
                "name": f"monitor-{suffix}",
                "target_kind": "local",
                "provider_id": None,
                "base_url": "http://127.0.0.1:8000/v1",
                "default_model": "mock",
                "credential_ref": None,
            }
        )
        policy = store.create_monitor_policy(
            {
                "name": f"pulse-{suffix}",
                "target_id": target["id"],
                "suite_revision_id": None,
                "run_kind": "component",
                "probe_type": "chat",
                "execution_backend": "local",
                "model": "mock",
                "stream": False,
                "timeout_seconds": 5,
                "interval_seconds": 60,
                "failure_threshold": 2,
                "recovery_threshold": 1,
                "enabled": True,
            }
        )
        claims = store.claim_due_monitor_policies(
            now=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
            limit=32,
            lease_seconds=30,
        )
        claim = next(item for item in claims if item["id"] == policy["id"])
        with self.assertRaisesRegex(ControlPlaneConflict, "already in progress"):
            store.request_monitor_policy_run(policy["id"])
        with self.assertRaisesRegex(ControlPlaneConflict, "already in progress"):
            store.update_monitor_policy(
                policy["id"],
                {"interval_seconds": 120},
                expected_version=policy["version"],
            )
        run_id = str(uuid4())
        store.create_run(
            run_id,
            {
                "target_id": target["id"],
                "suite_revision_id": None,
                "monitor_policy_id": policy["id"],
                "run_kind": "component",
                "execution_backend": "local",
                "base_url": target["base_url"],
                "model": "mock",
                "target_kind": "local",
                "provider_id": None,
                "run_mode": "single",
                "probe_type": "chat",
                "stream": False,
                "timeout_seconds": 5,
            },
            {"schema_version": "test", "status": "RUNNING"},
        )
        store.complete_monitor_policy_dispatch(
            policy["id"],
            lease_token=claim["lease_token"],
            scheduled_for=claim["scheduled_for"],
            run_id=run_id,
        )
        store.complete_run(
            run_id,
            {"status": "PASS", "dimension_scores": [], "measurements": []},
            {"schema_version": "test", "status": "PASS"},
        )
        projected = store.record_monitor_run(run_id)
        self.assertEqual(projected["state"]["status"], "UP")
        self.assertTrue(store.record_monitor_run(run_id)["replayed"])
        store.archive_run(run_id)
        with self.assertRaisesRegex(ControlPlaneConflict, "current monitor state"):
            store.purge_run(run_id)
        self.assertEqual(store.monitoring_overview(window="24h")["summary"]["up"], 1)
        removed = store.prune_monitoring_data(
            samples_before="2999-01-01T00:00:00+00:00",
            incidents_before="2999-01-01T00:00:00+00:00",
        )
        self.assertEqual(removed["samples"], 1)

        store.archive_monitor_policy(policy["id"])
        store.purge_monitor_policy(policy["id"])
        store.purge_run(run_id)
        store.archive_target(target["id"])
        store.purge_target(target["id"])

    def test_postgres_agent_checkpointer_supports_session_memory_and_events(self) -> None:
        from lexsond.web.postgres_control_store import PostgresControlPlaneStore

        store = PostgresControlPlaneStore(self.pool)
        suffix = uuid4().hex[:10]
        target = store.create_target(
            {
                "name": f"agent-{suffix}",
                "target_kind": "local",
                "provider_id": None,
                "base_url": "http://127.0.0.1:8000/v1",
                "default_model": "mock",
                "credential_ref": None,
            }
        )
        session = store.create_agent_session(
            {
                "title": "PostgreSQL memory",
                "target_id": target["id"],
                "target_version": target["version"],
                "base_url": target["base_url"],
                "target_kind": target["target_kind"],
                "provider_id": None,
                "model": "mock",
                "skill_id": "connection-diagnosis",
            }
        )
        turn_token = store.claim_agent_turn(session["session_id"], lease_seconds=30)
        with self.assertRaises(Exception):
            store.claim_agent_turn(session["session_id"], lease_seconds=30)
        with self.assertRaises(Exception):
            store.archive_agent_session(session["session_id"])
        with self.pool.connection() as connection:
            connection.execute(
                "UPDATE lexsond.agent_sessions SET turn_lease_until = clock_timestamp() - INTERVAL '1 second' WHERE session_id = %s",
                (session["session_id"],),
            )
        fresh_token = store.claim_agent_turn(session["session_id"], lease_seconds=30)
        with self.assertRaises(Exception):
            store.append_agent_message(
                session["session_id"],
                role="user",
                content="stale writer",
                metadata={},
                turn_token=turn_token,
            )
        fenced_message = store.append_agent_message(
            session["session_id"],
            role="user",
            content="fresh fenced writer",
            metadata={},
            turn_token=fresh_token,
        )
        self.assertEqual(fenced_message["sequence"], 1)
        store.renew_agent_turn(
            session["session_id"], fresh_token, lease_seconds=30
        )
        store.release_agent_turn(session["session_id"], fresh_token)
        message = store.append_agent_message(
            session["session_id"],
            role="user",
            content="检查目标",
            metadata={"redaction_applied": False},
        )
        event = store.append_agent_event(
            session["session_id"],
            event_type="LLM_STARTED",
            name="langchain-agent-model",
            status="RUNNING",
            payload={"iteration": 1},
        )

        self.assertEqual(message["sequence"], 2)
        self.assertEqual(event["sequence"], 1)
        self.assertEqual(len(store.list_agent_messages(session["session_id"])), 2)
        self.assertEqual(len(store.list_agent_events(session["session_id"])), 1)
        with self.assertRaises(ValueError):
            store.update_agent_session(
                session["session_id"],
                {"title": "sk-postgres-title-secret-123456"},
                expected_version=session["version"],
            )
        future_key = "plain-postgres-future-key-456789"
        store.append_agent_message(
            session["session_id"],
            role="user",
            content=f"future value {future_key}",
            metadata={},
        )
        self.assertTrue(
            store.quarantine_agent_session_credential(
                session["session_id"], future_key
            )
        )
        self.assertNotIn(
            future_key,
            json.dumps(store.list_agent_messages(session["session_id"])),
        )
        with self.assertRaises(Exception):
            store.append_agent_event(
                session["session_id"],
                event_type="LLM_COMPLETED",
                name="langchain-agent-model",
                status="PASS",
            )
        store.archive_target(target["id"])
        with self.assertRaises(Exception):
            store.restore_agent_session(session["session_id"])
        store.purge_agent_session(session["session_id"])
        store.purge_target(target["id"])

    def test_full_workflow_runs_and_replays_against_postgres_journal(self) -> None:
        from lexsond.storage.postgres import PostgresWorkflowJournal
        from lexsond.workflows import (
            ActivityOutcome,
            ActivityOutcomeStatus,
            CanaryWorkflow,
            WorkflowStatus,
        )

        workflow_input, _ = self._seed_run()
        journal = PostgresWorkflowJournal(self.pool)
        journal.prepare_run(workflow_input)

        class SuccessfulActivities:
            calls = 0

            def invoke(self, workflow_input, invocation, cancel_signal):
                self.calls += 1
                return ActivityOutcome(
                    ActivityOutcomeStatus.SUCCEEDED,
                    f"s3://probe-evidence/{workflow_input.run_id}/{invocation.activity_name.value}",
                )

        activities = SuccessfulActivities()
        state = CanaryWorkflow(journal).run(workflow_input, activities)
        replayed = CanaryWorkflow(journal).run(workflow_input, activities)

        self.assertEqual(state.status, WorkflowStatus.SUCCEEDED)
        self.assertEqual(replayed.status, WorkflowStatus.SUCCEEDED)
        self.assertEqual(activities.calls, 8)
        self.assertEqual(len(journal.load(workflow_input.run_id)), 18)

    def test_compare_and_append_allows_exact_replay_and_rejects_race(self) -> None:
        from lexsond.storage.postgres import PostgresWorkflowJournal
        from lexsond.workflows import (
            ConcurrentWorkflowUpdate,
            WorkflowEvent,
            WorkflowEventType,
            WorkflowPhase,
        )

        workflow_input, _ = self._seed_run()
        journal = PostgresWorkflowJournal(self.pool)
        journal.prepare_run(workflow_input)
        event = WorkflowEvent.deterministic(
            run_id=workflow_input.run_id,
            sequence=1,
            event_type=WorkflowEventType.WORKFLOW_STARTED,
            phase=WorkflowPhase.NONE,
            occurred_at="2026-07-20T00:00:00+00:00",
            workflow_input_sha256=workflow_input.content_hash(),
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(journal.append, event, expected_sequence=0)
                for _ in range(2)
            ]
            for future in futures:
                future.result()
        self.assertEqual(journal.load(workflow_input.run_id), (event,))

        second_input, _ = self._seed_run()
        second_journal = PostgresWorkflowJournal(self.pool)
        second_journal.prepare_run(second_input)
        first = WorkflowEvent.deterministic(
            run_id=second_input.run_id,
            sequence=1,
            event_type=WorkflowEventType.WORKFLOW_STARTED,
            phase=WorkflowPhase.NONE,
            occurred_at="2026-07-20T00:00:00+00:00",
            workflow_input_sha256=second_input.content_hash(),
        )
        different = WorkflowEvent.from_dict(
            {**first.to_dict(), "occurred_at": "2026-07-20T00:00:01+00:00"}
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(second_journal.append, candidate, expected_sequence=0)
                for candidate in (first, different)
            ]
            failures = []
            for future in futures:
                try:
                    future.result()
                except ConcurrentWorkflowUpdate as exc:
                    failures.append(exc)
        self.assertEqual(len(failures), 1)

    def test_activity_leases_failure_replay_and_immutable_results(self) -> None:
        from lexsond.models import NormalizedRunResult
        from lexsond.storage import (
            ActivityClaimDisposition,
            ActivityFailureRecord,
            CanaryRuntimeStoreIntegrityError,
            sanitized_result_for_persistence,
        )
        from lexsond.storage.postgres import (
            PostgresCanaryRuntimeStore,
            PostgresWorkflowJournal,
        )
        from lexsond.workflows import (
            ActivityInvocation,
            ActivityName,
            ActivityOutcome,
            ActivityOutcomeStatus,
            FailureKind,
        )

        workflow_input, _ = self._seed_run()
        PostgresWorkflowJournal(self.pool).prepare_run(workflow_input)
        store = PostgresCanaryRuntimeStore(self.pool)
        invocation = ActivityInvocation(
            run_id=workflow_input.run_id,
            activity_name=ActivityName.EXECUTE,
            attempt=1,
            idempotency_key=f"canary:{workflow_input.run_id}:execute_native_probe",
            input_ref="s3://probe-evidence/preflight",
        )
        claim = store.claim(invocation, lease_seconds=30)
        self.assertEqual(claim.disposition, ActivityClaimDisposition.ACQUIRED)
        busy = store.claim(invocation, lease_seconds=30)
        self.assertEqual(busy.disposition, ActivityClaimDisposition.BUSY)
        self.assertGreater(busy.retry_after_seconds, 0)
        store.renew(
            invocation,
            lease_token=claim.lease_token,
            lease_seconds=30,
        )
        failure = ActivityFailureRecord(
            "UPSTREAM_TIMEOUT",
            kind=FailureKind.INFRASTRUCTURE,
            retryable=True,
        )
        store.fail(invocation, lease_token=claim.lease_token, failure=failure)
        replay = store.claim(invocation, lease_seconds=30)
        self.assertEqual(replay.disposition, ActivityClaimDisposition.FAILED)
        self.assertEqual(replay.failure, failure)

        retry = ActivityInvocation(
            run_id=invocation.run_id,
            activity_name=invocation.activity_name,
            attempt=2,
            idempotency_key=invocation.idempotency_key,
            input_ref=invocation.input_ref,
        )
        retry_claim = store.claim(retry, lease_seconds=30)
        outcome = ActivityOutcome(
            ActivityOutcomeStatus.SUCCEEDED,
            "s3://probe-evidence/final",
        )
        store.complete(retry, lease_token=retry_claim.lease_token, outcome=outcome)
        self.assertEqual(
            store.claim(retry, lease_seconds=30).outcome,
            outcome,
        )

        result = sanitized_result_for_persistence(
            NormalizedRunResult(run_id=workflow_input.run_id)
        )
        store.persist_result(
            run_id=workflow_input.run_id,
            result_ref=outcome.result_ref,
            result=result,
        )
        self.assertEqual(store.load_result(workflow_input.run_id), result)
        with self.assertRaises(CanaryRuntimeStoreIntegrityError):
            store.persist_result(
                run_id=workflow_input.run_id,
                result_ref="s3://probe-evidence/changed",
                result=result,
            )

    def test_evidence_manifest_repository_is_naturally_idempotent(self) -> None:
        from lexsond.storage import (
            CanaryRuntimeStoreIntegrityError,
            EvidenceKind,
            EvidenceManifest,
            RedactionStatus,
        )
        from lexsond.storage.postgres import (
            PostgresEvidenceManifestRepository,
            PostgresWorkflowJournal,
        )

        workflow_input, _ = self._seed_run()
        PostgresWorkflowJournal(self.pool).prepare_run(workflow_input)
        repository = PostgresEvidenceManifestRepository(self.pool)
        values = {
            "run_id": workflow_input.run_id,
            "evidence_kind": EvidenceKind.NORMALIZED_RESULT,
            "object_uri": "s3://probe-evidence/objects/ab/" + "a" * 64,
            "object_sha256": "a" * 64,
            "byte_size": 123,
            "media_type": "application/json",
            "redaction_status": RedactionStatus.SANITIZED,
            "encrypted": False,
            "retention_until": None,
            "created_at": "2026-07-20T00:00:00+00:00",
        }
        first = EvidenceManifest(evidence_id=str(uuid4()), **values)
        duplicate = EvidenceManifest(evidence_id=str(uuid4()), **values)

        repository.add(first)
        repository.add(duplicate)

        changed_retention = EvidenceManifest(
            evidence_id=str(uuid4()),
            **{
                **values,
                "retention_until": "2026-08-20T00:00:00+00:00",
            },
        )
        with self.assertRaises(CanaryRuntimeStoreIntegrityError):
            repository.add(changed_retention)

        conflicting = EvidenceManifest(
            evidence_id=first.evidence_id,
            **{
                **values,
                "object_uri": "s3://probe-evidence/objects/bb/" + "b" * 64,
                "object_sha256": "b" * 64,
            },
        )
        with self.assertRaises(CanaryRuntimeStoreIntegrityError):
            repository.add(conflicting)

    def test_snapshots_are_immutable_and_worker_role_has_no_direct_write(self) -> None:
        import psycopg

        workflow_input, _ = self._seed_run()
        with self.pool.connection() as connection:
            with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
                connection.execute(
                    """
                    UPDATE lexsond.endpoint_snapshots SET model = 'changed'
                    WHERE endpoint_snapshot_id = %s
                    """,
                    (workflow_input.endpoint_snapshot_id,),
                )
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    has_table_privilege('lexsond_worker',
                        'lexsond.workflow_events', 'INSERT') AS direct_insert,
                    has_function_privilege('lexsond_worker',
                        'lexsond.append_workflow_event(uuid,bigint,jsonb)',
                        'EXECUTE') AS function_execute,
                    owner.rolsuper AS function_owner_is_superuser
                FROM pg_proc function
                JOIN pg_roles owner ON owner.oid = function.proowner
                WHERE function.oid =
                    'lexsond.append_workflow_event(uuid,bigint,jsonb)'::regprocedure
                """
            ).fetchone()
        self.assertFalse(row["direct_insert"])
        self.assertTrue(row["function_execute"])
        self.assertFalse(row["function_owner_is_superuser"])

    def test_database_rejects_nested_secrets_and_unencrypted_raw_evidence(self) -> None:
        import psycopg
        from psycopg.types.json import Jsonb

        from lexsond.storage.postgres import PostgresWorkflowJournal

        workflow_input, _ = self._seed_run()
        PostgresWorkflowJournal(self.pool).prepare_run(workflow_input)
        nested = {"provider": {"api_key": "must-never-be-stored"}}
        nested_sha = hashlib.sha256(
            json.dumps(
                nested,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with self.pool.connection() as connection:
            with self.assertRaises(psycopg.errors.CheckViolation):
                connection.execute(
                    """
                    INSERT INTO lexsond.endpoint_snapshots (
                        endpoint_snapshot_id, provider_id, protocol, base_url,
                        model, credential_ref, configuration_sha256,
                        configuration_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        f"nested-secret-{uuid4().hex}",
                        "test",
                        "openai-chat",
                        "https://relay.example/v1",
                        "gpt-test",
                        "vault://ai-probe/test",
                        nested_sha,
                        Jsonb(nested),
                    ),
                )
        with self.pool.connection() as connection:
            with self.assertRaises(psycopg.errors.CheckViolation):
                connection.execute(
                    """
                    INSERT INTO lexsond.evidence_objects (
                        evidence_id, run_id, evidence_kind, object_uri,
                        object_sha256, byte_size, media_type, redaction_status,
                        encrypted, retention_until
                    ) VALUES (%s, %s, 'RUNNER_LOG', %s, %s, 1,
                              'text/plain', 'RAW_RESTRICTED', FALSE,
                              '2026-08-20T00:00:00+00:00')
                    """,
                    (
                        str(uuid4()),
                        workflow_input.run_id,
                        "s3://probe-evidence/raw/" + uuid4().hex,
                        "b" * 64,
                    ),
                )

    def test_migrations_round_trip_in_a_fresh_database(self) -> None:
        database = f"probe_down_{uuid4().hex[:12]}"
        _run(
            POSTGRES_BIN / "createdb",
            "-h",
            self.root,
            "-p",
            str(self.port),
            database,
        )
        try:
            self._psql_file("0001_core.sql", database=database)
            self._psql_file("0002_access.sql", database=database)
            self._psql_file("0003_control_plane.sql", database=database)
            self._psql_file("0004_agent_control_plane.sql", database=database)
            self._psql_file("0005_continuous_monitoring.sql", database=database)
            self._psql_file("0005_continuous_monitoring.down.sql", database=database)
            self._psql_file("0004_agent_control_plane.down.sql", database=database)
            self._psql_file("0003_control_plane.down.sql", database=database)
            self._psql_file("0002_access.down.sql", database=database)
            self._psql_file("0001_core.down.sql", database=database)
            output = _run(
                POSTGRES_BIN / "psql",
                "-h",
                self.root,
                "-p",
                str(self.port),
                "-d",
                database,
                "-Atc",
                "SELECT COUNT(*) FROM pg_namespace WHERE nspname = 'lexsond'",
            )
            self.assertEqual(output.strip(), "0")
        finally:
            _run(
                POSTGRES_BIN / "dropdb",
                "-h",
                self.root,
                "-p",
                str(self.port),
                database,
            )

    def _seed_run(self):
        from psycopg.types.json import Jsonb

        from lexsond.storage.runtime_contracts import canonical_json_bytes
        from lexsond.workflows import CanaryWorkflowInput

        suffix = uuid4().hex
        endpoint_id = f"endpoint-{suffix}"
        suite_uri = f"s3://probe-suites/{suffix}.json"
        suite_name = f"postgres-canary-{suffix}"
        suite_document = {
            "apiVersion": "probe.ai/suite/v1alpha1",
            "kind": "ProbeSuite",
            "metadata": {"name": suite_name, "version": "1.0.0"},
            "spec": {},
        }
        suite_sha = hashlib.sha256(canonical_json_bytes(suite_document)).hexdigest()
        configuration = {}
        configuration_sha = hashlib.sha256(
            canonical_json_bytes(configuration)
        ).hexdigest()
        with self.pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO lexsond.endpoint_snapshots (
                    endpoint_snapshot_id, provider_id, protocol, base_url, model,
                    credential_ref, configuration_sha256, configuration_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    endpoint_id,
                    "test-provider",
                    "openai-chat",
                    "https://relay.example/v1",
                    "gpt-test",
                    "vault://ai-probe/test-key",
                    configuration_sha,
                    Jsonb(configuration),
                ),
            )
            connection.execute(
                """
                INSERT INTO lexsond.probe_suite_snapshots (
                    suite_sha256, suite_name, suite_version, suite_uri, suite_json
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    suite_sha,
                    suite_name,
                    "1.0.0",
                    suite_uri,
                    Jsonb(suite_document),
                ),
            )
        return (
            CanaryWorkflowInput(
                run_id=str(uuid4()),
                endpoint_snapshot_id=endpoint_id,
                suite_name=suite_name,
                suite_version="1.0.0",
                suite_uri=suite_uri,
                suite_sha256=suite_sha,
                region="local-postgres",
            ),
            suite_document,
        )


def _available_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return candidate.getsockname()[1]


def _run(command: Path, *arguments: object) -> str:
    completed = subprocess.run(
        [str(command), *(str(argument) for argument in arguments)],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


if __name__ == "__main__":
    unittest.main()
