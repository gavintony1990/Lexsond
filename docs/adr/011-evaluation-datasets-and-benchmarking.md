# ADR-011: Versioned evaluation datasets and deterministic benchmarking

Status: accepted, implemented after the workspace-tenancy baseline.

## Context

The native probe measures one provider request or one bounded `ProbeSuite`.
Those contracts intentionally mix neither a row-oriented corpus nor benchmark
accuracy into availability, protocol, or performance scores. A reproducible
multi-model evaluation needs a separate domain with immutable content,
deterministic sampling, explicit license policy, exact call accounting, and
workspace authorization.

## Decision

- Evaluation datasets are either read-only `SYSTEM` catalog records or private
  `WORKSPACE` records. Content changes always create a new immutable revision;
  historical runs retain their exact revision.
- PostgreSQL remains the only structured persistent-memory tool. The product
  requirement mentioning a new SQLite migration is intentionally not applied,
  because ADR-009 removed that runtime and its compatibility paths. Migration
  `0011_evaluations.sql` provides the PostgreSQL contract.
- `Lexsond QuickEval v1` contains 80 project-original text items under
  Apache-2.0. Its checked-in provenance manifest fixes the schema version,
  category counts, authorship statement, and canonical content SHA-256.
- External benchmark rows are metadata only. They do not grant redistribution
  rights and cannot be launched until their `distribution_policy` is satisfied.
- Seven versioned, code-registered scorers are deterministic. Users cannot
  upload executable scorers, and no LLM-as-Judge call exists in this version.
- An `EvaluationCoordinator` samples `first`, seeded `random`, or seeded
  `stratified` item IDs using the versioned `sha256-rank/v1` algorithm (not
  Python's version-dependent PRNG), caps models at 10, samples at 200, concurrency at two,
  output at 1,024 tokens, and timeout at 120 seconds. Each model/item call has
  one attempt. When source pricing is unknown, the USD value is an explicit
  user declaration, not an enforceable runtime ceiling; calls, output tokens,
  concurrency, timeout, cancellation, and exact-call limits remain enforced.
- Native transport remains the observation boundary. The frozen
  `lexsond-messages/v2-native-roles` contract sends the bounded system/user/
  assistant sequence without flattening roles and appends multiple-choice
  labels once to the final user message, with temperature zero. The dataset
  revision, chosen item IDs, template ID, scorer versions, target version,
  catalog snapshot, and limits are stored in the request snapshot.
- A temporary API key exists only in the HTTP request and local execution
  closure. A saved credential is represented by its non-secret profile ID and
  resolved only at execution. Neither is placed in snapshots, events, errors,
  or model results.
- Normal result tables keep the output SHA-256, deterministic score, safe facts,
  usage, cost completeness, and timing. Full model output is not retained.
- Item and event inserts are compare-and-append idempotent. Replaying an item
  cannot increment model totals twice, and conflicting evidence is rejected.
  Workers renew a fenced five-minute execution lease; a bounded 30-second
  maintenance scan marks only expired work `EXECUTION_LEASE_EXPIRED`. It never
  guesses which billable calls completed or silently retries them.
- Catalog-to-run credential binding uses a workspace- and purpose-scoped HMAC
  key supplied as
  `LEXSOND_CREDENTIAL_BINDING_KEY`. Authenticated cloud mode fails closed when
  it is absent; every web worker must receive the same value from a Secret
  Manager. The binding key and API credentials never enter PostgreSQL or logs;
  the secret-derived binding column is excluded from the reader-role projection.

## License catalog decision

The catalog metadata was rechecked against official project sources on
2026-07-22:

| Dataset | Recorded policy | Evidence used |
| --- | --- | --- |
| MMLU-Pro | `IMPORT_REQUIRED`, MIT | Hugging Face project dataset card |
| BIG-bench Lite | `IMPORT_REQUIRED`, Apache-2.0 | Google BIG-bench repository and license; task-level sources still require review |
| IFEval | `LICENSE_REVIEW` | Google Research repository; repository-wide code/data defaults are insufficient to assert every extracted artifact's terms |
| HumanEval | `RUNNER_REQUIRED`, MIT | OpenAI HumanEval repository and license; generated code requires a robust sandbox |
| C-Eval | `RESEARCH_ONLY`, CC BY-NC-SA 4.0 data | Official C-Eval repository and data-license notice |

No importer accepts arbitrary URLs. A future importer must pin a source version,
verify a content hash, preserve the specific license, and pass a security review.

## Failure and comparison semantics

- Missing or unparseable scoring evidence is `UNKNOWN`, not an invented zero.
- A catalog-visible model is not assumed callable, and a successful response is
  not proof of model identity.
- `401` and payment/`402` stop new work for the run. A per-model `403`, `404`, `429`, protocol
  failure, cancellation, or budget stop is retained as explicit evidence; no
  automatic retry is added.
- Scores may be compared only inside a run whose dataset revision, item IDs,
  prompt template, parameters, and scorer versions are identical. The UI does
  not generate a cross-run ranking when those conditions differ.
- Evaluation accuracy is never merged into native probe availability, protocol,
  performance, or quality dimensions.

## Consequences and deferred work

The first implementation executes locally and streams durable PostgreSQL events.
A web-control-plane bootstrap idempotently compiles and seeds QuickEval after
the schema migration; the migration intentionally contains no generated prompt
content. Therefore a newly migrated database exposes the system catalog only
after the control plane has completed this guarded bootstrap.
A catalog-only external record has no `source_version`; the verification date
is not presented as a pinned source revision, and import remains blocked until
a commit/release and content hash are fixed.
A Temporal adapter may later reuse the same frozen command, but must not place a
credential in Workflow History or change exact-call semantics. Sandboxed
HumanEval execution, LLM-as-Judge, public leaderboards, public dataset sharing,
multimodal datasets, and server-side URL import remain out of scope.
