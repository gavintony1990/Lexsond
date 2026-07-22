from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class EvaluationScoringError(ValueError):
    """A deterministic scorer contract or reference is invalid."""


class ScoreStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ScoreResult:
    score: float | None
    status: ScoreStatus
    reason_code: str
    facts: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.score is not None and (
            isinstance(self.score, bool)
            or not isinstance(self.score, (int, float))
            or not math.isfinite(float(self.score))
            or not 0.0 <= float(self.score) <= 1.0
        ):
            raise ValueError("score must be null or a finite value from zero to one")
        if self.status is ScoreStatus.UNKNOWN and self.score is not None:
            raise ValueError("UNKNOWN scores must be null")


@dataclass(frozen=True, slots=True)
class ScorerDescriptor:
    scorer_id: str
    version: str
    label: str
    description: str


class EvaluationScorer(Protocol):
    scorer_id: str
    version: str

    def validate_reference(self, reference: Mapping[str, Any]) -> None: ...

    def score(self, output: str, reference: Mapping[str, Any]) -> ScoreResult: ...


def _pass_fail(matches: bool, *, reason: str, facts: Mapping[str, Any]) -> ScoreResult:
    return ScoreResult(
        score=1.0 if matches else 0.0,
        status=ScoreStatus.PASS if matches else ScoreStatus.FAIL,
        reason_code=f"{reason}_{'MATCH' if matches else 'MISMATCH'}",
        facts=dict(facts),
    )


def _unknown(reason: str, **facts: Any) -> ScoreResult:
    return ScoreResult(None, ScoreStatus.UNKNOWN, reason, facts)


def _answer(reference: Mapping[str, Any]) -> str:
    answer = reference.get("answer")
    if not isinstance(answer, str) or not answer or len(answer) > 32_768:
        raise EvaluationScoringError("reference.answer must be a bounded non-empty string")
    return answer


class ExactMatchScorer:
    scorer_id = "exact_match"
    version = "1.0.0"

    def validate_reference(self, reference: Mapping[str, Any]) -> None:
        _answer(reference)

    def score(self, output: str, reference: Mapping[str, Any]) -> ScoreResult:
        try:
            answer = _answer(reference)
        except EvaluationScoringError:
            return _unknown("REFERENCE_MISSING")
        if not isinstance(output, str):
            return _unknown("OUTPUT_MISSING")
        return _pass_fail(
            output == answer,
            reason="EXACT",
            facts={"output_chars": len(output), "reference_chars": len(answer)},
        )


_WHITESPACE = re.compile(r"\s+")


def normalize_exact_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = "".join(
        " " if unicodedata.category(character).startswith("P") else character
        for character in normalized
    )
    return _WHITESPACE.sub(" ", normalized).strip()


class NormalizedExactMatchScorer:
    scorer_id = "normalized_exact_match"
    version = "1.0.0"

    def validate_reference(self, reference: Mapping[str, Any]) -> None:
        _answer(reference)

    def score(self, output: str, reference: Mapping[str, Any]) -> ScoreResult:
        try:
            answer = _answer(reference)
        except EvaluationScoringError:
            return _unknown("REFERENCE_MISSING")
        if not isinstance(output, str):
            return _unknown("OUTPUT_MISSING")
        return _pass_fail(
            normalize_exact_text(output) == normalize_exact_text(answer),
            reason="NORMALIZED_EXACT",
            facts={
                "normalization": "nfkc-casefold-ws-punct/v1",
                "output_chars": len(output),
                "reference_chars": len(answer),
            },
        )


class MultipleChoiceAccuracyScorer:
    scorer_id = "multiple_choice_accuracy"
    version = "1.0.0"

    def validate_reference(self, reference: Mapping[str, Any]) -> None:
        answer_index = reference.get("answer_index")
        choice_count = reference.get("choice_count")
        if (
            isinstance(answer_index, bool)
            or not isinstance(answer_index, int)
            or isinstance(choice_count, bool)
            or not isinstance(choice_count, int)
            or not 2 <= choice_count <= 26
            or not 0 <= answer_index < choice_count
        ):
            raise EvaluationScoringError(
                "multiple choice reference requires a valid answer_index and choice_count"
            )

    def score(self, output: str, reference: Mapping[str, Any]) -> ScoreResult:
        try:
            self.validate_reference(reference)
        except EvaluationScoringError:
            return _unknown("REFERENCE_INVALID")
        if not isinstance(output, str) or not output.strip():
            return _unknown("OUTPUT_MISSING")
        choice_count = int(reference["choice_count"])
        answer_index = int(reference["answer_index"])
        normalized = unicodedata.normalize("NFKC", output).strip().upper()
        direct = re.fullmatch(r"(?:OPTION\s*)?([A-Z])(?:[.、:)\s].*)?", normalized)
        if direct is None:
            direct = re.fullmatch(
                r"(?:THE\s+)?(?:ANSWER|OPTION)\s*(?:IS|:)?\s*([A-Z])[.、)]?",
                normalized,
            )
        if direct is None:
            return _unknown("CHOICE_NOT_PARSED", choice_count=choice_count)
        selected = ord(direct.group(1)) - ord("A")
        if not 0 <= selected < choice_count:
            return _unknown("CHOICE_OUT_OF_RANGE", choice_count=choice_count)
        return _pass_fail(
            selected == answer_index,
            reason="MULTIPLE_CHOICE",
            facts={"selected_index": selected, "choice_count": choice_count},
        )


def _tokens(value: str) -> list[str]:
    normalized = normalize_exact_text(value)
    return re.findall(r"[\w]+", normalized, flags=re.UNICODE)


class TokenF1Scorer:
    scorer_id = "token_f1"
    version = "1.0.0"

    def validate_reference(self, reference: Mapping[str, Any]) -> None:
        _answer(reference)

    def score(self, output: str, reference: Mapping[str, Any]) -> ScoreResult:
        try:
            answer = _answer(reference)
        except EvaluationScoringError:
            return _unknown("REFERENCE_MISSING")
        if not isinstance(output, str):
            return _unknown("OUTPUT_MISSING")
        predicted = _tokens(output)
        expected = _tokens(answer)
        if not predicted or not expected:
            return _unknown("TOKENS_MISSING")
        overlap = sum((Counter(predicted) & Counter(expected)).values())
        precision = overlap / len(predicted)
        recall = overlap / len(expected)
        score = 0.0 if not overlap else 2 * precision * recall / (precision + recall)
        return ScoreResult(
            score,
            ScoreStatus.PASS if math.isclose(score, 1.0) else ScoreStatus.FAIL,
            "TOKEN_F1_COMPLETE" if math.isclose(score, 1.0) else "TOKEN_F1_PARTIAL",
            {
                "predicted_tokens": len(predicted),
                "reference_tokens": len(expected),
                "overlap_tokens": overlap,
            },
        )


class ContainsAllScorer:
    scorer_id = "contains_all"
    version = "1.0.0"

    def validate_reference(self, reference: Mapping[str, Any]) -> None:
        values = reference.get("values")
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= 32
            or any(not isinstance(value, str) or not value or len(value) > 1024 for value in values)
        ):
            raise EvaluationScoringError("reference.values must contain bounded strings")

    def score(self, output: str, reference: Mapping[str, Any]) -> ScoreResult:
        try:
            self.validate_reference(reference)
        except EvaluationScoringError:
            return _unknown("REFERENCE_INVALID")
        if not isinstance(output, str):
            return _unknown("OUTPUT_MISSING")
        normalized = normalize_exact_text(output)
        values = [normalize_exact_text(value) for value in reference["values"]]
        matched = sum(value in normalized for value in values)
        return _pass_fail(
            matched == len(values),
            reason="CONTAINS_ALL",
            facts={"required_count": len(values), "matched_count": matched},
        )


_UNSAFE_REGEX = re.compile(r"[(){}|]|\\[1-9]|\(\?|\(\*")


def _regex_quantifier_count(pattern: str) -> int:
    count = 0
    escaped = False
    in_class = False
    for character in pattern:
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
        elif character == "[":
            in_class = True
        elif character == "]":
            in_class = False
        elif not in_class and character in "*+?":
            count += 1
    return count


class RegexMatchScorer:
    scorer_id = "regex_match"
    version = "1.0.0"

    def validate_reference(self, reference: Mapping[str, Any]) -> None:
        pattern = reference.get("pattern")
        if not isinstance(pattern, str) or not pattern or len(pattern) > 256:
            raise EvaluationScoringError("reference.pattern must be a bounded string")
        if _UNSAFE_REGEX.search(pattern):
            raise EvaluationScoringError("regex uses constructs outside the linear safe subset")
        if _regex_quantifier_count(pattern) > 1:
            raise EvaluationScoringError("regex safe subset permits at most one quantifier")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise EvaluationScoringError("regex pattern is invalid") from exc

    def score(self, output: str, reference: Mapping[str, Any]) -> ScoreResult:
        try:
            self.validate_reference(reference)
        except EvaluationScoringError:
            return _unknown("REFERENCE_INVALID")
        if not isinstance(output, str) or len(output) > 32_768:
            return _unknown("OUTPUT_MISSING_OR_TOO_LARGE")
        matched = re.search(str(reference["pattern"]), output) is not None
        return _pass_fail(
            matched,
            reason="REGEX",
            facts={"pattern_version": "linear-safe-subset/v1", "output_chars": len(output)},
        )


_SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "enum",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
    }
)
_JSON_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})


class JsonSchemaValidScorer:
    scorer_id = "json_schema_valid"
    version = "1.0.0"

    def validate_reference(self, reference: Mapping[str, Any]) -> None:
        schema = reference.get("schema")
        if not isinstance(schema, Mapping):
            raise EvaluationScoringError("reference.schema must be an object")
        counter = [0]
        _validate_schema_document(schema, depth=0, counter=counter)

    def score(self, output: str, reference: Mapping[str, Any]) -> ScoreResult:
        try:
            self.validate_reference(reference)
        except EvaluationScoringError:
            return _unknown("REFERENCE_INVALID")
        if not isinstance(output, str) or not output or len(output) > 32_768:
            return _unknown("OUTPUT_MISSING_OR_TOO_LARGE")
        try:
            value = json.loads(
                output,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError("non-finite JSON number")
                ),
            )
        except (json.JSONDecodeError, UnicodeError, ValueError):
            return _unknown("OUTPUT_JSON_PARSE_FAILED", output_chars=len(output))
        try:
            valid = _matches_schema(value, reference["schema"], depth=0, counter=[0])
        except _SchemaResourceLimit:
            return _unknown("OUTPUT_SCHEMA_LIMIT_EXCEEDED", output_chars=len(output))
        return _pass_fail(
            valid,
            reason="JSON_SCHEMA",
            facts={"schema_subset": "lexsond-json-schema/v1", "output_type": type(value).__name__},
        )


def _validate_schema_document(schema: Mapping[str, Any], *, depth: int, counter: list[int]) -> None:
    counter[0] += 1
    if depth > 8 or counter[0] > 256:
        raise EvaluationScoringError("JSON schema exceeds depth or node limits")
    unknown = set(schema) - _SCHEMA_KEYWORDS
    if unknown:
        raise EvaluationScoringError("JSON schema contains unsupported keywords")
    expected_type = schema.get("type")
    if not isinstance(expected_type, str) or expected_type not in _JSON_TYPES:
        raise EvaluationScoringError("JSON schema type is required and unsupported")
    properties = schema.get("properties")
    if properties is not None:
        if expected_type != "object" or not isinstance(properties, Mapping) or len(properties) > 64:
            raise EvaluationScoringError("JSON schema properties are invalid")
        for name, child in properties.items():
            if not isinstance(name, str) or len(name) > 128 or not isinstance(child, Mapping):
                raise EvaluationScoringError("JSON schema property is invalid")
            _validate_schema_document(child, depth=depth + 1, counter=counter)
    required = schema.get("required")
    if required is not None and (
        expected_type != "object"
        or not isinstance(required, list)
        or len(required) > 64
        or any(not isinstance(value, str) or len(value) > 128 for value in required)
    ):
        raise EvaluationScoringError("JSON schema required is invalid")
    items = schema.get("items")
    if items is not None:
        if expected_type != "array" or not isinstance(items, Mapping):
            raise EvaluationScoringError("JSON schema items is invalid")
        _validate_schema_document(items, depth=depth + 1, counter=counter)
    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or not 1 <= len(enum) <= 64):
        raise EvaluationScoringError("JSON schema enum is invalid")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        value = schema.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 32_768):
            raise EvaluationScoringError(f"JSON schema {key} is invalid")
    for key in ("minimum", "maximum"):
        value = schema.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise EvaluationScoringError(f"JSON schema {key} is invalid")
    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        raise EvaluationScoringError("additionalProperties must be boolean")


class _SchemaResourceLimit(RuntimeError):
    pass


def _matches_schema(value: Any, schema: Mapping[str, Any], *, depth: int, counter: list[int]) -> bool:
    counter[0] += 1
    if depth > 8 or counter[0] > 256:
        raise _SchemaResourceLimit
    expected = schema["type"]
    type_matches = {
        "null": value is None,
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
        "string": isinstance(value, str),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }[expected]
    if not type_matches or ("enum" in schema and value not in schema["enum"]):
        return False
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 32_768):
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            return False
    if isinstance(value, list):
        if len(value) > 256:
            raise _SchemaResourceLimit
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            return False
        child = schema.get("items")
        if child is not None and any(not _matches_schema(item, child, depth=depth + 1, counter=counter) for item in value):
            return False
    if isinstance(value, dict):
        required = schema.get("required", [])
        if any(key not in value for key in required):
            return False
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return False
        for key, child in properties.items():
            if key in value and not _matches_schema(value[key], child, depth=depth + 1, counter=counter):
                return False
    return True


_SCORERS: dict[str, EvaluationScorer] = {
    scorer.scorer_id: scorer
    for scorer in (
        ContainsAllScorer(),
        ExactMatchScorer(),
        JsonSchemaValidScorer(),
        MultipleChoiceAccuracyScorer(),
        NormalizedExactMatchScorer(),
        RegexMatchScorer(),
        TokenF1Scorer(),
    )
}
_LABELS = {
    "contains_all": ("包含全部", "输出包含全部受控短语"),
    "exact_match": ("精确匹配", "按原始字符完全匹配"),
    "json_schema_valid": ("JSON Schema", "验证受限 JSON Schema 子集"),
    "multiple_choice_accuracy": ("多选准确率", "解析 A–Z 单项答案"),
    "normalized_exact_match": ("规范化精确匹配", "NFKC、大小写、空白与标点规范化"),
    "regex_match": ("安全正则", "仅支持线性安全正则子集"),
    "token_f1": ("Token F1", "确定性词元重叠 F1"),
}


def get_scorer(scorer_id: str) -> EvaluationScorer:
    try:
        return _SCORERS[scorer_id]
    except KeyError as exc:
        raise EvaluationScoringError("scorer is not registered") from exc


def list_scorers() -> tuple[ScorerDescriptor, ...]:
    return tuple(
        ScorerDescriptor(scorer_id, scorer.version, *_LABELS[scorer_id])
        for scorer_id, scorer in sorted(_SCORERS.items())
    )
