BEGIN;

CREATE TABLE lexsond.targets (
    target_id UUID PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('local', 'cloud')),
    provider_id TEXT,
    base_url TEXT NOT NULL CHECK (base_url !~ '[?#]' AND base_url !~ '://[^/]*@'),
    default_model TEXT NOT NULL DEFAULT '',
    credential_ref TEXT CHECK (
        credential_ref IS NULL OR (
            credential_ref ~ '^(vault|aws-secretsmanager|gcp-secretmanager|azure-keyvault)://[^/?#@]+(/[^?#]*)?$'
            AND credential_ref !~ '[?#]'
            AND credential_ref !~ '://[^/]*@'
        )
    ),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    archived_at TIMESTAMPTZ,
    CHECK (target_kind = 'cloud' OR credential_ref IS NULL)
);

CREATE TABLE lexsond.suites (
    suite_id UUID PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    latest_revision_id UUID NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    archived_at TIMESTAMPTZ
);

CREATE TABLE lexsond.suite_revisions (
    revision_id UUID PRIMARY KEY,
    suite_id UUID NOT NULL REFERENCES lexsond.suites(suite_id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    document_sha256 CHAR(64) NOT NULL CHECK (document_sha256 ~ '^[0-9a-f]{64}$'),
    document_json JSONB NOT NULL
        CHECK (jsonb_typeof(document_json) = 'object')
        CHECK (NOT lexsond.contains_forbidden_secret_key(document_json)),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (suite_id, revision_id),
    UNIQUE (suite_id, revision),
    UNIQUE (suite_id, document_sha256)
);

ALTER TABLE lexsond.suites
    ADD CONSTRAINT suites_latest_revision_fk
    FOREIGN KEY (suite_id, latest_revision_id)
    REFERENCES lexsond.suite_revisions(suite_id, revision_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE lexsond.probe_runs (
    run_id UUID PRIMARY KEY,
    idempotency_key UUID UNIQUE,
    request_sha256 CHAR(64) CHECK (
        request_sha256 IS NULL OR request_sha256 ~ '^[0-9a-f]{64}$'
    ),
    target_id UUID REFERENCES lexsond.targets(target_id) ON DELETE RESTRICT,
    suite_revision_id UUID REFERENCES lexsond.suite_revisions(revision_id) ON DELETE RESTRICT,
    run_kind TEXT NOT NULL CHECK (run_kind IN ('component', 'suite')),
    execution_backend TEXT NOT NULL CHECK (execution_backend IN ('local', 'temporal')),
    state TEXT NOT NULL CHECK (state IN ('RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')),
    result_status TEXT CHECK (result_status IN ('PASS', 'WARN', 'FAIL', 'UNKNOWN')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at TIMESTAMPTZ,
    base_url TEXT NOT NULL CHECK (base_url !~ '[?#]' AND base_url !~ '://[^/]*@'),
    model TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('local', 'cloud')),
    provider_id TEXT,
    run_mode TEXT NOT NULL CHECK (run_mode IN ('single', 'canary')),
    probe_type TEXT NOT NULL CHECK (probe_type IN (
        'chat', 'vision', 'embedding', 'image_generation',
        'audio_speech', 'audio_transcription'
    )),
    streaming BOOLEAN NOT NULL,
    timeout_seconds DOUBLE PRECISION NOT NULL CHECK (timeout_seconds > 0),
    result_json JSONB CHECK (
        result_json IS NULL OR (
            jsonb_typeof(result_json) = 'object'
            AND NOT lexsond.contains_forbidden_secret_key(result_json)
        )
    ),
    failure_code TEXT CHECK (
        failure_code IS NULL OR failure_code ~ '^[A-Z][A-Z0-9_]{2,127}$'
    ),
    cancel_requested_at TIMESTAMPTZ,
    workflow_json JSONB NOT NULL
        CHECK (jsonb_typeof(workflow_json) = 'object')
        CHECK (NOT lexsond.contains_forbidden_secret_key(workflow_json)),
    archived_at TIMESTAMPTZ,
    CHECK ((state = 'RUNNING') = (finished_at IS NULL)),
    CHECK ((state = 'COMPLETED') = (result_json IS NOT NULL)),
    CHECK ((idempotency_key IS NULL) = (request_sha256 IS NULL)),
    CHECK ((run_kind = 'suite') = (suite_revision_id IS NOT NULL))
);

CREATE INDEX idx_probe_runs_state_created
    ON lexsond.probe_runs (state, created_at DESC);
CREATE INDEX idx_probe_runs_target_created
    ON lexsond.probe_runs (target_id, created_at DESC);

CREATE TABLE lexsond.probe_run_events (
    run_id UUID NOT NULL REFERENCES lexsond.probe_runs(run_id) ON DELETE CASCADE,
    sequence BIGINT NOT NULL CHECK (sequence >= 1),
    event_id UUID NOT NULL UNIQUE,
    event_type TEXT NOT NULL CHECK (event_type ~ '^[A-Z][A-Z0-9_]{2,127}$'),
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    source_event_id UUID,
    event_json JSONB NOT NULL
        CHECK (jsonb_typeof(event_json) = 'object')
        CHECK (NOT lexsond.contains_forbidden_secret_key(event_json)),
    PRIMARY KEY (run_id, sequence),
    UNIQUE (run_id, source_event_id),
    CHECK ((event_json ->> 'run_id')::UUID = run_id),
    CHECK ((event_json ->> 'sequence')::BIGINT = sequence),
    CHECK ((event_json ->> 'event_id')::UUID = event_id),
    CHECK (event_json ->> 'event_type' = event_type),
    CHECK (event_json ->> 'phase' = phase),
    CHECK (event_json ->> 'status' = status),
    CHECK (
        source_event_id IS NULL
        OR (event_json ->> 'source_event_id')::UUID = source_event_id
    )
);

CREATE TRIGGER suite_revisions_are_immutable
BEFORE UPDATE ON lexsond.suite_revisions
FOR EACH ROW EXECUTE FUNCTION lexsond.reject_append_only_mutation();

REVOKE ALL ON lexsond.targets, lexsond.suites,
    lexsond.suite_revisions, lexsond.probe_runs,
    lexsond.probe_run_events FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE, DELETE ON lexsond.targets,
    lexsond.suites, lexsond.probe_runs TO lexsond_control;
GRANT SELECT, INSERT, DELETE ON lexsond.suite_revisions,
    lexsond.probe_run_events TO lexsond_control;
GRANT SELECT ON lexsond.targets, lexsond.suites,
    lexsond.suite_revisions, lexsond.probe_runs,
    lexsond.probe_run_events TO lexsond_reader;

COMMIT;
