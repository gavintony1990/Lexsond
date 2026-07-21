"""Safe, local provider discovery for OpenAI-compatible API credentials.

Detection is deliberately limited to recognizable key prefixes.  It never
contacts a provider, and generic ``sk-`` credentials remain ambiguous instead
of being sprayed across third-party endpoints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    provider_id: str
    name: str
    english_name: str
    base_url: str
    default_model: str
    docs_url: str
    protocol: str = "openai-chat"
    target_kind: str = "cloud"
    requires_api_key: bool = True

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "name": self.name,
            "english_name": self.english_name,
            "base_url": self.base_url,
            "default_model": self.default_model,
            "docs_url": self.docs_url,
            "protocol": self.protocol,
            "target_kind": self.target_kind,
            "requires_api_key": self.requires_api_key,
        }


@dataclass(frozen=True, slots=True)
class ProviderDetection:
    status: str
    confidence: str
    provider: ProviderProfile | None
    candidates: tuple[ProviderProfile, ...]
    reason_code: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "confidence": self.confidence,
            "provider": (
                self.provider.to_public_dict() if self.provider is not None else None
            ),
            "candidates": [candidate.to_public_dict() for candidate in self.candidates],
            "reason_code": self.reason_code,
        }


PROVIDERS: tuple[ProviderProfile, ...] = (
    ProviderProfile(
        "ollama",
        "Ollama 本地服务",
        "Ollama",
        "http://127.0.0.1:11434/v1",
        "",
        "https://docs.ollama.com/api/openai-compatibility",
        target_kind="local",
        requires_api_key=False,
    ),
    ProviderProfile(
        "vllm",
        "vLLM 推理服务",
        "vLLM",
        "http://127.0.0.1:8000/v1",
        "",
        "https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html",
        target_kind="local",
        requires_api_key=False,
    ),
    ProviderProfile(
        "lm-studio",
        "LM Studio",
        "LM Studio",
        "http://127.0.0.1:1234/v1",
        "",
        "https://lmstudio.ai/docs/developer/openai-compat",
        target_kind="local",
        requires_api_key=False,
    ),
    ProviderProfile(
        "localai",
        "LocalAI",
        "LocalAI",
        "http://127.0.0.1:8080/v1",
        "",
        "https://localai.io/features/openai-functions/",
        target_kind="local",
        requires_api_key=False,
    ),
    ProviderProfile(
        "xinference",
        "Xinference",
        "Xinference",
        "http://127.0.0.1:9997/v1",
        "",
        "https://inference.readthedocs.io/en/latest/user_guide/client_api.html",
        target_kind="local",
        requires_api_key=False,
    ),
    ProviderProfile(
        "openai",
        "OpenAI",
        "OpenAI",
        "https://api.openai.com/v1",
        "gpt-4.1-mini",
        "https://platform.openai.com/docs/api-reference/chat",
    ),
    ProviderProfile(
        "deepseek",
        "深度求索",
        "DeepSeek",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        "https://api-docs.deepseek.com",
    ),
    ProviderProfile(
        "siliconflow",
        "硅基流动",
        "SiliconFlow",
        "https://api.siliconflow.cn/v1",
        "Qwen/Qwen2.5-72B-Instruct",
        "https://docs.siliconflow.cn",
    ),
    ProviderProfile(
        "dashscope",
        "阿里云百炼",
        "Alibaba Cloud Model Studio",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen-plus",
        "https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope",
    ),
    ProviderProfile(
        "moonshot",
        "月之暗面",
        "Moonshot AI",
        "https://api.moonshot.cn/v1",
        "kimi-k2.5",
        "https://platform.moonshot.cn/docs",
    ),
    ProviderProfile(
        "openrouter",
        "开放路由",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        "openai/gpt-4.1-mini",
        "https://openrouter.ai/docs/api-reference/overview",
    ),
    ProviderProfile(
        "groq",
        "Groq",
        "Groq",
        "https://api.groq.com/openai/v1",
        "llama-3.3-70b-versatile",
        "https://console.groq.com/docs/openai",
    ),
    ProviderProfile(
        "gemini",
        "谷歌 Gemini",
        "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "gemini-3.5-flash",
        "https://ai.google.dev/gemini-api/docs/openai",
    ),
    ProviderProfile(
        "mistral",
        "Mistral AI",
        "Mistral AI",
        "https://api.mistral.ai/v1",
        "mistral-small-latest",
        "https://docs.mistral.ai/api",
    ),
    ProviderProfile(
        "together",
        "Together AI",
        "Together AI",
        "https://api.together.ai/v1",
        "openai/gpt-oss-20b",
        "https://docs.together.ai/docs/inference/openai-compatibility",
    ),
    ProviderProfile(
        "xai",
        "xAI",
        "xAI",
        "https://api.x.ai/v1",
        "grok-4.5",
        "https://docs.x.ai/docs/api-reference",
    ),
    ProviderProfile(
        "nvidia",
        "英伟达 NIM",
        "NVIDIA NIM",
        "https://integrate.api.nvidia.com/v1",
        "openai/gpt-oss-20b",
        "https://docs.api.nvidia.com/nim/reference/openai-apis",
    ),
    ProviderProfile(
        "cerebras",
        "Cerebras",
        "Cerebras",
        "https://api.cerebras.ai/v1",
        "gpt-oss-120b",
        "https://inference-docs.cerebras.ai/api-reference/chat-completions",
    ),
    ProviderProfile(
        "perplexity",
        "Perplexity",
        "Perplexity",
        "https://api.perplexity.ai",
        "sonar-pro",
        "https://docs.perplexity.ai/api-reference/chat-completions-post",
    ),
)

_PROVIDER_BY_ID = {provider.provider_id: provider for provider in PROVIDERS}
_UNIQUE_KEY_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"sk-or-v1-[A-Za-z0-9_-]{8,}\Z"), "openrouter", "UNIQUE_PREFIX"),
    (re.compile(r"gsk_[A-Za-z0-9_-]{8,}\Z"), "groq", "UNIQUE_PREFIX"),
    (re.compile(r"AIza[A-Za-z0-9_-]{16,}\Z"), "gemini", "RECOGNIZABLE_PREFIX"),
    (re.compile(r"xai-[A-Za-z0-9_-]{8,}\Z"), "xai", "UNIQUE_PREFIX"),
    (re.compile(r"nvapi-[A-Za-z0-9_-]{8,}\Z"), "nvidia", "UNIQUE_PREFIX"),
    (re.compile(r"csk-[A-Za-z0-9_-]{8,}\Z"), "cerebras", "UNIQUE_PREFIX"),
    (re.compile(r"pplx-[A-Za-z0-9_-]{8,}\Z"), "perplexity", "UNIQUE_PREFIX"),
    (
        re.compile(r"sk-(?:proj|svcacct)-[A-Za-z0-9_-]{8,}\Z"),
        "openai",
        "OPENAI_SCOPED_PREFIX",
    ),
)
_GENERIC_SK = re.compile(r"sk-[A-Za-z0-9_-]{8,}\Z")
_GENERIC_SK_CANDIDATES = tuple(
    _PROVIDER_BY_ID[provider_id]
    for provider_id in ("openai", "deepseek", "siliconflow", "dashscope", "moonshot")
)


def public_providers(target_kind: str | None = None) -> list[dict[str, Any]]:
    """Return profiles safe to expose to the browser."""

    if target_kind is not None and target_kind not in {"local", "cloud"}:
        raise ValueError("target_kind must be local or cloud")
    return [
        provider.to_public_dict()
        for provider in PROVIDERS
        if target_kind is None or provider.target_kind == target_kind
    ]


def get_provider(provider_id: str) -> ProviderProfile | None:
    """Resolve a registered provider without exposing detection rules."""

    return _PROVIDER_BY_ID.get(provider_id)


def detect_provider_key(api_key: str) -> ProviderDetection:
    """Infer a provider from local key syntax without making network requests."""

    if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 8192:
        raise ValueError("api_key must be a non-empty string of at most 8192 characters")
    value = api_key.strip()

    for pattern, provider_id, reason_code in _UNIQUE_KEY_RULES:
        if pattern.fullmatch(value):
            confidence = "MEDIUM" if provider_id == "gemini" else "HIGH"
            return ProviderDetection(
                status="MATCHED",
                confidence=confidence,
                provider=_PROVIDER_BY_ID[provider_id],
                candidates=(),
                reason_code=reason_code,
            )

    if _GENERIC_SK.fullmatch(value):
        return ProviderDetection(
            status="AMBIGUOUS",
            confidence="NONE",
            provider=None,
            candidates=_GENERIC_SK_CANDIDATES,
            reason_code="SHARED_SK_PREFIX",
        )

    return ProviderDetection(
        status="UNKNOWN",
        confidence="NONE",
        provider=None,
        candidates=(),
        reason_code="UNRECOGNIZED_FORMAT",
    )


def resolve_provider_key(
    api_key: str,
    provider_id: str | None = None,
) -> ProviderDetection:
    """Resolve a key with an optional explicit cloud-provider confirmation."""

    detection = detect_provider_key(api_key)
    if provider_id is None:
        return detection
    provider = get_provider(provider_id)
    if provider is None or provider.target_kind != "cloud":
        raise ValueError("provider_id must identify a registered cloud provider")
    if detection.status == "MATCHED":
        if detection.provider != provider:
            raise ValueError("api_key does not match the selected provider")
        return detection
    if detection.status == "AMBIGUOUS":
        if provider not in detection.candidates:
            raise ValueError("selected provider is not compatible with api_key format")
        return ProviderDetection(
            status="CONFIRMED",
            confidence="USER",
            provider=provider,
            candidates=(),
            reason_code="SHARED_PREFIX_CONFIRMED",
        )
    return ProviderDetection(
        status="MANUAL",
        confidence="NONE",
        provider=provider,
        candidates=(),
        reason_code="MANUAL_PROVIDER_UNVERIFIED",
    )
