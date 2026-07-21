# ADR-009: PostgreSQL-only persistent memory

Status: accepted, implemented after 0.8.0.

## Context

Lexsond previously maintained parallel SQLite and PostgreSQL implementations
for the control plane, workflow journal, Activity leases, immutable results,
monitoring, and Agent checkpoints. The two paths duplicated migrations and
failure semantics. A passing local path did not prove that PostgreSQL locking,
role boundaries, JSON constraints, or transaction behavior were correct.

Incorrect persistence behavior changes measurements and audit evidence, so an
embedded fallback is more dangerous than an explicit startup failure.

## Decision

- PostgreSQL 16+ is the only structured persistent-memory backend for targets,
  suite revisions and snapshots, runs, workflow events, Activity leases,
  normalized results, evidence manifests, monitoring state, and Agent memory.
- The FastAPI application requires `LEXSOND_POSTGRES_DSN` (or an explicitly
  injected PostgreSQL DSN) and fails startup when it is absent or unusable.
- The Temporal worker always constructs PostgreSQL journal, runtime, snapshot,
  suite, and evidence-manifest repositories. Local backend flags and file-based
  endpoint/suite snapshot resolvers are removed.
- The standalone Temporal start command accepts only an immutable suite
  `s3://` or `https://` reference with its name, version, and canonical SHA-256.
  The referenced row must already exist in PostgreSQL.
- SQLite repositories, the legacy standard-library Web server, its static UI,
  and their package entry points are removed.
- Pure in-memory journals and test doubles may exercise deterministic domain
  logic, but they are not application persistence options.

The content-addressed `FileEvidenceStore` remains deliberately separate. It
stores sanitized artifact bytes and verifies size/hash on read; PostgreSQL
stores the immutable evidence manifest. This artifact boundary is not a second
source of mutable application memory. Production object storage can replace
the byte store without changing the PostgreSQL evidence contract.

## Failure and security semantics

- There is no fallback when PostgreSQL is unavailable.
- An absent Temporal target disables that optional execution backend; a present
  but invalid Temporal/PostgreSQL launcher configuration fails startup so
  durable recovery is never silently skipped.
- Credentials and authorization headers remain excluded from database JSON,
  workflow input, event streams, fixtures, and artifact manifests.
- Suite documents reject credential-shaped string values at validation, store,
  and PostgreSQL constraint boundaries; they are never silently rewritten.
- `401`/`403`, deterministic schema failures, `429 Retry-After`, missing
  evidence, immutable-result conflicts, and lease fencing keep their existing
  explicit semantics.
- PostgreSQL integration tests and SQL contract tests are the authoritative
  persistence gates. Process-local doubles cover only pure orchestration logic.

## Consequences

Deployment now requires PostgreSQL migrations before either the control plane
or worker starts. Local development has one more infrastructure dependency, but
there is one schema, one transaction model, one concurrency model, and one
durable secret boundary to operate and verify. Removing over ten thousand lines
of duplicate implementation and tests reduces the chance that local success
masks a production-only measurement defect.
