from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from uuid import uuid4

from .contracts import CanaryWorkflowInput, RetryPolicy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Start one AI Lexsond CanaryWorkflow"
    )
    parser.add_argument("--temporal-target", default="127.0.0.1:7233")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--task-queue", default="lexsond-canary-local")
    parser.add_argument("--endpoint-snapshot-id", required=True)
    parser.add_argument("--suite-uri", required=True)
    parser.add_argument("--suite-name", required=True)
    parser.add_argument("--suite-version", required=True)
    parser.add_argument("--suite-sha256", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--activity-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--activity-heartbeat-seconds", type=float, default=15.0)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--initial-backoff-seconds", type=float, default=1.0)
    parser.add_argument("--backoff-multiplier", type=float, default=2.0)
    parser.add_argument("--max-backoff-seconds", type=float, default=30.0)
    return parser


def build_workflow_input(args: argparse.Namespace) -> CanaryWorkflowInput:
    return CanaryWorkflowInput(
        run_id=args.run_id or str(uuid4()),
        endpoint_snapshot_id=args.endpoint_snapshot_id,
        suite_name=args.suite_name,
        suite_version=args.suite_version,
        suite_uri=args.suite_uri,
        suite_sha256=args.suite_sha256,
        region=args.region,
        activity_timeout_seconds=args.activity_timeout_seconds,
        activity_heartbeat_seconds=args.activity_heartbeat_seconds,
        retry_policy=RetryPolicy(
            max_attempts=args.max_attempts,
            initial_backoff_seconds=args.initial_backoff_seconds,
            backoff_multiplier=args.backoff_multiplier,
            max_backoff_seconds=args.max_backoff_seconds,
        ),
    )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    for field_name in (
        "temporal_target",
        "namespace",
        "task_queue",
        "endpoint_snapshot_id",
        "region",
    ):
        if not getattr(args, field_name).strip():
            parser.error(f"--{field_name.replace('_', '-')} must be non-empty")
    try:
        workflow_input = build_workflow_input(args)
        result = asyncio.run(_execute(args, workflow_input))
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("temporalio"):
            parser.error("Temporal support requires: pip install -e '.[temporal]'")
        raise
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(asdict(result), sort_keys=True, ensure_ascii=False))


async def _execute(
    args: argparse.Namespace, workflow_input: CanaryWorkflowInput
) -> object:
    from temporalio import common
    from temporalio.client import Client

    from .temporal_workflow import TemporalCanaryWorkflow

    client = await Client.connect(args.temporal_target, namespace=args.namespace)
    handle = await client.start_workflow(
        TemporalCanaryWorkflow.run,
        workflow_input,
        id=f"probe-{workflow_input.run_id}",
        task_queue=args.task_queue,
        id_reuse_policy=common.WorkflowIDReusePolicy.REJECT_DUPLICATE,
        id_conflict_policy=common.WorkflowIDConflictPolicy.FAIL,
    )
    try:
        return await handle.result()
    except asyncio.CancelledError:
        await handle.cancel()
        raise

if __name__ == "__main__":
    main()
