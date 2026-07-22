from __future__ import annotations

import threading
import time
import unittest

from lexsond.evaluations.coordinator import (
    EvaluationCallResult,
    EvaluationCoordinator,
    EvaluationPlan,
    EvaluationPlanError,
    select_evaluation_items,
)
from lexsond.evaluations.quickeval import quickeval_items
from lexsond.evaluations.compiler import compile_document_items


class EvaluationCoordinatorTests(unittest.TestCase):
    def test_first_random_and_stratified_sampling_are_seeded_and_stable(self) -> None:
        items = compile_document_items(quickeval_items()).items
        for strategy in ("first", "random", "stratified"):
            with self.subTest(strategy=strategy):
                first = select_evaluation_items(items, strategy=strategy, seed=8042, count=20)
                second = select_evaluation_items(items, strategy=strategy, seed=8042, count=20)
                self.assertEqual([item.item_id for item in first], [item.item_id for item in second])
                self.assertEqual(len({item.item_id for item in first}), 20)
        stratified = select_evaluation_items(items, strategy="stratified", seed=3, count=20)
        category_counts: dict[str, int] = {}
        for item in stratified:
            category_counts[item.category] = category_counts.get(item.category, 0) + 1
        self.assertLessEqual(max(category_counts.values()) - min(category_counts.values()), 1)

    def test_plan_rejects_unbounded_or_unknown_cost_without_confirmation(self) -> None:
        invalid = (
            {"models": tuple(f"m-{index}" for index in range(11))},
            {"sample_count": 201},
            {"concurrency": 3},
            {"max_output_tokens": 1025},
            {"timeout_seconds": 121},
            {"estimated_cost_usd": None, "confirm_unknown_cost": False},
            {"estimated_cost_usd": 2.0, "max_cost_usd": 1.0},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(EvaluationPlanError):
                values = dict(
                    models=("model-one",),
                    sample_strategy="first",
                    sample_seed=1,
                    sample_count=1,
                    concurrency=2,
                    max_output_tokens=64,
                    timeout_seconds=30,
                    max_cost_usd=1.0,
                    estimated_cost_usd=0.1,
                    confirm_unknown_cost=False,
                )
                values.update(changes)
                EvaluationPlan(**values)

    def test_each_model_item_is_called_once_concurrency_is_two_and_output_is_not_retained(self) -> None:
        items = compile_document_items(quickeval_items()[:20]).items
        lock = threading.Lock()
        active = 0
        max_active = 0
        calls: list[tuple[str, str]] = []
        secret = "sk-evaluation-execution-sentinel"

        def invoke(model_id, item, max_output_tokens, timeout_seconds):
            nonlocal active, max_active
            self.assertEqual(max_output_tokens, 64)
            self.assertEqual(timeout_seconds, 30)
            with lock:
                active += 1
                max_active = max(max_active, active)
                calls.append((model_id, item.item_id))
            time.sleep(0.001)
            with lock:
                active -= 1
            answer = str(item.reference.get("answer", "A"))
            return EvaluationCallResult(
                output_text=answer + secret if item.item_index == -1 else answer,
                status_code=200,
                error_class=None,
                latency={"ttfb_ms": 1.0, "ttft_ms": 2.0, "e2e_ms": 3.0},
                usage={"input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
                cost_usd=0.001,
            )

        plan = EvaluationPlan(
            models=("model-a", "model-b"),
            sample_strategy="first",
            sample_seed=9,
            sample_count=20,
            concurrency=2,
            max_output_tokens=64,
            timeout_seconds=30,
            max_cost_usd=1.0,
            estimated_cost_usd=0.04,
            confirm_unknown_cost=False,
        )
        outcome = EvaluationCoordinator().run(plan, items, invoke)
        self.assertEqual(len(calls), 40)
        self.assertEqual(len(set(calls)), 40)
        self.assertEqual(max_active, 2)
        self.assertEqual(outcome.state, "COMPLETED")
        self.assertEqual(len(outcome.items), 40)
        self.assertNotIn(secret, repr(outcome))
        for item in outcome.items:
            self.assertRegex(item.output_sha256 or "", r"^[0-9a-f]{64}$")
            self.assertFalse(hasattr(item, "output_text"))

    def test_authentication_failure_stops_new_calls_without_retry(self) -> None:
        items = compile_document_items(quickeval_items()[:10]).items
        calls: list[tuple[str, str]] = []
        lock = threading.Lock()

        def invoke(model_id, item, *_args):
            with lock:
                calls.append((model_id, item.item_id))
                ordinal = len(calls)
            if ordinal == 1:
                return EvaluationCallResult("", 401, "AUTHENTICATION", {}, {}, None)
            time.sleep(0.01)
            return EvaluationCallResult("irrelevant", 200, None, {}, {}, None)

        plan = EvaluationPlan(
            models=("model-a", "model-b"),
            sample_strategy="first",
            sample_seed=1,
            sample_count=10,
            concurrency=2,
            max_output_tokens=8,
            timeout_seconds=30,
            max_cost_usd=1.0,
            estimated_cost_usd=None,
            confirm_unknown_cost=True,
        )
        outcome = EvaluationCoordinator().run(plan, items, invoke)
        self.assertEqual(outcome.state, "FAILED")
        self.assertLessEqual(len(calls), 2)
        self.assertEqual(len(calls), len(set(calls)))
        self.assertEqual(outcome.failure_code, "AUTHENTICATION")
        failed_models = [model for model in outcome.models if model.completed_items]
        self.assertGreaterEqual(len(failed_models), 1)
        auth_models = [
            model
            for model in failed_models
            if any(
                item.model_id == model.model_id
                and item.reason_code == "PROVIDER_AUTHENTICATION"
                for item in outcome.items
            )
        ]
        self.assertEqual(len(auth_models), 1)
        self.assertEqual(auth_models[0].state, "FAILED")

    def test_cancellation_and_budget_stop_do_not_start_new_work(self) -> None:
        items = compile_document_items(quickeval_items()[:10]).items
        cancel = threading.Event()
        calls = 0

        def invoke(_model_id, _item, *_args):
            nonlocal calls
            calls += 1
            if calls == 1:
                cancel.set()
            return EvaluationCallResult("42", 200, None, {}, {}, 0.01)

        plan = EvaluationPlan(
            models=("model-a",),
            sample_strategy="first",
            sample_seed=1,
            sample_count=10,
            concurrency=1,
            max_output_tokens=8,
            timeout_seconds=30,
            max_cost_usd=1.0,
            estimated_cost_usd=0.1,
            confirm_unknown_cost=False,
        )
        outcome = EvaluationCoordinator().run(plan, items, invoke, cancellation=cancel)
        self.assertEqual(calls, 1)
        self.assertEqual(outcome.state, "CANCELLED")
        self.assertEqual(outcome.models[0].state, "PARTIAL")

    def test_pre_cancelled_models_are_cancelled_not_invented_failures(self) -> None:
        items = compile_document_items(quickeval_items()[:1]).items
        cancellation = threading.Event()
        cancellation.set()
        plan = EvaluationPlan(
            models=("model-a", "model-b"), sample_strategy="first", sample_seed=1,
            sample_count=1, concurrency=1, max_output_tokens=8,
            timeout_seconds=30, max_cost_usd=1.0, estimated_cost_usd=0.1,
        )
        outcome = EvaluationCoordinator().run(
            plan,
            items,
            lambda *_args: self.fail("pre-cancelled evaluation invoked the provider"),
            cancellation=cancellation,
        )

        self.assertEqual(outcome.state, "CANCELLED")
        self.assertEqual([model.state for model in outcome.models], ["CANCELLED", "CANCELLED"])

    def test_missing_usage_cost_and_refusal_evidence_remain_unknown(self) -> None:
        items = compile_document_items(quickeval_items()[:1]).items
        plan = EvaluationPlan(
            models=("model-a",), sample_strategy="first", sample_seed=1,
            sample_count=1, concurrency=1, max_output_tokens=8,
            timeout_seconds=30, max_cost_usd=1.0,
            estimated_cost_usd=None, confirm_unknown_cost=True,
        )

        outcome = EvaluationCoordinator().run(
            plan,
            items,
            lambda *_args: EvaluationCallResult("42", 200, None, {}, {}, None),
        )

        metrics = outcome.models[0].metrics
        self.assertEqual(metrics["usage"], {
            "input_tokens": None, "output_tokens": None, "total_tokens": None,
        })
        self.assertEqual(metrics["cost_completeness"], "UNKNOWN")
        self.assertIsNone(metrics["known_cost_usd"])
        self.assertIsNone(metrics["refusal_rate"])
        self.assertEqual(outcome.aggregate["cost_completeness"], "UNKNOWN")
        self.assertIsNone(outcome.aggregate["known_cost_usd"])

    def test_models_without_observations_do_not_invent_zero_rates(self) -> None:
        items = compile_document_items(quickeval_items()[:1]).items
        plan = EvaluationPlan(
            models=("model-a", "model-b"), sample_strategy="first", sample_seed=1,
            sample_count=1, concurrency=1, max_output_tokens=8,
            timeout_seconds=30, max_cost_usd=1.0, estimated_cost_usd=None,
            confirm_unknown_cost=True,
        )
        outcome = EvaluationCoordinator().run(
            plan,
            items,
            lambda *_args: EvaluationCallResult("", 401, "AUTHENTICATION", {}, {}, None),
        )

        untouched = outcome.models[1].metrics
        self.assertEqual(untouched["evaluated_items"], 0)
        self.assertIsNone(untouched["success_rate"])
        self.assertIsNone(untouched["unknown_rate"])
        self.assertIsNone(untouched["parse_failure_rate"])
        self.assertIsNone(untouched["known_cost_usd"])

    def test_identical_score_evidence_has_identical_confidence_intervals(self) -> None:
        items = compile_document_items(quickeval_items()[:20]).items
        plan = EvaluationPlan(
            models=("model-a", "model-b"), sample_strategy="first",
            sample_seed=71, sample_count=20, concurrency=2,
            max_output_tokens=8, timeout_seconds=30,
            max_cost_usd=1.0, estimated_cost_usd=0.1,
        )
        outcome = EvaluationCoordinator().run(
            plan,
            items,
            lambda *_args: EvaluationCallResult(
                "same-answer", 200, None, {}, {}, 0.0
            ),
        )
        self.assertEqual(
            outcome.models[0].metrics["confidence_interval_95"],
            outcome.models[1].metrics["confidence_interval_95"],
        )

    def test_rate_limit_preserves_retry_after_as_safe_capacity_evidence(self) -> None:
        items = compile_document_items(quickeval_items()[:1]).items
        plan = EvaluationPlan(
            models=("model-a",), sample_strategy="first", sample_seed=1,
            sample_count=1, concurrency=1, max_output_tokens=8,
            timeout_seconds=30, max_cost_usd=1.0,
            estimated_cost_usd=None, confirm_unknown_cost=True,
        )

        outcome = EvaluationCoordinator().run(
            plan,
            items,
            lambda *_args: EvaluationCallResult(
                "", 429, "RATE_LIMIT", {}, {}, None,
                {"retry_after_seconds": 3.0, "unsafe_header": "discarded"},
            ),
        )

        self.assertEqual(outcome.items[0].reason_code, "PROVIDER_RATE_LIMIT")
        self.assertEqual(outcome.items[0].safe_facts["retry_after_seconds"], 3.0)
        self.assertNotIn("unsafe_header", outcome.items[0].safe_facts)

    def test_model_terminal_provider_failure_stops_only_that_model(self) -> None:
        items = compile_document_items(quickeval_items()[:5]).items
        calls: list[tuple[str, str]] = []

        def invoke(model_id, item, *_args):
            calls.append((model_id, item.item_id))
            if model_id == "blocked-model":
                return EvaluationCallResult("", 403, "PERMISSION", {}, {}, None)
            return EvaluationCallResult(
                str(item.reference.get("answer", "A")), 200, None, {}, {}, 0.0
            )

        plan = EvaluationPlan(
            models=("blocked-model", "working-model"), sample_strategy="first",
            sample_seed=1, sample_count=5, concurrency=1, max_output_tokens=8,
            timeout_seconds=30, max_cost_usd=1.0, estimated_cost_usd=0.1,
        )
        outcome = EvaluationCoordinator().run(plan, items, invoke)

        self.assertEqual(
            [call for call in calls if call[0] == "blocked-model"],
            [("blocked-model", items[0].item_id)],
        )
        self.assertEqual(len([call for call in calls if call[0] == "working-model"]), 5)
        self.assertEqual(outcome.state, "PARTIAL")
        self.assertEqual(outcome.models[0].state, "FAILED")
        self.assertEqual(outcome.models[1].state, "COMPLETED")

    def test_payment_stops_batch_while_protocol_stops_only_that_model(self) -> None:
        items = compile_document_items(quickeval_items()[:3]).items
        for status_code, error_class in ((402, "PAYMENT_REQUIRED"), (200, "PROTOCOL")):
            with self.subTest(error_class=error_class):
                calls: list[str] = []

                def invoke(model_id, *_args):
                    calls.append(model_id)
                    if model_id == "blocked-model":
                        return EvaluationCallResult(
                            "", status_code, error_class, {}, {}, None
                        )
                    return EvaluationCallResult("42", 200, None, {}, {}, None)

                plan = EvaluationPlan(
                    models=("blocked-model", "working-model"),
                    sample_strategy="first", sample_seed=1, sample_count=3,
                    concurrency=1, max_output_tokens=8, timeout_seconds=30,
                    max_cost_usd=1.0, estimated_cost_usd=0.1,
                )
                outcome = EvaluationCoordinator().run(plan, items, invoke)
                self.assertEqual(calls.count("blocked-model"), 1)
                if error_class == "PAYMENT_REQUIRED":
                    self.assertEqual(calls.count("working-model"), 0)
                    self.assertEqual(outcome.failure_code, "PAYMENT_REQUIRED")
                else:
                    self.assertEqual(calls.count("working-model"), 3)

    def test_durable_cancellation_is_checked_before_every_new_call(self) -> None:
        items = compile_document_items(quickeval_items()[:5]).items
        calls = 0
        checks = 0

        def invoke(*_args):
            nonlocal calls
            calls += 1
            return EvaluationCallResult("42", 200, None, {}, {}, 0.0)

        def cancelled() -> bool:
            nonlocal checks
            checks += 1
            return checks > 1

        plan = EvaluationPlan(
            models=("model-a",), sample_strategy="first", sample_seed=1,
            sample_count=5, concurrency=1, max_output_tokens=8,
            timeout_seconds=30, max_cost_usd=1.0, estimated_cost_usd=0.1,
        )
        outcome = EvaluationCoordinator().run(
            plan, items, invoke, cancellation_check=cancelled
        )

        self.assertEqual(calls, 1)
        self.assertEqual(outcome.state, "CANCELLED")

    def test_item_sequences_are_global_and_finished_event_is_committed_by_store(self) -> None:
        items = compile_document_items(quickeval_items()[:2]).items
        observed = []
        plan = EvaluationPlan(
            models=("model-a", "model-b"), sample_strategy="first", sample_seed=1,
            sample_count=2, concurrency=1, max_output_tokens=8,
            timeout_seconds=30, max_cost_usd=1.0, estimated_cost_usd=0.1,
        )
        outcome = EvaluationCoordinator().run(
            plan,
            items,
            lambda *_args: EvaluationCallResult("42", 200, None, {}, {}, 0.0),
            event_observer=observed.append,
        )

        self.assertEqual([item.sequence for item in outcome.items], [1, 2, 3, 4])
        self.assertEqual(outcome.events[-1].event_type, "EVALUATION_FINISHED")
        self.assertNotIn("EVALUATION_FINISHED", [event.event_type for event in observed])


if __name__ == "__main__":
    unittest.main()
