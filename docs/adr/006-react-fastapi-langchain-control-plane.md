# ADR 006: React, FastAPI, LangChain, and a unified run control plane

Status: accepted, implemented in 0.6.0.

## Context

The original Web surface combined a standard-library HTTP server with one
Vanilla JavaScript file and a SQLite-only run table. Local component probes and
the Temporal/PostgreSQL canary workflow were separate entry paths. That made
resource lifecycle, immutable suite revisions, event resumption, and production
execution selection difficult to represent consistently.

The native transport is still the only layer that observes HTTP/SSE bytes,
TTFB, TTFT, ITL, reasoning deltas, protocol failures, and provider-specific
extensions. Replacing it with a generic SDK would change measurement semantics.

## Decision

- FastAPI/Pydantic v2 expose only `/api/v1`. Mutable resources live behind a
  repository contract; SQLite is the local default and PostgreSQL is selected
  explicitly with `LEXSOND_CONTROL_STORE=postgres`.
- Targets are mutable metadata records and never contain an API key. Suites
  create append-only revisions. Runs retain immutable configuration/result
  snapshots and support only cancel/archive/restore/purge lifecycle commands.
- Every Web invocation crosses a LangChain Runnable boundary. Chat/vision use a
  custom `BaseChatModel`; the other modalities use typed Runnables. Those
  wrappers call the native probe exactly once and configure no automatic retry.
- Local execution supports all six components. Temporal supports chat and chat
  suites, persists endpoint/suite snapshots to PostgreSQL, carries only snapshot
  identifiers in Workflow input, and sets the billable activity policy to one
  attempt. Its preflight resolves immutable configuration and credentials but
  makes no model request; `EXECUTE` is the only potentially billable activity.
- Run creation uses a client-generated `Idempotency-Key`. Temporal persists its
  secret-free Workflow input as a dispatch outbox before starting the background
  monitor, reconnects to the deterministic Workflow ID after restart, and
  deduplicates projected events by the source Workflow event ID.
- FastAPI SSE publishes sanitized sequence-numbered events with heartbeat and
  `Last-Event-ID` resumption. Invalid resume cursors fail before streaming starts.
- React/TypeScript/Vite, React Router, TanStack Query, React Hook Form, and Zod
  implement the same-origin console. API keys are cleared after request
  submission and are never placed in query caches or durable browser storage.

## Consequences

The control API, UI, local executor, and Temporal launcher now share one run
index and lifecycle. Refreshing a run page reconstructs progress from persisted
events. Production requires migrations `0001` through `0005`; the control
and worker roles remain distinct. A configured but unavailable Temporal backend
is a visible failure rather than an implicit local fallback.

LangChain is an integration/callback boundary, not the measurement transport.
Future provider integrations may add specialized LangChain models, but they
must preserve the native timing and redaction contracts and prove one requested
attempt results in one provider call.
