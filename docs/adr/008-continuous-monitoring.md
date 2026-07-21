# ADR-008: Durable continuous monitoring and health projection

Status: accepted, implemented in 0.8.0.

## Context

An interactive probe proves one request. Operational monitoring needs recurring
execution without duplicate billable calls, durable restart behavior, stable
health semantics, and bounded storage. The existing local and Temporal paths
must remain one execution boundary, and credentials must not become scheduler
configuration.

## Decision

1. A mutable `monitor_policy` references a saved target and, for suite runs, an
   immutable suite revision. It freezes model, modality, stream, timeout,
   backend, interval, and transition thresholds. It never accepts an API key.
2. Keyless targets may run locally. Cloud recurring work requires Temporal and
   a non-secret target `credential_ref`; failure to meet this requirement is a
   validation error, never an implicit backend fallback.
3. The PostgreSQL repository owns `next_run_at` and a paired lease
   token/deadline, claiming work with `FOR UPDATE SKIP LOCKED`. Completion is
   fenced by both lease token and scheduled slot.
4. Policy UUID and scheduled slot derive a stable UUID idempotency key. A missed
   interval advances to the first future slot, preventing restart catch-up
   storms. Each scheduler pass claims at most four policies.
5. Terminal runs project idempotently into compact samples and a current state.
   `UNKNOWN` observations do not change state. Consecutive failure and recovery
   thresholds control `DOWN`/`RECOVERED`; warnings enter `DEGRADED`. Transition
   events are immutable and link to the source run.
6. Scheduled chat components use a deterministic arithmetic moving challenge
   with a 128-bit slot nonce. The seed is the stable slot key, so dispatch replay
   preserves request identity. The answer is calculated separately and never
   appears in the prompt; the exact response must contain both the result and
   nonce. User-authored suites remain untouched.
7. The API aggregates aligned 90m, 24h, 7d, and 30d buckets. React renders the
   server timeline even when a policy has no sample, so rows remain comparable.
8. Derived samples and incidents have separate bounded retention windows.
   Maintenance deletes at most 1,000 of each per transaction, drains multiple
   batches within a five-second/100-batch cap, and retries within one minute if
   the cap is saturated. Run lifecycle and current state are not automatically
   purged.
9. Remote targets require HTTPS. At socket connection time every resolved
   address must be globally routable; mixed public/private answers are rejected.
   The checked numeric address is used without DNS re-resolution while the
   original hostname remains available to Host, SNI, and certificate checks.
   Numeric loopback is the explicit local-development exception. The same
   IP-pinned connection path protects Agent model calls; multicast and
   NAT64-embedded protected IPv4 destinations are rejected explicitly.

## Consequences

- Multiple control processes can safely share PostgreSQL scheduling work.
- PostgreSQL lease fencing permits multiple control processes to coordinate
  without an embedded-database fallback.
- There is no hidden provider retry. Dispatch idempotency protects control-plane
  retries, while each billable execution attempt still invokes the transport once.
- Policy mutation and run-now requests conflict with an active dispatch lease;
  a policy also cannot overlap a still-running previous probe.
- Notification delivery remains a separate future projection of incident events;
  transport failures must never rewrite health history.
