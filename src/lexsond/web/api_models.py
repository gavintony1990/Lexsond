from __future__ import annotations

from typing import Any, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from ..probe import ProbeType, validate_base_url_transport
from ..providers import get_provider
from ..suite import compile_suite


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


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
    api_key: SecretStr | None = Field(default=None, max_length=8192)

    @model_validator(mode="after")
    def validate_run_shape(self) -> Self:
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
