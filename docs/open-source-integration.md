# Open-source integration decisions

Research snapshot: 2026-07-20. Recheck release, license, and output contracts
before every dependency upgrade.

## Make-or-reuse boundary

The product owns the behaviors that establish trust:

- byte/chunk timing and protocol evidence;
- normalized versioned result and reason codes;
- suite validation, secret separation, budgets, and approval gates;
- deterministic scoring, confidence, baselines, billing reconciliation;
- durable orchestration, idempotency, retention, and alert state.

External projects run behind adapters for expensive, fast-moving specialist
capabilities. They never become the system of record.

| Project | License | Adopt for | Integration decision |
|---|---|---|---|
| [Promptfoo](https://github.com/promptfoo/promptfoo) | MIT | Declarative business cases, assertions, regression and red-team suites | Primary quality runner. Run headless in an isolated container and import a pinned JSON artifact. Its trusted configs can execute custom code, so do not run it inside the control plane. |
| [EvalScope](https://github.com/modelscope/evalscope) | Apache-2.0 | Standard LLM/VLM benchmarks, Chinese datasets, OpenAI-compatible online evaluation, Vendor Verifier | Primary benchmark/fidelity runner. Pin dataset revision as well as package/container version. Its 2026 Vendor Verifier suites are the preferred starting point for supported model families. |
| [AIPerf](https://github.com/ai-dynamo/aiperf) | Apache-2.0 | Controlled load, trace replay, TTFT/ITL/goodput and capacity profiles | Primary L6 engine. Always require an approval, maximum cost, duration, concurrency, and kill switch. |
| [GuideLLM](https://github.com/vllm-project/guidellm) | Apache-2.0 | SLO-aware workload profiles and cross-checking performance results | Secondary load runner and AIPerf cross-validator, not enabled in the MVP by default. |
| [Llama Verifications](https://github.com/meta-llama/llama-verifications) | MIT | OpenAI-compatible Llama protocol fixtures and model-card-relative capability checks | Reuse concepts and license-compatible fixtures for Llama routes. Never generalize its result into proof for other model families. |
| [Langfuse](https://github.com/langfuse/langfuse) | MIT except `ee` folders | Optional trace/evaluation exploration UI | Export sanitized traces through OTLP/API. Do not use it as the authoritative probe database or SLO evaluator. |

The orchestration adapter uses the official
[Temporal Python SDK](https://github.com/temporalio/sdk-python), MIT licensed and
pinned exactly to `1.30.0`. Temporal owns durable execution history, timers,
queries, Activity delivery, and cancellation delivery. It does not replace the
product event journal, normalized result, or evidence store.

The production PostgreSQL adapter uses
[Psycopg 3](https://github.com/psycopg/psycopg), LGPL-3.0 licensed. The optional
extra pins `psycopg[binary]==3.3.4` and `psycopg-pool==3.3.1` exactly. Psycopg
owns only PostgreSQL protocol and connection pooling; SQL invariants, replay,
leases, redaction, result schemas, and error classification remain native
project contracts. SQLite and the dependency-free probe do not import it.

The Web control plane pins
[FastAPI](https://github.com/fastapi/fastapi) `0.139.2` (MIT),
[Pydantic](https://github.com/pydantic/pydantic) `2.13.4` (MIT),
[Uvicorn](https://github.com/encode/uvicorn) `0.51.0` (BSD-3-Clause), and
[LangChain Core](https://github.com/langchain-ai/langchain) `1.4.9` (MIT).
FastAPI/Pydantic own HTTP validation and OpenAPI; LangChain owns the
Runnable/callback boundary only. None of them replaces the native HTTP/SSE
measurement transport or is allowed to retry a billable probe implicitly.

The console pins React/ReactDOM `19.2.7` (MIT), React Router DOM `7.18.1`
(MIT), TanStack Query `5.101.2` (MIT), React Hook Form `7.82.0` (MIT), Zod
`4.4.3` (MIT), Vite `8.1.5` (MIT), and Lucide React `1.25.0` (ISC). These
packages own view state, routing, request caching, form validation, bundling,
and icons. Secrets are submitted directly and cleared; they are not query data
or persistent application state.

Promptfoo's repository reported release `0.121.19` on 2026-07-14, and GuideLLM
reported `0.7.1` on 2026-07-02 at this snapshot. Those are research candidates,
not automatic upgrade targets. AIPerf is the successor path for GenAI-Perf and
is preferred for new load-test integration.

The frozen import boundaries are Promptfoo JSON output v3, AIPerf
`profile_export_aiperf.json` schema major version 1, and EvalScope report output
from exactly `1.9.0`. EvalScope's report JSON does not embed its producer
version, so the isolated runner must supply the pinned version separately and
the importer rejects every other value. The contract fixture mirrors the
official `Report -> Metric -> Category -> Subset` Pydantic hierarchy and checks
all micro-aggregated scores and sample counts for internal consistency.

EvalScope `reviews/*.jsonl` can contain prompts, targets, original predictions,
extracted predictions, explanations, metadata, and agent traces. Those files
are raw evidence, not an interchange format: keep them in access-controlled
object storage under the response-body retention policy. Only the aggregate
`reports/*.json` artifact enters `NormalizedRunResult`. The repository's review
fixture is intentionally sanitized to lock the 1.9.0 envelope without retaining
model or user content.

The first built-in EvalScope policies cover `kimi_verifier`, `k2_verifier`, and
`minimax_verifier`. Kimi transport/inference errors are evaluated separately
with a maximum of zero, so a failed request cannot be mistaken for a correct
parameter rejection. Raw K2 count metrics are retained as ignored report
metadata rather than incorrectly scaled into a 0–100 quality score.

## Adapter contract

Every runner adapter accepts an immutable job snapshot containing:

```text
job_id
runner_name + pinned runner_version/image_digest
endpoint_snapshot_id + model_route
suite_uri + suite_hash + dataset_revision
credential_handle (never the secret value)
timeout + request/concurrency/duration/cost limits
artifact_output_uri
```

It returns:

```text
runner_status: SUCCEEDED | TARGET_FAILED | RUNNER_FAILED | CANCELLED
runner_exit_code and sanitized stderr excerpt
normalized result schema version
artifact URI + SHA-256 + media type
started_at + finished_at + runner runtime metadata
```

`TARGET_FAILED` is a valid measurement outcome. It must not be collapsed into
`RUNNER_FAILED`; otherwise outages disappear from scorecards.

## Runner isolation profile

- read-only suite/dataset input and write-only artifact directory;
- no workspace, Docker socket, host credentials, or cloud metadata access;
- egress allowlist limited to the target API and explicitly configured graders;
- ephemeral low-scope key injection at process start and immediate revocation on
  cancellation when the secret provider supports it;
- CPU, memory, process, file-size, duration, request, token, and cost limits;
- telemetry and cloud upload disabled unless explicitly enabled and documented;
- stdout/stderr redaction before persistence, followed by a second scan at import.

The Phase 0 process launcher enforces the no-shell command boundary, configured
executable/version pairing, suite SHA-256, a minimal environment, timeouts and
cancellation, bounded log retention, expected-artifact paths/sizes/hashes, and
secret redaction. It deliberately does not claim to be a complete sandbox.
Production execution still requires a container or equivalent runtime to
enforce read-only mounts, egress allowlists, CPU/memory/process limits, and
credential revocation outside the child process.

## Upgrade gate

An external runner version enters the lock file only after:

1. upstream LICENSE and transitive dependencies are recorded;
2. success, target timeout, malformed output, runner crash, and cancellation
   fixtures pass;
3. golden output normalization and secret-redaction tests pass;
4. the runner cannot bypass network, filesystem, cost, or duration limits;
5. the old and new versions run on the same fixture set and material scoring
   changes are reviewed rather than silently accepted.
