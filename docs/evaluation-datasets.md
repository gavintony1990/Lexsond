# Evaluation datasets

## Deployment secret

Authenticated cloud deployments must inject
`LEXSOND_CREDENTIAL_BINDING_KEY` from the same Secret Manager into every web
worker. It must contain at least 32 printable characters. Lexsond uses it only
to HMAC-bind a catalog snapshot to the credential later used for execution;
the value is never stored or logged. Local single-user mode may use a
process-local key, which intentionally invalidates old catalog snapshots after
a restart.

Lexsond treats uploaded JSONL and CSV as untrusted workspace-private data. Use
**探测套件管理 → 评测数据集** to validate a file, inspect at most 20 preview
rows, confirm data rights and create an immutable revision. The canonical JSONL
shape is defined by
[`schemas/evaluation-dataset.schema.json`](../schemas/evaluation-dataset.schema.json).

## Limits

- 10 MiB upload, 10,000 rows, and a bounded expanded-character budget.
- At most 16 messages per item and 32 KiB per message.
- Metadata is limited to 8 KiB and nesting depth eight.
- CSV is UTF-8; the upload wizard maps six distinct source columns to
  `id,input,reference_answer,category,language,scorer`, and the server validates
  the mapping again before compilation.
- HTML, archives, executable objects, control characters, credential-shaped
  values, `Authorization`, and secret-bearing fields are rejected.
- Parse errors identify a line and field but do not echo the complete row.

The upload transaction creates the dataset, revision and every item together;
validation failures create no partial records. Re-uploading identical content
returns a duplicate-content conflict instead of silently copying the revision.

## Running QuickEval

1. Apply PostgreSQL migrations through `0011_evaluations.sql` and start the Web
   control plane.
2. Open **探测套件管理 → 评测数据集** and select **Lexsond QuickEval v1**.
3. Choose **使用该数据集评测**, then one channel, one temporary/saved
   credential, and one to ten models from that credential's fresh catalog.
4. Keep the default 20 seeded samples or choose a bounded strategy and budget.
5. Generate the budget preview, explicitly confirm unknown prices, and launch.
6. The result page shows reproducibility fields, category metrics, confidence
   intervals, Token/cost completeness, latency and per-item safe facts. It does
   not expose complete model answers.

The checked-in
[`datasets/lexsond-quickeval-v1.manifest.json`](../datasets/lexsond-quickeval-v1.manifest.json)
records provenance and the canonical content hash. External catalog records are
not bundled or downloaded automatically; their policy badge determines whether
they require import, license review, research-only use, or a dedicated runner.
External cards intentionally show “未固定导入版本” until an importer pins a
release/commit and content hash; a metadata review date is not a source version.

## Verification

The React contract tests run with `npm test -- --run`. Browser UI-contract flows are pinned
to `playwright@1.61.1` (Apache-2.0) and run with `npm run test:e2e`; artifacts
are written under `output/playwright/`. These fixtures mock `/api/v1` and exercise catalog policy
gating, JSONL upload, rights confirmation, a two-model bounded run, result
redaction, and empty browser storage without using a real credential or making
a billable provider call. PostgreSQL/API/SSE integration remains a separate
opt-in gate and must not be inferred from those mocked browser scenarios.
