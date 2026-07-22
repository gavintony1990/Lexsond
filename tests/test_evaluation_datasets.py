from __future__ import annotations

import json
import unittest

from lexsond.evaluations.compiler import (
    DatasetValidationError,
    compile_document_items,
    compile_csv_dataset,
    compile_jsonl_dataset,
)


def _item(item_id: str = "reasoning-001") -> dict[str, object]:
    return {
        "id": item_id,
        "category": "reasoning",
        "language": "zh-CN",
        "input": {"messages": [{"role": "user", "content": "2 + 3 = ?"}]},
        "reference": {"scorer": "normalized_exact_match", "answer": "5"},
        "metadata": {"difficulty": "basic", "tags": ["math"]},
    }


def _jsonl(*items: dict[str, object]) -> bytes:
    return ("\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n").encode()


class EvaluationDatasetCompilerTests(unittest.TestCase):
    def test_multiple_choice_reference_in_public_schema_derives_choice_count(self) -> None:
        document = _item("choice-example")
        document["input"]["choices"] = ["A", "B", "C", "D"]
        document["reference"] = {
            "scorer": "multiple_choice_accuracy",
            "answer_index": 2,
        }
        compiled = compile_document_items([document])
        self.assertEqual(compiled.items[0].reference["choice_count"], 4)

    def test_multiple_choice_requires_choices_and_forbids_client_choice_count(self) -> None:
        missing = _item("missing-choices")
        missing["reference"] = {
            "scorer": "multiple_choice_accuracy", "answer_index": 0
        }
        mismatch = _item("mismatch")
        mismatch["input"]["choices"] = ["A", "B"]
        mismatch["reference"] = {
            "scorer": "multiple_choice_accuracy",
            "answer_index": 0,
            "choice_count": 3,
        }
        for document in (missing, mismatch):
            with self.subTest(item=document["id"]), self.assertRaises(
                DatasetValidationError
            ):
                compile_document_items([document])

    def test_jsonl_compiles_to_canonical_items_and_stable_hash(self) -> None:
        payload = _jsonl(_item("one"), _item("two"))
        first = compile_jsonl_dataset(payload)
        second = compile_jsonl_dataset(payload)
        self.assertEqual(first.content_sha256, second.content_sha256)
        self.assertEqual(first.item_count, 2)
        self.assertEqual(first.category_count, 1)
        self.assertEqual(first.language_codes, ("zh-CN",))
        self.assertEqual(first.items[0].item_id, "one")
        self.assertNotEqual(
            first.content_sha256,
            compile_jsonl_dataset(_jsonl(_item("two"), _item("one"))).content_sha256,
        )

    def test_csv_mapping_is_utf8_only_and_produces_standard_items(self) -> None:
        payload = (
            "id,input,reference_answer,category,language,scorer\n"
            "math-1,What is 1 plus 1?,2,arithmetic,en,exact_match\n"
        ).encode()
        compiled = compile_csv_dataset(payload)
        self.assertEqual(compiled.item_count, 1)
        self.assertEqual(compiled.items[0].reference["answer"], "2")
        with self.assertRaises(DatasetValidationError):
            compile_csv_dataset(payload.decode().encode("utf-16"))
        custom = (
            "row_id,prompt,gold,task,locale,metric,ignored\n"
            "math-2,What is 2 plus 2?,4,arithmetic,en,exact_match,discarded\n"
        ).encode()
        mapping = {
            "id": "row_id", "input": "prompt", "reference_answer": "gold",
            "category": "task", "language": "locale", "scorer": "metric",
        }
        compiled = compile_csv_dataset(custom, mapping)
        self.assertEqual(compiled.items[0].item_id, "math-2")
        with self.assertRaises(DatasetValidationError):
            compile_csv_dataset(custom, {**mapping, "input": "row_id"})

    def test_limits_duplicate_ids_unknown_scorers_and_malformed_utf8(self) -> None:
        cases = (
            _jsonl(_item("duplicate"), _item("duplicate")),
            _jsonl({**_item(), "reference": {"scorer": "llm_judge", "answer": "5"}}),
            b"\xff\xfe\x00\x01",
            b"\n".join(_jsonl(_item(str(index))).strip() for index in range(10_001)),
        )
        for payload in cases:
            with self.subTest(size=len(payload)), self.assertRaises(DatasetValidationError):
                compile_jsonl_dataset(payload)

    def test_rejects_secret_fields_values_controls_html_and_oversized_shapes(self) -> None:
        variants = [
            {**_item(), "metadata": {"api_key": "redacted"}},
            {**_item(), "metadata": {"note": "Authorization: Bearer hidden-value"}},
            {**_item(), "input": {"messages": [{"role": "user", "content": "sk-livecredential123456"}]}},
            {**_item(), "input": {"messages": [{"role": "user", "content": "<script>alert(1)</script>"}]}},
            {**_item(), "input": {"messages": [{"role": "user", "content": "bad\u0000value"}]}},
            {**_item(), "input": {"messages": [{"role": "user", "content": "x" * 32_769}]}},
            {**_item(), "input": {"messages": [{"role": "user", "content": "x"}] * 17}},
            {**_item(), "metadata": {"note": "x" * 8_193}},
            {**_item(), "metadata": {"weight": float("nan")}},
        ]
        for value in variants:
            with self.subTest(value=list(value)), self.assertRaises(DatasetValidationError):
                compile_jsonl_dataset(_jsonl(value))

    def test_errors_report_line_and_field_without_echoing_sensitive_row(self) -> None:
        payload = _jsonl(_item("ok")) + b'{"id":"secret","input":{"api_key":"sk-livecredential123456"}}\n'
        with self.assertRaises(DatasetValidationError) as raised:
            compile_jsonl_dataset(payload)
        self.assertEqual(raised.exception.line_number, 2)
        self.assertTrue(raised.exception.field)
        self.assertNotIn("sk-livecredential", str(raised.exception))

    def test_choices_must_fit_the_native_user_message_bound(self) -> None:
        document = _item("oversized-choices")
        document["input"] = {
            "messages": [{"role": "user", "content": "x" * 32_000}],
            "choices": ["a" * 500, "b" * 500],
        }
        document["reference"] = {
            "scorer": "multiple_choice_accuracy",
            "answer_index": 0,
        }
        with self.assertRaises(DatasetValidationError) as raised:
            compile_document_items([document])
        self.assertEqual(raised.exception.reason_code, "CHOICES_RENDER_TOO_LARGE")

    def test_upload_size_is_rejected_before_content_parsing(self) -> None:
        with self.assertRaises(DatasetValidationError) as raised:
            compile_jsonl_dataset(b"x" * (10 * 1024 * 1024 + 1))
        self.assertEqual(raised.exception.reason_code, "FILE_TOO_LARGE")


if __name__ == "__main__":
    unittest.main()
