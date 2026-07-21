# Agent Development Contract

This repository measures third-party AI APIs. Incorrect measurements are product
defects, so changes must preserve evidence and failure semantics rather than only
making the happy path pass.

## Standard change loop

1. Restate the issue as observable acceptance criteria and explicit non-goals.
2. Identify the owning module and avoid unrelated files.
3. Add or update a failing unit, contract, replay, or integration test.
4. Implement the smallest complete behavior that satisfies the contract.
5. Run `PYTHONPATH=src python3 -m unittest discover -s tests -v` from `Lexsond/`.
6. Inspect serialized output for credentials, authorization headers, and raw
   sensitive prompts.
7. Update the README, schema, ADR, or fixture when a public contract changes.
8. Request an independent review for secret handling, scoring, billing,
   concurrency, migrations, or load generation.

## Module ownership

- `models.py`: versioned normalized evidence contract only.
- `probe.py` and `sse.py`: native protocol observation; never infer model identity.
- `suite.py`: static validation, budgets, and bounded execution intent.
- `scoring.py`: deterministic calculations; missing evidence becomes `UNKNOWN` or
  an explicit failed required assertion, never an invented zero.
- `mock_relay.py`: deterministic protocol and provider fault injection.
- `runners/`: adapters around pinned external tools. External internal objects
  must be normalized before leaving this boundary.
- `workflows/`: deterministic transitions, event projection, retry and
  target-failure semantics. SDK decorators and database code belong in adapters,
  not in the workflow core.
- `storage/` and `migrations/`: append-only history, immutable results, evidence
  manifests, redaction and database adapters. Never make raw response retention
  implicit in a metrics table.

Freeze schemas and fixtures before assigning parallel adapter work. One Agent
owns each external runner directory; the core result model has a single
integration owner.

## Non-negotiable safety rules

- API keys are supplied by environment or a secret handle. Never place them in
  suite documents, workflow arguments, logs, traces, fixtures, or result JSON.
- L0-L2 probes are bounded by request count, concurrency, timeout, output tokens,
  and cost. L6 additionally requires explicit approval and a kill switch.
- Do not retry 401/403 or deterministic schema failures. Respect `Retry-After`
  for 429 and preserve it as capacity evidence.
- Do not describe black-box model fidelity as proof of model identity. Use
  `CONSISTENT`, `SUSPECTED_DEGRADATION`, or `INSUFFICIENT_EVIDENCE`.
- Promptfoo and other code-capable evaluation configs run in an isolated process
  or container with a read-only artifact mount and an outbound network allowlist.

## Definition of done

A change is complete only when its normal, timeout/cancellation, malformed input,
and secret-redaction behavior are covered in proportion to risk; schemas match
serialized output; external dependencies are version-pinned with a recorded
license; and the full test suite passes.
