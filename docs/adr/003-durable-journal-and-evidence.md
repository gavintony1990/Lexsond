# ADR-003: Durable journal, result, and evidence boundaries

Status: Accepted for Phase 0.3
Date: 2026-07-19

## Context

Workflow replay is only trustworthy if event order, immutable input snapshots,
normalized results, and raw evidence have different persistence rules. Storing
all of them in a single mutable JSON table would allow accidental history
rewrites, mix short-lived response content into long-lived metrics, and make
Activity retries duplicate side effects.

## Decision

Use PostgreSQL 16+ as the production metadata and audit store. The first schema
is defined by `migrations/0001_core.sql` and separates:

- immutable endpoint and ProbeSuite snapshots;
- workflow run projections from append-only workflow events;
- one immutable normalized result per run;
- evidence manifests from evidence object bytes;
- leased, idempotent Activity executions from workflow history.

`append_workflow_event` performs compare-and-append and run-projection update in
one PostgreSQL transaction. A stale expected sequence raises SQLSTATE `40001`.
The events and normalized results tables reject update/delete operations with
append-only triggers. The application must still replay and validate events;
database JSON is not treated as a trusted Python object.

ADR-009 later removed the embedded journal. PostgreSQL is now the sole durable
journal; process-local tests use an in-memory journal only for deterministic
state-machine coverage.

Evidence bytes live in S3/MinIO in production. PostgreSQL stores only immutable
manifests: URI, SHA-256, size, media type, redaction status, encryption state,
and retention deadline. The local `FileEvidenceStore` provides content-addressed
`O_EXCL` writes, mode `0600`, size limits, and read-time size/hash verification.
It refuses unencrypted `RAW_RESTRICTED` content.

Before a `NormalizedRunResult` becomes durable, the persistence redactor removes
`output_text` and `error_message` while preserving their SHA-256 and character
counts as measurement evidence. Scoring therefore happens before persistence.
Raw timelines, reviews, and logs require an explicit retention deadline.

## Invariants

1. `(run_id, sequence)` and `event_id` are unique.
2. A reused `run_id` must have the same canonical workflow-input SHA-256.
3. Event columns must match the event JSON identity fields.
4. Durable JSON recursively contains no known credential or secret fields.
5. Probe results and workflow events are append-only.
6. Raw restricted evidence is encrypted and has a retention deadline in both
   application and PostgreSQL constraints.
7. A content-addressed object is never overwritten.
8. Secret-bearing or presigned URLs are not persistence references.

## Consequences and validation

The PostgreSQL 16 gate creates an ephemeral cluster, applies the core and access
migrations, exercises exact and conflicting concurrent appends, runs the full
Workflow, validates Activity lease/failure replay and immutable results, checks
least-privilege grants, verifies core migration down, and stops the cluster.
CI should run this gate in addition to the fast SQL contract tests.
