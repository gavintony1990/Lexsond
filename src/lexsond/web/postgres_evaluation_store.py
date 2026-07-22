from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
from psycopg.types.json import Jsonb

from ..evaluations.compiler import CompiledDataset, EvaluationItem
from ..evaluations.coordinator import (
    EvaluationEvent,
    EvaluationItemOutcome,
    EvaluationRunOutcome,
)
from ..evaluations.quickeval import quickeval_items, quickeval_manifest
from ..storage.postgres import PostgresPool
from .control_contracts import ControlPlaneConflict, ControlPlaneNotFound


SYSTEM_DATASETS: tuple[dict[str, Any], ...] = (
    {
        "slug": "mmlu-pro",
        "name": "MMLU-Pro",
        "description": "综合知识与推理公开基准目录；需要固定版本和 hash 后导入。",
        "license_spdx": "MIT",
        "license_url": "https://huggingface.co/datasets/TIGER-Lab/MMLU-Pro",
        "source_url": "https://huggingface.co/datasets/TIGER-Lab/MMLU-Pro",
        "source_version": None,
        "source_verified_at": "2026-07-22",
        "distribution_policy": "IMPORT_REQUIRED",
    },
    {
        "slug": "big-bench-lite",
        "name": "BIG-bench Lite",
        "description": "多任务推理目录；仓库许可不替代任务级来源审查。",
        "license_spdx": "Apache-2.0",
        "license_url": "https://github.com/google/BIG-bench/blob/main/LICENSE",
        "source_url": "https://github.com/google/BIG-bench",
        "source_version": None,
        "source_verified_at": "2026-07-22",
        "distribution_policy": "IMPORT_REQUIRED",
    },
    {
        "slug": "ifeval",
        "name": "IFEval",
        "description": "指令遵循目录；导入前必须复核具体数据与代码许可。",
        "license_spdx": "LicenseRef-Review-Required",
        "license_url": "https://github.com/google-research/google-research/tree/master/instruction_following_eval",
        "source_url": "https://github.com/google-research/google-research/tree/master/instruction_following_eval",
        "source_version": None,
        "source_verified_at": "2026-07-22",
        "distribution_policy": "LICENSE_REVIEW",
    },
    {
        "slug": "humaneval",
        "name": "HumanEval",
        "description": "代码能力目录；本版本不执行模型生成代码。",
        "license_spdx": "MIT",
        "license_url": "https://github.com/openai/human-eval/blob/master/LICENSE",
        "source_url": "https://github.com/openai/human-eval",
        "source_version": None,
        "source_verified_at": "2026-07-22",
        "distribution_policy": "RUNNER_REQUIRED",
    },
    {
        "slug": "c-eval",
        "name": "C-Eval",
        "description": "中文知识目录；非商业数据许可禁止商业云自动导入。",
        "license_spdx": "CC-BY-NC-SA-4.0",
        "license_url": "https://github.com/hkust-nlp/ceval/blob/main/LICENSE-DATA",
        "source_url": "https://github.com/hkust-nlp/ceval",
        "source_version": None,
        "source_verified_at": "2026-07-22",
        "distribution_policy": "RESEARCH_ONLY",
    },
)


class PostgresEvaluationStore:
    """PostgreSQL-only repository for immutable evaluation evidence."""

    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def ensure_system_catalog(self) -> None:
        manifest = quickeval_manifest()
        compiled = compile_quickeval()
        quick_dataset_id = str(uuid5(NAMESPACE_URL, "lexsond:evaluation-dataset:lexsond-quickeval"))
        quick_revision_id = str(uuid5(NAMESPACE_URL, "lexsond:evaluation-revision:lexsond-quickeval:1.0.0"))
        with self._pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO lexsond.evaluation_datasets (
                    dataset_id, scope, slug, name, description, license_spdx,
                    license_url, source_version, source_verified_at,
                    distribution_policy, default_scorer_id
                ) VALUES (%s, 'SYSTEM', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (dataset_id) DO NOTHING
                """,
                (
                    quick_dataset_id,
                    manifest["slug"],
                    manifest["name"],
                    "80 道项目原创、确定性、文本评测题。",
                    manifest["license_spdx"],
                    manifest["license_url"],
                    manifest["version"],
                    "2026-07-22",
                    manifest["distribution_policy"],
                    "normalized_exact_match",
                ),
            )
            existing = connection.execute(
                """
                SELECT content_sha256 FROM lexsond.evaluation_dataset_revisions
                WHERE dataset_id = %s AND revision = 1
                """,
                (quick_dataset_id,),
            ).fetchone()
            if existing is not None and existing["content_sha256"].strip() != compiled.content_sha256:
                raise RuntimeError("Lexsond QuickEval v1 content changed after publication")
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO lexsond.evaluation_dataset_revisions (
                        revision_id, dataset_id, revision, schema_version,
                        content_sha256, item_count, category_count,
                        language_codes, manifest_json
                    ) VALUES (%s, %s, 1, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        quick_revision_id,
                        quick_dataset_id,
                        compiled.schema_version,
                        compiled.content_sha256,
                        compiled.item_count,
                        compiled.category_count,
                        Jsonb(list(compiled.language_codes)),
                        Jsonb(manifest),
                    ),
                )
                self._insert_items(connection, quick_revision_id, compiled.items)
                connection.execute(
                    """
                    UPDATE lexsond.evaluation_dataset_revisions
                    SET sealed_at = clock_timestamp()
                    WHERE revision_id = %s AND sealed_at IS NULL
                    """,
                    (quick_revision_id,),
                )
                connection.execute(
                    """
                    UPDATE lexsond.evaluation_datasets
                    SET latest_revision_id = %s, updated_at = clock_timestamp()
                    WHERE dataset_id = %s
                    """,
                    (quick_revision_id, quick_dataset_id),
                )
            for entry in SYSTEM_DATASETS:
                dataset_id = str(uuid5(NAMESPACE_URL, f"lexsond:evaluation-dataset:{entry['slug']}"))
                connection.execute(
                    """
                    INSERT INTO lexsond.evaluation_datasets AS current (
                        dataset_id, scope, slug, name, description, license_spdx,
                        license_url, source_url, source_version,
                        source_verified_at, distribution_policy,
                        default_scorer_id
                    ) VALUES (%s, 'SYSTEM', %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        'multiple_choice_accuracy')
                    ON CONFLICT (dataset_id) DO UPDATE SET
                        description = EXCLUDED.description,
                        license_spdx = EXCLUDED.license_spdx,
                        license_url = EXCLUDED.license_url,
                        source_url = EXCLUDED.source_url,
                        source_version = EXCLUDED.source_version,
                        source_verified_at = EXCLUDED.source_verified_at,
                        distribution_policy = EXCLUDED.distribution_policy,
                        updated_at = clock_timestamp()
                    WHERE ROW(
                        current.description, current.license_spdx,
                        current.license_url, current.source_url,
                        current.source_version, current.source_verified_at,
                        current.distribution_policy
                    ) IS DISTINCT FROM ROW(
                        EXCLUDED.description, EXCLUDED.license_spdx,
                        EXCLUDED.license_url, EXCLUDED.source_url,
                        EXCLUDED.source_version, EXCLUDED.source_verified_at,
                        EXCLUDED.distribution_policy
                    )
                    """,
                    (
                        dataset_id,
                        entry["slug"], entry["name"], entry["description"],
                        entry["license_spdx"], entry["license_url"],
                        entry["source_url"], entry["source_version"],
                        entry["source_verified_at"], entry["distribution_policy"],
                    ),
                )

    def fail_expired_runs(self) -> int:
        """Fail only work whose durable execution lease has expired."""

        with self._pool.connection() as connection:
            expired = connection.execute(
                """
                SELECT evaluation_run_id, workspace_id, cancel_requested_at
                FROM lexsond.evaluation_runs
                WHERE state = 'RUNNING'
                  AND lease_expires_at < clock_timestamp()
                FOR UPDATE SKIP LOCKED
                """
            ).fetchall()
            if not expired:
                return 0
            projected = 0
            for expired_row in expired:
                run_id = str(expired_row["evaluation_run_id"])
                workspace_id = str(expired_row["workspace_id"])
                cancelled = expired_row["cancel_requested_at"] is not None
                terminal_state = "CANCELLED" if cancelled else "FAILED"
                failure_code = "CANCELLED" if cancelled else "EXECUTION_LEASE_EXPIRED"
                connection.execute(
                    """
                    UPDATE lexsond.evaluation_run_models
                    SET state = CASE
                        WHEN completed_items > 0 THEN 'PARTIAL'
                        ELSE %s
                    END
                    WHERE evaluation_run_id = %s AND workspace_id = %s
                      AND state IN ('PENDING', 'RUNNING')
                    """,
                    (terminal_state, run_id, workspace_id),
                )
                sequence_row = connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
                    FROM lexsond.evaluation_run_events
                    WHERE evaluation_run_id = %s AND workspace_id = %s
                    """,
                    (run_id, workspace_id),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO lexsond.evaluation_run_events (
                        evaluation_run_id, workspace_id, sequence, event_id,
                        event_type, state, safe_facts_json
                    ) VALUES (%s, %s, %s, %s, 'EVALUATION_FINISHED', %s, %s)
                    """,
                    (
                        run_id, workspace_id, int(sequence_row["next_sequence"]),
                        str(uuid4()), terminal_state,
                        Jsonb({"failure_code": failure_code}),
                    ),
                )
                cursor = connection.execute(
                    """
                    UPDATE lexsond.evaluation_runs
                    SET state = %s, failure_code = %s,
                        aggregate_result_json = jsonb_build_object(
                            'data_completeness', 'UNKNOWN',
                            'lease_recovery', 'NO_PROVIDER_RETRY',
                            'cancel_intent_preserved', %s
                        ), finished_at = clock_timestamp()
                    WHERE state = 'RUNNING' AND evaluation_run_id = %s
                      AND workspace_id = %s
                    """,
                    (
                        terminal_state, failure_code, cancelled,
                        run_id, workspace_id,
                    ),
                )
                projected += max(cursor.rowcount, 0)
        return projected

    @staticmethod
    def _insert_items(connection: Any, revision_id: str, items: Sequence[EvaluationItem]) -> None:
        for item in items:
            connection.execute(
                """
                INSERT INTO lexsond.evaluation_dataset_items (
                    revision_id, item_index, item_id, category, language,
                    input_json, reference_json, metadata_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    revision_id, item.item_index, item.item_id, item.category,
                    item.language, Jsonb(dict(item.input)),
                    Jsonb(dict(item.reference)), Jsonb(dict(item.metadata)),
                ),
            )

    def list_datasets(
        self,
        *,
        workspace_id: str,
        include_archived: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        archived = "" if include_archived else "AND d.archived_at IS NULL"
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT d.*, r.revision AS latest_revision,
                    r.content_sha256, r.item_count, r.category_count,
                    r.language_codes, r.manifest_json, r.created_at AS revision_created_at
                FROM lexsond.evaluation_datasets d
                LEFT JOIN lexsond.evaluation_dataset_revisions r
                  ON r.revision_id = d.latest_revision_id
                WHERE (d.scope = 'SYSTEM' OR d.workspace_id = %s) {archived}
                ORDER BY d.scope, d.updated_at DESC, d.dataset_id
                LIMIT %s
                """,
                (workspace_id, min(max(int(limit), 1), 100)),
            ).fetchall()
        return [_dataset(row) for row in rows]

    def get_dataset(
        self,
        dataset_id: str,
        *,
        workspace_id: str,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT d.*, r.revision AS latest_revision,
                    r.content_sha256, r.item_count, r.category_count,
                    r.language_codes, r.manifest_json, r.created_at AS revision_created_at
                FROM lexsond.evaluation_datasets d
                LEFT JOIN lexsond.evaluation_dataset_revisions r
                  ON r.revision_id = d.latest_revision_id
                WHERE d.dataset_id = %s
                  AND (d.scope = 'SYSTEM' OR d.workspace_id = %s)
                """,
                (dataset_id, workspace_id),
            ).fetchone()
        if row is None or (row["archived_at"] is not None and not include_archived):
            raise ControlPlaneNotFound("evaluation dataset was not found")
        return _dataset(row)

    def create_dataset(
        self,
        metadata: Mapping[str, Any],
        compiled: CompiledDataset,
        *,
        workspace_id: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        dataset_id = str(uuid4())
        revision_id = str(uuid4())
        manifest = {
            "format": metadata["format"],
            "categories": dict(compiled.categories),
            "schema_version": compiled.schema_version,
            "content_sha256": compiled.content_sha256,
            "item_count": compiled.item_count,
            "license_spdx": metadata["license_spdx"],
            "source_url": metadata.get("source_url"),
            "csv_mapping": metadata.get("csv_mapping"),
        }
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"evaluation-dataset:{workspace_id}:{compiled.content_sha256}",),
                )
                duplicate = connection.execute(
                    """
                    SELECT d.dataset_id FROM lexsond.evaluation_datasets d
                    JOIN lexsond.evaluation_dataset_revisions r ON r.dataset_id = d.dataset_id
                    WHERE d.workspace_id = %s AND r.content_sha256 = %s
                    LIMIT 1
                    """,
                    (workspace_id, compiled.content_sha256),
                ).fetchone()
                if duplicate is not None:
                    raise ControlPlaneConflict(
                        "the same dataset content already exists in this workspace"
                    )
                connection.execute(
                    """
                    INSERT INTO lexsond.evaluation_datasets (
                        dataset_id, workspace_id, scope, slug, name, description,
                        license_spdx, license_url, source_url, distribution_policy,
                        default_scorer_id, created_by
                    ) VALUES (%s, %s, 'WORKSPACE', %s, %s, %s, %s, %s, %s,
                        %s, %s, %s)
                    """,
                    (
                        dataset_id, workspace_id, metadata["slug"], metadata["name"],
                        metadata["description"], metadata["license_spdx"],
                        metadata["license_url"], metadata.get("source_url"),
                        metadata["distribution_policy"], metadata["default_scorer_id"],
                        actor_user_id,
                    ),
                )
                self._insert_revision(
                    connection, dataset_id, revision_id, 1, compiled, manifest,
                    actor_user_id,
                )
                connection.execute(
                    """
                    UPDATE lexsond.evaluation_datasets SET latest_revision_id = %s,
                        updated_at = clock_timestamp()
                    WHERE dataset_id = %s
                    """,
                    (revision_id, dataset_id),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise ControlPlaneConflict("evaluation dataset slug already exists") from exc
        return self.get_dataset(dataset_id, workspace_id=workspace_id)

    def _insert_revision(
        self,
        connection: Any,
        dataset_id: str,
        revision_id: str,
        revision: int,
        compiled: CompiledDataset,
        manifest: Mapping[str, Any],
        actor_user_id: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO lexsond.evaluation_dataset_revisions (
                revision_id, dataset_id, revision, schema_version,
                content_sha256, item_count, category_count, language_codes,
                manifest_json, created_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                revision_id, dataset_id, revision, compiled.schema_version,
                compiled.content_sha256, compiled.item_count,
                compiled.category_count, Jsonb(list(compiled.language_codes)),
                Jsonb(dict(manifest)), actor_user_id,
            ),
        )
        self._insert_items(connection, revision_id, compiled.items)
        connection.execute(
            """
            UPDATE lexsond.evaluation_dataset_revisions
            SET sealed_at = clock_timestamp()
            WHERE revision_id = %s AND sealed_at IS NULL
            """,
            (revision_id,),
        )

    def create_revision(
        self,
        dataset_id: str,
        compiled: CompiledDataset,
        *,
        workspace_id: str,
        actor_user_id: str,
        format: str,
        csv_mapping: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        revision_id = str(uuid4())
        with self._pool.connection() as connection:
            dataset = connection.execute(
                """
                SELECT * FROM lexsond.evaluation_datasets
                WHERE dataset_id = %s AND workspace_id = %s AND scope = 'WORKSPACE'
                FOR UPDATE
                """,
                (dataset_id, workspace_id),
            ).fetchone()
            if dataset is None:
                raise ControlPlaneNotFound("workspace evaluation dataset was not found")
            if dataset["archived_at"] is not None:
                raise ControlPlaneConflict("archived evaluation dataset cannot receive a revision")
            duplicate = connection.execute(
                """
                SELECT revision FROM lexsond.evaluation_dataset_revisions
                WHERE dataset_id = %s AND content_sha256 = %s
                """,
                (dataset_id, compiled.content_sha256),
            ).fetchone()
            if duplicate is not None:
                raise ControlPlaneConflict(
                    f"the same content already exists as revision {duplicate['revision']}"
                )
            revision = int(connection.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 AS value FROM lexsond.evaluation_dataset_revisions WHERE dataset_id = %s",
                (dataset_id,),
            ).fetchone()["value"])
            manifest = {
                "format": format,
                "categories": dict(compiled.categories),
                "schema_version": compiled.schema_version,
                "content_sha256": compiled.content_sha256,
                "item_count": compiled.item_count,
                "license_spdx": dataset["license_spdx"],
                "source_url": dataset["source_url"],
                "csv_mapping": dict(csv_mapping) if csv_mapping is not None else None,
            }
            self._insert_revision(
                connection, dataset_id, revision_id, revision, compiled,
                manifest, actor_user_id,
            )
            connection.execute(
                """
                UPDATE lexsond.evaluation_datasets SET latest_revision_id = %s,
                    version = version + 1, updated_at = clock_timestamp()
                WHERE dataset_id = %s
                """,
                (revision_id, dataset_id),
            )
        return self.get_revision(
            dataset_id, revision, workspace_id=workspace_id, item_limit=20
        )

    def list_revisions(
        self, dataset_id: str, *, workspace_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        self.get_dataset(dataset_id, workspace_id=workspace_id, include_archived=True)
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM lexsond.evaluation_dataset_revisions
                WHERE dataset_id = %s ORDER BY revision DESC LIMIT %s
                """,
                (dataset_id, min(max(limit, 1), 100)),
            ).fetchall()
        return [_revision(row) for row in rows]

    def get_revision(
        self,
        dataset_id: str,
        revision: int,
        *,
        workspace_id: str,
        item_limit: int = 20,
    ) -> dict[str, Any]:
        self.get_dataset(dataset_id, workspace_id=workspace_id, include_archived=True)
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM lexsond.evaluation_dataset_revisions
                WHERE dataset_id = %s AND revision = %s
                """,
                (dataset_id, revision),
            ).fetchone()
            if row is None:
                raise ControlPlaneNotFound("evaluation dataset revision was not found")
            items = connection.execute(
                """
                SELECT * FROM lexsond.evaluation_dataset_items
                WHERE revision_id = %s ORDER BY item_index LIMIT %s
                """,
                (row["revision_id"], min(max(item_limit, 0), 200)),
            ).fetchall()
        value = _revision(row)
        value["items"] = [_dataset_item(item) for item in items]
        return value

    def load_revision_items(
        self,
        revision_id: str,
        *,
        workspace_id: str,
    ) -> tuple[dict[str, Any], tuple[EvaluationItem, ...]]:
        with self._pool.connection() as connection:
            revision = connection.execute(
                """
                SELECT r.*, d.scope, d.workspace_id, d.archived_at
                FROM lexsond.evaluation_dataset_revisions r
                JOIN lexsond.evaluation_datasets d ON d.dataset_id = r.dataset_id
                WHERE r.revision_id = %s
                  AND (d.scope = 'SYSTEM' OR d.workspace_id = %s)
                """,
                (revision_id, workspace_id),
            ).fetchone()
            if revision is None or revision["archived_at"] is not None:
                raise ControlPlaneNotFound("evaluation dataset revision was not found")
            rows = connection.execute(
                """
                SELECT * FROM lexsond.evaluation_dataset_items
                WHERE revision_id = %s ORDER BY item_index
                """,
                (revision_id,),
            ).fetchall()
        items = tuple(
            EvaluationItem(
                int(row["item_index"]), row["item_id"], row["category"],
                row["language"], dict(row["input_json"]),
                dict(row["reference_json"]), dict(row["metadata_json"]),
            )
            for row in rows
        )
        return _revision(revision), items

    def update_dataset(
        self,
        dataset_id: str,
        changes: Mapping[str, Any],
        *,
        workspace_id: str,
        expected_version: int,
    ) -> dict[str, Any]:
        allowed = {"name", "description", "license_spdx", "license_url", "source_url", "default_scorer_id"}
        if not changes or not set(changes).issubset(allowed):
            raise ValueError("evaluation dataset update contains unknown fields")
        assignments = ", ".join(f"{key} = %s" for key in changes)
        with self._pool.connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE lexsond.evaluation_datasets
                SET {assignments}, version = version + 1, updated_at = clock_timestamp()
                WHERE dataset_id = %s AND workspace_id = %s AND scope = 'WORKSPACE'
                  AND version = %s
                """,
                (*changes.values(), dataset_id, workspace_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise ControlPlaneConflict("evaluation dataset version is stale or read-only")
        return self.get_dataset(dataset_id, workspace_id=workspace_id, include_archived=True)

    def archive_dataset(self, dataset_id: str, *, workspace_id: str) -> dict[str, Any]:
        return self._set_dataset_archived(dataset_id, workspace_id=workspace_id, archived=True)

    def restore_dataset(self, dataset_id: str, *, workspace_id: str) -> dict[str, Any]:
        return self._set_dataset_archived(dataset_id, workspace_id=workspace_id, archived=False)

    def _set_dataset_archived(self, dataset_id: str, *, workspace_id: str, archived: bool) -> dict[str, Any]:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE lexsond.evaluation_datasets
                SET archived_at = CASE WHEN %s THEN clock_timestamp() ELSE NULL END,
                    version = version + 1, updated_at = clock_timestamp()
                WHERE dataset_id = %s AND workspace_id = %s AND scope = 'WORKSPACE'
                """,
                (archived, dataset_id, workspace_id),
            )
            if cursor.rowcount != 1:
                raise ControlPlaneNotFound("workspace evaluation dataset was not found")
        return self.get_dataset(dataset_id, workspace_id=workspace_id, include_archived=True)

    def purge_dataset(self, dataset_id: str, *, workspace_id: str) -> None:
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    "SELECT lexsond.purge_evaluation_dataset(%s, %s)",
                    (workspace_id, dataset_id),
                )
        except psycopg.Error as exc:
            _raise_purge_domain_error(exc, resource="evaluation dataset")
            raise

    def resolve_run_context(
        self,
        *,
        workspace_id: str,
        revision_id: str,
        channel_id: str,
        catalog_snapshot_id: str,
        credential_profile_id: str | None,
        model_ids: Sequence[str],
        credential_fingerprint: str | None = None,
        credential_version: int | None = None,
        enforce_credential_binding: bool = False,
    ) -> dict[str, Any]:
        revision, items = self.load_revision_items(revision_id, workspace_id=workspace_id)
        with self._pool.connection() as connection:
            target = connection.execute(
                """
                SELECT * FROM lexsond.targets
                WHERE workspace_id = %s AND target_id = %s AND archived_at IS NULL
                """,
                (workspace_id, channel_id),
            ).fetchone()
            snapshot = connection.execute(
                """
                SELECT * FROM lexsond.model_catalog_snapshots
                WHERE workspace_id = %s AND snapshot_id = %s
                  AND expires_at > clock_timestamp() AND status = 'FRESH'
                """,
                (workspace_id, catalog_snapshot_id),
            ).fetchone()
        if target is None:
            raise ControlPlaneNotFound("evaluation channel was not found")
        if snapshot is None:
            raise ControlPlaneNotFound("fresh model catalog snapshot was not found")
        if str(snapshot["target_id"]) != channel_id or int(snapshot["target_version"]) != int(target["version"]):
            raise ControlPlaneConflict("model catalog snapshot no longer matches the channel")
        if not snapshot["target_base_url"] or not snapshot["target_kind"] or not snapshot["protocol"]:
            raise ControlPlaneConflict(
                "model catalog snapshot lacks an immutable endpoint contract"
            )
        snapshot_profile = str(snapshot["credential_profile_id"]) if snapshot["credential_profile_id"] else None
        if snapshot_profile != credential_profile_id:
            raise ControlPlaneConflict("evaluation credential does not match model discovery")
        snapshot_fingerprint = (
            snapshot["credential_fingerprint"].strip()
            if snapshot["credential_fingerprint"]
            else None
        )
        snapshot_version = snapshot["credential_version"]
        if enforce_credential_binding and snapshot_fingerprint != credential_fingerprint:
            raise ControlPlaneConflict(
                "evaluation execution credential does not match model discovery"
            )
        if (
            enforce_credential_binding
            and snapshot_profile is not None
            and snapshot_version != credential_version
        ):
            raise ControlPlaneConflict(
                "saved credential changed after model discovery"
            )
        catalog_entries = {
            str(entry.get("id")): entry
            for entry in snapshot["models_json"]
            if isinstance(entry, Mapping) and entry.get("id") is not None
        }
        visible = set(catalog_entries)
        if not set(model_ids).issubset(visible):
            raise ControlPlaneConflict("all evaluation models must be visible in the frozen catalog")
        unsupported: list[str] = []
        unknown: list[str] = []
        for model_id in model_ids:
            probe_types = catalog_entries[model_id].get("probe_types")
            if not isinstance(probe_types, list) or not probe_types:
                unknown.append(model_id)
            elif "chat" not in probe_types:
                unsupported.append(model_id)
        if unsupported:
            raise ControlPlaneConflict(
                "evaluation models must explicitly support the chat probe"
            )
        return {
            "revision": revision,
            "items": items,
            "target": {
                "id": str(snapshot["target_id"]),
                "base_url": snapshot["target_base_url"],
                "target_kind": snapshot["target_kind"],
                "provider_id": snapshot["provider_id"],
                "protocol": snapshot["protocol"],
                "version": int(snapshot["target_version"]),
            },
            "model_source_id": target["provider_id"] or "custom-openai-compatible",
            "target_version": int(target["version"]),
            "target_base_url_sha256": hashlib.sha256(
                snapshot["target_base_url"].encode("utf-8")
            ).hexdigest(),
            "catalog_content_sha256": snapshot["content_sha256"].strip(),
            "unknown_chat_capability_models": unknown,
        }

    def find_run_by_idempotency(
        self,
        idempotency_key: str,
        request_sha256: str | None,
        *,
        workspace_id: str,
    ) -> dict[str, Any] | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT evaluation_run_id, request_sha256 FROM lexsond.evaluation_runs
                WHERE workspace_id = %s AND idempotency_key = %s
                """,
                (workspace_id, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        if request_sha256 is not None and row["request_sha256"].strip() != request_sha256:
            raise ControlPlaneConflict("idempotency key was used for another evaluation")
        return self.get_run(str(row["evaluation_run_id"]), workspace_id=workspace_id)

    def create_run(self, value: Mapping[str, Any], *, workspace_id: str) -> dict[str, Any]:
        replay = self.find_run_by_idempotency(
            value["idempotency_key"], value["request_sha256"], workspace_id=workspace_id
        )
        if replay is not None:
            return replay
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO lexsond.evaluation_runs (
                        evaluation_run_id, workspace_id, idempotency_key,
                        request_sha256, dataset_id, dataset_revision_id,
                        channel_id, catalog_snapshot_id, credential_profile_id,
                        execution_lease_id, model_source_id,
                        state, scorer_id, scorer_version, sample_strategy,
                        sample_seed, sample_count, model_count, concurrency,
                        max_output_tokens, timeout_seconds, max_cost_usd,
                        request_snapshot_json, created_by
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        'RUNNING', %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s)
                    """,
                    (
                        value["evaluation_run_id"], workspace_id,
                        value["idempotency_key"], value["request_sha256"],
                        value["dataset_id"], value["dataset_revision_id"],
                        value["channel_id"], value["catalog_snapshot_id"],
                        value.get("credential_profile_id"),
                        value["execution_lease_id"], value["model_source_id"],
                        value["scorer_id"],
                        value["scorer_version"], value["sample_strategy"],
                        value["sample_seed"], value["sample_count"],
                        len(value["model_ids"]), value["concurrency"],
                        value["max_output_tokens"], value["timeout_seconds"],
                        value["max_cost_usd"], Jsonb(value["request_snapshot_json"]),
                        value["created_by"],
                    ),
                )
                for model_id in value["model_ids"]:
                    connection.execute(
                        """
                        INSERT INTO lexsond.evaluation_run_models (
                            evaluation_run_id, workspace_id, model_id,
                            provider_model_id, state
                        ) VALUES (%s, %s, %s, %s, 'PENDING')
                        """,
                        (value["evaluation_run_id"], workspace_id, model_id, model_id),
                    )
        except psycopg.errors.UniqueViolation as exc:
            replay = self.find_run_by_idempotency(
                value["idempotency_key"], value["request_sha256"], workspace_id=workspace_id
            )
            if replay is not None:
                return replay
            raise ControlPlaneConflict("evaluation run already exists") from exc
        return self.get_run(value["evaluation_run_id"], workspace_id=workspace_id)

    @staticmethod
    def _touch_execution_lease(
        connection: Any,
        run_id: str,
        *,
        workspace_id: str,
        lease_id: str,
    ) -> Mapping[str, Any]:
        row = connection.execute(
            """
            UPDATE lexsond.evaluation_runs
            SET lease_expires_at = clock_timestamp() + INTERVAL '5 minutes'
            WHERE workspace_id = %s AND evaluation_run_id = %s
              AND execution_lease_id = %s AND state = 'RUNNING'
              AND lease_expires_at >= clock_timestamp()
            RETURNING state, cancel_requested_at
            """,
            (workspace_id, run_id, lease_id),
        ).fetchone()
        if row is None:
            raise ControlPlaneConflict("evaluation execution lease is no longer active")
        return row

    def is_cancel_requested(
        self, run_id: str, *, workspace_id: str, lease_id: str
    ) -> bool:
        with self._pool.connection() as connection:
            row = self._touch_execution_lease(
                connection, run_id, workspace_id=workspace_id, lease_id=lease_id
            )
        return row["cancel_requested_at"] is not None

    def append_event(
        self,
        run_id: str,
        event: EvaluationEvent,
        *,
        workspace_id: str,
        lease_id: str,
    ) -> None:
        with self._pool.connection() as connection:
            self._touch_execution_lease(
                connection, run_id, workspace_id=workspace_id, lease_id=lease_id
            )
            inserted = connection.execute(
                """
                INSERT INTO lexsond.evaluation_run_events (
                    evaluation_run_id, workspace_id, sequence, event_id,
                    event_type, model_id, item_id, state, safe_facts_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (evaluation_run_id, sequence) DO NOTHING
                """,
                (
                    run_id, workspace_id, event.sequence, str(uuid4()),
                    event.event_type, event.model_id, event.item_id,
                    event.state, Jsonb(dict(event.safe_facts or {})),
                ),
            )
            if inserted.rowcount != 1:
                existing = connection.execute(
                    """
                    SELECT event_type, model_id, item_id, state, safe_facts_json
                    FROM lexsond.evaluation_run_events
                    WHERE evaluation_run_id = %s AND workspace_id = %s
                      AND sequence = %s
                    """,
                    (run_id, workspace_id, event.sequence),
                ).fetchone()
                expected = (
                    event.event_type,
                    event.model_id,
                    event.item_id,
                    event.state,
                    dict(event.safe_facts or {}),
                )
                actual = None if existing is None else (
                    existing["event_type"], existing["model_id"],
                    existing["item_id"], existing["state"],
                    dict(existing["safe_facts_json"]),
                )
                if actual != expected:
                    raise ControlPlaneConflict(
                        "evaluation event sequence conflicts with durable evidence"
                    )
            if event.model_id is not None and event.event_type == "ITEM_STARTED":
                connection.execute(
                    """
                    UPDATE lexsond.evaluation_run_models SET state = 'RUNNING'
                    WHERE workspace_id = %s AND evaluation_run_id = %s
                      AND model_id = %s AND state = 'PENDING'
                    """,
                    (workspace_id, run_id, event.model_id),
                )

    def record_item(
        self,
        run_id: str,
        item: EvaluationItemOutcome,
        *,
        workspace_id: str,
        lease_id: str,
    ) -> None:
        with self._pool.connection() as connection:
            self._touch_execution_lease(
                connection, run_id, workspace_id=workspace_id, lease_id=lease_id
            )
            inserted = connection.execute(
                """
                INSERT INTO lexsond.evaluation_run_items (
                    evaluation_run_id, workspace_id, model_id, item_id, category,
                    sequence, state, score, status, reason_code, latency_json,
                    usage_json, output_sha256, safe_facts_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (evaluation_run_id, model_id, sequence) DO NOTHING
                """,
                (
                    run_id, workspace_id, item.model_id, item.item_id, item.category,
                    item.sequence, item.state, item.score, item.status,
                    item.reason_code, Jsonb(dict(item.latency)),
                    Jsonb(dict(item.usage)), item.output_sha256,
                    Jsonb(dict(item.safe_facts)),
                ),
            )
            if inserted.rowcount != 1:
                existing = connection.execute(
                    """
                    SELECT item_id, category, state, score, status, reason_code,
                        latency_json, usage_json, output_sha256, safe_facts_json
                    FROM lexsond.evaluation_run_items
                    WHERE evaluation_run_id = %s AND workspace_id = %s
                      AND model_id = %s AND sequence = %s
                    """,
                    (run_id, workspace_id, item.model_id, item.sequence),
                ).fetchone()
                expected = (
                    item.item_id, item.category, item.state, item.score,
                    item.status, item.reason_code, dict(item.latency),
                    dict(item.usage), item.output_sha256, dict(item.safe_facts),
                )
                actual = None if existing is None else (
                    existing["item_id"], existing["category"], existing["state"],
                    existing["score"], existing["status"], existing["reason_code"],
                    dict(existing["latency_json"]), dict(existing["usage_json"]),
                    existing["output_sha256"].strip()
                    if existing["output_sha256"] else None,
                    dict(existing["safe_facts_json"]),
                )
                if actual != expected:
                    raise ControlPlaneConflict(
                        "evaluation item sequence conflicts with durable evidence"
                    )
                return
            connection.execute(
                """
                UPDATE lexsond.evaluation_run_models SET
                    completed_items = completed_items + 1,
                    passed_items = passed_items + CASE WHEN %s = 'PASS' THEN 1 ELSE 0 END,
                    failed_items = failed_items + CASE WHEN %s = 'FAIL' THEN 1 ELSE 0 END,
                    unknown_items = unknown_items + CASE WHEN %s = 'UNKNOWN' THEN 1 ELSE 0 END
                WHERE workspace_id = %s AND evaluation_run_id = %s AND model_id = %s
                """,
                (item.status, item.status, item.status, workspace_id, run_id, item.model_id),
            )

    def finish_run(
        self,
        run_id: str,
        outcome: EvaluationRunOutcome,
        *,
        workspace_id: str,
        lease_id: str,
    ) -> dict[str, Any]:
        already_terminal = False
        with self._pool.connection() as connection:
            self._touch_execution_lease(
                connection, run_id, workspace_id=workspace_id, lease_id=lease_id
            )
            current = connection.execute(
                """
                SELECT state FROM lexsond.evaluation_runs
                WHERE workspace_id = %s AND evaluation_run_id = %s
                FOR UPDATE
                """,
                (workspace_id, run_id),
            ).fetchone()
            if current is None:
                raise ControlPlaneNotFound("evaluation run was not found")
            if current["state"] != "RUNNING":
                already_terminal = True
            else:
                for model in outcome.models:
                    connection.execute(
                        """
                        UPDATE lexsond.evaluation_run_models
                        SET state = %s, metrics_json = %s
                        WHERE workspace_id = %s AND evaluation_run_id = %s AND model_id = %s
                        """,
                        (model.state, Jsonb(dict(model.metrics)), workspace_id, run_id, model.model_id),
                    )
                terminal = outcome.events[-1]
                if terminal.event_type != "EVALUATION_FINISHED":
                    raise ControlPlaneConflict(
                        "evaluation outcome is missing its terminal event"
                    )
                connection.execute(
                    """
                    INSERT INTO lexsond.evaluation_run_events (
                        evaluation_run_id, workspace_id, sequence, event_id,
                        event_type, model_id, item_id, state, safe_facts_json
                    ) VALUES (%s, %s, %s, %s, 'EVALUATION_FINISHED', %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        workspace_id,
                        terminal.sequence,
                        str(uuid4()),
                        terminal.model_id,
                        terminal.item_id,
                        terminal.state,
                        Jsonb(dict(terminal.safe_facts or {})),
                    ),
                )
                connection.execute(
                    """
                    UPDATE lexsond.evaluation_runs
                    SET state = %s, aggregate_result_json = %s, failure_code = %s,
                        finished_at = clock_timestamp()
                    WHERE workspace_id = %s AND evaluation_run_id = %s AND state = 'RUNNING'
                    """,
                    (
                        outcome.state,
                        Jsonb(dict(outcome.aggregate)),
                        outcome.failure_code,
                        workspace_id,
                        run_id,
                    ),
                )
        return self.get_run(
            run_id,
            workspace_id=workspace_id,
            include_archived=already_terminal,
        )

    def fail_run(
        self,
        run_id: str,
        failure_code: str,
        *,
        workspace_id: str,
        lease_id: str,
    ) -> None:
        with self._pool.connection() as connection:
            self._touch_execution_lease(
                connection, run_id, workspace_id=workspace_id, lease_id=lease_id
            )
            connection.execute(
                """
                UPDATE lexsond.evaluation_run_models
                SET state = CASE WHEN completed_items > 0 THEN 'PARTIAL' ELSE 'FAILED' END
                WHERE workspace_id = %s AND evaluation_run_id = %s
                  AND state IN ('PENDING', 'RUNNING')
                """,
                (workspace_id, run_id),
            )
            next_sequence = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1 AS value
                    FROM lexsond.evaluation_run_events
                    WHERE evaluation_run_id = %s AND workspace_id = %s
                    """,
                    (run_id, workspace_id),
                ).fetchone()["value"]
            )
            connection.execute(
                """
                INSERT INTO lexsond.evaluation_run_events (
                    evaluation_run_id, workspace_id, sequence, event_id,
                    event_type, state, safe_facts_json
                ) VALUES (%s, %s, %s, %s, 'EVALUATION_FINISHED',
                    'FAILED', %s)
                """,
                (
                    run_id, workspace_id, next_sequence, str(uuid4()),
                    Jsonb({"failure_code": failure_code}),
                ),
            )
            connection.execute(
                """
                UPDATE lexsond.evaluation_runs SET state = 'FAILED',
                    failure_code = %s,
                    aggregate_result_json = jsonb_build_object(
                        'data_completeness', 'UNKNOWN',
                        'infrastructure_failure', true
                    ), finished_at = clock_timestamp()
                WHERE workspace_id = %s AND evaluation_run_id = %s AND state = 'RUNNING'
                """,
                (failure_code, workspace_id, run_id),
            )

    def get_run(self, run_id: str, *, workspace_id: str, include_archived: bool = False) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM lexsond.evaluation_runs
                WHERE workspace_id = %s AND evaluation_run_id = %s
                """,
                (workspace_id, run_id),
            ).fetchone()
            if row is None or (row["archived_at"] is not None and not include_archived):
                raise ControlPlaneNotFound("evaluation run was not found")
            models = connection.execute(
                """
                SELECT * FROM lexsond.evaluation_run_models
                WHERE workspace_id = %s AND evaluation_run_id = %s ORDER BY model_id
                """,
                (workspace_id, run_id),
            ).fetchall()
        return _run(row, models)

    def list_runs(self, *, workspace_id: str, include_archived: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        archived = "" if include_archived else "AND archived_at IS NULL"
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT evaluation_run_id FROM lexsond.evaluation_runs
                WHERE workspace_id = %s {archived}
                ORDER BY created_at DESC, evaluation_run_id DESC LIMIT %s
                """,
                (workspace_id, min(max(limit, 1), 100)),
            ).fetchall()
        return [self.get_run(str(row["evaluation_run_id"]), workspace_id=workspace_id, include_archived=True) for row in rows]

    def list_run_items(self, run_id: str, *, workspace_id: str, after_sequence: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        self.get_run(run_id, workspace_id=workspace_id, include_archived=True)
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM lexsond.evaluation_run_items
                WHERE workspace_id = %s AND evaluation_run_id = %s
                  AND sequence > %s
                ORDER BY sequence LIMIT %s
                """,
                (workspace_id, run_id, max(after_sequence, 0), min(max(limit, 1), 2000)),
            ).fetchall()
        return [_run_item(row) for row in rows]

    def list_run_events(self, run_id: str, *, workspace_id: str, after_sequence: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        self.get_run(run_id, workspace_id=workspace_id, include_archived=True)
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM lexsond.evaluation_run_events
                WHERE workspace_id = %s AND evaluation_run_id = %s
                  AND sequence > %s ORDER BY sequence LIMIT %s
                """,
                (workspace_id, run_id, max(after_sequence, 0), min(max(limit, 1), 500)),
            ).fetchall()
        return [_run_event(row) for row in rows]

    def request_cancel(self, run_id: str, *, workspace_id: str) -> dict[str, Any]:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE lexsond.evaluation_runs SET
                    cancel_requested_at = COALESCE(cancel_requested_at, clock_timestamp())
                WHERE workspace_id = %s AND evaluation_run_id = %s AND state = 'RUNNING'
                """,
                (workspace_id, run_id),
            )
            if cursor.rowcount != 1:
                self.get_run(run_id, workspace_id=workspace_id, include_archived=True)
        return self.get_run(run_id, workspace_id=workspace_id, include_archived=True)

    def set_run_archived(self, run_id: str, *, workspace_id: str, archived: bool) -> dict[str, Any]:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE lexsond.evaluation_runs SET
                    archived_at = CASE WHEN %s THEN clock_timestamp() ELSE NULL END
                WHERE workspace_id = %s AND evaluation_run_id = %s AND state <> 'RUNNING'
                """,
                (archived, workspace_id, run_id),
            )
            if cursor.rowcount != 1:
                raise ControlPlaneConflict("running evaluation cannot be archived or was not found")
        return self.get_run(run_id, workspace_id=workspace_id, include_archived=True)

    def purge_run(self, run_id: str, *, workspace_id: str) -> None:
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    "SELECT lexsond.purge_evaluation_run(%s, %s)",
                    (workspace_id, run_id),
                )
        except psycopg.Error as exc:
            _raise_purge_domain_error(exc, resource="evaluation run")
            raise


def compile_quickeval() -> CompiledDataset:
    from ..evaluations.compiler import compile_document_items

    return compile_document_items(quickeval_items())


def _raise_purge_domain_error(exc: psycopg.Error, *, resource: str) -> None:
    """Translate stable SQLSTATEs without exposing database error text."""

    if exc.sqlstate == "P0002":
        raise ControlPlaneNotFound(f"{resource} was not found") from None
    if exc.sqlstate in {"55000", "23503"}:
        raise ControlPlaneConflict(
            f"{resource} must be archived and unreferenced before purge"
        ) from None


def _dataset(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["dataset_id"]),
        "workspace_id": str(row["workspace_id"]) if row["workspace_id"] else None,
        "scope": row["scope"], "slug": row["slug"], "name": row["name"],
        "description": row["description"], "license_spdx": row["license_spdx"],
        "license_url": row["license_url"], "source_url": row["source_url"],
        "source_version": row["source_version"],
        "source_verified_at": (
            row["source_verified_at"].isoformat()
            if row["source_verified_at"] else None
        ),
        "distribution_policy": row["distribution_policy"],
        "default_scorer_id": row["default_scorer_id"],
        "version": int(row["version"]), "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "archived_at": row["archived_at"].isoformat() if row["archived_at"] else None,
        "latest_revision": None if row.get("latest_revision") is None else {
            "id": str(row["latest_revision_id"]),
            "revision": int(row["latest_revision"]),
            "content_sha256": row["content_sha256"].strip(),
            "item_count": int(row["item_count"]),
            "category_count": int(row["category_count"]),
            "language_codes": list(row["language_codes"]),
            "manifest": dict(row["manifest_json"]),
            "created_at": row["revision_created_at"].isoformat(),
        },
    }


def _revision(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["revision_id"]), "dataset_id": str(row["dataset_id"]),
        "revision": int(row["revision"]), "schema_version": row["schema_version"],
        "content_sha256": row["content_sha256"].strip(),
        "item_count": int(row["item_count"]), "category_count": int(row["category_count"]),
        "language_codes": list(row["language_codes"]), "manifest": dict(row["manifest_json"]),
        "created_at": row["created_at"].isoformat(),
    }


def _dataset_item(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "item_index": int(row["item_index"]), "item_id": row["item_id"],
        "category": row["category"], "language": row["language"],
        "input": dict(row["input_json"]), "reference": dict(row["reference_json"]),
        "metadata": dict(row["metadata_json"]),
    }


def _target_context(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["target_id"]), "base_url": row["base_url"],
        "target_kind": row["target_kind"], "provider_id": row["provider_id"],
        "version": int(row["version"]),
    }


def _run(row: Mapping[str, Any], models: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "id": str(row["evaluation_run_id"]), "workspace_id": str(row["workspace_id"]),
        "dataset_id": str(row["dataset_id"]), "dataset_revision_id": str(row["dataset_revision_id"]),
        "channel_id": str(row["channel_id"]),
        "credential_profile_id": str(row["credential_profile_id"]) if row["credential_profile_id"] else None,
        "model_source_id": row["model_source_id"], "state": row["state"],
        "scorer_id": row["scorer_id"], "scorer_version": row["scorer_version"],
        "sample_strategy": row["sample_strategy"], "sample_seed": int(row["sample_seed"]),
        "sample_count": int(row["sample_count"]), "model_count": int(row["model_count"]),
        "concurrency": int(row["concurrency"]), "max_output_tokens": int(row["max_output_tokens"]),
        "timeout_seconds": float(row["timeout_seconds"]), "max_cost_usd": float(row["max_cost_usd"]),
        "request_snapshot": dict(row["request_snapshot_json"]),
        "aggregate_result": dict(row["aggregate_result_json"]) if row["aggregate_result_json"] else None,
        "failure_code": row["failure_code"],
        "cancel_requested_at": row["cancel_requested_at"].isoformat() if row["cancel_requested_at"] else None,
        "created_at": row["created_at"].isoformat(),
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
        "archived_at": row["archived_at"].isoformat() if row["archived_at"] else None,
        "models": [
            {
                "model_id": model["model_id"], "provider_model_id": model["provider_model_id"],
                "state": model["state"], "completed_items": int(model["completed_items"]),
                "passed_items": int(model["passed_items"]), "failed_items": int(model["failed_items"]),
                "unknown_items": int(model["unknown_items"]), "metrics": dict(model["metrics_json"]),
            }
            for model in models
        ],
    }


def _run_item(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "model_id": row["model_id"], "item_id": row["item_id"],
        "category": row["category"],
        "sequence": int(row["sequence"]), "state": row["state"],
        "score": row["score"], "status": row["status"],
        "reason_code": row["reason_code"], "latency": dict(row["latency_json"]),
        "usage": dict(row["usage_json"]), "output_sha256": row["output_sha256"].strip() if row["output_sha256"] else None,
        "safe_facts": dict(row["safe_facts_json"]), "created_at": row["created_at"].isoformat(),
    }


def _run_event(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "sequence": int(row["sequence"]), "event_id": str(row["event_id"]),
        "event_type": row["event_type"], "model_id": row["model_id"],
        "item_id": row["item_id"], "state": row["state"],
        "safe_facts": dict(row["safe_facts_json"]), "occurred_at": row["occurred_at"].isoformat(),
    }
