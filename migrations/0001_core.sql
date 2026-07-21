BEGIN;

CREATE SCHEMA IF NOT EXISTS lexsond;

CREATE OR REPLACE FUNCTION lexsond.contains_forbidden_secret_key(p_value JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    child JSONB;
BEGIN
    IF jsonb_typeof(p_value) = 'object' THEN
        IF p_value ?| ARRAY[
            'api_key', 'authorization', 'credential', 'credential_handle',
            'credential_ref', 'access_token', 'refresh_token', 'secret'
        ] THEN
            RETURN TRUE;
        END IF;
        FOR child IN SELECT value FROM jsonb_each(p_value) LOOP
            IF lexsond.contains_forbidden_secret_key(child) THEN
                RETURN TRUE;
            END IF;
        END LOOP;
    ELSIF jsonb_typeof(p_value) = 'array' THEN
        FOR child IN SELECT value FROM jsonb_array_elements(p_value) LOOP
            IF lexsond.contains_forbidden_secret_key(child) THEN
                RETURN TRUE;
            END IF;
        END LOOP;
    END IF;
    RETURN FALSE;
END;
$$;

CREATE TABLE lexsond.endpoint_snapshots (
    endpoint_snapshot_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    protocol TEXT NOT NULL CHECK (protocol IN ('openai-chat', 'anthropic-messages')),
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    credential_ref TEXT NOT NULL,
    configuration_sha256 CHAR(64) NOT NULL
        CHECK (configuration_sha256 ~ '^[0-9a-f]{64}$'),
    configuration_json JSONB NOT NULL
        CHECK (jsonb_typeof(configuration_json) = 'object')
        CHECK (NOT lexsond.contains_forbidden_secret_key(configuration_json)),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (base_url ~ '^https://[^/?#@]+(/[^?#]*)?$'),
    CHECK (base_url !~ '[?#]'),
    CHECK (base_url !~ '://[^/]*@'),
    CHECK (credential_ref ~ '^(vault|aws-secretsmanager|gcp-secretmanager|azure-keyvault)://[^/?#@]+(/[^?#]*)?$'),
    CHECK (credential_ref !~ '[?#]'),
    CHECK (credential_ref !~ '://[^/]*@')
);

CREATE TABLE lexsond.probe_suite_snapshots (
    suite_sha256 CHAR(64) PRIMARY KEY CHECK (suite_sha256 ~ '^[0-9a-f]{64}$'),
    suite_name TEXT NOT NULL,
    suite_version TEXT NOT NULL,
    suite_uri TEXT NOT NULL,
    suite_json JSONB NOT NULL
        CHECK (jsonb_typeof(suite_json) = 'object')
        CHECK (NOT lexsond.contains_forbidden_secret_key(suite_json)),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (suite_name, suite_version, suite_sha256),
    UNIQUE (suite_uri),
    CHECK (suite_uri ~ '^(https|s3)://[^/?#@]+(/[^?#]*)?$'),
    CHECK (suite_uri !~ '[?#]'),
    CHECK (suite_uri !~ '://[^/]*@')
);

CREATE TABLE lexsond.workflow_runs (
    run_id UUID PRIMARY KEY,
    workflow_api_version TEXT NOT NULL
        CHECK (workflow_api_version = 'probe.ai/workflow/v1alpha1'),
    workflow_kind TEXT NOT NULL CHECK (workflow_kind = 'CanaryWorkflowInput'),
    workflow_input_sha256 CHAR(64) NOT NULL
        CHECK (workflow_input_sha256 ~ '^[0-9a-f]{64}$'),
    workflow_input_json JSONB NOT NULL
        CHECK (jsonb_typeof(workflow_input_json) = 'object')
        CHECK (NOT lexsond.contains_forbidden_secret_key(workflow_input_json)),
    endpoint_snapshot_id TEXT NOT NULL
        REFERENCES lexsond.endpoint_snapshots(endpoint_snapshot_id) ON DELETE RESTRICT,
    suite_sha256 CHAR(64) NOT NULL
        REFERENCES lexsond.probe_suite_snapshots(suite_sha256) ON DELETE RESTRICT,
    region TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'RUNNING'
        CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'REJECTED', 'CANCELLED')),
    phase TEXT NOT NULL DEFAULT 'NONE'
        CHECK (phase IN (
            'NONE', 'VALIDATE', 'PREFLIGHT', 'EXECUTE', 'NORMALIZE',
            'SCORE', 'PERSIST', 'COMPARE', 'NOTIFY', 'COMPLETE'
        )),
    target_failed_seen BOOLEAN NOT NULL DEFAULT FALSE,
    last_sequence BIGINT NOT NULL DEFAULT 0 CHECK (last_sequence >= 0),
    last_result_ref TEXT,
    terminal_error_code TEXT CHECK (
        terminal_error_code IS NULL OR terminal_error_code ~ '^[A-Z][A-Z0-9_]{2,127}$'
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    CHECK (
        (status IN ('SUCCEEDED', 'FAILED', 'REJECTED', 'CANCELLED'))
        = (completed_at IS NOT NULL)
    )
);

CREATE INDEX idx_workflow_runs_status_updated
    ON lexsond.workflow_runs (status, updated_at DESC);
CREATE INDEX idx_workflow_runs_endpoint_created
    ON lexsond.workflow_runs (endpoint_snapshot_id, created_at DESC);

CREATE TABLE lexsond.workflow_events (
    run_id UUID NOT NULL
        REFERENCES lexsond.workflow_runs(run_id) ON DELETE RESTRICT,
    sequence BIGINT NOT NULL CHECK (sequence >= 1),
    event_id UUID NOT NULL UNIQUE,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'WORKFLOW_STARTED', 'ACTIVITY_STARTED', 'ACTIVITY_ATTEMPT_FAILED',
        'ACTIVITY_COMPLETED', 'WORKFLOW_SUCCEEDED', 'WORKFLOW_FAILED',
        'WORKFLOW_REJECTED', 'WORKFLOW_CANCELLED'
    )),
    phase TEXT NOT NULL CHECK (phase IN (
        'NONE', 'VALIDATE', 'PREFLIGHT', 'EXECUTE', 'NORMALIZE',
        'SCORE', 'PERSIST', 'COMPARE', 'NOTIFY', 'COMPLETE'
    )),
    occurred_at TIMESTAMPTZ NOT NULL,
    event_json JSONB NOT NULL
        CHECK (jsonb_typeof(event_json) = 'object')
        CHECK (NOT lexsond.contains_forbidden_secret_key(event_json)),
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, sequence),
    CHECK ((event_json ->> 'run_id')::UUID = run_id),
    CHECK ((event_json ->> 'sequence')::BIGINT = sequence),
    CHECK ((event_json ->> 'event_id')::UUID = event_id),
    CHECK (event_json ->> 'event_type' = event_type),
    CHECK (event_json ->> 'phase' = phase)
);

CREATE INDEX idx_workflow_events_type_time
    ON lexsond.workflow_events (event_type, occurred_at DESC);

CREATE TABLE lexsond.probe_results (
    run_id UUID PRIMARY KEY
        REFERENCES lexsond.workflow_runs(run_id) ON DELETE RESTRICT,
    result_ref TEXT NOT NULL,
    result_schema_version TEXT NOT NULL
        CHECK (result_schema_version = 'probe.ai/result/v1alpha1'),
    result_status TEXT NOT NULL
        CHECK (result_status IN ('PASS', 'WARN', 'FAIL', 'UNKNOWN')),
    result_sha256 CHAR(64) NOT NULL CHECK (result_sha256 ~ '^[0-9a-f]{64}$'),
    normalized_result JSONB NOT NULL
        CHECK (jsonb_typeof(normalized_result) = 'object')
        CHECK (NOT lexsond.contains_forbidden_secret_key(normalized_result)),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (normalized_result ->> 'schema_version' = result_schema_version),
    CHECK (normalized_result ->> 'status' = result_status),
    CHECK ((normalized_result ->> 'run_id')::UUID = run_id)
);

CREATE TABLE lexsond.evidence_objects (
    evidence_id UUID PRIMARY KEY,
    run_id UUID NOT NULL
        REFERENCES lexsond.workflow_runs(run_id) ON DELETE RESTRICT,
    evidence_kind TEXT NOT NULL CHECK (evidence_kind IN (
        'NORMALIZED_RESULT', 'REQUEST_TIMELINE', 'RUNNER_REPORT',
        'RUNNER_REVIEW', 'RUNNER_LOG', 'BILLING_SNAPSHOT'
    )),
    object_uri TEXT NOT NULL CHECK (object_uri !~ '[?#]'),
    object_sha256 CHAR(64) NOT NULL CHECK (object_sha256 ~ '^[0-9a-f]{64}$'),
    byte_size BIGINT NOT NULL CHECK (byte_size >= 0),
    media_type TEXT NOT NULL,
    redaction_status TEXT NOT NULL CHECK (redaction_status IN (
        'NOT_REQUIRED', 'SANITIZED', 'RAW_RESTRICTED'
    )),
    encrypted BOOLEAN NOT NULL,
    retention_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    deleted_at TIMESTAMPTZ,
    UNIQUE (run_id, evidence_kind, object_sha256, object_uri),
    CHECK (deleted_at IS NULL OR deleted_at >= created_at),
    CHECK (
        evidence_kind NOT IN ('REQUEST_TIMELINE', 'RUNNER_REVIEW', 'RUNNER_LOG')
        OR retention_until IS NOT NULL
    ),
    CHECK (
        redaction_status <> 'RAW_RESTRICTED'
        OR (encrypted = TRUE AND retention_until IS NOT NULL)
    )
);

CREATE INDEX idx_evidence_retention
    ON lexsond.evidence_objects (retention_until)
    WHERE deleted_at IS NULL AND retention_until IS NOT NULL;

CREATE TABLE lexsond.activity_executions (
    idempotency_key TEXT PRIMARY KEY,
    run_id UUID NOT NULL
        REFERENCES lexsond.workflow_runs(run_id) ON DELETE RESTRICT,
    activity_name TEXT NOT NULL,
    input_ref TEXT,
    status TEXT NOT NULL CHECK (status IN ('LEASED', 'SUCCEEDED', 'FAILED')),
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    outcome_status TEXT CHECK (outcome_status IN ('SUCCEEDED', 'TARGET_FAILED')),
    result_ref TEXT,
    error_code TEXT CHECK (
        error_code IS NULL OR error_code ~ '^[A-Z][A-Z0-9_]{2,127}$'
    ),
    failure_kind TEXT CHECK (failure_kind IN (
        'CONFIGURATION', 'POLICY', 'INFRASTRUCTURE', 'RUNNER'
    )),
    retryable BOOLEAN,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        (status = 'LEASED' AND lease_token IS NOT NULL
            AND lease_expires_at IS NOT NULL
            AND outcome_status IS NULL AND result_ref IS NULL
            AND error_code IS NULL AND failure_kind IS NULL AND retryable IS NULL)
        OR (status = 'SUCCEEDED' AND lease_token IS NULL
            AND lease_expires_at IS NULL
            AND outcome_status IS NOT NULL AND result_ref IS NOT NULL
            AND error_code IS NULL AND failure_kind IS NULL AND retryable IS NULL)
        OR (status = 'FAILED' AND lease_token IS NULL
            AND lease_expires_at IS NULL
            AND outcome_status IS NULL AND result_ref IS NULL
            AND error_code IS NOT NULL AND failure_kind IS NOT NULL
            AND retryable IS NOT NULL)
    )
);

CREATE INDEX idx_activity_lease_expiry
    ON lexsond.activity_executions (lease_expires_at)
    WHERE status = 'LEASED';

CREATE OR REPLACE FUNCTION lexsond.create_workflow_run(
    p_run_id UUID,
    p_workflow_input_sha256 CHAR(64),
    p_workflow_input JSONB
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = lexsond, pg_temp
AS $$
DECLARE
    existing_sha CHAR(64);
    existing_input JSONB;
    suite_record RECORD;
BEGIN
    IF p_run_id IS DISTINCT FROM (p_workflow_input ->> 'run_id')::UUID THEN
        RAISE EXCEPTION 'workflow run_id does not match input'
            USING ERRCODE = '22023';
    END IF;
    SELECT suite_name, suite_version, suite_uri
    INTO suite_record
    FROM lexsond.probe_suite_snapshots
    WHERE suite_sha256 = p_workflow_input ->> 'suite_sha256';
    IF NOT FOUND
       OR suite_record.suite_name IS DISTINCT FROM p_workflow_input ->> 'suite_name'
       OR suite_record.suite_version IS DISTINCT FROM p_workflow_input ->> 'suite_version'
       OR suite_record.suite_uri IS DISTINCT FROM p_workflow_input ->> 'suite_uri' THEN
        RAISE EXCEPTION 'workflow suite fields do not match the immutable snapshot'
            USING ERRCODE = '23503';
    END IF;

    INSERT INTO lexsond.workflow_runs (
        run_id, workflow_api_version, workflow_kind, workflow_input_sha256,
        workflow_input_json, endpoint_snapshot_id, suite_sha256, region
    ) VALUES (
        p_run_id, p_workflow_input ->> 'api_version', p_workflow_input ->> 'kind',
        p_workflow_input_sha256, p_workflow_input,
        p_workflow_input ->> 'endpoint_snapshot_id',
        p_workflow_input ->> 'suite_sha256', p_workflow_input ->> 'region'
    )
    ON CONFLICT (run_id) DO NOTHING;

    SELECT workflow_input_sha256, workflow_input_json
    INTO existing_sha, existing_input
    FROM lexsond.workflow_runs
    WHERE run_id = p_run_id;

    IF existing_sha IS DISTINCT FROM p_workflow_input_sha256
       OR existing_input IS DISTINCT FROM p_workflow_input THEN
        RAISE EXCEPTION 'workflow run_id already belongs to another input snapshot'
            USING ERRCODE = '23505';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION lexsond.append_workflow_event(
    p_run_id UUID,
    p_expected_sequence BIGINT,
    p_event JSONB
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = lexsond, pg_temp
AS $$
DECLARE
    updated_count INTEGER;
    next_sequence BIGINT;
    requested_event_type TEXT;
BEGIN
    next_sequence := p_expected_sequence + 1;
    requested_event_type := p_event ->> 'event_type';
    IF (p_event ->> 'run_id')::UUID IS DISTINCT FROM p_run_id
       OR (p_event ->> 'sequence')::BIGINT IS DISTINCT FROM next_sequence THEN
        RAISE EXCEPTION 'event identity does not match append arguments'
            USING ERRCODE = '22023';
    END IF;
    IF (p_expected_sequence = 0 AND requested_event_type <> 'WORKFLOW_STARTED')
       OR (p_expected_sequence > 0 AND requested_event_type = 'WORKFLOW_STARTED') THEN
        RAISE EXCEPTION 'WORKFLOW_STARTED must occupy only the first event slot'
            USING ERRCODE = '22023';
    END IF;
    IF requested_event_type = 'ACTIVITY_COMPLETED'
       AND (
           p_event ->> 'result_ref' IS NULL
           OR p_event ->> 'outcome_status' NOT IN ('SUCCEEDED', 'TARGET_FAILED')
       ) THEN
        RAISE EXCEPTION 'completed Activity event lacks a valid outcome'
            USING ERRCODE = '22023';
    END IF;
    IF requested_event_type IN (
        'WORKFLOW_FAILED', 'WORKFLOW_REJECTED', 'WORKFLOW_CANCELLED'
    ) AND p_event ->> 'error_code' IS NULL THEN
        RAISE EXCEPTION 'terminal failure event lacks error_code'
            USING ERRCODE = '22023';
    END IF;

    IF EXISTS (
        SELECT 1 FROM lexsond.workflow_events
        WHERE run_id = p_run_id AND sequence = next_sequence
          AND event_id = (p_event ->> 'event_id')::UUID
          AND event_json = p_event
    ) THEN
        RETURN;
    END IF;

    UPDATE lexsond.workflow_runs
    SET last_sequence = next_sequence,
        status = CASE requested_event_type
            WHEN 'WORKFLOW_SUCCEEDED' THEN 'SUCCEEDED'
            WHEN 'WORKFLOW_FAILED' THEN 'FAILED'
            WHEN 'WORKFLOW_REJECTED' THEN 'REJECTED'
            WHEN 'WORKFLOW_CANCELLED' THEN 'CANCELLED'
            ELSE status
        END,
        phase = CASE
            WHEN requested_event_type IN (
                'WORKFLOW_SUCCEEDED', 'WORKFLOW_FAILED',
                'WORKFLOW_REJECTED', 'WORKFLOW_CANCELLED'
            ) THEN 'COMPLETE'
            ELSE p_event ->> 'phase'
        END,
        target_failed_seen = target_failed_seen OR (
            requested_event_type = 'ACTIVITY_COMPLETED'
            AND p_event ->> 'outcome_status' = 'TARGET_FAILED'
        ),
        last_result_ref = CASE
            WHEN requested_event_type = 'ACTIVITY_COMPLETED'
                THEN p_event ->> 'result_ref'
            ELSE last_result_ref
        END,
        terminal_error_code = CASE
            WHEN requested_event_type IN (
                'WORKFLOW_FAILED', 'WORKFLOW_REJECTED', 'WORKFLOW_CANCELLED'
            ) THEN p_event ->> 'error_code'
            ELSE terminal_error_code
        END,
        updated_at = clock_timestamp(),
        completed_at = CASE
            WHEN requested_event_type IN (
                'WORKFLOW_SUCCEEDED', 'WORKFLOW_FAILED',
                'WORKFLOW_REJECTED', 'WORKFLOW_CANCELLED'
            )
                THEN COALESCE(completed_at, clock_timestamp())
            ELSE NULL
        END
    WHERE run_id = p_run_id
      AND last_sequence = p_expected_sequence
      AND status = 'RUNNING';

    GET DIAGNOSTICS updated_count = ROW_COUNT;
    IF updated_count <> 1 THEN
        IF EXISTS (
            SELECT 1 FROM lexsond.workflow_events
            WHERE run_id = p_run_id AND sequence = next_sequence
              AND event_id = (p_event ->> 'event_id')::UUID
              AND event_json = p_event
        ) THEN
            RETURN;
        END IF;
        RAISE EXCEPTION 'workflow journal compare-and-append conflict'
            USING ERRCODE = '40001';
    END IF;

    INSERT INTO lexsond.workflow_events (
        run_id, sequence, event_id, event_type, phase, occurred_at, event_json
    ) VALUES (
        p_run_id, next_sequence, (p_event ->> 'event_id')::UUID,
        p_event ->> 'event_type', p_event ->> 'phase',
        (p_event ->> 'occurred_at')::TIMESTAMPTZ, p_event
    );
END;
$$;

CREATE OR REPLACE FUNCTION lexsond.claim_activity_execution(
    p_idempotency_key TEXT,
    p_run_id UUID,
    p_activity_name TEXT,
    p_input_ref TEXT,
    p_attempt INTEGER,
    p_lease_token UUID,
    p_lease_seconds INTEGER
) RETURNS TABLE (
    disposition TEXT,
    returned_lease_token UUID,
    outcome_status TEXT,
    result_ref TEXT,
    error_code TEXT,
    failure_kind TEXT,
    retryable BOOLEAN,
    retry_after_seconds DOUBLE PRECISION
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = lexsond, pg_temp
AS $$
DECLARE
    execution lexsond.activity_executions%ROWTYPE;
BEGIN
    IF p_attempt < 1 OR p_lease_seconds < 1 OR p_lease_seconds > 1800 THEN
        RAISE EXCEPTION 'invalid Activity attempt or lease duration'
            USING ERRCODE = '22023';
    END IF;

    INSERT INTO lexsond.activity_executions (
        idempotency_key, run_id, activity_name, input_ref, status, attempt,
        lease_token, lease_expires_at
    ) VALUES (
        p_idempotency_key, p_run_id, p_activity_name, p_input_ref, 'LEASED',
        p_attempt, p_lease_token,
        clock_timestamp() + make_interval(secs => p_lease_seconds)
    ) ON CONFLICT (idempotency_key) DO NOTHING;

    SELECT * INTO execution
    FROM lexsond.activity_executions
    WHERE idempotency_key = p_idempotency_key
    FOR UPDATE;

    IF execution.run_id IS DISTINCT FROM p_run_id
       OR execution.activity_name IS DISTINCT FROM p_activity_name
       OR execution.input_ref IS DISTINCT FROM p_input_ref THEN
        RAISE EXCEPTION 'idempotency key belongs to another Activity invocation'
            USING ERRCODE = '55000';
    END IF;
    IF p_attempt < execution.attempt THEN
        RAISE EXCEPTION 'Activity attempt moved backwards'
            USING ERRCODE = '55000';
    END IF;
    IF execution.status = 'SUCCEEDED' THEN
        RETURN QUERY SELECT 'COMPLETED', NULL::UUID, execution.outcome_status,
            execution.result_ref, NULL::TEXT, NULL::TEXT, NULL::BOOLEAN,
            NULL::DOUBLE PRECISION;
        RETURN;
    END IF;
    IF execution.status = 'FAILED' AND p_attempt = execution.attempt THEN
        RETURN QUERY SELECT 'FAILED', NULL::UUID, NULL::TEXT, NULL::TEXT,
            execution.error_code, execution.failure_kind, execution.retryable,
            NULL::DOUBLE PRECISION;
        RETURN;
    END IF;
    IF execution.status = 'LEASED'
       AND execution.lease_token = p_lease_token THEN
        RETURN QUERY SELECT 'ACQUIRED', p_lease_token, NULL::TEXT, NULL::TEXT,
            NULL::TEXT, NULL::TEXT, NULL::BOOLEAN, NULL::DOUBLE PRECISION;
        RETURN;
    END IF;
    IF execution.status = 'LEASED'
       AND execution.lease_expires_at > clock_timestamp() THEN
        RETURN QUERY SELECT 'BUSY', NULL::UUID, NULL::TEXT, NULL::TEXT,
            NULL::TEXT, NULL::TEXT, NULL::BOOLEAN,
            GREATEST(
                EXTRACT(EPOCH FROM execution.lease_expires_at - clock_timestamp()),
                0.001
            )::DOUBLE PRECISION;
        RETURN;
    END IF;

    UPDATE lexsond.activity_executions
    SET status = 'LEASED', attempt = p_attempt, lease_token = p_lease_token,
        lease_expires_at = clock_timestamp() + make_interval(secs => p_lease_seconds),
        outcome_status = NULL, result_ref = NULL, error_code = NULL,
        failure_kind = NULL, retryable = NULL, updated_at = clock_timestamp()
    WHERE idempotency_key = p_idempotency_key;
    RETURN QUERY SELECT 'ACQUIRED', p_lease_token, NULL::TEXT, NULL::TEXT,
        NULL::TEXT, NULL::TEXT, NULL::BOOLEAN, NULL::DOUBLE PRECISION;
END;
$$;

CREATE OR REPLACE FUNCTION lexsond.renew_activity_execution(
    p_idempotency_key TEXT,
    p_run_id UUID,
    p_activity_name TEXT,
    p_input_ref TEXT,
    p_attempt INTEGER,
    p_lease_token UUID,
    p_lease_seconds INTEGER
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = lexsond, pg_temp
AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    IF p_lease_seconds < 1 OR p_lease_seconds > 1800 THEN
        RAISE EXCEPTION 'invalid Activity lease duration' USING ERRCODE = '22023';
    END IF;
    UPDATE lexsond.activity_executions
    SET lease_expires_at = clock_timestamp() + make_interval(secs => p_lease_seconds),
        updated_at = clock_timestamp()
    WHERE idempotency_key = p_idempotency_key
      AND run_id = p_run_id
      AND activity_name = p_activity_name
      AND input_ref IS NOT DISTINCT FROM p_input_ref
      AND status = 'LEASED'
      AND attempt = p_attempt
      AND lease_token = p_lease_token;
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    IF updated_count <> 1 THEN
        RAISE EXCEPTION 'Activity lease no longer belongs to this execution'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION lexsond.complete_activity_execution(
    p_idempotency_key TEXT,
    p_run_id UUID,
    p_activity_name TEXT,
    p_input_ref TEXT,
    p_attempt INTEGER,
    p_lease_token UUID,
    p_outcome_status TEXT,
    p_result_ref TEXT
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = lexsond, pg_temp
AS $$
DECLARE
    execution lexsond.activity_executions%ROWTYPE;
BEGIN
    SELECT * INTO execution FROM lexsond.activity_executions
    WHERE idempotency_key = p_idempotency_key FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Activity execution does not exist' USING ERRCODE = '55000';
    END IF;
    IF execution.run_id IS DISTINCT FROM p_run_id
       OR execution.activity_name IS DISTINCT FROM p_activity_name
       OR execution.input_ref IS DISTINCT FROM p_input_ref THEN
        RAISE EXCEPTION 'idempotency key belongs to another Activity invocation'
            USING ERRCODE = '55000';
    END IF;
    IF execution.status = 'SUCCEEDED'
       AND execution.outcome_status = p_outcome_status
       AND execution.result_ref = p_result_ref THEN
        RETURN;
    END IF;
    IF execution.status <> 'LEASED' OR execution.attempt <> p_attempt
       OR execution.lease_token <> p_lease_token THEN
        RAISE EXCEPTION 'Activity lease no longer belongs to this execution'
            USING ERRCODE = '55000';
    END IF;
    UPDATE lexsond.activity_executions
    SET status = 'SUCCEEDED', lease_token = NULL, lease_expires_at = NULL,
        outcome_status = p_outcome_status, result_ref = p_result_ref,
        error_code = NULL, failure_kind = NULL, retryable = NULL,
        updated_at = clock_timestamp()
    WHERE idempotency_key = p_idempotency_key;
END;
$$;

CREATE OR REPLACE FUNCTION lexsond.fail_activity_execution(
    p_idempotency_key TEXT,
    p_run_id UUID,
    p_activity_name TEXT,
    p_input_ref TEXT,
    p_attempt INTEGER,
    p_lease_token UUID,
    p_error_code TEXT,
    p_failure_kind TEXT,
    p_retryable BOOLEAN
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = lexsond, pg_temp
AS $$
DECLARE
    execution lexsond.activity_executions%ROWTYPE;
BEGIN
    SELECT * INTO execution FROM lexsond.activity_executions
    WHERE idempotency_key = p_idempotency_key FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Activity execution does not exist' USING ERRCODE = '55000';
    END IF;
    IF execution.run_id IS DISTINCT FROM p_run_id
       OR execution.activity_name IS DISTINCT FROM p_activity_name
       OR execution.input_ref IS DISTINCT FROM p_input_ref THEN
        RAISE EXCEPTION 'idempotency key belongs to another Activity invocation'
            USING ERRCODE = '55000';
    END IF;
    IF execution.status = 'FAILED' AND execution.attempt = p_attempt
       AND execution.error_code = p_error_code
       AND execution.failure_kind = p_failure_kind
       AND execution.retryable = p_retryable THEN
        RETURN;
    END IF;
    IF execution.status <> 'LEASED' OR execution.attempt <> p_attempt
       OR execution.lease_token <> p_lease_token THEN
        RAISE EXCEPTION 'Activity lease no longer belongs to this execution'
            USING ERRCODE = '55000';
    END IF;
    UPDATE lexsond.activity_executions
    SET status = 'FAILED', lease_token = NULL, lease_expires_at = NULL,
        outcome_status = NULL, result_ref = NULL, error_code = p_error_code,
        failure_kind = p_failure_kind, retryable = p_retryable,
        updated_at = clock_timestamp()
    WHERE idempotency_key = p_idempotency_key;
END;
$$;

CREATE OR REPLACE FUNCTION lexsond.persist_probe_result(
    p_run_id UUID,
    p_result_ref TEXT,
    p_result_sha256 CHAR(64),
    p_result JSONB
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = lexsond, pg_temp
AS $$
DECLARE
    existing lexsond.probe_results%ROWTYPE;
BEGIN
    INSERT INTO lexsond.probe_results (
        run_id, result_ref, result_schema_version, result_status,
        result_sha256, normalized_result
    ) VALUES (
        p_run_id, p_result_ref, p_result ->> 'schema_version',
        p_result ->> 'status', p_result_sha256, p_result
    ) ON CONFLICT (run_id) DO NOTHING;
    SELECT * INTO existing FROM lexsond.probe_results WHERE run_id = p_run_id;
    IF existing.result_ref IS DISTINCT FROM p_result_ref
       OR existing.result_sha256 IS DISTINCT FROM p_result_sha256
       OR existing.normalized_result IS DISTINCT FROM p_result THEN
        RAISE EXCEPTION 'probe result is immutable for a workflow run'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION lexsond.reject_append_only_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = lexsond, pg_temp
AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER workflow_events_are_append_only
BEFORE UPDATE OR DELETE ON lexsond.workflow_events
FOR EACH ROW EXECUTE FUNCTION lexsond.reject_append_only_mutation();

CREATE TRIGGER endpoint_snapshots_are_append_only
BEFORE UPDATE OR DELETE ON lexsond.endpoint_snapshots
FOR EACH ROW EXECUTE FUNCTION lexsond.reject_append_only_mutation();

CREATE TRIGGER probe_suite_snapshots_are_append_only
BEFORE UPDATE OR DELETE ON lexsond.probe_suite_snapshots
FOR EACH ROW EXECUTE FUNCTION lexsond.reject_append_only_mutation();

CREATE TRIGGER probe_results_are_append_only
BEFORE UPDATE OR DELETE ON lexsond.probe_results
FOR EACH ROW EXECUTE FUNCTION lexsond.reject_append_only_mutation();

COMMIT;
