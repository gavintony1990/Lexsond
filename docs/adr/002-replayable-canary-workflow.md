# ADR-002: Replayable CanaryWorkflow core

Status: Accepted for Phase 0.2
Date: 2026-07-19

## Context

Canary execution must survive worker crashes, retries, cancellation, and later
workflow-engine migrations without changing what a probe failure means. A target
API outage is a valid measurement and must still be normalized, scored,
persisted, compared, and notified. It is not a workflow-engine failure.

Putting all transition logic directly in Temporal decorators would make replay
tests require the SDK and would couple the domain contract to one orchestrator.
It would also make an accidental retry-policy change on resume difficult to
detect.

## Decision

Keep an SDK-independent, event-sourced CanaryWorkflow core under
`lexsond.workflows` and later implement Temporal as an adapter around its
Activity and Journal ports.

The immutable workflow input contains only snapshot identifiers, a suite object
reference and SHA-256, region, and the frozen retry policy. It never contains a
base URL credential, secret handle, API key, authorization header, presigned
URL, or arbitrary payload. `WORKFLOW_STARTED` records the canonical input hash;
replaying the same `run_id` with a changed snapshot is rejected.

The activity order is:

```text
validate -> preflight -> execute -> normalize -> score -> persist -> compare -> notify
```

If preflight returns `TARGET_FAILED`, `execute` is skipped and the preflight
evidence continues through normalization. If execute returns `TARGET_FAILED`,
the remaining activities still run. Configuration and policy failures produce
`REJECTED`; exhausted infrastructure or runner failures produce `FAILED`.

Each activity receives a stable key:

```text
canary:{run_id}:{activity_name}
```

Retries change the attempt number but not the key. An Activity implementation
must use that key to return the prior durable result after a crash between its
side effect and the completion event.

Every event is appended with an expected sequence number. A PostgreSQL journal
must enforce `UNIQUE (run_id, sequence)` and compare-and-append semantics in one
transaction. Result bodies do not enter events; activities exchange immutable
object references.

## Temporal adapter requirements

- Use Temporal timers for retry waits; do not call the local polling waiter in a
  Temporal workflow.
- Resolve endpoint configuration and credentials inside Activities, never in
  workflow input or history.
- Heartbeat long-running probes and pass cancellation into the native or
  external runner kill switch.
- Disable Temporal automatic retries for deterministic policy/configuration
  errors. Do not retry 401/403.
- Preserve the domain event journal even though Temporal has its own history;
  the journal is the product audit contract and supports engine-independent
  querying.

## Consequences

The transition system is replay-testable without infrastructure, and target
failure semantics are stable across local and Temporal execution. This adds a
small adapter layer and requires idempotent Activities. The in-memory journal
is for tests and local development only; it does not claim crash durability.
