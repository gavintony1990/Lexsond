from __future__ import annotations

import json
import unittest
from pathlib import Path

from lexsond.evaluations.compiler import compile_document_items
from lexsond.evaluations.quickeval import quickeval_items, quickeval_manifest
from lexsond.storage.redaction import contains_recognizable_credential


class QuickEvalDatasetTests(unittest.TestCase):
    def test_quickeval_v1_is_original_bundled_and_deterministic(self) -> None:
        items = quickeval_items()
        manifest = quickeval_manifest()
        self.assertEqual(len(items), 80)
        self.assertEqual(len({item["id"] for item in items}), 80)
        self.assertEqual(manifest["slug"], "lexsond-quickeval")
        self.assertEqual(manifest["version"], "1.0.0")
        self.assertEqual(manifest["license_spdx"], "Apache-2.0")
        self.assertEqual(manifest["distribution_policy"], "BUNDLED")
        self.assertEqual(manifest["item_count"], 80)
        self.assertEqual(manifest["content_sha256"], compile_document_items(items).content_sha256)
        self.assertEqual(
            manifest["categories"],
            {
                "arithmetic": 10,
                "classification": 10,
                "en_instruction": 10,
                "extraction": 10,
                "json_structure": 10,
                "logic": 10,
                "reading": 10,
                "zh_instruction": 10,
            },
        )
        self.assertFalse(contains_recognizable_credential(items))
        self.assertNotIn("http://", repr(items))
        self.assertNotIn("https://", repr(items))

        checked_in = json.loads(
            (Path(__file__).parents[1] / "datasets" / "lexsond-quickeval-v1.manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(checked_in, manifest)

    def test_every_item_compiles_and_has_a_registered_scorer(self) -> None:
        compiled = compile_document_items(quickeval_items())
        self.assertEqual(compiled.item_count, 80)
        for item in compiled.items:
            self.assertTrue(item.reference["scorer"])
            self.assertIn(item.language, {"zh-CN", "en"})


if __name__ == "__main__":
    unittest.main()
