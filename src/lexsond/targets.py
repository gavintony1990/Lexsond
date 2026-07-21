"""Real target connectivity checks for OpenAI-compatible deployments."""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .probe import (
    UnsafeTargetAddress,
    _create_guarded_http_connection,
    _wrap_connection_with_deadline,
    validate_api_key_value,
    validate_base_url_transport,
)
from .providers import detect_provider_key


MAX_MODEL_CATALOG_BYTES = 16 * 1024 * 1024
MAX_MODELS = 2_000
MAX_CATALOG_LIST_ITEMS = 128


@dataclass(frozen=True, slots=True)
class ModelCatalogEntry:
    """Bounded public model metadata reported by the provider catalog itself."""

    model_id: str
    name: str
    owned_by: str | None
    created: int | None
    context_length: int | None
    input_modalities: tuple[str, ...]
    output_modalities: tuple[str, ...]
    endpoint_types: tuple[str, ...]
    probe_types: tuple[str, ...]
    supported_parameters: tuple[str, ...]
    supported_voices: tuple[str, ...]
    capability_source: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.model_id,
            "name": self.name,
            "owned_by": self.owned_by,
            "created": self.created,
            "context_length": self.context_length,
            "input_modalities": list(self.input_modalities),
            "output_modalities": list(self.output_modalities),
            "endpoint_types": list(self.endpoint_types),
            "probe_types": list(self.probe_types),
            "supported_parameters": list(self.supported_parameters),
            "supported_voices": list(self.supported_voices),
            "capability_source": self.capability_source,
        }


class TargetConnectionError(RuntimeError):
    """A safe, user-facing target connectivity failure."""


def fetch_model_catalog(
    base_url: str,
    *,
    api_key: str | None,
    provider_id: str | None = None,
    timeout_seconds: float = 5.0,
) -> list[str]:
    """Backward-compatible model-id view of the provider's full catalog."""

    return [
        entry.model_id
        for entry in fetch_model_catalog_entries(
            base_url,
            api_key=api_key,
            provider_id=provider_id,
            timeout_seconds=timeout_seconds,
        )
    ]


def fetch_model_catalog_entries(
    base_url: str,
    *,
    api_key: str | None,
    provider_id: str | None = None,
    timeout_seconds: float = 5.0,
) -> list[ModelCatalogEntry]:
    """Fetch bounded model metadata without retaining credentials or descriptions."""

    validate_base_url_transport(base_url)
    validate_api_key_value(api_key)
    if provider_id is not None and (
        not isinstance(provider_id, str) or not provider_id
    ):
        raise ValueError("provider_id must be a non-empty string or null")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0.1 <= float(timeout_seconds) <= 30
    ):
        raise ValueError("timeout_seconds must be between 0.1 and 30")

    parsed = urlsplit(base_url.rstrip("/"))
    connection = _create_guarded_http_connection(parsed, float(timeout_seconds))
    if provider_id == "ollama":
        path = "/api/tags"
    else:
        path = f"{parsed.path.rstrip('/')}/models"
        # OpenRouter defaults this endpoint to text-output models.  The fixed
        # query is required to retrieve image, audio, embedding, and video rows.
        if provider_id == "openrouter":
            path += "?output_modalities=all"
    headers = {
        "Accept": "application/json",
        "User-Agent": "lexsond/0.8",
    }
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key}"

    deadline_ns = time.perf_counter_ns() + int(float(timeout_seconds) * 1_000_000_000)
    try:
        connection.request("GET", path, headers=headers)
        _wrap_connection_with_deadline(connection, deadline_ns)
        response = connection.getresponse()
        body = _read_catalog_response(
            response,
            connection,
            MAX_MODEL_CATALOG_BYTES,
            deadline_ns,
        )
    except ssl.SSLError as exc:
        raise TargetConnectionError("target model catalog TLS handshake failed") from exc
    except TimeoutError as exc:
        raise TargetConnectionError("target model catalog request timed out") from exc
    except UnsafeTargetAddress as exc:
        raise TargetConnectionError("target model catalog resolved to a blocked network") from exc
    except (ConnectionError, socket.gaierror, OSError) as exc:
        raise TargetConnectionError("target model catalog is unreachable") from exc
    finally:
        connection.close()

    if len(body) > MAX_MODEL_CATALOG_BYTES:
        raise TargetConnectionError("target model catalog exceeds the response limit")
    if response.status != 200:
        raise TargetConnectionError(
            f"target model catalog returned HTTP {response.status}"
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetConnectionError("target model catalog is not valid JSON") from exc
    if provider_id == "ollama":
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            raise TargetConnectionError("Ollama model catalog has an invalid format")
        catalog = payload["models"]
        identity_fields = ("model", "name")
    else:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise TargetConnectionError("target model catalog does not use OpenAI format")
        catalog = payload["data"]
        identity_fields = ("id",)

    if len(catalog) > MAX_MODELS:
        raise TargetConnectionError("target model catalog exceeds the model count limit")

    entries: list[ModelCatalogEntry] = []
    seen: set[str] = set()
    for item in catalog:
        if not isinstance(item, dict):
            continue
        model_id = next(
            (
                item.get(field)
                for field in identity_fields
                if isinstance(item.get(field), str) and item.get(field)
            ),
            None,
        )
        if (
            isinstance(model_id, str)
            and 0 < len(model_id) <= 256
            and not _is_sensitive_model_id(model_id, api_key)
            and model_id not in seen
        ):
            seen.add(model_id)
            entries.append(_catalog_entry(model_id, item, api_key, provider_id))
    return entries


def _catalog_entry(
    model_id: str,
    item: dict[str, Any],
    api_key: str | None,
    provider_id: str | None,
) -> ModelCatalogEntry:
    architecture = item.get("architecture")
    if not isinstance(architecture, dict):
        architecture = {}
    input_modalities = _catalog_list(
        architecture.get("input_modalities", item.get("input_modalities")),
        api_key,
    )
    output_modalities = _catalog_list(
        architecture.get("output_modalities", item.get("output_modalities")),
        api_key,
    )
    modality = architecture.get("modality", item.get("modality"))
    if (
        not input_modalities
        and not output_modalities
        and isinstance(modality, str)
        and "->" in modality
        and not _is_sensitive_model_id(modality, api_key)
    ):
        left, right = modality.split("->", 1)
        input_modalities = _catalog_list(left.split("+"), api_key)
        output_modalities = _catalog_list(right.split("+"), api_key)

    name = _catalog_text(item.get("name"), api_key, maximum=256) or model_id
    owned_by = _catalog_text(item.get("owned_by"), api_key, maximum=256)
    created = _catalog_nonnegative_int(item.get("created"))
    context_length = _catalog_nonnegative_int(item.get("context_length"))
    supported_parameters = _catalog_list(item.get("supported_parameters"), api_key)
    supported_voices = _catalog_list(
        item.get("supported_voices"),
        api_key,
        normalize_lower=False,
    )
    endpoint_types, probe_types = _classify_modalities(
        input_modalities,
        output_modalities,
        provider_id=provider_id,
        supported_voices=supported_voices,
    )
    capability_source = (
        "PROVIDER_METADATA"
        if input_modalities or output_modalities
        else "UNSPECIFIED"
    )
    return ModelCatalogEntry(
        model_id=model_id,
        name=name,
        owned_by=owned_by,
        created=created,
        context_length=context_length,
        input_modalities=input_modalities,
        output_modalities=output_modalities,
        endpoint_types=endpoint_types,
        probe_types=probe_types,
        supported_parameters=supported_parameters,
        supported_voices=supported_voices,
        capability_source=capability_source,
    )


def _classify_modalities(
    input_modalities: tuple[str, ...],
    output_modalities: tuple[str, ...],
    *,
    provider_id: str | None,
    supported_voices: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    inputs = set(input_modalities)
    outputs = set(output_modalities)
    endpoints: list[str] = []
    probes: list[str] = []

    if "text" in inputs and "text" in outputs:
        endpoints.append("chat_completions")
        probes.append("chat")
    if "image" in inputs and "text" in outputs:
        if "chat_completions" not in endpoints:
            endpoints.append("chat_completions")
        if "text" in inputs:
            probes.append("vision")
    if "embeddings" in outputs or "embedding" in outputs:
        endpoints.append("embeddings")
        if "text" in inputs:
            probes.append("embedding")
    if "image" in outputs:
        endpoints.append("image_generation")
        if "text" in inputs:
            probes.append("image_generation")
    if provider_id == "openrouter" and ("audio" in inputs or "audio" in outputs):
        if "chat_completions" not in endpoints:
            endpoints.append("chat_completions")
    if "speech" in outputs:
        endpoints.append("audio_speech")
        if "text" in inputs and supported_voices:
            probes.append("audio_speech")
    if "transcription" in outputs:
        endpoints.append("audio_transcription")
        if "audio" in inputs:
            probes.append("audio_transcription")
    if "video" in outputs:
        endpoints.append("video_generation")
    return tuple(endpoints), tuple(probes)


def _catalog_list(
    value: Any,
    api_key: str | None,
    *,
    normalize_lower: bool = True,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    values: list[str] = []
    seen: set[str] = set()
    for item in value[:MAX_CATALOG_LIST_ITEMS]:
        text = _catalog_text(item, api_key, maximum=128)
        if text is None:
            continue
        normalized = text.strip().lower() if normalize_lower else text.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            values.append(normalized)
    return tuple(values)


def _catalog_text(
    value: Any,
    api_key: str | None,
    *,
    maximum: int,
) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > maximum or _is_sensitive_model_id(text, api_key):
        return None
    return text


def _catalog_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _is_sensitive_model_id(model_id: str, api_key: str | None) -> bool:
    if api_key is not None and api_key in model_id:
        return True
    try:
        return detect_provider_key(model_id).status != "UNKNOWN"
    except ValueError:
        return False


def _read_catalog_response(
    response: http.client.HTTPResponse,
    connection: http.client.HTTPConnection,
    limit: int,
    deadline_ns: int,
) -> bytes:
    payload = bytearray()
    while True:
        if response.isclosed():
            break
        remaining = (deadline_ns - time.perf_counter_ns()) / 1_000_000_000
        if remaining <= 0:
            raise TimeoutError("model catalog absolute deadline exceeded")
        if connection.sock is not None:
            connection.sock.settimeout(max(remaining, 0.001))
        block = response.read1(min(65_536, limit + 1 - len(payload)))
        if not block:
            break
        payload.extend(block)
        if len(payload) > limit:
            break
    return bytes(payload)
