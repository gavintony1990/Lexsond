from __future__ import annotations

from typing import Any, Generic, Literal, Self, TypeVar
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from ..probe import ProbeType, validate_base_url_transport
from ..providers import get_provider
from ..suite import compile_suite
from ..evaluations.scorers import get_scorer
from ..storage.redaction import contains_recognizable_credential


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AuthInputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthRegisterRequest(AuthInputModel):
    email: str = Field(min_length=3, max_length=320)
    password: SecretStr = Field(min_length=12, max_length=1024)
    display_name: str = Field(min_length=1, max_length=120)


class AuthLoginRequest(AuthInputModel):
    email: str = Field(min_length=3, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=1024)
    return_to: str | None = Field(default=None, max_length=2048)


class AuthTokenRequest(AuthInputModel):
    token: SecretStr = Field(min_length=32, max_length=8192)


class AuthForgotPasswordRequest(AuthInputModel):
    email: str = Field(min_length=3, max_length=320)


class AuthResetPasswordRequest(AuthInputModel):
    token: SecretStr = Field(min_length=32, max_length=8192)
    new_password: SecretStr = Field(min_length=12, max_length=1024)


class AuthChangePasswordRequest(AuthInputModel):
    current_password: SecretStr = Field(min_length=1, max_length=1024)
    new_password: SecretStr = Field(min_length=12, max_length=1024)


class CredentialProfileCreate(StrictModel):
    label: str = Field(min_length=1, max_length=120)
    provider_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]{0,63}$",
    )
    api_key: SecretStr = Field(min_length=1, max_length=8192)


class CredentialProfilePatch(StrictModel):
    version: int = Field(ge=1)
    label: str = Field(min_length=1, max_length=120)


class CredentialProfileReplace(StrictModel):
    version: int = Field(ge=1)
    api_key: SecretStr = Field(min_length=1, max_length=8192)


class PartnerApplicationCreate(StrictModel):
    site_name: str = Field(min_length=1, max_length=120)
    website_url: str = Field(min_length=1, max_length=2048)
    terms_url: str = Field(min_length=1, max_length=2048)
    privacy_url: str = Field(min_length=1, max_length=2048)
    contact_email: str = Field(min_length=3, max_length=320)
    api_base_url: str = Field(min_length=1, max_length=2048)
    protocol: Literal["openai-compatible", "anthropic-messages", "gemini-native"]
    region: str = Field(min_length=2, max_length=64)
    model_claims: list[str] = Field(min_length=1, max_length=100)
    pricing_notes: str = Field(min_length=1, max_length=4000)
    source_evidence_url: str = Field(min_length=1, max_length=2048)
    monitoring_credential_id: UUID | None = None

    @field_validator("website_url", "terms_url", "privacy_url", "source_evidence_url")
    @classmethod
    def validate_public_https_url(cls, value: str) -> str:
        return _validated_partner_url(value, allow_query=False)

    @field_validator("api_base_url")
    @classmethod
    def validate_api_base_url(cls, value: str) -> str:
        return _validated_partner_url(value, allow_query=False)

    @field_validator("contact_email")
    @classmethod
    def validate_contact_email(cls, value: str) -> str:
        if value.count("@") != 1 or any(character.isspace() for character in value):
            raise ValueError("contact_email is invalid")
        return value

    @field_validator("model_claims")
    @classmethod
    def validate_model_claims(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item or len(item) > 256 for item in normalized):
            raise ValueError("model_claims must contain bounded model identifiers")
        if len(set(normalized)) != len(normalized):
            raise ValueError("model_claims cannot contain duplicates")
        return normalized


class PartnerApplicationPatch(PartnerApplicationCreate):
    version: int = Field(ge=1)


class TargetCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    target_kind: Literal["local", "cloud"]
    provider_id: str | None = Field(default=None, min_length=1, max_length=64)
    base_url: str = Field(min_length=1, max_length=2048)
    default_model: str = Field(default="", max_length=256)
    credential_ref: str | None = Field(default=None, max_length=2048)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be an absolute credential-free HTTP(S) URL")
        validate_base_url_transport(value)
        return value.rstrip("/")

    @field_validator("credential_ref")
    @classmethod
    def validate_credential_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
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
            raise ValueError("credential_ref must use a supported secret-manager URI")
        return value

    @model_validator(mode="after")
    def validate_provider_binding(self) -> Self:
        if self.target_kind == "local" and self.credential_ref is not None:
            raise ValueError("local target cannot persist a credential_ref")
        if self.provider_id is None:
            return self
        provider = get_provider(self.provider_id)
        if provider is None:
            raise ValueError("provider_id is not registered")
        if provider.target_kind != self.target_kind:
            raise ValueError("provider_id does not match target_kind")
        if provider.base_url.rstrip("/") != self.base_url.rstrip("/"):
            raise ValueError("base_url does not match the selected provider")
        return self


class TargetPatch(StrictModel):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    target_kind: Literal["local", "cloud"] | None = None
    provider_id: str | None = Field(default=None, max_length=64)
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    default_model: str | None = Field(default=None, max_length=256)
    credential_ref: str | None = Field(default=None, max_length=2048)


class CatalogRequest(StrictModel):
    api_key: SecretStr | None = Field(default=None, max_length=8192)
    credential_profile_id: UUID | None = None

    @model_validator(mode="after")
    def validate_credential_source(self) -> Self:
        if self.api_key is not None and self.credential_profile_id is not None:
            raise ValueError("api_key and credential_profile_id are mutually exclusive")
        return self


class ProviderDetectRequest(StrictModel):
    api_key: SecretStr = Field(max_length=8192)
    provider_id: str | None = Field(default=None, min_length=1, max_length=64)


class AgentSessionCreate(StrictModel):
    title: str = Field(min_length=1, max_length=120)
    target_id: UUID
    model: str | None = Field(default=None, min_length=1, max_length=256)
    skill_id: str = Field(
        default="connection-diagnosis",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )


class AgentSessionPatch(StrictModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=120)
    skill_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )


class AgentMessageCreate(StrictModel):
    content: str = Field(min_length=1, max_length=4000)
    api_key: SecretStr | None = Field(default=None, max_length=8192)
    timeout_seconds: float = Field(default=45.0, ge=0.1, le=120)


class SuiteCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    document: dict[str, Any]

    @field_validator("document")
    @classmethod
    def validate_document(cls, value: dict[str, Any]) -> dict[str, Any]:
        compile_suite(value)
        return value


class SuitePatch(StrictModel):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    document: dict[str, Any] | None = None

    @field_validator("document")
    @classmethod
    def validate_document(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None:
            compile_suite(value)
        return value


class RunCreate(StrictModel):
    target_id: UUID
    run_kind: Literal["component", "suite"] = "component"
    probe_type: ProbeType | None = ProbeType.CHAT
    suite_revision_id: UUID | None = None
    execution_backend: Literal["local", "temporal"] = "local"
    model: str | None = Field(default=None, max_length=256)
    stream: bool = True
    timeout_seconds: float = Field(default=30.0, ge=0.1, le=300)
    max_output_tokens: int = Field(default=64, ge=1, le=4096)
    api_key: SecretStr | None = Field(default=None, max_length=8192)
    credential_profile_id: UUID | None = None

    @model_validator(mode="after")
    def validate_run_shape(self) -> Self:
        if self.api_key is not None and self.credential_profile_id is not None:
            raise ValueError("api_key and credential_profile_id are mutually exclusive")
        if self.run_kind == "suite":
            if self.suite_revision_id is None:
                raise ValueError("suite_revision_id is required for suite runs")
            if self.probe_type not in {None, ProbeType.CHAT}:
                raise ValueError("suite runs use the chat probe")
        elif self.suite_revision_id is not None:
            raise ValueError("suite_revision_id is only valid for suite runs")
        if self.run_kind == "component" and self.probe_type is None:
            raise ValueError("probe_type is required for component runs")
        if self.stream and self.probe_type not in {ProbeType.CHAT, ProbeType.VISION}:
            raise ValueError("stream is supported only for chat and vision probes")
        if (
            self.execution_backend == "temporal"
            and self.run_kind == "component"
            and self.probe_type != ProbeType.CHAT
        ):
            raise ValueError("Temporal currently supports chat and chat suites only")
        return self


class ProbeBatchCreate(StrictModel):
    target_id: UUID
    catalog_snapshot_id: UUID
    mode: Literal["catalog_only", "smoke", "quality_suite"] = "smoke"
    model_ids: list[str] = Field(min_length=1, max_length=10)
    suite_revision_id: UUID | None = None
    max_concurrency: int = Field(default=2, ge=1, le=2)
    max_output_tokens: int = Field(default=8, ge=1, le=64)
    timeout_seconds: float = Field(default=30.0, ge=0.1, le=120)
    api_key: SecretStr | None = Field(default=None, max_length=8192)
    credential_profile_id: UUID | None = None
    confirm_unknown_cost: bool = False

    @field_validator("model_ids")
    @classmethod
    def validate_model_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 256 for value in normalized):
            raise ValueError("model IDs must contain 1 to 256 characters")
        if len(set(normalized)) != len(normalized):
            raise ValueError("model IDs must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_batch_shape(self) -> Self:
        if self.api_key is not None and self.credential_profile_id is not None:
            raise ValueError("api_key and credential_profile_id are mutually exclusive")
        if self.mode == "quality_suite" and self.suite_revision_id is None:
            raise ValueError("quality_suite requires suite_revision_id")
        if self.mode != "quality_suite" and self.suite_revision_id is not None:
            raise ValueError("suite_revision_id is only valid for quality_suite")
        return self


class EvaluationDatasetMetadata(StrictModel):
    slug: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9-]{0,119}$",
    )
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    license_spdx: str = Field(min_length=1, max_length=64)
    license_url: str = Field(min_length=1, max_length=2048)
    source_url: str | None = Field(default=None, max_length=2048)
    distribution_policy: Literal[
        "BUNDLED",
        "IMPORT_REQUIRED",
        "LICENSE_REVIEW",
        "RESEARCH_ONLY",
        "RUNNER_REQUIRED",
        "BLOCKED",
    ] = "BUNDLED"
    default_scorer_id: Literal[
        "exact_match",
        "normalized_exact_match",
        "multiple_choice_accuracy",
        "token_f1",
        "contains_all",
        "regex_match",
        "json_schema_valid",
    ] = "normalized_exact_match"
    format: Literal["jsonl", "csv"]
    csv_mapping: dict[
        Literal["id", "input", "reference_answer", "category", "language", "scorer"],
        str,
    ] | None = None
    rights_confirmed: bool

    @field_validator(
        "name", "description", "license_spdx", "license_url", "source_url"
    )
    @classmethod
    def reject_dataset_metadata_secrets(cls, value: str | None) -> str | None:
        if value is not None and contains_recognizable_credential(value):
            raise ValueError("dataset metadata contains credential-shaped content")
        return value

    @field_validator("license_url", "source_url")
    @classmethod
    def validate_dataset_urls(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("dataset URL must be an absolute credential-free HTTPS URL")
        return value.rstrip("/")

    @field_validator("default_scorer_id")
    @classmethod
    def validate_default_scorer(cls, value: str) -> str:
        get_scorer(value)
        return value

    @model_validator(mode="after")
    def validate_rights(self) -> Self:
        if not self.rights_confirmed:
            raise ValueError("dataset upload rights must be confirmed")
        if self.distribution_policy in {"RESEARCH_ONLY", "RUNNER_REQUIRED", "BLOCKED"}:
            raise ValueError("this distribution policy cannot be uploaded as a runnable workspace dataset")
        if self.format == "jsonl" and self.csv_mapping is not None:
            raise ValueError("csv_mapping is only valid for CSV uploads")
        if self.format == "csv" and self.csv_mapping is not None:
            expected = {"id", "input", "reference_answer", "category", "language", "scorer"}
            if (
                set(self.csv_mapping) != expected
                or len(set(self.csv_mapping.values())) != len(expected)
                or any(not value or len(value) > 128 for value in self.csv_mapping.values())
            ):
                raise ValueError("csv_mapping must map six distinct bounded columns")
        return self


class EvaluationDatasetPatch(StrictModel):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    license_spdx: str | None = Field(default=None, min_length=1, max_length=64)
    license_url: str | None = Field(default=None, min_length=1, max_length=2048)
    source_url: str | None = Field(default=None, max_length=2048)
    default_scorer_id: Literal[
        "exact_match",
        "normalized_exact_match",
        "multiple_choice_accuracy",
        "token_f1",
        "contains_all",
        "regex_match",
        "json_schema_valid",
    ] | None = None

    @field_validator(
        "name", "description", "license_spdx", "license_url", "source_url"
    )
    @classmethod
    def reject_dataset_patch_secrets(cls, value: str | None) -> str | None:
        if value is not None and contains_recognizable_credential(value):
            raise ValueError("dataset metadata contains credential-shaped content")
        return value

    @field_validator("license_url", "source_url")
    @classmethod
    def validate_patch_urls(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("dataset URL must be an absolute credential-free HTTPS URL")
        return value.rstrip("/")


class EvaluationRunPreview(StrictModel):
    dataset_revision_id: UUID
    channel_id: UUID
    catalog_snapshot_id: UUID
    credential_profile_id: UUID | None = None
    model_ids: list[str] = Field(min_length=1, max_length=10)
    sample_strategy: Literal["first", "random", "stratified"] = "random"
    sample_seed: int = Field(default=42, ge=-(2**63), le=(2**63) - 1)
    sample_count: int = Field(default=20, ge=1, le=200)
    scorer_id: Literal[
        "dataset_reference",
        "exact_match",
        "normalized_exact_match",
        "multiple_choice_accuracy",
        "token_f1",
        "contains_all",
        "regex_match",
        "json_schema_valid",
    ] = "dataset_reference"
    max_output_tokens: int = Field(default=64, ge=1, le=1024)
    timeout_seconds: float = Field(default=30, ge=1, le=120)
    concurrency: int = Field(default=2, ge=1, le=2)
    max_cost_usd: float = Field(default=1.0, gt=0, le=10_000)
    confirm_unknown_chat_capability: bool = False

    @field_validator("model_ids")
    @classmethod
    def validate_evaluation_models(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 256 for value in normalized):
            raise ValueError("model IDs must contain 1 to 256 characters")
        if len(set(normalized)) != len(normalized):
            raise ValueError("model IDs must be unique")
        return normalized


class EvaluationRunCreate(EvaluationRunPreview):
    api_key: SecretStr | None = Field(default=None, max_length=8192)
    confirm_unknown_cost: bool = False

    @model_validator(mode="after")
    def validate_evaluation_credential(self) -> Self:
        if self.api_key is not None and self.credential_profile_id is not None:
            raise ValueError("api_key and credential_profile_id are mutually exclusive")
        if not self.confirm_unknown_cost:
            raise ValueError("unknown model prices require explicit confirmation")
        return self


ResponseT = TypeVar("ResponseT")


class ApiDataEnvelope(StrictModel, Generic[ResponseT]):
    data: ResponseT


class ApiListMeta(StrictModel):
    total: int = Field(ge=0)
    limit: int | None = Field(default=None, ge=1)


class ApiListEnvelope(StrictModel, Generic[ResponseT]):
    data: list[ResponseT]
    meta: ApiListMeta


class ApiErrorBody(StrictModel):
    code: str
    message: str
    details: list[dict[str, Any]]
    request_id: str | None = None


class ApiErrorEnvelope(StrictModel):
    error: ApiErrorBody


class EvaluationDatasetItemView(StrictModel):
    item_index: int | None = Field(default=None, ge=0)
    item_id: str | None = None
    id: str | None = None
    category: str
    language: str
    input: dict[str, Any]
    reference: dict[str, Any]
    metadata: dict[str, Any]


class EvaluationDatasetRevisionSummaryView(StrictModel):
    id: str
    revision: int = Field(ge=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    item_count: int = Field(ge=0, le=10_000)
    category_count: int = Field(ge=0)
    language_codes: list[str]
    manifest: dict[str, Any]
    created_at: str


class EvaluationDatasetRevisionView(EvaluationDatasetRevisionSummaryView):
    dataset_id: str
    schema_version: str
    items: list[EvaluationDatasetItemView] | None = None


class EvaluationDatasetView(StrictModel):
    id: str
    workspace_id: str | None
    scope: Literal["SYSTEM", "WORKSPACE"]
    slug: str
    name: str
    description: str
    license_spdx: str
    license_url: str
    source_url: str | None
    source_version: str | None
    source_verified_at: str | None
    distribution_policy: Literal[
        "BUNDLED",
        "IMPORT_REQUIRED",
        "LICENSE_REVIEW",
        "RESEARCH_ONLY",
        "RUNNER_REQUIRED",
        "BLOCKED",
    ]
    default_scorer_id: str
    version: int = Field(ge=1)
    created_at: str
    updated_at: str
    archived_at: str | None
    latest_revision: EvaluationDatasetRevisionSummaryView | None


class EvaluationUploadPreviewView(StrictModel):
    schema_version: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    item_count: int = Field(ge=0, le=10_000)
    category_count: int = Field(ge=0)
    categories: dict[str, int]
    language_codes: list[str]
    preview: list[EvaluationDatasetItemView]
    preview_truncated: bool


class EvaluationScorerView(StrictModel):
    scorer_id: str
    version: str
    label: str
    description: str
    execution: Literal["DETERMINISTIC_LOCAL"]


class EvaluationRunPreviewView(StrictModel):
    dataset_revision_id: str
    model_count: int = Field(ge=1, le=10)
    sample_count: int = Field(ge=1, le=200)
    maximum_calls: int = Field(ge=1, le=2_000)
    maximum_output_tokens: int = Field(ge=1)
    concurrency: int = Field(ge=1, le=2)
    estimated_cost_usd: float | None
    cost_status: Literal["KNOWN", "UNKNOWN"]
    cost_budget_enforcement: Literal[
        "ENFORCED_KNOWN_PRICING", "UNAVAILABLE_UNKNOWN_PRICING"
    ]
    requires_unknown_cost_confirmation: bool
    model_source_id: str
    unknown_chat_capability_models: list[str]
    comparable: bool


class EvaluationRunModelView(StrictModel):
    model_id: str
    provider_model_id: str
    state: str
    completed_items: int = Field(ge=0)
    passed_items: int = Field(ge=0)
    failed_items: int = Field(ge=0)
    unknown_items: int = Field(ge=0)
    metrics: dict[str, Any]


class EvaluationRunView(StrictModel):
    id: str
    workspace_id: str
    dataset_id: str
    dataset_revision_id: str
    channel_id: str
    credential_profile_id: str | None
    model_source_id: str
    state: Literal["RUNNING", "COMPLETED", "PARTIAL", "FAILED", "CANCELLED"]
    scorer_id: str
    scorer_version: str
    sample_strategy: Literal["first", "random", "stratified"]
    sample_seed: int
    sample_count: int = Field(ge=1, le=200)
    model_count: int = Field(ge=1, le=10)
    concurrency: int = Field(ge=1, le=2)
    max_output_tokens: int = Field(ge=1, le=1024)
    timeout_seconds: float = Field(ge=1, le=120)
    max_cost_usd: float = Field(gt=0)
    request_snapshot: dict[str, Any]
    aggregate_result: dict[str, Any] | None
    failure_code: str | None
    cancel_requested_at: str | None
    created_at: str
    finished_at: str | None
    archived_at: str | None
    models: list[EvaluationRunModelView]


class EvaluationRunItemView(StrictModel):
    model_id: str
    item_id: str
    category: str
    sequence: int = Field(ge=1, le=2_000)
    state: str
    score: float | None
    status: Literal["PASS", "FAIL", "UNKNOWN"]
    reason_code: str
    latency: dict[str, Any]
    usage: dict[str, Any]
    output_sha256: str | None
    safe_facts: dict[str, Any]
    created_at: str


class MonitorPolicyCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    target_id: UUID
    run_kind: Literal["component", "suite"] = "component"
    probe_type: ProbeType | None = ProbeType.CHAT
    suite_revision_id: UUID | None = None
    execution_backend: Literal["local", "temporal"] = "local"
    model: str | None = Field(default=None, max_length=256)
    stream: bool = True
    timeout_seconds: float = Field(default=30.0, ge=0.1, le=300)
    interval_seconds: int = Field(default=300, ge=60, le=2_592_000)
    failure_threshold: int = Field(default=2, ge=1, le=10)
    recovery_threshold: int = Field(default=1, ge=1, le=10)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_policy_shape(self) -> Self:
        if self.run_kind == "suite":
            if self.suite_revision_id is None:
                raise ValueError("suite_revision_id is required for suite monitor policies")
            if self.probe_type not in {None, ProbeType.CHAT}:
                raise ValueError("suite monitor policies use the chat probe")
        elif self.suite_revision_id is not None:
            raise ValueError("suite_revision_id is only valid for suite monitor policies")
        if self.run_kind == "component" and self.probe_type is None:
            raise ValueError("probe_type is required for component monitor policies")
        if self.stream and self.probe_type not in {ProbeType.CHAT, ProbeType.VISION}:
            raise ValueError("stream is supported only for chat and vision probes")
        if (
            self.execution_backend == "temporal"
            and self.run_kind == "component"
            and self.probe_type != ProbeType.CHAT
        ):
            raise ValueError("Temporal currently supports chat monitor policies only")
        return self


class MonitorPolicyPatch(StrictModel):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    target_id: UUID | None = None
    run_kind: Literal["component", "suite"] | None = None
    probe_type: ProbeType | None = None
    suite_revision_id: UUID | None = None
    execution_backend: Literal["local", "temporal"] | None = None
    model: str | None = Field(default=None, max_length=256)
    stream: bool | None = None
    timeout_seconds: float | None = Field(default=None, ge=0.1, le=300)
    interval_seconds: int | None = Field(default=None, ge=60, le=2_592_000)
    failure_threshold: int | None = Field(default=None, ge=1, le=10)
    recovery_threshold: int | None = Field(default=None, ge=1, le=10)
    enabled: bool | None = None


def _validated_partner_url(value: str, *, allow_query: bool) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or (parsed.query and not allow_query)
    ):
        raise ValueError("partner URL must be an absolute credential-free HTTPS URL")
    return value.rstrip("/")
