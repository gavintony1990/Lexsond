# ADR-004: Temporal CanaryWorkflow adapter

Status: Accepted for Phase 0.4
Date: 2026-07-19

## Context

The SDK-independent CanaryWorkflow proves the domain state machine, but a
production worker also needs durable timers, Activity delivery, cancellation,
queries, and recovery after worker loss. Those engine semantics must not change
the meaning of a target outage, retry, or audit event.

Temporal workflow code is replayed and therefore cannot perform network,
database, process, thread, or wall-clock operations. Activity delivery is at
least once, so a journal append can commit even when its completion response is
lost.

## Decision

Use the official MIT-licensed Temporal Python SDK, pinned exactly to `1.30.0` in
the optional `temporal` dependency group. `ProbeCanaryWorkflow` mirrors the
accepted domain transition policy and calls three named Activity boundaries:

```text
probe.load_canary_history
probe.append_canary_event
probe.execute_canary_step
```

Workflow input is a single typed `CanaryWorkflowInput`. It contains snapshot
references, suite digest, region, retry policy, and frozen Activity/heartbeat
timeouts. It contains no endpoint URL, secret handle, credential, prompt, or
response body. Numeric timing values are canonicalized to floats before the
input hash is calculated so typed serialization cannot change replay identity.

The Workflow uses only `workflow.now()` and `workflow.sleep()`. Domain event IDs
are UUIDv5 values derived from `(run_id, sequence)`. The external journal accepts
an exact duplicate append as success but rejects any same-sequence event whose
canonical payload differs. This makes a lost Activity response safe without
weakening compare-and-append.

Temporal SDK retries are separated from domain retries:

- load/append Journal Activities retry up to five times because they are
  idempotent infrastructure operations;
- `probe.execute_canary_step` sets the SDK maximum attempts to one;
- the Workflow records every structured failed attempt and applies the frozen
  domain retry policy with a durable Temporal timer;
- configuration and policy failures remain non-retryable, while target failure
  is a measurement and continues through normalization and persistence.

The async Step Activity supervises its synchronous delegate in a worker thread,
heartbeats on an independent timer even while HTTP is blocked, and propagates a
thread-safe cancellation signal. It runs with `WAIT_CANCELLATION_COMPLETED`, so
the worker waits for bounded cancellation cleanup.
An Activity cancellation is not converted into a retryable infrastructure
failure: its `ActivityError` cause is restored to workflow cancellation. The
Workflow appends `WORKFLOW_CANCELLED` before rethrowing, preserving both a
cancelled Temporal execution and the product audit event.

The Workflow exposes a read-only `current_state` query. It returns the same
projected status, phase, target-failure flag, result reference, terminal error,
and sequence used by the engine-independent journal reader.

## Verification

Default tests validate contracts and skip infrastructure when the optional SDK
is absent. The opt-in integration gate starts Temporal's official local
development server and verifies:

1. the complete successful Activity chain;
2. a bounded manual retry with a stable idempotency key;
3. preflight target failure skips execution but continues persistence;
4. a live query while an Activity is running;
5. cancellation reaches a heartbeat Activity and is appended to SQLite;
6. completed histories replay with Temporal `Replayer` without nondeterminism;
7. the concrete native delegate runs through Temporal against the fault-injecting
   mock relay and persists a sanitized immutable result.

## Consequences

Temporal and the product journal intentionally contain overlapping execution
facts: Temporal is the execution authority, while the journal is the stable
product/audit contract. This costs extra writes but keeps querying and future
engine migration independent of Temporal's internal history schema.

`TemporalCanaryStepActivity` supervises the synchronous delegate from an async
Activity: it emits periodic heartbeats even while the HTTP client is blocked,
sets a thread-safe cancellation signal, waits for bounded cleanup, and consumes
the delegate's terminal exception before reporting cancellation.

The local composition root now wires a concrete native OpenAI-compatible
delegate, SQLite Journal/idempotency/final-result stores, content-addressed file
evidence, immutable local endpoint/suite snapshots, and Activity-only
`env://LEXSOND_SECRET_*` resolution. A separate start command builds the
Workflow input from exact suite bytes and rejects duplicate Workflow IDs.

Phase 0.5 wires the PostgreSQL repositories, role boundary, immutable suite
reference start contract, and a PostgreSQL worker composition mode. Production
still needs S3/MinIO object bytes, native cloud Secret Manager clients,
encrypted restricted evidence, regional task-queue deployment, and concrete
Promptfoo/EvalScope/AIPerf Activity delegates.

## Observed SDK note

On Python 3.14, cancelling the running Step Activity can emit
`ActivityError exception in shielded future` from Temporal SDK 1.30.0's internal
`asyncio.shield(handle._result_fut)` path. The execution is still cancelled,
`WORKFLOW_CANCELLED` is durable, the delegate exits, and history replay passes.
The adapter does not patch SDK private state or suppress the event-loop warning;
CI should track this behavior across pinned SDK/Python upgrades before treating
that log signature as a worker incident.
