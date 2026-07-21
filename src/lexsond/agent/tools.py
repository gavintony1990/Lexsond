from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from ..probe import ProbeType
from ..storage.redaction import redact_value
from .catalog import TOOLS


def build_agent_tools(
    store: Any,
    allowed_names: tuple[str, ...],
    *,
    sensitive_values: tuple[str, ...] = (),
) -> list[StructuredTool]:
    """Bind safe control-plane reads as LangChain tools.

    The tool layer deliberately exposes no API key parameter and no mutation
    that can trigger a billable provider request.  An Agent can diagnose and
    prepare a plan; a human still confirms execution in the run composer.
    """

    definitions = {definition.id: definition for definition in TOOLS}

    def list_probe_targets(limit: int = 10) -> dict[str, Any]:
        """List active Lexsond targets without credentials."""

        targets = store.list_targets()[: _bounded(limit, maximum=25)]
        return _safe({
            "count": len(targets),
            "targets": [
                {
                    "id": target["id"],
                    "name": target["name"],
                    "provider_id": target["provider_id"],
                    "base_url": target["base_url"],
                    "default_model": target["default_model"],
                    "target_kind": target["target_kind"],
                    "credential_ref_configured": target["credential_ref_configured"],
                }
                for target in targets
            ],
        }, sensitive_values)

    def list_recent_probe_runs(limit: int = 5, state: str = "ALL") -> dict[str, Any]:
        """List recent run summaries, optionally filtered by lifecycle state."""

        normalized = state.upper()
        if normalized not in {"ALL", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"}:
            raise ValueError("state must be ALL, RUNNING, COMPLETED, FAILED, or CANCELLED")
        runs = store.list_runs(limit=100)
        if normalized != "ALL":
            runs = [run for run in runs if run["state"] == normalized]
        runs = runs[: _bounded(limit, maximum=20)]
        return _safe({
            "count": len(runs),
            "runs": [
                {
                    "run_id": run["run_id"],
                    "state": run["state"],
                    "result_status": run["result_status"],
                    "failure_code": run["failure_code"],
                    "created_at": run["created_at"],
                    "model": run["config"]["model"],
                    "probe_type": run["config"]["probe_type"],
                    "execution_backend": run["execution_backend"],
                }
                for run in runs
            ],
        }, sensitive_values)

    def inspect_probe_run(run_id: str) -> dict[str, Any]:
        """Read sanitized evidence and workflow for one probe run."""

        run = store.get_run(run_id, include_archived=True)
        result = run.get("result")
        if isinstance(result, dict):
            result = {
                "status": result.get("status"),
                "reason_codes": result.get("reason_codes", []),
                "dimension_scores": result.get("dimension_scores", []),
                "measurements": result.get("measurements", [])[:10],
            }
        return _safe({
            "run_id": run["run_id"],
            "state": run["state"],
            "result_status": run["result_status"],
            "failure_code": run["failure_code"],
            "config": run["config"],
            "workflow": run.get("workflow"),
            "result": result,
        }, sensitive_values)

    def inspect_run_events(run_id: str, after_sequence: int = 0) -> dict[str, Any]:
        """Read ordered lifecycle events for one run."""

        events = store.list_run_events(run_id, after_sequence=max(after_sequence, 0))[:100]
        return _safe(
            {"run_id": run_id, "count": len(events), "events": events},
            sensitive_values,
        )

    def list_probe_suites(limit: int = 10) -> dict[str, Any]:
        """List active chat suites and bounded request budgets."""

        suites = store.list_suites()[: _bounded(limit, maximum=25)]
        values = []
        for suite in suites:
            revision = suite["latest_revision"]
            sampling = revision["document"]["spec"]["sampling"]
            values.append(
                {
                    "suite_id": suite["id"],
                    "name": suite["name"],
                    "revision": revision["revision"],
                    "requests": sampling["requests"],
                    "concurrency": sampling["concurrency"],
                    "timeout_seconds": sampling["timeout_seconds"],
                    "max_cost_usd": sampling["max_cost_usd"],
                }
            )
        return _safe({"count": len(values), "suites": values}, sensitive_values)

    def design_probe_plan(
        target_id: str,
        symptom: str,
        probe_type: str = "chat",
    ) -> dict[str, Any]:
        """Create a non-executing bounded plan for a target and symptom."""

        target = store.get_target(target_id)
        try:
            component = ProbeType(probe_type)
        except ValueError as exc:
            raise ValueError("probe_type is not supported") from exc
        normalized_symptom = symptom.strip()[:500]
        stream = component in {ProbeType.CHAT, ProbeType.VISION}
        return _safe({
            "kind": "PROBE_PLAN",
            "requires_human_confirmation": True,
            "execution_path": "/runs/new",
            "target": {
                "id": target["id"],
                "name": target["name"],
                "model": target["default_model"],
            },
            "problem": normalized_symptom,
            "proposal": {
                "run_kind": "component",
                "probe_type": component.value,
                "execution_backend": "local",
                "stream": stream,
                "timeout_seconds": 30,
                "maximum_provider_requests": 1,
            },
            "stop_conditions": [
                "authentication or authorization failure",
                "rate limit with Retry-After evidence",
                "timeout or malformed protocol response",
            ],
        }, sensitive_values)

    implementations = {
        "list_probe_targets": list_probe_targets,
        "list_recent_probe_runs": list_recent_probe_runs,
        "inspect_probe_run": inspect_probe_run,
        "inspect_run_events": inspect_run_events,
        "list_probe_suites": list_probe_suites,
        "design_probe_plan": design_probe_plan,
    }
    values: list[StructuredTool] = []
    for name in allowed_names:
        definition = definitions.get(name)
        function = implementations.get(name)
        if definition is None or function is None:
            raise ValueError(f"Agent skill references unknown tool: {name}")
        values.append(
            StructuredTool.from_function(
                function,
                name=definition.id,
                description=definition.description,
            )
        )
    return values


def _bounded(value: int, *, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError("limit must be an integer")
    return min(max(int(value), 1), maximum)


def _safe(value: dict[str, Any], sensitive_values: tuple[str, ...]) -> dict[str, Any]:
    """Return a trace-safe value before StructuredTool exposes its output."""

    scrubbed = redact_value(value, sensitive_values=sensitive_values)
    if not isinstance(scrubbed, dict):
        raise RuntimeError("Agent tool returned an invalid value")
    return scrubbed
