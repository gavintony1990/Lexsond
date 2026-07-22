BEGIN;

ALTER TABLE lexsond.probe_runs
    ADD COLUMN credential_profile_id UUID,
    ADD COLUMN max_output_tokens INTEGER NOT NULL DEFAULT 64
        CHECK (max_output_tokens BETWEEN 1 AND 4096),
    ADD CONSTRAINT probe_runs_workspace_credential_fkey
        FOREIGN KEY (workspace_id, credential_profile_id)
        REFERENCES lexsond.credential_profiles(workspace_id, credential_id)
        ON DELETE SET NULL (credential_profile_id);

CREATE TABLE lexsond.model_catalog_snapshots (
    snapshot_id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL
        REFERENCES lexsond.workspaces(workspace_id) ON DELETE RESTRICT,
    target_id UUID NOT NULL,
    credential_profile_id UUID,
    target_version INTEGER NOT NULL CHECK (target_version >= 1),
    provider_id TEXT,
    models_json JSONB NOT NULL
        CHECK (jsonb_typeof(models_json) = 'array')
        CHECK (jsonb_array_length(models_json) <= 2000)
        CHECK (NOT lexsond.contains_forbidden_secret_key(models_json))
        CHECK (NOT lexsond.contains_recognizable_secret_value(models_json)),
    model_count INTEGER NOT NULL CHECK (model_count BETWEEN 0 AND 2000),
    status TEXT NOT NULL DEFAULT 'FRESH' CHECK (status IN ('FRESH', 'STALE')),
    content_sha256 CHAR(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    UNIQUE (workspace_id, snapshot_id),
    FOREIGN KEY (workspace_id, target_id)
        REFERENCES lexsond.targets(workspace_id, target_id) ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, credential_profile_id)
        REFERENCES lexsond.credential_profiles(workspace_id, credential_id)
        ON DELETE SET NULL (credential_profile_id),
    CHECK (model_count = jsonb_array_length(models_json)),
    CHECK (expires_at > fetched_at)
);

CREATE INDEX idx_catalog_snapshots_workspace_fetched
    ON lexsond.model_catalog_snapshots
    (workspace_id, target_id, fetched_at DESC, snapshot_id);

CREATE TABLE lexsond.probe_batches (
    batch_id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL
        REFERENCES lexsond.workspaces(workspace_id) ON DELETE RESTRICT,
    target_id UUID NOT NULL,
    credential_profile_id UUID,
    catalog_snapshot_id UUID NOT NULL,
    suite_revision_id UUID,
    mode TEXT NOT NULL CHECK (mode IN ('catalog_only', 'smoke', 'quality_suite')),
    state TEXT NOT NULL CHECK (state IN (
        'RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED', 'CANCELLED'
    )),
    model_count INTEGER NOT NULL CHECK (model_count BETWEEN 1 AND 10),
    max_concurrency INTEGER NOT NULL DEFAULT 2
        CHECK (max_concurrency BETWEEN 1 AND 2),
    max_output_tokens INTEGER NOT NULL CHECK (max_output_tokens BETWEEN 1 AND 64),
    timeout_seconds DOUBLE PRECISION NOT NULL CHECK (
        timeout_seconds > 0 AND timeout_seconds <= 120
    ),
    confirm_unknown_cost BOOLEAN NOT NULL DEFAULT FALSE,
    idempotency_key UUID,
    request_sha256 CHAR(64) CHECK (
        request_sha256 IS NULL OR request_sha256 ~ '^[0-9a-f]{64}$'
    ),
    cancel_requested_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at TIMESTAMPTZ,
    UNIQUE (workspace_id, batch_id),
    UNIQUE (workspace_id, idempotency_key),
    FOREIGN KEY (workspace_id, target_id)
        REFERENCES lexsond.targets(workspace_id, target_id) ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, credential_profile_id)
        REFERENCES lexsond.credential_profiles(workspace_id, credential_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, catalog_snapshot_id)
        REFERENCES lexsond.model_catalog_snapshots(workspace_id, snapshot_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, suite_revision_id)
        REFERENCES lexsond.suite_revisions(workspace_id, revision_id)
        ON DELETE RESTRICT,
    CHECK ((idempotency_key IS NULL) = (request_sha256 IS NULL)),
    CHECK ((mode = 'quality_suite') = (suite_revision_id IS NOT NULL)),
    CHECK ((state = 'RUNNING') = (finished_at IS NULL))
);

CREATE INDEX idx_probe_batches_workspace_created
    ON lexsond.probe_batches (workspace_id, created_at DESC, batch_id);

CREATE TABLE lexsond.probe_batch_items (
    item_id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    batch_id UUID NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 10),
    model_id TEXT NOT NULL CHECK (char_length(model_id) BETWEEN 1 AND 256)
        CHECK (NOT lexsond.contains_recognizable_secret_value(to_jsonb(model_id))),
    state TEXT NOT NULL CHECK (state IN (
        'PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED', 'SKIPPED'
    )),
    run_id UUID,
    failure_code TEXT CHECK (
        failure_code IS NULL OR failure_code ~ '^[A-Z][A-Z0-9_]{2,127}$'
    ),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    UNIQUE (workspace_id, item_id),
    UNIQUE (workspace_id, batch_id, ordinal),
    UNIQUE (workspace_id, batch_id, model_id),
    UNIQUE (workspace_id, run_id),
    FOREIGN KEY (workspace_id, batch_id)
        REFERENCES lexsond.probe_batches(workspace_id, batch_id) ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, run_id)
        REFERENCES lexsond.probe_runs(workspace_id, run_id) ON DELETE RESTRICT,
    CHECK ((state = 'PENDING') = (started_at IS NULL)),
    CHECK ((state IN ('PENDING', 'RUNNING')) = (finished_at IS NULL))
);

CREATE INDEX idx_probe_batch_items_dispatch
    ON lexsond.probe_batch_items (workspace_id, batch_id, state, ordinal);

CREATE TABLE lexsond.probe_batch_events (
    batch_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    sequence BIGINT NOT NULL CHECK (sequence >= 1),
    event_id UUID NOT NULL UNIQUE,
    event_type TEXT NOT NULL CHECK (event_type ~ '^[A-Z][A-Z0-9_]{2,127}$'),
    item_id UUID,
    model_id TEXT,
    state TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (batch_id, sequence),
    FOREIGN KEY (workspace_id, batch_id)
        REFERENCES lexsond.probe_batches(workspace_id, batch_id) ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, item_id)
        REFERENCES lexsond.probe_batch_items(workspace_id, item_id) ON DELETE RESTRICT
);

CREATE INDEX idx_probe_batch_events_workspace_batch
    ON lexsond.probe_batch_events (workspace_id, batch_id, sequence);

REVOKE ALL ON lexsond.model_catalog_snapshots, lexsond.probe_batches,
    lexsond.probe_batch_items, lexsond.probe_batch_events FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON lexsond.model_catalog_snapshots,
    lexsond.probe_batches, lexsond.probe_batch_items TO lexsond_control;
GRANT SELECT, INSERT ON lexsond.probe_batch_events TO lexsond_control;
GRANT SELECT ON lexsond.model_catalog_snapshots, lexsond.probe_batches,
    lexsond.probe_batch_items, lexsond.probe_batch_events TO lexsond_reader;

COMMIT;
