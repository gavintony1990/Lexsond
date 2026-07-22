from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..storage.redaction import contains_recognizable_credential
from .scorers import EvaluationScoringError, get_scorer


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_ITEMS = 10_000
MAX_MESSAGE_CHARS = 32 * 1024
MAX_MESSAGES = 16
MAX_METADATA_BYTES = 8 * 1024
MAX_TOTAL_EXPANDED_CHARS = 32 * 1024 * 1024
CSV_FIELDS = ("id", "input", "reference_answer", "category", "language", "scorer")
_ITEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CATEGORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_LANGUAGE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_HTML_OR_SCRIPT = re.compile(r"(?is)<\s*(?:script|html|iframe|object)\b|<!doctype\s+html")
_FORBIDDEN_KEYS = frozenset(
    {
        "apikey",
        "authorization",
        "accesstoken",
        "refreshtoken",
        "oauthtoken",
        "password",
        "secret",
        "credentialref",
        "credentialhandle",
        "clientsecret",
        "sessiontoken",
        "codeobject",
        "executable",
        "script",
    }
)


class DatasetValidationError(ValueError):
    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        line_number: int | None = None,
        field: str | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.line_number = line_number
        self.field = field
        location = ""
        if line_number is not None:
            location += f" line {line_number}"
        if field:
            location += f" field {field}"
        super().__init__(f"{reason_code}{location}: {message}")


@dataclass(frozen=True, slots=True)
class EvaluationItem:
    item_index: int
    item_id: str
    category: str
    language: str
    input: Mapping[str, Any]
    reference: Mapping[str, Any]
    metadata: Mapping[str, Any]

    def to_document(self) -> dict[str, Any]:
        return {
            "id": self.item_id,
            "category": self.category,
            "language": self.language,
            "input": dict(self.input),
            "reference": dict(self.reference),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CompiledDataset:
    schema_version: str
    content_sha256: str
    item_count: int
    category_count: int
    language_codes: tuple[str, ...]
    categories: Mapping[str, int]
    items: tuple[EvaluationItem, ...]


def compile_jsonl_dataset(payload: bytes) -> CompiledDataset:
    text = _decode_upload(payload)
    documents: list[Mapping[str, Any]] = []
    source_lines: list[int] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if len(documents) >= MAX_ITEMS:
            raise DatasetValidationError("TOO_MANY_ITEMS", "dataset exceeds 10,000 items", line_number=line_number)
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetValidationError("MALFORMED_JSON", "line is not valid JSON", line_number=line_number) from exc
        if not isinstance(value, Mapping):
            raise DatasetValidationError("ITEM_NOT_OBJECT", "item must be a JSON object", line_number=line_number)
        documents.append(value)
        source_lines.append(line_number)
    return compile_document_items(documents, source_lines=source_lines)


def compile_csv_dataset(
    payload: bytes,
    column_mapping: Mapping[str, str] | None = None,
) -> CompiledDataset:
    text = _decode_upload(payload)
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        headers = reader.fieldnames
        if (
            headers is None
            or len(headers) != len(set(headers))
            or any(
                not isinstance(value, str)
                or not value
                or len(value) > 128
                or any(ord(character) < 32 for character in value)
                for value in headers
            )
        ):
            raise DatasetValidationError(
                "CSV_COLUMNS_INVALID",
                "CSV header contains duplicate or invalid column names",
                line_number=1,
                field="header",
            )
        mapping = dict(column_mapping or zip(CSV_FIELDS, CSV_FIELDS, strict=True))
        if (
            set(mapping) != set(CSV_FIELDS)
            or len(set(mapping.values())) != len(CSV_FIELDS)
            or any(value not in headers for value in mapping.values())
        ):
            raise DatasetValidationError(
                "CSV_MAPPING_INVALID",
                "CSV mapping must select six distinct existing columns",
                line_number=1,
                field="mapping",
            )
        documents: list[Mapping[str, Any]] = []
        lines: list[int] = []
        for row_number, row in enumerate(reader, 2):
            if len(documents) >= MAX_ITEMS:
                raise DatasetValidationError("TOO_MANY_ITEMS", "dataset exceeds 10,000 items", line_number=row_number)
            documents.append(
                {
                    "id": row.get(mapping["id"]),
                    "category": row.get(mapping["category"]),
                    "language": row.get(mapping["language"]),
                    "input": {"messages": [{"role": "user", "content": row.get(mapping["input"])} ]},
                    "reference": {"scorer": row.get(mapping["scorer"]), "answer": row.get(mapping["reference_answer"])},
                    "metadata": {},
                }
            )
            lines.append(row_number)
    except csv.Error as exc:
        raise DatasetValidationError("MALFORMED_CSV", "CSV structure is invalid") from exc
    return compile_document_items(documents, source_lines=lines)


def compile_document_items(
    documents: Sequence[Mapping[str, Any]],
    *,
    source_lines: Sequence[int] | None = None,
) -> CompiledDataset:
    if not isinstance(documents, Sequence) or isinstance(documents, (str, bytes)):
        raise DatasetValidationError("DATASET_INVALID", "dataset items must be a sequence")
    if not 1 <= len(documents) <= MAX_ITEMS:
        raise DatasetValidationError("ITEM_COUNT_INVALID", "dataset must contain 1 to 10,000 items")
    if source_lines is not None and len(source_lines) != len(documents):
        raise ValueError("source_lines must align with documents")
    items: list[EvaluationItem] = []
    seen: set[str] = set()
    total_chars = 0
    canonical_lines: list[bytes] = []
    for index, document in enumerate(documents):
        line = source_lines[index] if source_lines is not None else index + 1
        try:
            item = _compile_item(document, index=index, line_number=line)
        except DatasetValidationError:
            raise
        if item.item_id in seen:
            raise DatasetValidationError("DUPLICATE_ID", "item id is duplicated", line_number=line, field="id")
        seen.add(item.item_id)
        canonical = json.dumps(
            item.to_document(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        canonical_lines.append(canonical)
        total_chars += len(canonical.decode("utf-8"))
        if total_chars > MAX_TOTAL_EXPANDED_CHARS:
            raise DatasetValidationError("EXPANDED_CONTENT_TOO_LARGE", "expanded dataset content exceeds the limit", line_number=line)
        items.append(item)
    digest = hashlib.sha256(b"\n".join(canonical_lines) + b"\n").hexdigest()
    categories = Counter(item.category for item in items)
    language_codes = tuple(sorted({item.language for item in items}))
    if len(language_codes) > 128:
        raise DatasetValidationError(
            "TOO_MANY_LANGUAGES", "dataset exceeds 128 language codes"
        )
    return CompiledDataset(
        schema_version="lexsond.evaluation-dataset/v1",
        content_sha256=digest,
        item_count=len(items),
        category_count=len(categories),
        language_codes=language_codes,
        categories=dict(sorted(categories.items())),
        items=tuple(items),
    )


def _decode_upload(payload: bytes) -> str:
    if not isinstance(payload, bytes):
        raise DatasetValidationError("UPLOAD_TYPE_INVALID", "upload must be bytes")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise DatasetValidationError("FILE_TOO_LARGE", "upload exceeds 10 MiB")
    if b"\x00" in payload:
        raise DatasetValidationError("BINARY_CONTENT", "binary upload is not accepted")
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DatasetValidationError("INVALID_UTF8", "upload must be UTF-8") from exc


def _compile_item(document: Mapping[str, Any], *, index: int, line_number: int) -> EvaluationItem:
    if not isinstance(document, Mapping):
        raise DatasetValidationError("ITEM_NOT_OBJECT", "item must be an object", line_number=line_number)
    _reject_untrusted_value(document, line_number=line_number, field="item", depth=0)
    expected = {"id", "category", "language", "input", "reference", "metadata"}
    if set(document) != expected:
        raise DatasetValidationError("ITEM_FIELDS_INVALID", "item fields do not match the versioned schema", line_number=line_number, field="item")
    item_id = _bounded_identifier(document.get("id"), _ITEM_ID, "id", line_number)
    category = _bounded_identifier(document.get("category"), _CATEGORY, "category", line_number)
    language = _bounded_identifier(document.get("language"), _LANGUAGE, "language", line_number)
    input_value = document.get("input")
    reference = document.get("reference")
    metadata = document.get("metadata")
    if not isinstance(input_value, Mapping):
        raise DatasetValidationError("INPUT_INVALID", "input must be an object", line_number=line_number, field="input")
    if not isinstance(reference, Mapping):
        raise DatasetValidationError("REFERENCE_INVALID", "reference must be an object", line_number=line_number, field="reference")
    if not isinstance(metadata, Mapping):
        raise DatasetValidationError("METADATA_INVALID", "metadata must be an object", line_number=line_number, field="metadata")
    if set(input_value) - {"messages", "choices"}:
        raise DatasetValidationError("INPUT_FIELDS_INVALID", "input contains unsupported fields", line_number=line_number, field="input")
    messages = input_value.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        raise DatasetValidationError("MESSAGES_INVALID", "input.messages must contain 1 to 16 messages", line_number=line_number, field="input.messages")
    normalized_messages: list[dict[str, str]] = []
    for message_index, message in enumerate(messages):
        if not isinstance(message, Mapping) or set(message) != {"role", "content"}:
            raise DatasetValidationError("MESSAGE_INVALID", "message fields are invalid", line_number=line_number, field=f"input.messages.{message_index}")
        role = message.get("role")
        content = message.get("content")
        if role not in {"system", "user", "assistant"}:
            raise DatasetValidationError("MESSAGE_ROLE_INVALID", "message role is unsupported", line_number=line_number, field=f"input.messages.{message_index}.role")
        if not isinstance(content, str) or not content or len(content) > MAX_MESSAGE_CHARS:
            raise DatasetValidationError("MESSAGE_CONTENT_INVALID", "message content is empty or too long", line_number=line_number, field=f"input.messages.{message_index}.content")
        normalized_messages.append({"role": role, "content": content})
    normalized_input: dict[str, Any] = {"messages": normalized_messages}
    choices = input_value.get("choices")
    if choices is not None:
        if not isinstance(choices, list) or not 2 <= len(choices) <= 26 or any(not isinstance(value, str) or not value or len(value) > 4096 for value in choices):
            raise DatasetValidationError("CHOICES_INVALID", "choices must contain 2 to 26 bounded strings", line_number=line_number, field="input.choices")
        user_messages = [
            message for message in normalized_messages if message["role"] == "user"
        ]
        rendered_choices = "\n".join(
            f"{chr(65 + index)}. {choice}"
            for index, choice in enumerate(choices)
        )
        if (
            not user_messages
            or len(user_messages[-1]["content"])
            + len("\n\nChoices:\n")
            + len(rendered_choices)
            > MAX_MESSAGE_CHARS
        ):
            raise DatasetValidationError(
                "CHOICES_RENDER_TOO_LARGE",
                "choices do not fit the bounded native user message",
                line_number=line_number,
                field="input.choices",
            )
        normalized_input["choices"] = list(choices)
    normalized_reference = dict(reference)
    scorer_id = normalized_reference.get("scorer")
    if not isinstance(scorer_id, str):
        raise DatasetValidationError("SCORER_INVALID", "reference.scorer is required", line_number=line_number, field="reference.scorer")
    try:
        if scorer_id == "multiple_choice_accuracy":
            if choices is None:
                raise DatasetValidationError(
                    "REFERENCE_INVALID",
                    "multiple_choice_accuracy requires input.choices",
                    line_number=line_number,
                    field="input.choices",
                )
            if "choice_count" in normalized_reference:
                raise DatasetValidationError(
                    "REFERENCE_INVALID",
                    "choice_count is derived from input.choices",
                    line_number=line_number,
                    field="reference.choice_count",
                )
            normalized_reference["choice_count"] = len(choices)
        get_scorer(scorer_id).validate_reference(normalized_reference)
    except DatasetValidationError:
        raise
    except EvaluationScoringError as exc:
        raise DatasetValidationError("REFERENCE_INVALID", str(exc), line_number=line_number, field="reference") from exc
    metadata_bytes = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if len(metadata_bytes) > MAX_METADATA_BYTES:
        raise DatasetValidationError("METADATA_TOO_LARGE", "metadata exceeds 8 KiB", line_number=line_number, field="metadata")
    return EvaluationItem(
        index,
        item_id,
        category,
        language,
        normalized_input,
        normalized_reference,
        dict(metadata),
    )


def _bounded_identifier(value: Any, pattern: re.Pattern[str], field: str, line: int) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise DatasetValidationError("IDENTIFIER_INVALID", "identifier has invalid characters or length", line_number=line, field=field)
    return value


def _reject_untrusted_value(value: Any, *, line_number: int, field: str, depth: int) -> None:
    if depth > 8:
        raise DatasetValidationError("NESTING_TOO_DEEP", "JSON nesting exceeds eight levels", line_number=line_number, field=field)
    if contains_recognizable_credential(value):
        raise DatasetValidationError("SECRET_DETECTED", "credential-shaped content is forbidden", line_number=line_number, field=field)
    if isinstance(value, Mapping):
        if len(value) > 128:
            raise DatasetValidationError("OBJECT_TOO_LARGE", "object has too many fields", line_number=line_number, field=field)
        for key, child in value.items():
            if not isinstance(key, str):
                raise DatasetValidationError("KEY_INVALID", "object keys must be strings", line_number=line_number, field=field)
            normalized_key = re.sub(r"[^a-z0-9]", "", key.casefold())
            if normalized_key in _FORBIDDEN_KEYS:
                raise DatasetValidationError("SECRET_FIELD", "secret or executable fields are forbidden", line_number=line_number, field=field)
            _reject_untrusted_value(child, line_number=line_number, field=f"{field}.{key}", depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 10_000:
            raise DatasetValidationError("ARRAY_TOO_LARGE", "array has too many values", line_number=line_number, field=field)
        for index, child in enumerate(value):
            _reject_untrusted_value(child, line_number=line_number, field=f"{field}.{index}", depth=depth + 1)
    elif isinstance(value, str):
        if any(ord(character) < 0x20 and character not in "\n\r\t" for character in value):
            raise DatasetValidationError("CONTROL_CHARACTER", "control characters are forbidden", line_number=line_number, field=field)
        if _HTML_OR_SCRIPT.search(value) or "\\write18" in value or "javascript:" in value.casefold():
            raise DatasetValidationError("ACTIVE_CONTENT", "HTML, script, or executable macro content is forbidden", line_number=line_number, field=field)
    elif isinstance(value, float) and not math.isfinite(value):
        raise DatasetValidationError("NONFINITE_NUMBER", "numbers must be finite", line_number=line_number, field=field)
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise DatasetValidationError("VALUE_TYPE_INVALID", "value is not JSON-compatible", line_number=line_number, field=field)
