# Lexsond（码海测深）

“码海”指模型、协议与 Token 汇成的计算海域；“测深”取自声呐测量——
不只判断接口通不通，还要从响应信号中量出协议、性能与输出质量的深浅。

Current implementation of the design in
[`docs/lexsond-blueprint.md`](docs/lexsond-blueprint.md).

The native probe deliberately has no runtime dependencies. FastAPI, LangChain,
Temporal, and PostgreSQL are pinned optional boundaries around that core:

- an incremental SSE parser that preserves event timing;
- a normalized, versioned result contract;
- native OpenAI-compatible probes for chat, vision, embeddings, image
  generation, speech synthesis, and audio transcription;
- a bounded ProbeSuite compiler and aggregate scoring engine;
- a React/TypeScript observability console and versioned `/api/v1` control API;
- a LangChain Agent workbench with bounded Tools, Skills, and repository-backed memory;
- SQLite and PostgreSQL repositories for target, suite-revision, run, and Agent-session CRUD.

## Open the visual console

Build and start the React + FastAPI console:

```bash
cd Lexsond
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[web]'
cd frontend
npm install
npm run build
cd ..
.venv/bin/lexsond-web
```

For React development, run `npm run dev` in `frontend/`; Vite proxies
`/api/v1` to port 8090. Production builds are served by FastAPI from the same
origin. Open [http://127.0.0.1:8090](http://127.0.0.1:8090). The console sends
real requests only. Create a target, discover its model catalog, then launch a
component or suite run. It provides:

- live run state plus availability, protocol, performance, and quality scores;
- a persisted seven-stage component flow that shows the current detection step,
  completed checks, failure stop, skipped work, per-step duration, and safe facts;
- TTFT/E2E traces, throughput, token totals, and per-request evidence;
- direct probing of real OpenAI-compatible `chat/completions`, `embeddings`,
  `images/generations`, `audio/speech`, and `audio/transcriptions` endpoints,
  with provider-specific request adapters where the wire contract differs;
- keyless local targets such as Ollama, vLLM, LM Studio, LocalAI, and Xinference;
- local API-key format recognition with provider, Base URL, and suggested-model
  autofill for common cloud services;
- local run history in `.local/web.sqlite3`.

Targets and suites support create/read/update/archive/restore/purge. Suite edits
create immutable revisions; a run always points to the exact revision it used.
Runs can be cancelled and archived but their configuration and result are not
arbitrarily patchable. Purge requires an archived resource and is rejected while
another retained run references it. The old `lexsond-web-legacy` entrypoint is
kept only for manual comparison; `/api/v1` has no `/api/*` compatibility layer.

`POST /api/v1/runs` accepts a UUID `Idempotency-Key` header. The React console
always supplies one, so an HTTP transport retry resolves to the original run
instead of launching another billable request. Temporal launches persist their
secret-free Workflow input before asynchronous dispatch; a restarted control
process attaches to the same deterministic Workflow ID and projects source
events idempotently.

API keys are `SecretStr` request fields used only for model discovery or one
local run. React clears them after submission. They are not returned, logged, or
written to SQLite/PostgreSQL/SSE/Temporal History. Temporal targets instead keep
a non-secret `credential_ref`; the worker resolves the value from its approved
environment/secret-manager binding.

### LangChain Agent, Tools, Skills, and memory

The **探针智能体** route maps the reference architecture directly into the
running application: React is the intent/message entrance, FastAPI owns input
validation and redaction, `AgentCoordinator` binds a custom LangChain
`BaseChatModel`, and the selected Skill restricts the Tool registry. The
repository checkpointer persists sanitized user/assistant messages plus a small
LLM/Tool event trace so a refreshed page can restore the conversation.

Built-in Skills cover connection/authentication diagnosis, quality-evidence
triage, and bounded probe-plan design. Their Tools read targets, recent runs,
sanitized evidence, run events, and suites, or return a non-executing plan.
No Agent Tool can launch a provider request in version 0.7: a proposed run links
back to `/runs/new` and still requires human confirmation. The model loop is
limited to four iterations and configures zero automatic retries.

Agent model credentials are transient `SecretStr` request data. React excludes
both the key and prompt from mutation variables and clears the key field on
both success and failure. Before any provider value becomes a LangChain trace,
tool invocation, checkpointer message, or event, recognizable credentials and
the exact submitted key are replaced with `[REDACTED]`; tool names/IDs and
arguments are allowlisted. A key that collides with a frozen session field
or prior checkpoint causes the full session to be scrubbed and
security-quarantined. A bounded repository turn lease rejects concurrent turns
for the same session, so collision scanning and history loading cannot race.
Long turns renew the lease in the background, and every durable turn write is
fenced by its current token. See
[`docs/adr/007-langchain-agent-tools-skills.md`](docs/adr/007-langchain-agent-tools-skills.md)
for the component and call-sequence decision.

### Multimodal detection components and live flow

Version 0.5 turns each probe family into an explicit detection component. Every
component follows the same observable seven-stage control flow—capability
binding, fixture preparation, request dispatch, transport check, response
validation, modality assertion, and evidence sealing—but owns different fixed
fixtures and assertions:

| Component | Scenario | Modality-specific assertion |
| --- | --- | --- |
| Text chat | text → text | non-empty output plus valid JSON/SSE termination |
| Vision | text + generated image → text | exact `RED` answer for the built-in red PNG |
| Embeddings | text → vector | non-empty, consistent dimensions and finite values |
| Image generation | text → PNG | bounded base64 transport and complete PNG decode |
| Speech synthesis | text → audio | content type and complete WAV/MP3 frame structure |
| Audio transcription | generated WAV → text | bounded JSON string field; no semantic-accuracy claim for silence |

The Web API persists this state in `workflow_json` next to the sanitized run and
returns it as `run.workflow`. Polling therefore shows a real step emitted by the
probe, not a simulated animation, and selecting an old run reconstructs the same
flow. A failed step is retained as the stop point; remaining execution steps are
`SKIPPED`, while evidence sealing still completes when a sanitized target result
exists. The versioned public shape is
[`schemas/probe-component-workflow.schema.json`](schemas/probe-component-workflow.schema.json).
Descriptions and facts are locally defined or numeric/allowlisted measurements.
They never include API keys, authorization headers, raw prompts, model reasoning,
generated images, audio bytes, vectors, or transcripts.

The binding step distinguishes provider-declared capability metadata from a
manual probe-type confirmation, so an unknown custom or local catalog is never
presented as vendor-verified. Progress persistence runs outside the timed
network path, and observer latency is excluded from request deadlines and
latency metrics. Evidence sealing becomes live before redaction and
serialization; a persistence failure is therefore shown at `evidence_seal`
instead of being mislabeled as a model-quality failure. Runs created before
workflow tracking are labeled as legacy records with workflow data unavailable,
not as no-traffic previews; early workflow rows with an unverifiable binding
claim are migrated to `LEGACY_UNSPECIFIED`.

Generic OpenAI-compatible targets use `GET /models`. OpenRouter is requested
with the fixed `output_modalities=all` query because its unfiltered endpoint
defaults to text-output models; this keeps image, audio, embedding, and video
rows in the catalog. Ollama is adapted to its
native `GET /api/tags` catalog while chat measurements still use the
OpenAI-compatible `/v1/chat/completions` endpoint. An empty catalog is reported
as a successful connection with no installed models.

The console preserves every safe, unique model ID returned by the provider. It
also normalizes explicit provider metadata such as `architecture.input_modalities`,
`architecture.output_modalities`, supported parameters, supported voices, and
context length into the versioned model-catalog contract. Text-to-text,
text-plus-image-to-text, text-to-embedding, text-to-image, text-to-`speech`, and
audio-to-`transcription` rows are mapped to a probe only when both declared input
and output modalities match that probe's fixed request. A speech probe also
requires a provider-declared voice. On OpenRouter, ordinary `audio` input/output
belongs to Chat Completions and is not confused with the dedicated TTS/STT
routes. Other declared combinations, video output, and future modalities remain
visible as `CATALOG ONLY` until a bounded endpoint adapter exists. To avoid
presenting a partial result as a full catalog, a response above the bounded
2,000-model contract fails explicitly instead of being silently truncated.

OpenAI and DeepSeek currently return only basic owner/availability information
from their standard model-list APIs, not trustworthy modality declarations.
Those models are still listed in full, but the console marks their capability
source as `UNSPECIFIED` and requires the operator to choose the probe type. It
does not infer capabilities from model names. OpenRouter and compatible catalogs
that return explicit architecture metadata can be routed automatically.

Vision smoke tests send a generated 64×64 red PNG owned by the probe—never a
user file—and validate the returned color. Audio transcription uses a generated,
one-second silent WAV. Embedding vectors, generated-image payloads, audio bytes,
and transcripts are inspected only in memory. The v0.5 strict image probe asks
for PNG and base64 image outputs must pass a bounded PNG structural/pixel decoder;
remote image URLs and other image formats are treated as transport-only and
cannot pass this strict probe. Generated WAV/MP3 output must contain a valid
bounded container/frame structure. Normalized results retain counts, dimensions,
formats, allowlisted content types, timings, and status, not the raw artifacts.
OpenRouter image generation uses its `/images` route, OpenRouter TTS selects the
first provider-declared voice and requests MP3, and OpenRouter transcription uses
its JSON `input_audio` contract. Generic targets retain the OpenAI-compatible
routes and multipart transcription contract. Before any active run, the server
re-reads explicit catalog metadata and rejects a probe type that conflicts with
the selected model. Image and audio generation can be billable even for one
bounded request.

Chat/vision and non-chat response readers enforce a 16 MiB total body limit,
bounded stream event/output counts, and an absolute wall-clock deadline. Provider
controlled output and metadata are credential-scrubbed before a result can be
returned; durable history additionally removes all raw output and error text.

Local deployments can be probed without an API key. If a local server has
authentication enabled, its key can be supplied optionally. Cloud targets require
an API key. Keys are accepted only by the local connection/detection/launch
requests and kept in memory for that operation. Provider discovery analyzes recognizable key prefixes locally and makes no
outbound request. Because several services share the generic `sk-` prefix, the
console presents candidates and requires an explicit selection instead of
guessing or trying the credential against multiple providers. Keys are never
returned by the API or written to history. The confirmed provider and Base URL
are bound again at the server boundary before any probe runs. Remote targets
must use HTTPS; plain HTTP is allowed only for numeric loopback addresses such
as `127.0.0.1`. Persisted results pass through the same sanitizer as durable
Workflow results, removing raw output, provider error text, and credentials
reflected into response metadata while retaining hashes, lengths, metrics,
reason codes, and protocol evidence.

Typical local defaults are:

- Ollama: `http://127.0.0.1:11434/v1`
- vLLM: `http://127.0.0.1:8000/v1`
- LM Studio: `http://127.0.0.1:1234/v1`

For cloud services, enter the key first; a recognizable prefix fills the
provider, Base URL, and a suggested model. Unknown or ambiguous keys can use
the explicit provider selector or a confirmed custom HTTPS endpoint.

### DeepSeek `sk-` key troubleshooting

DeepSeek and several other OpenAI-compatible services use the same generic
`sk-` prefix, so the prefix alone cannot prove which company issued a key. In
the console, switch to **云服务**, choose **DeepSeek**, and then paste the key.
The server verifies that DeepSeek is one of the detected candidates and keeps
that explicit selection instead of clearing it. The confirmed official Base URL
is `https://api.deepseek.com`.

**连接并读取模型** calls the provider's catalog endpoint and verifies the
credential without generating a completion. **开始探测** sends real inference
requests and therefore consumes billable model tokens. A key that has been
pasted into chat, logs, screenshots, or source code should be revoked and
replaced before further use.

DeepSeek V4 enables thinking mode by default. Streaming measurements count both
`reasoning_content` and final `content` events when calculating TTFT, ITL,
throughput, and pseudo-stream evidence, while exact-output quality assertions
continue to use final `content` only. The probe stores reasoning character counts
and arrival timing, never the reasoning text itself. A final answer that arrives
in a short burst after a genuinely streamed reasoning phase is labeled
`FINAL BURST` and is not misclassified as a pseudo stream.

A single non-empty SSE event carrying an entire multi-character or multi-token
answer is still treated as suspected pseudo-streaming. Whitespace-only output is
empty for both single probes and suite quality scoring. Non-200 provider bodies
are used only for in-memory error classification; arbitrary body text is never
copied into normalized results because it may echo prompts, reasoning, or keys.

After an editable install, the equivalent entrypoint is:

```bash
python3 -m pip install -e .
lexsond-web
```

## Run the tests

```bash
cd Lexsond
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The real Temporal integration test is opt-in because it starts Temporal's
official local development server (and downloads that server on first use):

```bash
python3 -m pip install -e '.[temporal]'
RUN_TEMPORAL_TESTS=1 PYTHONPATH=src \
  python3 -m unittest tests.test_temporal_workflow -v
```

The PostgreSQL gate starts an ephemeral PostgreSQL 16 cluster, applies the real
migrations, exercises concurrent append and Activity leases, verifies least
privilege, runs a complete Workflow, tests up/down, then stops the cluster:

```bash
python3 -m pip install -e '.[postgres]'
RUN_POSTGRES_TESTS=1 PYTHONPATH=src \
  python3 -m unittest tests.test_postgres_integration -v
```

## Run the local Temporal worker

The example endpoint file contains only an `env://` credential handle. The key
itself is resolved inside the Activity and never enters Workflow input/history.

```bash
python3 -m pip install -e '.[temporal]'
export LEXSOND_SECRET_LOCAL_RELAY=test-key
mkdir -p .local/evidence

lexsond-temporal-worker \
  --endpoint-snapshots config/local-endpoints.example.json \
  --suite-root suites \
  --evidence-root .local/evidence \
  --sqlite-database .local/probe.sqlite3
```

With Temporal and the configured OpenAI-compatible endpoint running, start one
immutable canary:

```bash
lexsond-temporal-start \
  --endpoint-snapshot-id local-relay-v1 \
  --suite-file suites/canary/openai-compatible.json \
  --region local-dev
```

The worker rejects inline keys, endpoint config symlinks, suite symlinks,
digest drift, and credential handles outside the
`env://LEXSOND_SECRET_*` namespace. SQLite and the file evidence store are
local-development adapters, not the production HA storage recommendation.

## Run a PostgreSQL-backed Temporal worker

Apply `0001_core.sql`, `0002_access.sql`, `0003_control_plane.sql`, then
`0004_agent_control_plane.sql` using a migration-owner
connection. Give the worker login membership in `lexsond_worker`; it receives
read access plus `SECURITY DEFINER` functions, not direct journal/result writes.
The DSN and secret values are supplied only through namespaced environment
variables:

```bash
python3 -m pip install -e '.[production]'
export LEXSOND_POSTGRES_DSN='postgresql://probe_worker@db/probe'
export LEXSOND_SECRET_OPENAI_RELAY='injected-by-secret-manager'

lexsond-temporal-worker \
  --storage-backend postgres \
  --credential-bindings config/postgres-credential-bindings.example.json \
  --evidence-root /var/lib/lexsond/evidence \
  --task-queue lexsond-canary-cn-east-1
```

To run the FastAPI control plane on PostgreSQL and enable Temporal launches:

```bash
export LEXSOND_CONTROL_STORE=postgres
export LEXSOND_POSTGRES_DSN='postgresql://probe_control@db/probe'
export LEXSOND_TEMPORAL_TARGET='temporal:7233'
export LEXSOND_TEMPORAL_NAMESPACE='default'
export LEXSOND_TEMPORAL_TASK_QUEUE='lexsond-canary-cn-east-1'
export LEXSOND_REGION='cn-east-1'
lexsond-web --host 0.0.0.0 --port 8090
```

If Temporal/PostgreSQL configuration is absent or cannot initialize, the UI
marks Temporal unavailable and rejects that backend explicitly; it never falls
back to local execution.

Start an immutable PostgreSQL-backed suite by reference, without passing a
credential or DSN on the command line:

```bash
lexsond-temporal-start \
  --endpoint-snapshot-id relay-cn-v3 \
  --suite-uri s3://probe-suites/openai-canary-2026-07-20.json \
  --suite-name openai-compatible-canary \
  --suite-version 2026.07.20 \
  --suite-sha256 <64-lowercase-hex-digest> \
  --region cn-east-1 \
  --task-queue lexsond-canary-cn-east-1
```

PostgreSQL stores suite documents as `JSONB`, so `suite_sha256` is the SHA-256
of UTF-8 JSON serialized with sorted keys, no insignificant whitespace, and
`ensure_ascii=False`; the future control plane owns this canonicalization.

This composition still uses node-local, sanitized file evidence. S3/MinIO
bytes, encrypted restricted evidence, and native Vault/cloud Secret Manager
clients remain Phase 1 deployment work; the binding adapter is for environments
where the secret manager injects values into the worker.

## Probe a real target from the CLI

Local deployments do not require a token unless their server-side authentication
is enabled:

```bash
cd Lexsond
PYTHONPATH=src python3 -m lexsond.cli \
  --base-url http://127.0.0.1:11434/v1 \
  --model qwen3:8b
```

For a cloud endpoint, pass `--api-key` or set `LEXSOND_KEY`.

Select another bounded endpoint family with `--probe-type vision`,
`embedding`, `image_generation`, `audio_speech`, or `audio_transcription`.
Use `--provider-id openrouter` when invoking OpenRouter from the CLI so its
provider-specific image, speech, and transcription request contracts are
selected. An OpenRouter speech CLI probe also requires `--audio-voice` with a
voice declared for that model.
Non-chat probes are single-request, non-streaming smoke tests; the multi-sample
ProbeSuite remains a chat-only scoring contract in v1alpha1.

## Run a scored canary suite

The suite document contains no credential. The key is injected separately at
runtime and is excluded from object representations and result JSON.

```bash
cd Lexsond
PYTHONPATH=src python3 -m lexsond.cli \
  --base-url http://127.0.0.1:11434/v1 \
  --model qwen3:8b \
  --suite-json suites/canary/openai-compatible.json
```

The suite compiler rejects unknown fields, inline secrets, missing timeouts,
unbounded output, incompatible streaming assertions, and L6 load tests without
a cost budget. Results contain independent availability, protocol, performance,
and quality scores. A failed assertion cannot be hidden by a successful HTTP
request.

## Current scope

Implemented:

- `/v1/chat/completions`, streaming and non-streaming;
- reasoning-aware TTFT, TTFB, E2E, chunk timeline, ITL and output throughput;
- error classification without exposing authorization headers;
- provider-reported usage capture;
- real model-catalog discovery and keyless local API probing;
- bounded L0-L6 suite compilation and concurrent native L0-L3 sampling;
- aggregate success-rate confidence intervals and P95 latency scoring;
- protocol assertions for `[DONE]`, finish reason, and reasoning-aware
  pseudo-streaming;
- deterministic exact-output quality checks.
- version-locked adapters for Promptfoo JSON v3, AIPerf summary schema 1.x,
  and EvalScope 1.9.0 reports;
- strict EvalScope Vendor Verifier policies for Kimi, K2, and MiniMax.
- a no-shell external runner launcher with executable/version allowlisting,
  suite digest verification, minimal environment injection, timeout/cancel
  process-group termination, bounded logs, secret redaction, and hashed artifacts.
- a replayable CanaryWorkflow domain core with immutable input hashes, ordered
  Activities, target-vs-workflow failure separation, bounded retries, stable
  idempotency keys, cancellation, optimistic journal appends, and crash resume.
- durable local workflow replay through SQLite plus the production PostgreSQL
  workflow/result/evidence/activity schema and compare-and-append function;
- content-addressed local evidence storage, manifests, retention/redaction
  policy, and raw-output removal before normalized results become durable.
- a pinned Temporal Python SDK 1.30.0 adapter with durable Journal Activities,
  deterministic event IDs/timers, manual domain retry, heartbeat-driven
  cancellation, live queries, target-failure continuation, and history replay.
- a concrete native Canary Activity delegate and local worker composition root:
  snapshot/digest validation, Activity-only Secret resolution, preflight/full
  suite execution, sanitized content-addressed evidence, exact outcome
  idempotency, immutable final results, and a no-secret start command.
- a psycopg 3.3.4 / PostgreSQL 16 metadata adapter with validated pooling,
  immutable snapshot lookup, run initialization, atomic journal projection,
  leased Activity execution, exact failure replay, immutable result/evidence
  repositories, background lease renewal, retry-budget-neutral BUSY handling,
  recursive secret constraints, least-privilege roles, and real integration tests;
- a PostgreSQL Temporal worker mode and secret-free production suite start contract.
- a React/FastAPI local visual console with bounded launch validation,
  ephemeral API keys, sanitized SQLite history, live polling, four-dimension
  scorecards, latency traces, token/throughput readouts, request evidence, six
  modality-specific components, and a persisted seven-stage live flow diagram.
- a LangChain Agent workbench with custom OpenAI-compatible `BaseChatModel`,
  Skill-scoped read-only `StructuredTool` registry, four-iteration bound,
  SQLite/PostgreSQL checkpointer, safe execution trace, and human-gated plans.

Deferred to the next slices:

- local tokenizer estimates and billing reconciliation dimensions;
- cloud object/native Secret Manager adapters, production container sandbox
  profiles, regional worker deployment, and
  external runner Activity delegates;
- provider capability suites for model tool-calling, JSON Schema and long context.

## Design and contribution contracts

- [`AGENTS.md`](AGENTS.md): the required Agent development loop and module boundaries.
- [`docs/open-source-integration.md`](docs/open-source-integration.md): what is reused from
  GitHub projects, what remains native, and how external runners are isolated.
- [`schemas/probe-suite.schema.json`](schemas/probe-suite.schema.json): the public suite
  document contract.
- [`docs/adr/002-replayable-canary-workflow.md`](docs/adr/002-replayable-canary-workflow.md):
  deterministic workflow semantics and the Temporal adapter boundary.
- [`docs/adr/003-durable-journal-and-evidence.md`](docs/adr/003-durable-journal-and-evidence.md):
  PostgreSQL/SQLite journal, immutable result, evidence and redaction boundaries.
- [`docs/adr/004-temporal-canary-adapter.md`](docs/adr/004-temporal-canary-adapter.md):
  pinned SDK, deterministic replay, retry ownership, query and cancellation rules.
- [`docs/adr/005-postgresql-runtime-adapter.md`](docs/adr/005-postgresql-runtime-adapter.md):
  pool, role, snapshot, CAS journal, lease, failure-replay and worker wiring.
- [`docs/adr/006-react-fastapi-langchain-control-plane.md`](docs/adr/006-react-fastapi-langchain-control-plane.md):
  React/FastAPI control plane and the evidence-preserving LangChain probe boundary.
- [`docs/adr/007-langchain-agent-tools-skills.md`](docs/adr/007-langchain-agent-tools-skills.md):
  Agent model, Tool/Skill registry, checkpointer, approval and credential boundaries.
- [`schemas/canary-workflow-input.schema.json`](schemas/canary-workflow-input.schema.json)
  and [`schemas/workflow-event.schema.json`](schemas/workflow-event.schema.json):
  immutable command and audit-event contracts.
- [`migrations/0001_core.sql`](migrations/0001_core.sql): the production
  PostgreSQL 16+ schema; [`schemas/evidence-manifest.schema.json`](schemas/evidence-manifest.schema.json):
  the object-store evidence contract.
- [`schemas/local-endpoint-snapshots.schema.json`](schemas/local-endpoint-snapshots.schema.json):
  the no-inline-secret local worker endpoint contract.

Implemented external result adapters cover Promptfoo JSON output v3, AIPerf
summary schema 1.x, and the exact EvalScope 1.9.0 report contract. They reject
incompatible schema/producer versions. Promptfoo omits raw model output,
AIPerf treats metric units as authoritative, and EvalScope imports aggregate
reports only—never prediction or review text.
