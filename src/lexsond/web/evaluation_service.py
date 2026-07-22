from __future__ import annotations

import hashlib
import threading
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from pydantic import SecretStr

from ..evaluations.compiler import (
    CompiledDataset,
    compile_csv_dataset,
    compile_jsonl_dataset,
)
from ..evaluations.coordinator import (
    EvaluationCallResult,
    EvaluationCoordinator,
    EvaluationPlan,
    select_evaluation_items,
)
from ..evaluations.scorers import get_scorer, list_scorers
from ..probe import ProbeConfig, ProbeType, validate_api_key_value
from ..storage.runtime_contracts import canonical_json_bytes
from .api_models import (
    EvaluationDatasetMetadata,
    EvaluationDatasetPatch,
    EvaluationRunCreate,
    EvaluationRunPreview,
)
from .control_contracts import ControlPlaneConflict
from .langchain_runtime import invoke_native_probe


DATASET_REFERENCE_DISPATCHER_VERSION = "1.0.0"


class EvaluationUnavailable(ControlPlaneConflict):
    pass


class EvaluationService:
    """Application boundary for versioned datasets and bounded local evals."""

    def __init__(
        self,
        *,
        store: Any,
        submit_background: Any,
        maintenance_interval_seconds: float | None = None,
    ) -> None:
        self.store = store
        self._submit_background = submit_background
        self._coordinator = EvaluationCoordinator()
        self._cancel_signals: dict[str, threading.Event] = {}
        self._cancel_lock = threading.Lock()
        self._maintenance_stop = threading.Event()
        self._maintenance_thread: threading.Thread | None = None
        self.store.ensure_system_catalog()
        # Only expired, fenced leases may be recovered. A process start must
        # never fail work that another web worker is still executing.
        self.store.fail_expired_runs()
        if maintenance_interval_seconds is not None:
            interval = max(float(maintenance_interval_seconds), 0.05)
            self._maintenance_thread = threading.Thread(
                target=self._maintenance_loop,
                args=(interval,),
                name="lexsond-evaluation-maintenance",
                daemon=True,
            )
            self._maintenance_thread.start()

    def _maintenance_loop(self, interval_seconds: float) -> None:
        while not self._maintenance_stop.wait(interval_seconds):
            try:
                self.store.fail_expired_runs()
            except Exception:
                # Database outages remain infrastructure evidence. The bounded
                # maintenance loop retries only its own non-billable lease scan.
                continue

    def close(self) -> None:
        self._maintenance_stop.set()
        thread = self._maintenance_thread
        if thread is not None:
            thread.join(timeout=2.0)
            if thread.is_alive():
                raise RuntimeError("evaluation maintenance did not stop")

    @staticmethod
    def compile_upload(
        payload: bytes,
        format: str,
        csv_mapping: Mapping[str, str] | None = None,
    ) -> CompiledDataset:
        if format == "jsonl":
            return compile_jsonl_dataset(payload)
        if format == "csv":
            return compile_csv_dataset(payload, csv_mapping)
        raise ValueError("evaluation upload format must be jsonl or csv")

    def validate_upload(
        self,
        payload: bytes,
        format: str,
        csv_mapping: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        compiled = self.compile_upload(payload, format, csv_mapping)
        return {
            "schema_version": compiled.schema_version,
            "content_sha256": compiled.content_sha256,
            "item_count": compiled.item_count,
            "category_count": compiled.category_count,
            "categories": dict(compiled.categories),
            "language_codes": list(compiled.language_codes),
            "preview": [item.to_document() for item in compiled.items[:20]],
            "preview_truncated": compiled.item_count > 20,
        }

    def create_dataset(
        self,
        metadata: EvaluationDatasetMetadata,
        payload: bytes,
        *,
        workspace_id: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        compiled = self.compile_upload(payload, metadata.format, metadata.csv_mapping)
        return self.store.create_dataset(
            metadata.model_dump(),
            compiled,
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
        )

    def list_datasets(self, *, workspace_id: str, include_archived: bool, limit: int) -> list[dict[str, Any]]:
        return self.store.list_datasets(
            workspace_id=workspace_id,
            include_archived=include_archived,
            limit=limit,
        )

    def get_dataset(self, dataset_id: str, *, workspace_id: str, include_archived: bool = False) -> dict[str, Any]:
        return self.store.get_dataset(
            dataset_id,
            workspace_id=workspace_id,
            include_archived=include_archived,
        )

    def list_revisions(
        self, dataset_id: str, *, workspace_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        return self.store.list_revisions(
            dataset_id, workspace_id=workspace_id, limit=limit
        )

    def get_revision(self, dataset_id: str, revision: int, *, workspace_id: str) -> dict[str, Any]:
        return self.store.get_revision(
            dataset_id,
            revision,
            workspace_id=workspace_id,
            item_limit=20,
        )

    def archive_dataset(self, dataset_id: str, *, workspace_id: str) -> dict[str, Any]:
        return self.store.archive_dataset(dataset_id, workspace_id=workspace_id)

    def restore_dataset(self, dataset_id: str, *, workspace_id: str) -> dict[str, Any]:
        return self.store.restore_dataset(dataset_id, workspace_id=workspace_id)

    def purge_dataset(self, dataset_id: str, *, workspace_id: str) -> None:
        self.store.purge_dataset(dataset_id, workspace_id=workspace_id)

    def create_revision(
        self,
        dataset_id: str,
        payload: bytes,
        *,
        format: str,
        workspace_id: str,
        actor_user_id: str,
        csv_mapping: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        compiled = self.compile_upload(payload, format, csv_mapping)
        return self.store.create_revision(
            dataset_id,
            compiled,
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            format=format,
            csv_mapping=csv_mapping,
        )

    def update_dataset(
        self,
        dataset_id: str,
        patch: EvaluationDatasetPatch,
        *,
        workspace_id: str,
    ) -> dict[str, Any]:
        fields = patch.model_fields_set - {"version"}
        changes = {field: getattr(patch, field) for field in fields}
        return self.store.update_dataset(
            dataset_id,
            changes,
            workspace_id=workspace_id,
            expected_version=patch.version,
        )

    @staticmethod
    def scorer_catalog() -> list[dict[str, str]]:
        return [
            {
                "scorer_id": item.scorer_id,
                "version": item.version,
                "label": item.label,
                "description": item.description,
                "execution": "DETERMINISTIC_LOCAL",
            }
            for item in list_scorers()
        ]

    def preview_run(self, model: EvaluationRunPreview, *, workspace_id: str) -> dict[str, Any]:
        context = self.store.resolve_run_context(
            workspace_id=workspace_id,
            revision_id=str(model.dataset_revision_id),
            channel_id=str(model.channel_id),
            catalog_snapshot_id=str(model.catalog_snapshot_id),
            credential_profile_id=(str(model.credential_profile_id) if model.credential_profile_id else None),
            model_ids=model.model_ids,
            enforce_credential_binding=False,
        )
        if model.sample_count > len(context["items"]):
            raise ValueError("sample_count exceeds the dataset revision")
        if (
            context["unknown_chat_capability_models"]
            and not model.confirm_unknown_chat_capability
        ):
            raise ValueError(
                "models with unknown chat capability require explicit confirmation"
            )
        if model.scorer_id != "dataset_reference":
            scorer = get_scorer(model.scorer_id)
            sample = select_evaluation_items(
                context["items"],
                strategy=model.sample_strategy,
                seed=model.sample_seed,
                count=model.sample_count,
            )
            for item in sample:
                scorer.validate_reference(item.reference)
        maximum_calls = len(model.model_ids) * model.sample_count
        return {
            "dataset_revision_id": str(model.dataset_revision_id),
            "model_count": len(model.model_ids),
            "sample_count": model.sample_count,
            "maximum_calls": maximum_calls,
            "maximum_output_tokens": maximum_calls * model.max_output_tokens,
            "concurrency": model.concurrency,
            "estimated_cost_usd": None,
            "cost_status": "UNKNOWN",
            "cost_budget_enforcement": "UNAVAILABLE_UNKNOWN_PRICING",
            "requires_unknown_cost_confirmation": True,
            "model_source_id": context["model_source_id"],
            "unknown_chat_capability_models": context[
                "unknown_chat_capability_models"
            ],
            "comparable": True,
        }

    def start_run(
        self,
        model: EvaluationRunCreate,
        *,
        workspace_id: str,
        actor_user_id: str,
        idempotency_key: str,
        api_key_override: SecretStr | None = None,
        credential_fingerprint: str | None = None,
        credential_version: int | None = None,
    ) -> dict[str, Any]:
        normalized_idempotency = str(UUID(idempotency_key))
        execution_secret = api_key_override or model.api_key
        raw_secret = execution_secret.get_secret_value() if execution_secret is not None else None
        if raw_secret is not None:
            validate_api_key_value(raw_secret)
            if (
                credential_fingerprint is None
                or len(credential_fingerprint) != 64
                or any(character not in "0123456789abcdef" for character in credential_fingerprint)
            ):
                raise ValueError("execution credential requires a keyed fingerprint binding")
        if model.credential_profile_id is not None and credential_version is None:
            raise ValueError("saved execution credential requires a version binding")
        credential_binding_sha256 = hashlib.sha256(
            f"{credential_fingerprint or 'keyless'}:{credential_version or 'temporary'}".encode()
        ).hexdigest()
        submission = model.model_dump(mode="json", exclude={"api_key"})
        submission["credential_binding_sha256"] = credential_binding_sha256
        submission_sha256 = hashlib.sha256(canonical_json_bytes(submission)).hexdigest()
        replay = self.store.find_run_by_idempotency(
            normalized_idempotency, None, workspace_id=workspace_id
        )
        if replay is not None:
            prior_submission = replay.get("request_snapshot", {}).get(
                "submission_sha256"
            )
            if prior_submission != submission_sha256:
                raise ControlPlaneConflict(
                    "idempotency key was used for another evaluation"
                )
            return replay
        context = self.store.resolve_run_context(
            workspace_id=workspace_id,
            revision_id=str(model.dataset_revision_id),
            channel_id=str(model.channel_id),
            catalog_snapshot_id=str(model.catalog_snapshot_id),
            credential_profile_id=(str(model.credential_profile_id) if model.credential_profile_id else None),
            model_ids=model.model_ids,
            credential_fingerprint=credential_fingerprint,
            credential_version=credential_version,
            enforce_credential_binding=True,
        )
        if (
            context["unknown_chat_capability_models"]
            and not model.confirm_unknown_chat_capability
        ):
            raise ValueError(
                "models with unknown chat capability require explicit confirmation"
            )
        plan = EvaluationPlan(
            models=tuple(model.model_ids),
            sample_strategy=model.sample_strategy,
            sample_seed=model.sample_seed,
            sample_count=model.sample_count,
            concurrency=model.concurrency,
            max_output_tokens=model.max_output_tokens,
            timeout_seconds=model.timeout_seconds,
            max_cost_usd=model.max_cost_usd,
            estimated_cost_usd=None,
            confirm_unknown_cost=model.confirm_unknown_cost,
            scorer_id=model.scorer_id,
        )
        sample = select_evaluation_items(
            context["items"],
            strategy=plan.sample_strategy,
            seed=plan.sample_seed,
            count=plan.sample_count,
        )
        if model.scorer_id != "dataset_reference":
            scorer = get_scorer(model.scorer_id)
            for item in sample:
                scorer.validate_reference(item.reference)
        if model.scorer_id == "dataset_reference":
            scorer_versions = {
                str(item.reference["scorer"]): get_scorer(
                    str(item.reference["scorer"])
                ).version
                for item in sample
            }
            run_scorer_version = DATASET_REFERENCE_DISPATCHER_VERSION
        else:
            selected_scorer = get_scorer(model.scorer_id)
            scorer_versions = {model.scorer_id: selected_scorer.version}
            run_scorer_version = selected_scorer.version
        durable = {
            "submission_sha256": submission_sha256,
            "dataset_revision_id": str(model.dataset_revision_id),
            "dataset_id": context["revision"]["dataset_id"],
            "channel_id": str(model.channel_id),
            "catalog_snapshot_id": str(model.catalog_snapshot_id),
            "credential_profile_id": str(model.credential_profile_id) if model.credential_profile_id else None,
            "model_source_id": context["model_source_id"],
            "target_version": context["target_version"],
            "target_base_url_sha256": context["target_base_url_sha256"],
            "catalog_content_sha256": context["catalog_content_sha256"],
            "endpoint_snapshot": {
                "target_id": context["target"]["id"],
                "target_version": context["target"]["version"],
                "base_url": context["target"]["base_url"],
                "target_kind": context["target"]["target_kind"],
                "provider_id": context["target"].get("provider_id"),
                "protocol": context["target"]["protocol"],
            },
            "credential_version": credential_version,
            "credential_binding_sha256": credential_binding_sha256,
            "model_ids": model.model_ids,
            "sample_strategy": model.sample_strategy,
            "sampling_algorithm": "sha256-rank/v1",
            "sample_seed": model.sample_seed,
            "sample_count": model.sample_count,
            "sample_item_ids": [item.item_id for item in sample],
            "scorer_id": model.scorer_id,
            "scorer_versions": scorer_versions,
            "prompt_template": "lexsond-messages/v2-native-roles",
            "temperature": 0,
            "concurrency": model.concurrency,
            "max_output_tokens": model.max_output_tokens,
            "timeout_seconds": model.timeout_seconds,
            "max_cost_usd": model.max_cost_usd,
            "confirm_unknown_cost": model.confirm_unknown_cost,
            "confirm_unknown_chat_capability": model.confirm_unknown_chat_capability,
            "cost_budget_enforcement": "UNAVAILABLE_UNKNOWN_PRICING",
        }
        request_sha256 = hashlib.sha256(canonical_json_bytes(durable)).hexdigest()
        target = context["target"]
        if target["target_kind"] == "cloud" and not raw_secret:
            raise ValueError("a credential is required for a cloud evaluation")
        run_id = str(uuid4())
        lease_id = str(uuid4())
        run = self.store.create_run(
            {
                "evaluation_run_id": run_id,
                "idempotency_key": normalized_idempotency,
                "request_sha256": request_sha256,
                "dataset_id": context["revision"]["dataset_id"],
                "dataset_revision_id": str(model.dataset_revision_id),
                "channel_id": str(model.channel_id),
                "catalog_snapshot_id": str(model.catalog_snapshot_id),
                "credential_profile_id": str(model.credential_profile_id) if model.credential_profile_id else None,
                "model_source_id": context["model_source_id"],
                "scorer_id": model.scorer_id,
                "scorer_version": run_scorer_version,
                "sample_strategy": model.sample_strategy,
                "sample_seed": model.sample_seed,
                "sample_count": model.sample_count,
                "model_ids": model.model_ids,
                "concurrency": model.concurrency,
                "max_output_tokens": model.max_output_tokens,
                "timeout_seconds": model.timeout_seconds,
                "max_cost_usd": model.max_cost_usd,
                "request_snapshot_json": durable,
                "created_by": actor_user_id,
                "execution_lease_id": lease_id,
            },
            workspace_id=workspace_id,
        )
        if run.get("id") != run_id:
            # Another request won the idempotency race after the optimistic
            # lookup. The repository returned that durable run; scheduling the
            # losing UUID would duplicate billable model calls.
            return run
        cancel_signal = threading.Event()
        with self._cancel_lock:
            self._cancel_signals[run_id] = cancel_signal
        try:
            self._submit_background(
                self._execute_run,
                run_id,
                workspace_id,
                plan,
                context["items"],
                target,
                execution_secret,
                cancel_signal,
                lease_id,
            )
        except Exception as exc:
            raw_secret = None
            execution_secret = None
            with self._cancel_lock:
                self._cancel_signals.pop(run_id, None)
            self.store.fail_run(
                run_id,
                "EVALUATION_SCHEDULING_FAILURE",
                workspace_id=workspace_id,
                lease_id=lease_id,
            )
            raise EvaluationUnavailable(
                "evaluation execution could not be scheduled safely"
            ) from exc
        return run

    def _execute_run(
        self,
        run_id: str,
        workspace_id: str,
        plan: EvaluationPlan,
        items: Any,
        target: Mapping[str, Any],
        execution_secret: SecretStr | None,
        cancellation: threading.Event,
        lease_id: str,
    ) -> None:
        def invoke(model_id: str, item: Any, max_output_tokens: int, timeout_seconds: float) -> EvaluationCallResult:
            raw_secret = (
                execution_secret.get_secret_value()
                if execution_secret is not None
                else None
            )
            result = invoke_native_probe(
                ProbeConfig(
                    base_url=target["base_url"],
                    api_key=raw_secret,
                    model=model_id,
                    timeout_seconds=timeout_seconds,
                    stream=False,
                    chat_messages=_native_messages(item.input),
                    max_output_tokens=max_output_tokens,
                    probe_type=ProbeType.CHAT,
                    provider_id=target.get("provider_id"),
                )
            )
            if not result.measurements:
                return EvaluationCallResult("", None, "PROTOCOL", {}, {}, None)
            measurement = result.measurements[0]
            return EvaluationCallResult(
                output_text=measurement.output_text,
                status_code=measurement.status_code,
                error_class=(measurement.error_class.value if measurement.error_class else None),
                latency={
                    "connect_ms": measurement.connect_ms,
                    "ttfb_ms": measurement.ttfb_ms,
                    "ttft_ms": measurement.ttft_ms,
                    "e2e_ms": measurement.e2e_ms,
                },
                usage={
                    "input_tokens": measurement.provider_reported_input_tokens,
                    "output_tokens": measurement.provider_reported_output_tokens,
                    "total_tokens": measurement.provider_reported_total_tokens,
                },
                cost_usd=None,
                safe_facts={
                    "retry_after_seconds": measurement.evidence.get("retry_after_seconds")
                },
            )

        try:
            outcome = self._coordinator.run(
                plan,
                items,
                invoke,
                cancellation=cancellation,
                cancellation_check=lambda: self.store.is_cancel_requested(
                    run_id, workspace_id=workspace_id, lease_id=lease_id
                ),
                event_observer=lambda event: self.store.append_event(
                    run_id, event, workspace_id=workspace_id, lease_id=lease_id
                ),
                item_observer=lambda item: self.store.record_item(
                    run_id, item, workspace_id=workspace_id, lease_id=lease_id
                ),
            )
            self.store.finish_run(
                run_id, outcome, workspace_id=workspace_id, lease_id=lease_id
            )
        except Exception:
            self.store.fail_run(
                run_id,
                "EVALUATION_INFRASTRUCTURE_FAILURE",
                workspace_id=workspace_id,
                lease_id=lease_id,
            )
        finally:
            execution_secret = None
            with self._cancel_lock:
                self._cancel_signals.pop(run_id, None)

    def cancel_run(self, run_id: str, *, workspace_id: str) -> dict[str, Any]:
        run = self.store.request_cancel(run_id, workspace_id=workspace_id)
        with self._cancel_lock:
            signal = self._cancel_signals.get(run_id)
        if signal is not None:
            signal.set()
        return run

    def list_runs(self, *, workspace_id: str, include_archived: bool, limit: int) -> list[dict[str, Any]]:
        return self.store.list_runs(
            workspace_id=workspace_id,
            include_archived=include_archived,
            limit=limit,
        )

    def get_run(self, run_id: str, *, workspace_id: str, include_archived: bool = False) -> dict[str, Any]:
        return self.store.get_run(
            run_id,
            workspace_id=workspace_id,
            include_archived=include_archived,
        )

    def list_run_items(self, run_id: str, *, workspace_id: str, after_sequence: int, limit: int) -> list[dict[str, Any]]:
        return self.store.list_run_items(
            run_id,
            workspace_id=workspace_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    def list_run_events(self, run_id: str, *, workspace_id: str, after_sequence: int, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.list_run_events(
            run_id,
            workspace_id=workspace_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    def archive_run(self, run_id: str, *, workspace_id: str) -> dict[str, Any]:
        return self.store.set_run_archived(run_id, workspace_id=workspace_id, archived=True)

    def restore_run(self, run_id: str, *, workspace_id: str) -> dict[str, Any]:
        return self.store.set_run_archived(run_id, workspace_id=workspace_id, archived=False)

    def purge_run(self, run_id: str, *, workspace_id: str) -> None:
        self.store.purge_run(run_id, workspace_id=workspace_id)


class UnavailableEvaluationService:
    def close(self) -> None:
        return None

    def __getattr__(self, _name: str) -> Any:
        def unavailable(*_args: Any, **_kwargs: Any) -> Any:
            raise EvaluationUnavailable("evaluation storage requires migration 0011")

        return unavailable


def _native_messages(input_value: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    messages = input_value.get("messages")
    if not isinstance(messages, list):
        raise ValueError("evaluation item messages are invalid")
    native: list[tuple[str, str]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            raise ValueError("evaluation message is invalid")
        native.append((str(message["role"]), str(message["content"])))
    choices = input_value.get("choices")
    if isinstance(choices, list):
        rendered = "\n".join(
            f"{chr(65 + index)}. {choice}"
            for index, choice in enumerate(choices)
        )
        suffix = f"\n\nChoices:\n{rendered}"
        for index in range(len(native) - 1, -1, -1):
            if native[index][0] == "user":
                native[index] = ("user", native[index][1] + suffix)
                break
        else:
            native.append(("user", f"Choices:\n{rendered}"))
    return tuple(native)
