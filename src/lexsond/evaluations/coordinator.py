from __future__ import annotations

import hashlib
import math
import statistics
import threading
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Protocol

from .compiler import EvaluationItem
from .scorers import ScoreResult, ScoreStatus, get_scorer


class EvaluationPlanError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EvaluationPlan:
    models: tuple[str, ...]
    sample_strategy: str = "random"
    sample_seed: int = 42
    sample_count: int = 20
    concurrency: int = 2
    max_output_tokens: int = 64
    timeout_seconds: float = 30.0
    max_cost_usd: float = 1.0
    estimated_cost_usd: float | None = None
    confirm_unknown_cost: bool = False
    scorer_id: str = "dataset_reference"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.models, tuple)
            or not 1 <= len(self.models) <= 10
            or len(set(self.models)) != len(self.models)
            or any(
                not isinstance(model, str)
                or not model.strip()
                or len(model) > 256
                for model in self.models
            )
        ):
            raise EvaluationPlanError("models must contain 1 to 10 unique model IDs")
        if self.sample_strategy not in {"first", "random", "stratified"}:
            raise EvaluationPlanError("sample strategy is unsupported")
        if isinstance(self.sample_seed, bool) or not isinstance(self.sample_seed, int):
            raise EvaluationPlanError("sample seed must be an integer")
        if not 1 <= self.sample_count <= 200:
            raise EvaluationPlanError("sample count must be between 1 and 200")
        if not 1 <= self.concurrency <= 2:
            raise EvaluationPlanError("concurrency must be one or two")
        if not 1 <= self.max_output_tokens <= 1024:
            raise EvaluationPlanError("max output tokens must be between 1 and 1024")
        if not 1 <= self.timeout_seconds <= 120:
            raise EvaluationPlanError("timeout must be between 1 and 120 seconds")
        if (
            isinstance(self.max_cost_usd, bool)
            or not isinstance(self.max_cost_usd, (int, float))
            or not math.isfinite(self.max_cost_usd)
            or not 0 < self.max_cost_usd <= 10_000
        ):
            raise EvaluationPlanError("max cost must be a positive finite amount")
        if self.estimated_cost_usd is None:
            if not self.confirm_unknown_cost:
                raise EvaluationPlanError("unknown prices require explicit confirmation")
        elif (
            isinstance(self.estimated_cost_usd, bool)
            or not isinstance(self.estimated_cost_usd, (int, float))
            or not math.isfinite(self.estimated_cost_usd)
            or self.estimated_cost_usd < 0
            or self.estimated_cost_usd > self.max_cost_usd
        ):
            raise EvaluationPlanError("estimated cost exceeds the run budget")
        if self.scorer_id != "dataset_reference":
            get_scorer(self.scorer_id)

    @property
    def maximum_calls(self) -> int:
        return len(self.models) * self.sample_count

    @property
    def maximum_output_tokens(self) -> int:
        return self.maximum_calls * self.max_output_tokens


@dataclass(frozen=True, slots=True)
class EvaluationCallResult:
    output_text: str
    status_code: int | None
    error_class: str | None
    latency: Mapping[str, float | None]
    usage: Mapping[str, int | None]
    cost_usd: float | None
    safe_facts: Mapping[str, Any] = field(default_factory=dict)


class EvaluationInvoker(Protocol):
    def __call__(
        self,
        model_id: str,
        item: EvaluationItem,
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> EvaluationCallResult: ...


@dataclass(frozen=True, slots=True)
class EvaluationItemOutcome:
    model_id: str
    item_id: str
    category: str
    sequence: int
    state: str
    score: float | None
    status: str
    reason_code: str
    latency: Mapping[str, float | None]
    usage: Mapping[str, int | None]
    output_sha256: str | None
    safe_facts: Mapping[str, Any]
    cost_usd: float | None


@dataclass(frozen=True, slots=True)
class EvaluationModelOutcome:
    model_id: str
    state: str
    completed_items: int
    passed_items: int
    failed_items: int
    unknown_items: int
    metrics: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class EvaluationEvent:
    sequence: int
    event_type: str
    state: str
    model_id: str | None = None
    item_id: str | None = None
    safe_facts: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class EvaluationRunOutcome:
    state: str
    failure_code: str | None
    items: tuple[EvaluationItemOutcome, ...]
    models: tuple[EvaluationModelOutcome, ...]
    aggregate: Mapping[str, Any]
    events: tuple[EvaluationEvent, ...]


def select_evaluation_items(
    items: Sequence[EvaluationItem],
    *,
    strategy: str,
    seed: int,
    count: int,
) -> tuple[EvaluationItem, ...]:
    if strategy not in {"first", "random", "stratified"}:
        raise EvaluationPlanError("sample strategy is unsupported")
    if not 1 <= count <= min(200, len(items)):
        raise EvaluationPlanError("sample count exceeds the dataset revision")
    ordered = tuple(sorted(items, key=lambda item: (item.item_index, item.item_id)))
    if strategy == "first":
        return ordered[:count]
    if strategy == "random":
        return tuple(sorted(ordered, key=lambda item: _sample_rank(seed, item))[:count])
    buckets: dict[str, list[EvaluationItem]] = defaultdict(list)
    for item in ordered:
        buckets[item.category].append(item)
    for values in buckets.values():
        values.sort(key=lambda item: _sample_rank(seed, item))
    selected: list[EvaluationItem] = []
    categories = sorted(buckets)
    offsets = {category: 0 for category in categories}
    while len(selected) < count:
        progressed = False
        for category in categories:
            offset = offsets[category]
            values = buckets[category]
            if offset < len(values):
                selected.append(values[offset])
                offsets[category] = offset + 1
                progressed = True
                if len(selected) == count:
                    break
        if not progressed:
            break
    return tuple(selected)


class EvaluationCoordinator:
    """Bounded local evaluation scheduler with no transport retries."""

    def run(
        self,
        plan: EvaluationPlan,
        items: Sequence[EvaluationItem],
        invoke: EvaluationInvoker,
        *,
        cancellation: threading.Event | None = None,
        cancellation_check: Callable[[], bool] | None = None,
        event_observer: Callable[[EvaluationEvent], None] | None = None,
        item_observer: Callable[[EvaluationItemOutcome], None] | None = None,
    ) -> EvaluationRunOutcome:
        cancellation = cancellation or threading.Event()
        sample = select_evaluation_items(
            items,
            strategy=plan.sample_strategy,
            seed=plan.sample_seed,
            count=plan.sample_count,
        )
        work = [
            (
                model_order,
                model_id,
                model_order * len(sample) + sample_sequence,
                item,
            )
            for model_order, model_id in enumerate(plan.models)
            for sample_sequence, item in enumerate(sample, 1)
        ]
        work_index = 0
        outcomes: list[EvaluationItemOutcome] = []
        events: list[EvaluationEvent] = []
        authentication_failed = False
        payment_failed = False
        budget_stopped = False
        stopped_models: set[str] = set()
        total_known_cost = 0.0

        def emit(
            event_type: str,
            state: str,
            model_id: str | None = None,
            item_id: str | None = None,
            *,
            observe: bool = True,
            **facts: Any,
        ) -> None:
            event = EvaluationEvent(len(events) + 1, event_type, state, model_id, item_id, facts)
            events.append(event)
            if observe and event_observer is not None:
                event_observer(event)

        def refresh_cancellation() -> None:
            if (
                not cancellation.is_set()
                and cancellation_check is not None
                and cancellation_check()
            ):
                cancellation.set()

        emit("EVALUATION_STARTED", "RUNNING", maximum_calls=len(work))

        with ThreadPoolExecutor(
            max_workers=plan.concurrency,
            thread_name_prefix="lexsond-evaluation",
        ) as pool:
            active: dict[Future[EvaluationCallResult], tuple[int, str, int, EvaluationItem]] = {}
            while active or work_index < len(work):
                while work_index < len(work) and len(active) < plan.concurrency:
                    refresh_cancellation()
                    if (
                        cancellation.is_set()
                        or authentication_failed
                        or payment_failed
                        or budget_stopped
                    ):
                        break
                    model_order, model_id, sequence, item = work[work_index]
                    work_index += 1
                    if model_id in stopped_models:
                        continue
                    emit("ITEM_STARTED", "RUNNING", model_id, item.item_id, sequence=sequence)
                    future = pool.submit(
                        invoke,
                        model_id,
                        item,
                        plan.max_output_tokens,
                        plan.timeout_seconds,
                    )
                    active[future] = (model_order, model_id, sequence, item)
                if not active:
                    break
                completed, _ = wait(tuple(active), return_when=FIRST_COMPLETED)
                for future in completed:
                    model_order, model_id, sequence, item = active.pop(future)
                    del model_order
                    try:
                        call = future.result()
                    except Exception:
                        call = EvaluationCallResult("", None, "INTERNAL", {}, {}, None)
                    outcome = _score_call(plan, model_id, sequence, item, call)
                    outcomes.append(outcome)
                    if item_observer is not None:
                        item_observer(outcome)
                    emit(
                        "ITEM_FINISHED",
                        outcome.state,
                        model_id,
                        item.item_id,
                        sequence=sequence,
                        status=outcome.status,
                        reason_code=outcome.reason_code,
                    )
                    if call.cost_usd is not None and math.isfinite(call.cost_usd) and call.cost_usd >= 0:
                        total_known_cost += call.cost_usd
                        if total_known_cost >= plan.max_cost_usd:
                            budget_stopped = True
                    if call.error_class == "AUTHENTICATION" or call.status_code == 401:
                        authentication_failed = True
                        stopped_models.add(model_id)
                    elif call.status_code == 402 or call.error_class == "PAYMENT_REQUIRED":
                        payment_failed = True
                        stopped_models.add(model_id)
                    elif call.status_code in {403, 404, 429} or call.error_class in {
                        "AUTHORIZATION", "PERMISSION",
                        "MODEL_NOT_FOUND", "RATE_LIMIT", "PROTOCOL", "SCHEMA",
                    }:
                        stopped_models.add(model_id)

        if authentication_failed:
            state, failure_code = "FAILED", "AUTHENTICATION"
        elif payment_failed:
            state, failure_code = "FAILED", "PAYMENT_REQUIRED"
        elif cancellation.is_set():
            state, failure_code = "CANCELLED", "CANCELLED"
        elif budget_stopped and len(outcomes) < len(work):
            state, failure_code = ("PARTIAL" if outcomes else "FAILED"), "BUDGET_EXHAUSTED"
        else:
            provider_failures = sum(item.state == "FAILED" for item in outcomes)
            successful = len(outcomes) - provider_failures
            if len(outcomes) < len(work):
                state, failure_code = ("PARTIAL" if successful else "FAILED"), "EXECUTION_INCOMPLETE"
            elif provider_failures and successful:
                state, failure_code = "PARTIAL", None
            elif provider_failures:
                state, failure_code = "FAILED", "TARGET_FAILURE"
            else:
                state, failure_code = "COMPLETED", None
        empty_model_state = (
            "CANCELLED"
            if state == "CANCELLED"
            else "SKIPPED"
            if failure_code in {
                "AUTHENTICATION", "PAYMENT_REQUIRED", "BUDGET_EXHAUSTED"
            }
            else "FAILED"
        )
        model_outcomes = tuple(
            _aggregate_model(
                model_id,
                [item for item in outcomes if item.model_id == model_id],
                seed=plan.sample_seed,
                expected=plan.sample_count,
                force_failed=model_id in stopped_models,
                empty_state=empty_model_state,
            )
            for model_id in plan.models
        )
        ordered_outcomes = tuple(
            sorted(
                outcomes,
                key=lambda item: (plan.models.index(item.model_id), item.sequence),
            )
        )
        aggregate = {
            "model_count": len(plan.models),
            "sample_count": plan.sample_count,
            "maximum_calls": plan.maximum_calls,
            "completed_calls": len(outcomes),
            "known_cost_usd": (
                round(total_known_cost, 6)
                if any(item.cost_usd is not None for item in outcomes)
                else None
            ),
            "cost_completeness": (
                "COMPLETE"
                if len(outcomes) == len(work) and all(item.cost_usd is not None for item in outcomes)
                else "UNKNOWN"
            ),
            "comparable": len(outcomes) == len(work),
            "dataset_item_ids_sha256": hashlib.sha256(
                "\n".join(item.item_id for item in sample).encode()
            ).hexdigest(),
        }
        # The observer deliberately does not persist the terminal event. The
        # repository commits it atomically with the terminal run projection.
        emit(
            "EVALUATION_FINISHED",
            state,
            observe=False,
            failure_code=failure_code or "NONE",
        )
        return EvaluationRunOutcome(
            state,
            failure_code,
            ordered_outcomes,
            model_outcomes,
            aggregate,
            tuple(events),
        )


def _score_call(
    plan: EvaluationPlan,
    model_id: str,
    sequence: int,
    item: EvaluationItem,
    call: EvaluationCallResult,
) -> EvaluationItemOutcome:
    output_hash = (
        hashlib.sha256(call.output_text.encode("utf-8")).hexdigest()
        if isinstance(call.output_text, str) and call.output_text
        else None
    )
    if call.error_class is not None or call.status_code != 200:
        error = call.error_class or f"HTTP_{call.status_code or 0}"
        facts = {"status_code": call.status_code}
        facts.update(_bounded_numeric_facts(call.safe_facts, {"retry_after_seconds"}))
        score = ScoreResult(None, ScoreStatus.UNKNOWN, f"PROVIDER_{error}", facts)
        state = "FAILED"
    else:
        scorer_id = item.reference["scorer"] if plan.scorer_id == "dataset_reference" else plan.scorer_id
        score = get_scorer(str(scorer_id)).score(call.output_text, item.reference)
        state = "COMPLETED"
    return EvaluationItemOutcome(
        model_id=model_id,
        item_id=item.item_id,
        category=item.category,
        sequence=sequence,
        state=state,
        score=score.score,
        status=score.status.value,
        reason_code=score.reason_code,
        latency=_bounded_numeric_facts(call.latency, {"connect_ms", "ttfb_ms", "ttft_ms", "e2e_ms"}),
        usage=_bounded_integer_facts(call.usage, {"input_tokens", "output_tokens", "total_tokens"}),
        output_sha256=output_hash,
        safe_facts=dict(score.facts),
        cost_usd=call.cost_usd if call.cost_usd is None or (math.isfinite(call.cost_usd) and call.cost_usd >= 0) else None,
    )


def _bounded_numeric_facts(value: Mapping[str, Any], allowlist: set[str]) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for key in sorted(allowlist):
        item = value.get(key)
        result[key] = float(item) if isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) and item >= 0 else None
    return result


def _bounded_integer_facts(value: Mapping[str, Any], allowlist: set[str]) -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    for key in sorted(allowlist):
        item = value.get(key)
        result[key] = item if isinstance(item, int) and not isinstance(item, bool) and item >= 0 else None
    return result


def _aggregate_model(
    model_id: str,
    items: Sequence[EvaluationItemOutcome],
    *,
    seed: int,
    expected: int,
    force_failed: bool = False,
    empty_state: str = "FAILED",
) -> EvaluationModelOutcome:
    passed = sum(item.status == "PASS" for item in items)
    failed = sum(item.status == "FAIL" for item in items)
    unknown = sum(item.status == "UNKNOWN" for item in items)
    scores = [float(item.score) for item in items if item.score is not None]
    category_scores: dict[str, float | None] = {}
    for category in sorted({item.category for item in items}):
        values = [float(item.score) for item in items if item.category == category and item.score is not None]
        category_scores[category] = round(statistics.fmean(values), 6) if values else None
    latencies = {
        key: [value for item in items if (value := item.latency.get(key)) is not None]
        for key in ("ttfb_ms", "ttft_ms", "e2e_ms")
    }
    usage: dict[str, int | None] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        values = [value for item in items if (value := item.usage.get(key)) is not None]
        usage[key] = sum(values) if len(items) == expected and len(values) == expected else None
    known_costs = [item.cost_usd for item in items if item.cost_usd is not None]
    observed_count = len(items)
    metrics: dict[str, Any] = {
        "evaluated_items": observed_count,
        "expected_items": expected,
        "overall_score": round(statistics.fmean(scores), 6) if scores else None,
        "confidence_interval_95": _bootstrap_confidence_interval(scores, seed=seed),
        "category_scores": category_scores,
        "success_rate": (
            round(sum(item.state == "COMPLETED" for item in items) / observed_count, 6)
            if observed_count else None
        ),
        "unknown_rate": round(unknown / observed_count, 6) if observed_count else None,
        "parse_failure_rate": (
            round(
                sum(
                    "NOT_PARSED" in item.reason_code or "PARSE_FAILED" in item.reason_code
                    for item in items
                ) / observed_count,
                6,
            )
            if observed_count else None
        ),
        # The native transport does not infer refusal intent from black-box text.
        "refusal_rate": None,
        "usage": usage,
        "known_cost_usd": round(sum(known_costs), 6) if known_costs else None,
        "cost_completeness": (
            "COMPLETE"
            if len(items) == expected and len(known_costs) == expected
            else "UNKNOWN"
        ),
        "latency": {
            key: {"p50": _percentile(values, 0.50), "p95": _percentile(values, 0.95)}
            for key, values in latencies.items()
        },
        "data_completeness": round(len(items) / expected, 6),
    }
    if force_failed:
        state = "FAILED"
    elif len(items) == expected and all(item.state == "COMPLETED" for item in items):
        state = "COMPLETED"
    elif items:
        state = "PARTIAL"
    else:
        state = empty_state
    return EvaluationModelOutcome(model_id, state, len(items), passed, failed, unknown, metrics)


def _sample_rank(seed: int, item: EvaluationItem) -> bytes:
    return hashlib.sha256(
        f"sha256-rank/v1\0{seed}\0{item.item_index}\0{item.item_id}".encode("utf-8")
    ).digest()


def _bootstrap_confidence_interval(scores: Sequence[float], *, seed: int) -> Mapping[str, float] | None:
    if not scores:
        return None
    if len(scores) == 1:
        value = round(float(scores[0]), 6)
        return {"low": value, "high": value, "method": "bootstrap-sha256/v1"}
    means = []
    for iteration in range(1000):
        sample = []
        for draw in range(len(scores)):
            digest = hashlib.sha256(
                f"bootstrap-sha256/v1\0{seed}\0{iteration}\0{draw}".encode(
                    "utf-8"
                )
            ).digest()
            sample.append(scores[int.from_bytes(digest[:8], "big") % len(scores)])
        means.append(statistics.fmean(sample))
    means.sort()
    return {
        "low": round(_percentile(means, 0.025) or 0.0, 6),
        "high": round(_percentile(means, 0.975) or 0.0, 6),
        "method": "bootstrap-sha256/v1",
    }


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * quantile
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return round(ordered[low], 6)
    fraction = position - low
    return round(ordered[low] * (1 - fraction) + ordered[high] * fraction, 6)
