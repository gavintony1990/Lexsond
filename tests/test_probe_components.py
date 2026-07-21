from __future__ import annotations

import json
import unittest
from pathlib import Path

from lexsond.probe_components import (
    ComponentStepStatus,
    advance_component_run,
    begin_component_evidence,
    component_catalog,
    create_component_run,
    fail_component_run,
    finalize_component_run,
)


class ProbeComponentContractTests(unittest.TestCase):
    def test_catalog_defines_six_distinct_multimodal_components(self) -> None:
        components = component_catalog()
        expected_modalities = {
            "chat": (["text"], ["text"]),
            "vision": (["text", "image"], ["text"]),
            "embedding": (["text"], ["embeddings"]),
            "image_generation": (["text"], ["image"]),
            "audio_speech": (["text"], ["audio"]),
            "audio_transcription": (["audio"], ["text"]),
        }

        self.assertEqual({item["id"] for item in components}, set(expected_modalities))
        for component in components:
            with self.subTest(component=component["id"]):
                inputs, outputs = expected_modalities[component["id"]]
                self.assertEqual(component["input_modalities"], inputs)
                self.assertEqual(component["output_modalities"], outputs)
                self.assertEqual(
                    [step["id"] for step in component["steps"]],
                    [
                        "target_binding",
                        "fixture_prepare",
                        "request_dispatch",
                        "transport_check",
                        "response_validate",
                        "quality_assert",
                        "evidence_seal",
                    ],
                )
                self.assertEqual(len({step["label"] for step in component["steps"]}), 7)
                serialized = json.dumps(component, ensure_ascii=False).lower()
                self.assertNotIn("api_key", serialized)
                self.assertNotIn("authorization", serialized)

    def test_component_run_is_ordered_and_tracks_current_step(self) -> None:
        workflow = create_component_run(
            "vision",
            run_mode="single",
            occurred_at="2026-07-20T01:00:00+00:00",
            binding_source="MANUAL_CONFIRMATION",
        )
        self.assertEqual(workflow["status"], "RUNNING")
        self.assertEqual(workflow["binding_source"], "MANUAL_CONFIRMATION")
        self.assertEqual(
            workflow["steps"][0]["facts"],
            ["MANUAL PROBE TYPE CONFIRMED"],
        )
        self.assertEqual(workflow["steps"][0]["status"], "PASS")
        self.assertTrue(
            all(step["status"] == "PENDING" for step in workflow["steps"][1:])
        )

        workflow = advance_component_run(
            workflow,
            "fixture_prepare",
            ComponentStepStatus.RUNNING,
            occurred_at="2026-07-20T01:00:01+00:00",
        )
        self.assertEqual(workflow["current_step_id"], "fixture_prepare")
        workflow = advance_component_run(
            workflow,
            "fixture_prepare",
            ComponentStepStatus.PASS,
            occurred_at="2026-07-20T01:00:02+00:00",
        )
        self.assertIsNone(workflow["current_step_id"])
        self.assertEqual(workflow["steps"][1]["finished_at"], "2026-07-20T01:00:02+00:00")

        with self.assertRaisesRegex(ValueError, "ordered"):
            advance_component_run(
                workflow,
                "transport_check",
                ComponentStepStatus.RUNNING,
                occurred_at="2026-07-20T01:00:03+00:00",
            )

    def test_failure_is_anchored_and_later_execution_steps_are_skipped(self) -> None:
        workflow = create_component_run(
            "image_generation",
            run_mode="single",
            occurred_at="2026-07-20T01:00:00+00:00",
            binding_source="PROVIDER_METADATA",
        )
        self.assertEqual(
            workflow["steps"][0]["facts"],
            ["PROVIDER CAPABILITY VERIFIED"],
        )
        for step_id in ("fixture_prepare", "request_dispatch", "transport_check"):
            workflow = advance_component_run(
                workflow,
                step_id,
                ComponentStepStatus.RUNNING,
                occurred_at="2026-07-20T01:00:01+00:00",
            )
            workflow = advance_component_run(
                workflow,
                step_id,
                ComponentStepStatus.PASS,
                occurred_at="2026-07-20T01:00:02+00:00",
            )
        workflow = advance_component_run(
            workflow,
            "response_validate",
            ComponentStepStatus.RUNNING,
            occurred_at="2026-07-20T01:00:03+00:00",
        )
        workflow = advance_component_run(
            workflow,
            "response_validate",
            ComponentStepStatus.FAIL,
            occurred_at="2026-07-20T01:00:04+00:00",
        )

        by_id = {step["id"]: step for step in workflow["steps"]}
        self.assertEqual(by_id["response_validate"]["status"], "FAIL")
        self.assertEqual(by_id["quality_assert"]["status"], "SKIPPED")
        self.assertEqual(by_id["evidence_seal"]["status"], "PENDING")

        completed = finalize_component_run(
            workflow,
            result={"status": "FAIL", "measurements": []},
            occurred_at="2026-07-20T01:00:05+00:00",
        )
        self.assertEqual(completed["status"], "FAIL")
        self.assertEqual(completed["steps"][-1]["status"], "PASS")
        self.assertIsNone(completed["current_step_id"])

    def test_internal_failure_stops_at_the_active_step_without_raw_detail(self) -> None:
        workflow = create_component_run(
            "audio_speech",
            run_mode="single",
            occurred_at="2026-07-20T01:00:00+00:00",
            binding_source="MANUAL_CONFIRMATION",
        )
        workflow = advance_component_run(
            workflow,
            "fixture_prepare",
            ComponentStepStatus.RUNNING,
            occurred_at="2026-07-20T01:00:01+00:00",
        )
        failed = fail_component_run(
            workflow,
            failure_code="EXECUTION_ERROR",
            occurred_at="2026-07-20T01:00:02+00:00",
        )

        self.assertEqual(failed["status"], "FAIL")
        self.assertEqual(failed["steps"][1]["status"], "FAIL")
        self.assertEqual(failed["steps"][-1]["status"], "SKIPPED")
        self.assertNotIn("detail", json.dumps(failed))

    def test_evidence_failure_does_not_overwrite_a_passed_quality_assertion(self) -> None:
        workflow = create_component_run(
            "embedding",
            run_mode="single",
            occurred_at="2026-07-20T01:00:00+00:00",
            binding_source="MANUAL_CONFIRMATION",
        )
        for step_id in (
            "fixture_prepare",
            "request_dispatch",
            "transport_check",
            "response_validate",
            "quality_assert",
        ):
            workflow = advance_component_run(
                workflow,
                step_id,
                ComponentStepStatus.RUNNING,
                occurred_at="2026-07-20T01:00:01+00:00",
            )
            workflow = advance_component_run(
                workflow,
                step_id,
                ComponentStepStatus.PASS,
                occurred_at="2026-07-20T01:00:02+00:00",
            )
        workflow = begin_component_evidence(
            workflow,
            result={"status": "PASS", "measurements": []},
            occurred_at="2026-07-20T01:00:03+00:00",
        )
        failed = fail_component_run(
            workflow,
            failure_code="EVIDENCE_PERSISTENCE_ERROR",
            occurred_at="2026-07-20T01:00:04+00:00",
        )

        by_id = {step["id"]: step for step in failed["steps"]}
        self.assertEqual(by_id["quality_assert"]["status"], "PASS")
        self.assertEqual(by_id["evidence_seal"]["status"], "FAIL")

    def test_canary_protocol_dimension_failure_is_attributed_to_protocol(self) -> None:
        workflow = create_component_run(
            "chat",
            run_mode="canary",
            occurred_at="2026-07-20T01:00:00+00:00",
            binding_source="MANUAL_CONFIRMATION",
        )
        completed = finalize_component_run(
            workflow,
            result={
                "status": "FAIL",
                "reason_codes": ["PSEUDO_STREAM_SUSPECTED"],
                "dimension_scores": [
                    {"dimension": "protocol", "status": "FAIL"},
                    {"dimension": "quality", "status": "PASS"},
                ],
                "measurements": [],
            },
            occurred_at="2026-07-20T01:00:05+00:00",
        )

        by_id = {step["id"]: step for step in completed["steps"]}
        self.assertEqual(by_id["response_validate"]["status"], "FAIL")
        self.assertEqual(by_id["quality_assert"]["status"], "SKIPPED")
        self.assertEqual(by_id["evidence_seal"]["status"], "PASS")

    def test_public_workflow_matches_schema_shape(self) -> None:
        schema_path = (
            Path(__file__).parents[1] / "schemas" / "probe-component-workflow.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        workflow = create_component_run(
            "embedding",
            run_mode="single",
            occurred_at="2026-07-20T01:00:00+00:00",
            binding_source="MANUAL_CONFIRMATION",
        )

        self.assertTrue(set(schema["required"]).issubset(workflow))
        self.assertTrue(
            set(schema["$defs"]["stepRun"]["required"]).issubset(workflow["steps"][0])
        )


if __name__ == "__main__":
    unittest.main()
