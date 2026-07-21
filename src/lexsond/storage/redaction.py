from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

from ..models import NormalizedRunResult


_AUTHORIZATION_VALUE = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+"
)
_RECOGNIZABLE_SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{8,}|gsk_[A-Za-z0-9_-]{8,}|"
    r"AIza[A-Za-z0-9_-]{16,}|xai-[A-Za-z0-9_-]{8,}|"
    r"nvapi-[A-Za-z0-9_-]{8,}|csk-[A-Za-z0-9_-]{8,}|"
    r"pplx-[A-Za-z0-9_-]{8,})"
)


def redact_text(value: str, *, sensitive_values: Iterable[str] = ()) -> str:
    """Remove recognizable credentials before text reaches durable memory.

    Agent prompts and model replies are intentionally checkpointed.  This
    helper gives that memory boundary the same credential rules as probe
    evidence without retaining the original string in metadata or hashes.
    """

    if not isinstance(value, str):
        raise ValueError("value must be a string")
    secrets = tuple(dict.fromkeys(sensitive_values))
    if any(not isinstance(secret, str) or not secret for secret in secrets):
        raise ValueError("sensitive_values must contain only non-empty strings")
    return _redact_sensitive_strings(value, secrets)


def redact_value(value: Any, *, sensitive_values: Iterable[str] = ()) -> Any:
    """Recursively scrub a JSON-compatible Agent tool or event payload."""

    secrets = tuple(dict.fromkeys(sensitive_values))
    if any(not isinstance(secret, str) or not secret for secret in secrets):
        raise ValueError("sensitive_values must contain only non-empty strings")
    return _redact_sensitive_strings(value, secrets)


def sanitized_result_for_persistence(
    result: NormalizedRunResult,
    *,
    sensitive_values: Iterable[str] = (),
) -> dict[str, Any]:
    """Return a schema-compatible result with raw response/error text removed.

    Scoring must happen before this boundary. Hashes and lengths retain useful
    regression evidence without making durable metrics storage a response-body
    archive.
    """

    if not isinstance(result, NormalizedRunResult):
        raise ValueError("result must be a NormalizedRunResult")
    secrets = tuple(dict.fromkeys(sensitive_values))
    if any(not isinstance(secret, str) or not secret for secret in secrets):
        raise ValueError("sensitive_values must contain only non-empty strings")
    value = deepcopy(result.to_dict())
    for index, measurement in enumerate(value["measurements"]):
        endpoint = measurement.get("endpoint")
        if isinstance(endpoint, str):
            parsed = urlsplit(endpoint)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(
                    f"measurement {index} endpoint contains credentials, query, or fragment"
                )
        evidence = measurement.get("evidence")
        if not isinstance(evidence, dict):
            raise ValueError(f"measurement {index} evidence must be an object")

        output_text = measurement.get("output_text")
        if not isinstance(output_text, str):
            raise ValueError(f"measurement {index} output_text must be a string")
        if output_text:
            evidence["output_text_sha256"] = hashlib.sha256(
                output_text.encode("utf-8")
            ).hexdigest()
            evidence["output_text_chars"] = len(output_text)
        measurement["output_text"] = ""

        error_message = measurement.get("error_message")
        if error_message is not None:
            if not isinstance(error_message, str):
                raise ValueError(
                    f"measurement {index} error_message must be a string or null"
                )
            evidence["error_message_sha256"] = hashlib.sha256(
                error_message.encode("utf-8")
            ).hexdigest()
            evidence["error_message_chars"] = len(error_message)
        measurement["error_message"] = None

        # A hostile endpoint can reflect the bearer credential into otherwise
        # structured response metadata. Scrub every provider-controlled string
        # before the normalized result crosses a durable boundary.
        for field in (
            "response_model",
            "finish_reason",
            "provider_reported_input_tokens",
            "provider_reported_output_tokens",
            "provider_reported_total_tokens",
            "chunks",
            "evidence",
        ):
            measurement[field] = _redact_sensitive_strings(
                measurement.get(field),
                secrets,
            )

    for case_result in value.get("case_results", []):
        if isinstance(case_result, dict):
            case_result["evidence"] = _redact_sensitive_strings(
                case_result.get("evidence"),
                secrets,
            )
    for dimension in value.get("dimension_scores", []):
        if isinstance(dimension, dict):
            dimension["metrics"] = _redact_sensitive_strings(
                dimension.get("metrics"),
                secrets,
            )
    return value


def _redact_sensitive_strings(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        scrubbed = value
        for secret in secrets:
            scrubbed = scrubbed.replace(secret, "[REDACTED]")
        scrubbed = _AUTHORIZATION_VALUE.sub(r"\1[REDACTED]", scrubbed)
        return _RECOGNIZABLE_SECRET.sub("[REDACTED]", scrubbed)
    if isinstance(value, dict):
        return {
            (
                _redact_sensitive_strings(key, secrets)
                if isinstance(key, str)
                else key
            ): _redact_sensitive_strings(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_strings(item, secrets) for item in value]
    return value
