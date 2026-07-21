from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol
from urllib.parse import urlsplit

from ..models import NormalizedRunResult, RunStatus
from ..storage import (
    ActivityClaimDisposition,
    ActivityFailureRecord,
    CanaryRuntimeStore,
    CanaryRuntimeStoreIntegrityError,
    EvidenceKind,
    EvidenceManifestRepository,
    EvidenceStore,
    RedactionStatus,
    sanitized_result_for_persistence,
)
from ..suite import (
    ProbeSuite,
    SuiteExecutionCancelled,
    SuiteValidationError,
    compile_suite,
    run_suite,
)
from .canary import CancellationSignal
from .contracts import (
    ActivityFailure,
    ActivityInvocation,
    ActivityLeaseBusy,
    ActivityName,
    ActivityOutcome,
    ActivityOutcomeStatus,
    CanaryWorkflowInput,
    FailureKind,
)


@dataclass(frozen=True, slots=True)
class EndpointSnapshot:
    endpoint_snapshot_id: str
    protocol: str
    base_url: str
    model: str
    credential_handle: str = field(repr=False)
    mock_mode: str | None = None

    def __post_init__(self) -> None:
        for name in ("endpoint_snapshot_id", "model", "credential_handle"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if self.protocol != "openai-chat":
            raise ValueError("native Canary Activities require protocol=openai-chat")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain credentials, query, or fragment")
        if self.mock_mode is not None and (
            not isinstance(self.mock_mode, str) or not self.mock_mode
        ):
            raise ValueError("mock_mode must be a non-empty string or null")


class EndpointSnapshotResolver(Protocol):
    def resolve(self, endpoint_snapshot_id: str) -> EndpointSnapshot: ...


class SuiteDocumentResolver(Protocol):
    def read(self, suite_uri: str) -> bytes: ...


class SecretResolver(Protocol):
    def resolve(self, credential_handle: str) -> str: ...


class MappingEndpointSnapshotResolver:
    def __init__(self, snapshots: Mapping[str, EndpointSnapshot]) -> None:
        self._snapshots = dict(snapshots)

    def resolve(self, endpoint_snapshot_id: str) -> EndpointSnapshot:
        try:
            return self._snapshots[endpoint_snapshot_id]
        except KeyError as exc:
            raise LookupError("endpoint snapshot was not found") from exc


class MappingSecretResolver:
    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(handles={len(self._values)})"

    def resolve(self, credential_handle: str) -> str:
        try:
            value = self._values[credential_handle]
        except KeyError as exc:
            raise LookupError("credential was not found") from exc
        if not isinstance(value, str) or not value:
            raise LookupError("credential was empty")
        return value


class CredentialReferenceEnvironmentSecretResolver:
    """Bind production credential references to injected environment names.

    The binding document contains references and variable names only. Secret
    values remain in the worker process environment and are resolved inside an
    Activity, so neither the file nor Temporal history contains credentials.
    """

    def __init__(self, bindings: Mapping[str, str]) -> None:
        self._bindings = dict(bindings)
        if not self._bindings:
            raise ValueError("credential bindings must not be empty")
        for credential_ref, environment_variable in self._bindings.items():
            parsed = urlsplit(credential_ref)
            if (
                parsed.scheme
                not in {
                    "vault",
                    "aws-secretsmanager",
                    "gcp-secretmanager",
                    "azure-keyvault",
                }
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("credential_ref has an unsupported or unsafe URI")
            if _BOUND_SECRET_ENV.fullmatch(environment_variable) is None:
                raise ValueError(
                    "bound environment variable must use LEXSOND_SECRET_*"
                )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(bindings={len(self._bindings)})"

    @classmethod
    def from_file(
        cls, path: Path, *, max_bytes: int = 1_000_000
    ) -> CredentialReferenceEnvironmentSecretResolver:
        if not isinstance(path, Path) or not path.is_absolute():
            raise ValueError("credential binding path must be absolute")
        if path.is_symlink() or not path.is_file():
            raise ValueError("credential binding path must be a regular non-symlink file")
        if not 1 <= max_bytes <= 10_000_000 or path.stat().st_size > max_bytes:
            raise ValueError("credential binding document exceeds max_bytes")
        try:
            document = json.loads(path.read_bytes())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("credential binding document is invalid JSON") from exc
        if not isinstance(document, dict) or set(document) != {
            "apiVersion",
            "kind",
            "items",
        }:
            raise ValueError("credential binding document fields differ from contract")
        if document["apiVersion"] != "probe.ai/credential-bindings/v1alpha1":
            raise ValueError("unsupported credential binding apiVersion")
        if document["kind"] != "CredentialEnvironmentBindingList":
            raise ValueError("credential binding kind is invalid")
        items = document["items"]
        if not isinstance(items, list) or not items:
            raise ValueError("credential binding items must be a non-empty array")
        bindings: dict[str, str] = {}
        for index, item in enumerate(items):
            if not isinstance(item, dict) or set(item) != {
                "credential_ref",
                "environment_variable",
            }:
                raise ValueError(
                    f"credential binding item {index} fields differ from contract"
                )
            credential_ref = item["credential_ref"]
            if credential_ref in bindings:
                raise ValueError("credential references must be unique")
            bindings[credential_ref] = item["environment_variable"]
        return cls(bindings)

    def resolve(self, credential_handle: str) -> str:
        try:
            environment_variable = self._bindings[credential_handle]
            value = os.environ[environment_variable]
        except KeyError as exc:
            raise LookupError("credential binding or injected value was not found") from exc
        if not value:
            raise LookupError("injected credential was empty")
        return value


_BOUND_SECRET_ENV = re.compile(r"^LEXSOND_SECRET_[A-Z0-9_]{1,96}$")


class NativeCanaryActivities:
    """Concrete L0-L3 OpenAI-compatible Canary Activity delegate.

    Endpoint configuration and credentials are resolved inside the Activity.
    Scoring happens before raw response text crosses an Activity boundary. The
    sanitized, content-addressed result then flows through later audit stages.
    """

    def __init__(
        self,
        *,
        endpoint_resolver: EndpointSnapshotResolver,
        suite_resolver: SuiteDocumentResolver,
        secret_resolver: SecretResolver,
        evidence_store: EvidenceStore,
        runtime_store: CanaryRuntimeStore,
        evidence_manifest_repository: EvidenceManifestRepository | None = None,
    ) -> None:
        self._endpoint_resolver = endpoint_resolver
        self._suite_resolver = suite_resolver
        self._secret_resolver = secret_resolver
        self._evidence_store = evidence_store
        self._runtime_store = runtime_store
        self._evidence_manifest_repository = evidence_manifest_repository

    def invoke(
        self,
        workflow_input: CanaryWorkflowInput,
        invocation: ActivityInvocation,
        cancel_signal: CancellationSignal | None,
    ) -> ActivityOutcome:
        try:
            claim = self._runtime_store.claim(
                invocation,
                lease_seconds=workflow_input.activity_timeout_seconds,
            )
        except CanaryRuntimeStoreIntegrityError as exc:
            raise ActivityFailure(
                "ACTIVITY_IDEMPOTENCY_CONFLICT",
                kind=FailureKind.POLICY,
                retryable=False,
            ) from exc
        except Exception as exc:
            raise ActivityFailure(
                "ACTIVITY_STATE_UNAVAILABLE",
                kind=FailureKind.INFRASTRUCTURE,
                retryable=True,
            ) from exc
        if claim.disposition is ActivityClaimDisposition.COMPLETED:
            if claim.outcome is None:
                raise RuntimeError("completed claim lost its outcome")
            return claim.outcome
        if claim.disposition is ActivityClaimDisposition.FAILED:
            if claim.failure is None:
                raise RuntimeError("failed claim lost its failure")
            raise ActivityFailure(
                claim.failure.error_code,
                kind=claim.failure.kind,
                retryable=claim.failure.retryable,
            )
        if claim.disposition is ActivityClaimDisposition.BUSY:
            if claim.retry_after_seconds is None:
                raise RuntimeError("busy claim lost its retry duration")
            raise ActivityLeaseBusy(claim.retry_after_seconds)
        lease_token = claim.lease_token
        if lease_token is None:
            raise RuntimeError("acquired claim lost its lease token")
        if cancel_signal is not None and cancel_signal.is_set():
            failure = ActivityFailure(
                "ACTIVITY_CANCELLED",
                kind=FailureKind.INFRASTRUCTURE,
                retryable=False,
            )
            self._record_failure(invocation, lease_token, failure)
            raise failure

        renewer = _ActivityLeaseRenewer(
            self._runtime_store,
            invocation,
            lease_token,
            workflow_input.activity_timeout_seconds,
        )
        combined_cancel = _CombinedCancellationSignal(
            cancel_signal,
            renewer.failure_signal,
        )
        renewer.start()
        try:
            outcome = self._invoke_uncached(
                workflow_input, invocation, combined_cancel
            )
            renewer.stop()
            renewer.raise_if_failed()
            self._runtime_store.complete(
                invocation,
                lease_token=lease_token,
                outcome=outcome,
            )
            return outcome
        except CanaryRuntimeStoreIntegrityError as exc:
            raise ActivityFailure(
                "ACTIVITY_IDEMPOTENCY_CONFLICT",
                kind=FailureKind.POLICY,
                retryable=False,
            ) from exc
        except ActivityFailure as failure:
            if renewer.failed:
                raise renewer.activity_failure() from failure
            self._record_failure(invocation, lease_token, failure)
            raise failure
        except Exception as exc:
            if renewer.failed:
                raise renewer.activity_failure() from exc
            failure = ActivityFailure(
                "NATIVE_ACTIVITY_INFRASTRUCTURE_FAILED",
                kind=FailureKind.INFRASTRUCTURE,
                retryable=True,
            )
            self._record_failure(invocation, lease_token, failure)
            raise failure from exc
        finally:
            renewer.stop()

    def _record_failure(
        self,
        invocation: ActivityInvocation,
        lease_token: str,
        failure: ActivityFailure,
    ) -> None:
        try:
            self._runtime_store.fail(
                invocation,
                lease_token=lease_token,
                failure=ActivityFailureRecord(
                    error_code=failure.error_code,
                    kind=failure.kind,
                    retryable=failure.retryable,
                ),
            )
        except CanaryRuntimeStoreIntegrityError as exc:
            raise ActivityFailure(
                "ACTIVITY_IDEMPOTENCY_CONFLICT",
                kind=FailureKind.POLICY,
                retryable=False,
            ) from exc
        except Exception as exc:
            raise ActivityFailure(
                "ACTIVITY_STATE_PERSISTENCE_FAILED",
                kind=FailureKind.INFRASTRUCTURE,
                retryable=True,
            ) from exc

    def _invoke_uncached(
        self,
        workflow_input: CanaryWorkflowInput,
        invocation: ActivityInvocation,
        cancel_signal: CancellationSignal | None,
    ) -> ActivityOutcome:
        activity_name = invocation.activity_name
        if activity_name is ActivityName.VALIDATE:
            self._resolve_context(workflow_input)
            return ActivityOutcome(
                ActivityOutcomeStatus.SUCCEEDED,
                f"validated:{workflow_input.content_hash()}",
            )
        if activity_name is ActivityName.PREFLIGHT:
            endpoint, suite, secret = self._resolve_context(workflow_input)
            # Resolve and validate every immutable dependency without issuing a
            # model request. EXECUTE is the only potentially billable activity.
            del endpoint, suite, secret
            if cancel_signal is not None and cancel_signal.is_set():
                raise ActivityFailure(
                    "ACTIVITY_CANCELLED",
                    kind=FailureKind.INFRASTRUCTURE,
                    retryable=False,
                )
            return ActivityOutcome(
                ActivityOutcomeStatus.SUCCEEDED,
                f"preflight:{workflow_input.content_hash()}",
            )
        if activity_name is ActivityName.EXECUTE:
            endpoint, suite, secret = self._resolve_context(workflow_input)
            try:
                result = run_suite(
                    suite,
                    base_url=endpoint.base_url,
                    api_key=secret,
                    model=endpoint.model,
                    mock_mode=endpoint.mock_mode,
                    cancel_signal=cancel_signal,
                )
            except SuiteExecutionCancelled as exc:
                raise ActivityFailure(
                    "ACTIVITY_CANCELLED",
                    kind=FailureKind.INFRASTRUCTURE,
                    retryable=False,
                ) from exc
            result.run_id = workflow_input.run_id
            return self._store_scored_result(
                workflow_input.run_id,
                result,
                sensitive_values=(secret,),
            )

        result_ref = invocation.input_ref
        if not result_ref:
            raise ActivityFailure(
                "RESULT_REFERENCE_MISSING",
                kind=FailureKind.RUNNER,
                retryable=False,
            )
        result = self._read_scored_result(workflow_input.run_id, result_ref)
        if activity_name is ActivityName.NORMALIZE:
            return ActivityOutcome(ActivityOutcomeStatus.SUCCEEDED, result_ref)
        if activity_name is ActivityName.SCORE:
            dimensions = result.get("dimension_scores")
            if not isinstance(dimensions, list) or len(dimensions) != 4:
                raise ActivityFailure(
                    "DIMENSION_SCORES_MISSING",
                    kind=FailureKind.RUNNER,
                    retryable=False,
                )
            return ActivityOutcome(ActivityOutcomeStatus.SUCCEEDED, result_ref)
        if activity_name is ActivityName.PERSIST:
            try:
                persisted_ref = self._runtime_store.persist_result(
                    run_id=workflow_input.run_id,
                    result_ref=result_ref,
                    result=result,
                )
            except CanaryRuntimeStoreIntegrityError as exc:
                raise ActivityFailure(
                    "PROBE_RESULT_IMMUTABILITY_CONFLICT",
                    kind=FailureKind.POLICY,
                    retryable=False,
                ) from exc
            return ActivityOutcome(ActivityOutcomeStatus.SUCCEEDED, persisted_ref)
        if activity_name in {ActivityName.COMPARE, ActivityName.NOTIFY}:
            if self._runtime_store.load_result(workflow_input.run_id) is None:
                raise ActivityFailure(
                    "PERSISTED_RESULT_NOT_FOUND",
                    kind=FailureKind.INFRASTRUCTURE,
                    retryable=True,
                )
            return ActivityOutcome(ActivityOutcomeStatus.SUCCEEDED, result_ref)
        raise ActivityFailure(
            "UNSUPPORTED_CANARY_ACTIVITY",
            kind=FailureKind.CONFIGURATION,
            retryable=False,
        )

    def _resolve_context(
        self, workflow_input: CanaryWorkflowInput
    ) -> tuple[EndpointSnapshot, ProbeSuite, str]:
        try:
            endpoint = self._endpoint_resolver.resolve(
                workflow_input.endpoint_snapshot_id
            )
        except LookupError as exc:
            raise ActivityFailure(
                "ENDPOINT_SNAPSHOT_NOT_FOUND",
                kind=FailureKind.CONFIGURATION,
                retryable=False,
            ) from exc
        if endpoint.endpoint_snapshot_id != workflow_input.endpoint_snapshot_id:
            raise ActivityFailure(
                "ENDPOINT_SNAPSHOT_MISMATCH",
                kind=FailureKind.CONFIGURATION,
                retryable=False,
            )
        try:
            suite_bytes = self._suite_resolver.read(workflow_input.suite_uri)
        except LookupError as exc:
            raise ActivityFailure(
                "SUITE_SNAPSHOT_NOT_FOUND",
                kind=FailureKind.CONFIGURATION,
                retryable=False,
            ) from exc
        if hashlib.sha256(suite_bytes).hexdigest() != workflow_input.suite_sha256:
            raise ActivityFailure(
                "SUITE_DIGEST_MISMATCH",
                kind=FailureKind.CONFIGURATION,
                retryable=False,
            )
        try:
            document = json.loads(suite_bytes)
            suite = compile_suite(document)
        except (json.JSONDecodeError, UnicodeDecodeError, SuiteValidationError) as exc:
            raise ActivityFailure(
                "SUITE_VALIDATION_FAILED",
                kind=FailureKind.CONFIGURATION,
                retryable=False,
            ) from exc
        if (
            suite.name != workflow_input.suite_name
            or suite.version != workflow_input.suite_version
        ):
            raise ActivityFailure(
                "SUITE_IDENTITY_MISMATCH",
                kind=FailureKind.CONFIGURATION,
                retryable=False,
            )
        try:
            secret = self._secret_resolver.resolve(endpoint.credential_handle)
        except LookupError as exc:
            raise ActivityFailure(
                "CREDENTIAL_NOT_FOUND",
                kind=FailureKind.CONFIGURATION,
                retryable=False,
            ) from exc
        return endpoint, suite, secret

    def _store_scored_result(
        self,
        run_id: str,
        result: NormalizedRunResult,
        *,
        sensitive_values: tuple[str, ...] = (),
    ) -> ActivityOutcome:
        sanitized = sanitized_result_for_persistence(
            result,
            sensitive_values=sensitive_values,
        )
        manifest = self._evidence_store.put_json(
            run_id=run_id,
            evidence_kind=EvidenceKind.NORMALIZED_RESULT,
            value=sanitized,
            redaction_status=RedactionStatus.SANITIZED,
        )
        if self._evidence_manifest_repository is not None:
            self._evidence_manifest_repository.add(manifest)
        status = (
            ActivityOutcomeStatus.SUCCEEDED
            if result.status is RunStatus.PASS
            else ActivityOutcomeStatus.TARGET_FAILED
        )
        return ActivityOutcome(status, manifest.object_uri)

    def _read_scored_result(self, run_id: str, result_ref: str) -> dict:
        try:
            result = self._evidence_store.read_json_ref(result_ref)
        except (OSError, ValueError, RuntimeError) as exc:
            raise ActivityFailure(
                "RESULT_EVIDENCE_INVALID",
                kind=FailureKind.INFRASTRUCTURE,
                retryable=True,
            ) from exc
        if result.get("run_id") != run_id:
            raise ActivityFailure(
                "RESULT_RUN_ID_MISMATCH",
                kind=FailureKind.RUNNER,
                retryable=False,
            )
        if result.get("schema_version") != "probe.ai/result/v1alpha1":
            raise ActivityFailure(
                "RESULT_SCHEMA_MISMATCH",
                kind=FailureKind.RUNNER,
                retryable=False,
            )
        return result


class _CombinedCancellationSignal:
    def __init__(
        self,
        external: CancellationSignal | None,
        internal: threading.Event,
    ) -> None:
        self._external = external
        self._internal = internal

    def is_set(self) -> bool:
        return self._internal.is_set() or (
            self._external is not None and self._external.is_set()
        )


class _ActivityLeaseRenewer:
    def __init__(
        self,
        runtime_store: CanaryRuntimeStore,
        invocation: ActivityInvocation,
        lease_token: str,
        lease_seconds: float,
    ) -> None:
        self._runtime_store = runtime_store
        self._invocation = invocation
        self._lease_token = lease_token
        self._lease_seconds = lease_seconds
        self._interval = min(30.0, max(0.05, lease_seconds / 3))
        self._stop = threading.Event()
        self.failure_signal = threading.Event()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"lexsond-lease-{invocation.activity_name.value}",
            daemon=True,
        )
        self._started = False

    @property
    def failed(self) -> bool:
        return self.failure_signal.is_set()

    def start(self) -> None:
        self._thread.start()
        self._started = True

    def stop(self) -> None:
        self._stop.set()
        if self._started and threading.current_thread() is not self._thread:
            self._thread.join()

    def raise_if_failed(self) -> None:
        if self.failed:
            raise self.activity_failure() from self._failure

    def activity_failure(self) -> ActivityFailure:
        return ActivityFailure(
            "ACTIVITY_LEASE_LOST",
            kind=FailureKind.INFRASTRUCTURE,
            retryable=True,
        )

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._runtime_store.renew(
                    self._invocation,
                    lease_token=self._lease_token,
                    lease_seconds=self._lease_seconds,
                )
            except BaseException as exc:
                self._failure = exc
                self.failure_signal.set()
                return
