# ADR-005: PostgreSQL runtime adapter and lease boundary

Status: Accepted for Phase 0.5
Date: 2026-07-20

## Context

The Phase 0.3 migration described production tables, but SQLite remained the
only executable repository. A production Temporal worker needs a real pool,
idempotent Workflow-run creation before the first history read, immutable
snapshot resolution, atomic projected journal writes, and an Activity protocol
that prevents two at-least-once deliveries from calling a paid model together.

## Decision

Pin `psycopg[binary]==3.3.4` and `psycopg-pool==3.3.1`, and use the synchronous
`ConnectionPool` for
the synchronous Journal and native Activity delegate. The pool is opened with a
connectivity check; every repository operation owns a short connection/transaction
scope. The dependency-free native CLI remains independent of this adapter;
control-plane and worker processes require the pinned PostgreSQL extra.

`TemporalHistoryRequest` carries the complete, secret-free
`CanaryWorkflowInput`. Before loading history, `TemporalJournalActivities`
calls `prepare_run`. PostgreSQL creates the run idempotently only if the full
JSON and canonical SHA-256 match, and verifies the suite name, version, URI, and
digest against the immutable suite row.

`PostgresWorkflowJournal` reads head and history under `REPEATABLE READ` so a
concurrent append cannot create a mixed snapshot. `append` validates the existing history,
projects the candidate event through the pure domain state machine, then calls
`append_workflow_event`. The function updates the run projection and inserts the
event in one transaction. PostgreSQL derives status, phase, target-failure,
result-reference, and terminal-error projections from the event; the worker
cannot supply them. Exact duplicate delivery succeeds, including the race where
another transaction commits while compare-and-append waits; another event at
the same sequence raises SQLSTATE `40001`.

Activity execution uses four rules:

1. `claim` creates a bounded lease or returns `BUSY`;
2. a completed execution returns its exact outcome;
3. a failed delivery returns its structured error/kind/retryable tuple;
4. only the current UUID lease token can complete or fail an attempt.

The native delegate renews the lease while it is running and turns renewal loss
into cooperative cancellation plus a retryable `ACTIVITY_LEASE_LOST`. `BUSY`
includes the remaining lease duration; local and Temporal Workflows wait and
retry the same logical attempt without consuming the domain retry budget.
An expired lease can be taken over. A failed attempt is replayed exactly, while
a higher Workflow-domain attempt may acquire a new lease. Pure in-memory test
doubles cover domain transitions; PostgreSQL tests own durable lease semantics.

Endpoint and suite snapshots reject update/delete. Endpoint rows hold an HTTPS
base URL, model, and a non-secret Vault/cloud Secret Manager reference. The
production worker reads its DSN from an `LEXSOND_*` environment variable and
uses a secret-free binding document to map each credential reference to an
`LEXSOND_SECRET_*` variable injected by deployment infrastructure.

`0002_access.sql` creates NOLOGIN group roles. Public schema/table/function
access is revoked. The worker selects immutable/configuration data and executes
the `SECURITY DEFINER` mutation functions; it cannot insert workflow events or
probe results directly.

Database JSON constraints recursively reject known secret-bearing keys, rather
than checking only the root object. `RAW_RESTRICTED` evidence requires both
encryption and a retention deadline. Canonical SHA-256 values are computed and
verified by the repositories on every resolution/load; the database enforces
shape, immutability, and exact replay but intentionally does not install a hash
extension with a different JSON canonicalization algorithm.

## Validation

`tests/test_postgres_integration.py` starts a disposable PostgreSQL 16 cluster
and validates migration up/down, snapshot digests, idempotent and conflicting
run creation, exact and conflicting concurrent append, complete Workflow replay,
lease/failure semantics, immutable results/snapshots, and role privileges.

## Consequences

The production control/journal/runtime persistence boundary is executable and
tested against PostgreSQL, not merely specified as SQL. The worker composition
currently stores sanitized evidence bytes on a node-local content-addressed
filesystem; S3/MinIO, encrypted restricted evidence, native secret-manager API
clients, TLS/mTLS database policy, backups, and regional deployment are separate
Phase 1 infrastructure gates.
