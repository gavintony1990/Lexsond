BEGIN;

-- Keep the durable endpoint snapshot guard aligned with the native transport:
-- remote endpoints require HTTPS, while plain HTTP is limited to numeric
-- loopback addresses.  Parsing the authority before the inet cast prevents
-- prefix tricks such as 127.0.0.1.example.com.
CREATE FUNCTION lexsond.is_safe_snapshot_base_url(p_value TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
STRICT
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_scheme TEXT;
    v_authority TEXT;
    v_host TEXT;
    v_address INET;
BEGIN
    IF char_length(p_value) > 2048 OR p_value ~ '[?#[:space:]]' THEN
        RETURN FALSE;
    END IF;
    v_scheme := lower(split_part(p_value, '://', 1));
    IF v_scheme NOT IN ('http', 'https') OR p_value !~ '^[A-Za-z]+://' THEN
        RETURN FALSE;
    END IF;
    v_authority := split_part(split_part(p_value, '://', 2), '/', 1);
    IF v_authority = '' OR v_authority LIKE '%@%' THEN
        RETURN FALSE;
    END IF;
    IF v_scheme = 'https' THEN
        RETURN v_authority ~ '^(\[[0-9A-Fa-f:.]+\]|[^:]+)(:[0-9]{1,5})?$';
    END IF;
    IF v_authority LIKE '[%' THEN
        v_host := substring(v_authority FROM '^\[([0-9A-Fa-f:.]+)\](:[0-9]{1,5})?$');
    ELSE
        v_host := substring(v_authority FROM '^([0-9.]+)(:[0-9]{1,5})?$');
    END IF;
    IF v_host IS NULL THEN
        RETURN FALSE;
    END IF;
    BEGIN
        v_address := v_host::INET;
    EXCEPTION WHEN OTHERS THEN
        RETURN FALSE;
    END;
    RETURN (family(v_address) = 4 AND v_address << inet '127.0.0.0/8')
        OR v_address = inet '::1';
END;
$$;

ALTER TABLE lexsond.model_catalog_snapshots
    ADD COLUMN credential_fingerprint CHAR(64)
        CHECK (credential_fingerprint IS NULL OR credential_fingerprint ~ '^[0-9a-f]{64}$'),
    ADD COLUMN credential_version INTEGER
        CHECK (credential_version IS NULL OR credential_version >= 1),
    ADD COLUMN target_base_url TEXT CHECK (
        target_base_url IS NULL OR lexsond.is_safe_snapshot_base_url(target_base_url)
    ),
    ADD COLUMN target_kind TEXT CHECK (
        target_kind IS NULL OR target_kind IN ('local', 'cloud')
    ),
    ADD COLUMN protocol TEXT CHECK (
        protocol IS NULL OR protocol = 'openai-compatible'
    );

-- Harden the shared JSON secret-key guard for mixed case and separator
-- variants such as API-Key, clientSecret, and Password. Existing CHECK
-- constraints call this function dynamically, so the stronger definition
-- also protects pre-evaluation evidence tables after this migration.
CREATE OR REPLACE FUNCTION lexsond.contains_forbidden_secret_key(p_value JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    child JSONB;
    child_key TEXT;
    normalized_key TEXT;
BEGIN
    IF jsonb_typeof(p_value) = 'object' THEN
        FOR child_key, child IN SELECT key, value FROM jsonb_each(p_value) LOOP
            normalized_key := lower(regexp_replace(child_key, '[^a-zA-Z0-9]', '', 'g'));
            IF normalized_key = ANY (ARRAY[
                'apikey', 'authorization', 'credential', 'credentialhandle',
                'credentialref', 'accesstoken', 'refreshtoken', 'sessiontoken',
                'oauthtoken', 'clientsecret', 'password', 'secret'
            ]) THEN
                RETURN TRUE;
            END IF;
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

CREATE TABLE lexsond.evaluation_datasets (
    dataset_id UUID PRIMARY KEY,
    workspace_id UUID REFERENCES lexsond.workspaces(workspace_id) ON DELETE RESTRICT,
    scope TEXT NOT NULL CHECK (scope IN ('SYSTEM', 'WORKSPACE')),
    slug TEXT NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,119}$'),
    name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
    description TEXT NOT NULL DEFAULT '' CHECK (char_length(description) <= 4000),
    license_spdx TEXT NOT NULL CHECK (char_length(license_spdx) BETWEEN 1 AND 64),
    license_url TEXT NOT NULL CHECK (
        license_url ~ '^https://' AND license_url !~ '[?#]' AND license_url !~ '://[^/]*@'
    ),
    source_url TEXT CHECK (
        source_url IS NULL OR (
            source_url ~ '^https://' AND source_url !~ '[?#]' AND source_url !~ '://[^/]*@'
        )
    ),
    source_version TEXT CHECK (
        source_version IS NULL OR char_length(source_version) BETWEEN 1 AND 128
    ),
    source_verified_at DATE,
    distribution_policy TEXT NOT NULL CHECK (distribution_policy IN (
        'BUNDLED', 'IMPORT_REQUIRED', 'LICENSE_REVIEW', 'RESEARCH_ONLY',
        'RUNNER_REQUIRED', 'BLOCKED'
    )),
    default_scorer_id TEXT NOT NULL CHECK (
        default_scorer_id IN (
            'exact_match', 'normalized_exact_match', 'multiple_choice_accuracy',
            'token_f1', 'contains_all', 'regex_match', 'json_schema_valid'
        )
    ),
    latest_revision_id UUID,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_by UUID REFERENCES lexsond.users(user_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    archived_at TIMESTAMPTZ,
    UNIQUE (dataset_id, workspace_id),
    CHECK ((scope = 'SYSTEM') = (workspace_id IS NULL)),
    CHECK (scope <> 'SYSTEM' OR created_by IS NULL),
    CHECK (NOT lexsond.contains_recognizable_secret_value(to_jsonb(name))),
    CHECK (NOT lexsond.contains_recognizable_secret_value(to_jsonb(description))),
    CHECK (NOT lexsond.contains_recognizable_secret_value(to_jsonb(license_spdx))),
    CHECK (NOT lexsond.contains_recognizable_secret_value(to_jsonb(license_url))),
    CHECK (source_url IS NULL OR NOT lexsond.contains_recognizable_secret_value(to_jsonb(source_url)))
);

CREATE UNIQUE INDEX uq_evaluation_datasets_system_slug
    ON lexsond.evaluation_datasets (slug) WHERE scope = 'SYSTEM';
CREATE UNIQUE INDEX uq_evaluation_datasets_workspace_slug
    ON lexsond.evaluation_datasets (workspace_id, slug) WHERE scope = 'WORKSPACE';
CREATE INDEX idx_evaluation_datasets_workspace_updated
    ON lexsond.evaluation_datasets (workspace_id, updated_at DESC, dataset_id);

CREATE TABLE lexsond.evaluation_dataset_revisions (
    revision_id UUID PRIMARY KEY,
    dataset_id UUID NOT NULL
        REFERENCES lexsond.evaluation_datasets(dataset_id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    schema_version TEXT NOT NULL CHECK (schema_version = 'lexsond.evaluation-dataset/v1'),
    content_sha256 CHAR(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    item_count INTEGER NOT NULL CHECK (item_count BETWEEN 1 AND 10000),
    category_count INTEGER NOT NULL CHECK (category_count BETWEEN 1 AND 10000),
    language_codes JSONB NOT NULL
        CHECK (jsonb_typeof(language_codes) = 'array')
        CHECK (jsonb_array_length(language_codes) BETWEEN 1 AND 128)
        CHECK (NOT lexsond.contains_forbidden_secret_key(language_codes))
        CHECK (NOT lexsond.contains_recognizable_secret_value(language_codes)),
    manifest_json JSONB NOT NULL
        CHECK (jsonb_typeof(manifest_json) = 'object')
        CHECK (NOT lexsond.contains_forbidden_secret_key(manifest_json))
        CHECK (NOT lexsond.contains_recognizable_secret_value(manifest_json)),
    sealed_at TIMESTAMPTZ,
    created_by UUID REFERENCES lexsond.users(user_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (dataset_id, revision_id),
    UNIQUE (dataset_id, revision),
    UNIQUE (dataset_id, content_sha256)
);

ALTER TABLE lexsond.evaluation_datasets
    ADD CONSTRAINT evaluation_datasets_latest_revision_fkey
    FOREIGN KEY (dataset_id, latest_revision_id)
    REFERENCES lexsond.evaluation_dataset_revisions(dataset_id, revision_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE lexsond.evaluation_dataset_items (
    revision_id UUID NOT NULL
        REFERENCES lexsond.evaluation_dataset_revisions(revision_id) ON DELETE RESTRICT,
    item_index INTEGER NOT NULL CHECK (item_index BETWEEN 0 AND 9999),
    item_id TEXT NOT NULL CHECK (
        char_length(item_id) BETWEEN 1 AND 128
        AND item_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
    ),
    category TEXT NOT NULL CHECK (
        char_length(category) BETWEEN 1 AND 64
        AND category ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'
    ),
    language TEXT NOT NULL CHECK (char_length(language) BETWEEN 2 AND 64),
    input_json JSONB NOT NULL
        CHECK (jsonb_typeof(input_json) = 'object')
        CHECK (NOT lexsond.contains_forbidden_secret_key(input_json))
        CHECK (NOT lexsond.contains_recognizable_secret_value(input_json)),
    reference_json JSONB NOT NULL
        CHECK (jsonb_typeof(reference_json) = 'object')
        CHECK (NOT lexsond.contains_forbidden_secret_key(reference_json))
        CHECK (NOT lexsond.contains_recognizable_secret_value(reference_json)),
    metadata_json JSONB NOT NULL
        CHECK (jsonb_typeof(metadata_json) = 'object')
        CHECK (pg_column_size(metadata_json) <= 8192)
        CHECK (NOT lexsond.contains_forbidden_secret_key(metadata_json))
        CHECK (NOT lexsond.contains_recognizable_secret_value(metadata_json)),
    PRIMARY KEY (revision_id, item_index),
    UNIQUE (revision_id, item_id)
);

CREATE TABLE lexsond.evaluation_runs (
    evaluation_run_id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL
        REFERENCES lexsond.workspaces(workspace_id) ON DELETE RESTRICT,
    idempotency_key UUID NOT NULL,
    request_sha256 CHAR(64) NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    dataset_id UUID NOT NULL
        REFERENCES lexsond.evaluation_datasets(dataset_id) ON DELETE RESTRICT,
    dataset_revision_id UUID NOT NULL,
    channel_id UUID NOT NULL,
    catalog_snapshot_id UUID NOT NULL,
    credential_profile_id UUID,
    execution_lease_id UUID NOT NULL,
    lease_expires_at TIMESTAMPTZ NOT NULL
        DEFAULT (clock_timestamp() + INTERVAL '5 minutes'),
    model_source_id TEXT NOT NULL CHECK (
        char_length(model_source_id) BETWEEN 1 AND 64
        AND model_source_id ~ '^[a-z0-9][a-z0-9-]{0,63}$'
    ),
    state TEXT NOT NULL CHECK (state IN (
        'RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED', 'CANCELLED'
    )),
    scorer_id TEXT NOT NULL CHECK (scorer_id IN (
        'dataset_reference',
        'exact_match', 'normalized_exact_match', 'multiple_choice_accuracy',
        'token_f1', 'contains_all', 'regex_match', 'json_schema_valid'
    )),
    scorer_version TEXT NOT NULL CHECK (scorer_version ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
    sample_strategy TEXT NOT NULL CHECK (sample_strategy IN ('first', 'random', 'stratified')),
    sample_seed BIGINT NOT NULL,
    sample_count INTEGER NOT NULL CHECK (sample_count BETWEEN 1 AND 200),
    model_count INTEGER NOT NULL CHECK (model_count BETWEEN 1 AND 10),
    concurrency INTEGER NOT NULL DEFAULT 2 CHECK (concurrency BETWEEN 1 AND 2),
    max_output_tokens INTEGER NOT NULL CHECK (max_output_tokens BETWEEN 1 AND 1024),
    timeout_seconds DOUBLE PRECISION NOT NULL CHECK (timeout_seconds BETWEEN 1 AND 120),
    max_cost_usd NUMERIC(12, 6) NOT NULL CHECK (max_cost_usd > 0 AND max_cost_usd <= 10000),
    request_snapshot_json JSONB NOT NULL
        CHECK (jsonb_typeof(request_snapshot_json) = 'object')
        CHECK (NOT lexsond.contains_forbidden_secret_key(request_snapshot_json))
        CHECK (NOT lexsond.contains_recognizable_secret_value(request_snapshot_json)),
    aggregate_result_json JSONB
        CHECK (aggregate_result_json IS NULL OR (
            jsonb_typeof(aggregate_result_json) = 'object'
            AND NOT lexsond.contains_forbidden_secret_key(aggregate_result_json)
            AND NOT lexsond.contains_recognizable_secret_value(aggregate_result_json)
        )),
    failure_code TEXT CHECK (
        failure_code IS NULL OR failure_code ~ '^[A-Z][A-Z0-9_]{2,127}$'
    ),
    cancel_requested_at TIMESTAMPTZ,
    created_by UUID NOT NULL REFERENCES lexsond.users(user_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at TIMESTAMPTZ,
    archived_at TIMESTAMPTZ,
    UNIQUE (workspace_id, evaluation_run_id),
    UNIQUE (workspace_id, idempotency_key),
    FOREIGN KEY (dataset_id, dataset_revision_id)
        REFERENCES lexsond.evaluation_dataset_revisions(dataset_id, revision_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, channel_id)
        REFERENCES lexsond.targets(workspace_id, target_id) ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, catalog_snapshot_id)
        REFERENCES lexsond.model_catalog_snapshots(workspace_id, snapshot_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, credential_profile_id)
        REFERENCES lexsond.credential_profiles(workspace_id, credential_id)
        ON DELETE RESTRICT,
    CHECK ((state = 'RUNNING') = (finished_at IS NULL)),
    CHECK ((state = 'RUNNING') = (aggregate_result_json IS NULL))
);

CREATE INDEX idx_evaluation_runs_workspace_created
    ON lexsond.evaluation_runs (workspace_id, created_at DESC, evaluation_run_id);

CREATE TABLE lexsond.evaluation_run_models (
    evaluation_run_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    model_id TEXT NOT NULL CHECK (char_length(model_id) BETWEEN 1 AND 256),
    provider_model_id TEXT NOT NULL CHECK (char_length(provider_model_id) BETWEEN 1 AND 256),
    state TEXT NOT NULL CHECK (state IN (
        'PENDING', 'RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED', 'CANCELLED', 'SKIPPED'
    )),
    completed_items INTEGER NOT NULL DEFAULT 0 CHECK (completed_items BETWEEN 0 AND 200),
    passed_items INTEGER NOT NULL DEFAULT 0 CHECK (passed_items BETWEEN 0 AND 200),
    failed_items INTEGER NOT NULL DEFAULT 0 CHECK (failed_items BETWEEN 0 AND 200),
    unknown_items INTEGER NOT NULL DEFAULT 0 CHECK (unknown_items BETWEEN 0 AND 200),
    metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metrics_json) = 'object')
        CHECK (NOT lexsond.contains_forbidden_secret_key(metrics_json))
        CHECK (NOT lexsond.contains_recognizable_secret_value(metrics_json)),
    PRIMARY KEY (evaluation_run_id, model_id),
    UNIQUE (workspace_id, evaluation_run_id, model_id),
    FOREIGN KEY (workspace_id, evaluation_run_id)
        REFERENCES lexsond.evaluation_runs(workspace_id, evaluation_run_id)
        ON DELETE RESTRICT,
    CHECK (NOT lexsond.contains_recognizable_secret_value(to_jsonb(model_id))),
    CHECK (NOT lexsond.contains_recognizable_secret_value(to_jsonb(provider_model_id)))
);

CREATE TABLE lexsond.evaluation_run_items (
    evaluation_run_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    model_id TEXT NOT NULL,
    item_id TEXT NOT NULL CHECK (char_length(item_id) BETWEEN 1 AND 128),
    category TEXT NOT NULL CHECK (
        char_length(category) BETWEEN 1 AND 64
        AND category ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'
    ),
    sequence INTEGER NOT NULL CHECK (sequence BETWEEN 1 AND 2000),
    state TEXT NOT NULL CHECK (state IN ('COMPLETED', 'FAILED', 'CANCELLED', 'SKIPPED')),
    score DOUBLE PRECISION CHECK (score IS NULL OR (score >= 0 AND score <= 1)),
    status TEXT NOT NULL CHECK (status IN ('PASS', 'FAIL', 'UNKNOWN')),
    reason_code TEXT NOT NULL CHECK (reason_code ~ '^[A-Z][A-Z0-9_]{2,127}$'),
    latency_json JSONB NOT NULL
        CHECK (jsonb_typeof(latency_json) = 'object')
        CHECK (NOT lexsond.contains_forbidden_secret_key(latency_json)),
    usage_json JSONB NOT NULL
        CHECK (jsonb_typeof(usage_json) = 'object')
        CHECK (NOT lexsond.contains_forbidden_secret_key(usage_json)),
    output_sha256 CHAR(64) CHECK (
        output_sha256 IS NULL OR output_sha256 ~ '^[0-9a-f]{64}$'
    ),
    safe_facts_json JSONB NOT NULL
        CHECK (jsonb_typeof(safe_facts_json) = 'object')
        CHECK (NOT lexsond.contains_forbidden_secret_key(safe_facts_json))
        CHECK (NOT lexsond.contains_recognizable_secret_value(safe_facts_json)),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (evaluation_run_id, model_id, sequence),
    UNIQUE (evaluation_run_id, sequence),
    UNIQUE (evaluation_run_id, model_id, item_id),
    FOREIGN KEY (workspace_id, evaluation_run_id, model_id)
        REFERENCES lexsond.evaluation_run_models(workspace_id, evaluation_run_id, model_id)
        ON DELETE RESTRICT,
    CHECK (NOT lexsond.contains_recognizable_secret_value(to_jsonb(model_id))),
    CHECK (NOT lexsond.contains_recognizable_secret_value(to_jsonb(item_id))),
    CHECK (NOT lexsond.contains_recognizable_secret_value(latency_json)),
    CHECK (NOT lexsond.contains_recognizable_secret_value(usage_json))
);

CREATE TABLE lexsond.evaluation_run_events (
    evaluation_run_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    sequence BIGINT NOT NULL CHECK (sequence >= 1),
    event_id UUID NOT NULL UNIQUE,
    event_type TEXT NOT NULL CHECK (event_type ~ '^[A-Z][A-Z0-9_]{2,127}$'),
    model_id TEXT,
    item_id TEXT,
    state TEXT NOT NULL CHECK (state ~ '^[A-Z][A-Z0-9_]{2,127}$'),
    safe_facts_json JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(safe_facts_json) = 'object')
        CHECK (NOT lexsond.contains_forbidden_secret_key(safe_facts_json))
        CHECK (NOT lexsond.contains_recognizable_secret_value(safe_facts_json)),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (evaluation_run_id, sequence),
    FOREIGN KEY (workspace_id, evaluation_run_id)
        REFERENCES lexsond.evaluation_runs(workspace_id, evaluation_run_id)
        ON DELETE RESTRICT,
    CHECK (model_id IS NULL OR NOT lexsond.contains_recognizable_secret_value(to_jsonb(model_id))),
    CHECK (item_id IS NULL OR NOT lexsond.contains_recognizable_secret_value(to_jsonb(item_id))),
    CHECK (NOT lexsond.contains_recognizable_secret_value(to_jsonb(state)))
);

CREATE OR REPLACE FUNCTION lexsond.reject_evaluation_append_only_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE'
       AND current_setting('lexsond.evaluation_purge', true) = 'on'
       AND current_user <> session_user THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '% is immutable', TG_TABLE_NAME
        USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION lexsond.protect_evaluation_dataset_revision()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE'
       AND current_setting('lexsond.evaluation_purge', true) = 'on'
       AND current_user <> session_user THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.sealed_at IS NULL
       AND NEW.sealed_at IS NOT NULL
       AND ROW(
           NEW.revision_id, NEW.dataset_id, NEW.revision,
           NEW.schema_version, NEW.content_sha256, NEW.item_count,
           NEW.category_count, NEW.language_codes, NEW.manifest_json,
           NEW.created_by, NEW.created_at
       ) IS NOT DISTINCT FROM ROW(
           OLD.revision_id, OLD.dataset_id, OLD.revision,
           OLD.schema_version, OLD.content_sha256, OLD.item_count,
           OLD.category_count, OLD.language_codes, OLD.manifest_json,
           OLD.created_by, OLD.created_at
       ) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'evaluation_dataset_revisions is immutable'
        USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION lexsond.require_open_evaluation_dataset_revision()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_sealed_at TIMESTAMPTZ;
BEGIN
    SELECT sealed_at INTO v_sealed_at
    FROM lexsond.evaluation_dataset_revisions
    WHERE revision_id = NEW.revision_id
    FOR UPDATE;
    IF NOT FOUND OR v_sealed_at IS NOT NULL THEN
        RAISE EXCEPTION 'evaluation dataset revision is sealed'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION lexsond.require_sealed_evaluation_dataset_revision()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_sealed_at TIMESTAMPTZ;
    v_declared_count INTEGER;
    v_actual_count INTEGER;
BEGIN
    SELECT sealed_at, item_count INTO v_sealed_at, v_declared_count
    FROM lexsond.evaluation_dataset_revisions
    WHERE revision_id = NEW.revision_id;
    SELECT COUNT(*)::INTEGER INTO v_actual_count
    FROM lexsond.evaluation_dataset_items
    WHERE revision_id = NEW.revision_id;
    IF v_sealed_at IS NULL OR v_actual_count <> v_declared_count THEN
        RAISE EXCEPTION 'evaluation dataset revision must be sealed with its declared item count'
            USING ERRCODE = '55000';
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION lexsond.protect_evaluation_run_model()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_completed INTEGER;
    v_passed INTEGER;
    v_failed INTEGER;
    v_unknown INTEGER;
BEGIN
    IF ROW(
        NEW.evaluation_run_id, NEW.workspace_id,
        NEW.model_id, NEW.provider_model_id
    ) IS DISTINCT FROM ROW(
        OLD.evaluation_run_id, OLD.workspace_id,
        OLD.model_id, OLD.provider_model_id
    ) THEN
        RAISE EXCEPTION 'evaluation model identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.state NOT IN ('PENDING', 'RUNNING') THEN
        RAISE EXCEPTION 'terminal evaluation model result is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.state = 'RUNNING' AND NEW.state = 'PENDING' THEN
        RAISE EXCEPTION 'evaluation model state cannot move backwards'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.state IN ('PENDING', 'RUNNING')
       AND NEW.metrics_json IS DISTINCT FROM OLD.metrics_json THEN
        RAISE EXCEPTION 'running evaluation model cannot publish final metrics'
            USING ERRCODE = '55000';
    END IF;
    SELECT
        COUNT(*)::INTEGER,
        COUNT(*) FILTER (WHERE status = 'PASS')::INTEGER,
        COUNT(*) FILTER (WHERE status = 'FAIL')::INTEGER,
        COUNT(*) FILTER (WHERE status = 'UNKNOWN')::INTEGER
    INTO v_completed, v_passed, v_failed, v_unknown
    FROM lexsond.evaluation_run_items
    WHERE evaluation_run_id = NEW.evaluation_run_id
      AND workspace_id = NEW.workspace_id
      AND model_id = NEW.model_id;
    IF ROW(
        NEW.completed_items, NEW.passed_items,
        NEW.failed_items, NEW.unknown_items
    ) IS DISTINCT FROM ROW(
        v_completed, v_passed, v_failed, v_unknown
    ) THEN
        RAISE EXCEPTION 'evaluation model counters must match immutable item evidence'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION lexsond.protect_evaluation_run_snapshot()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF ROW(
        NEW.evaluation_run_id, NEW.workspace_id, NEW.idempotency_key,
        NEW.request_sha256, NEW.dataset_id, NEW.dataset_revision_id,
        NEW.channel_id, NEW.catalog_snapshot_id, NEW.credential_profile_id,
        NEW.execution_lease_id, NEW.model_source_id,
        NEW.scorer_id, NEW.scorer_version, NEW.sample_strategy,
        NEW.sample_seed, NEW.sample_count, NEW.model_count, NEW.concurrency,
        NEW.max_output_tokens, NEW.timeout_seconds, NEW.max_cost_usd,
        NEW.request_snapshot_json, NEW.created_by, NEW.created_at
    ) IS DISTINCT FROM ROW(
        OLD.evaluation_run_id, OLD.workspace_id, OLD.idempotency_key,
        OLD.request_sha256, OLD.dataset_id, OLD.dataset_revision_id,
        OLD.channel_id, OLD.catalog_snapshot_id, OLD.credential_profile_id,
        OLD.execution_lease_id, OLD.model_source_id,
        OLD.scorer_id, OLD.scorer_version, OLD.sample_strategy,
        OLD.sample_seed, OLD.sample_count, OLD.model_count, OLD.concurrency,
        OLD.max_output_tokens, OLD.timeout_seconds, OLD.max_cost_usd,
        OLD.request_snapshot_json, OLD.created_by, OLD.created_at
    ) THEN
        RAISE EXCEPTION 'evaluation run request snapshot is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.state <> 'RUNNING' AND ROW(
        NEW.state, NEW.aggregate_result_json, NEW.failure_code,
        NEW.cancel_requested_at, NEW.finished_at
    ) IS DISTINCT FROM ROW(
        OLD.state, OLD.aggregate_result_json, OLD.failure_code,
        OLD.cancel_requested_at, OLD.finished_at
    ) THEN
        RAISE EXCEPTION 'terminal evaluation result is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.state = 'RUNNING' AND NEW.state = 'RUNNING' AND ROW(
        NEW.aggregate_result_json, NEW.failure_code, NEW.finished_at
    ) IS DISTINCT FROM ROW(
        OLD.aggregate_result_json, OLD.failure_code, OLD.finished_at
    ) THEN
        RAISE EXCEPTION 'running evaluation accepts only cancel intent'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.cancel_requested_at IS NOT NULL AND NEW.cancel_requested_at IS NULL THEN
        RAISE EXCEPTION 'evaluation cancel intent is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.state = 'RUNNING' AND NEW.archived_at IS DISTINCT FROM OLD.archived_at THEN
        RAISE EXCEPTION 'running evaluation cannot be archived'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION lexsond.require_evaluation_run_dataset_scope()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_scope TEXT;
    v_dataset_workspace UUID;
BEGIN
    SELECT scope, workspace_id INTO v_scope, v_dataset_workspace
    FROM lexsond.evaluation_datasets
    WHERE dataset_id = NEW.dataset_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'evaluation dataset was not found'
            USING ERRCODE = '23503';
    END IF;
    IF v_scope = 'WORKSPACE' AND v_dataset_workspace <> NEW.workspace_id THEN
        RAISE EXCEPTION 'workspace evaluation dataset cannot be referenced across workspaces'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION lexsond.require_running_evaluation_parent()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM 1 FROM lexsond.evaluation_runs
    WHERE evaluation_run_id = NEW.evaluation_run_id
      AND workspace_id = NEW.workspace_id AND state = 'RUNNING'
    FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'evaluation evidence requires a running parent'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER evaluation_dataset_revisions_are_immutable
BEFORE UPDATE OR DELETE ON lexsond.evaluation_dataset_revisions
FOR EACH ROW EXECUTE FUNCTION lexsond.protect_evaluation_dataset_revision();
CREATE TRIGGER evaluation_dataset_items_are_immutable
BEFORE UPDATE OR DELETE ON lexsond.evaluation_dataset_items
FOR EACH ROW EXECUTE FUNCTION lexsond.reject_evaluation_append_only_mutation();
CREATE TRIGGER evaluation_dataset_items_require_open_revision
BEFORE INSERT ON lexsond.evaluation_dataset_items
FOR EACH ROW EXECUTE FUNCTION lexsond.require_open_evaluation_dataset_revision();
CREATE CONSTRAINT TRIGGER evaluation_dataset_revision_must_be_sealed
AFTER INSERT OR UPDATE ON lexsond.evaluation_dataset_revisions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION lexsond.require_sealed_evaluation_dataset_revision();
CREATE TRIGGER evaluation_run_items_are_immutable
BEFORE UPDATE OR DELETE ON lexsond.evaluation_run_items
FOR EACH ROW EXECUTE FUNCTION lexsond.reject_evaluation_append_only_mutation();
CREATE TRIGGER evaluation_run_events_are_immutable
BEFORE UPDATE OR DELETE ON lexsond.evaluation_run_events
FOR EACH ROW EXECUTE FUNCTION lexsond.reject_evaluation_append_only_mutation();
CREATE TRIGGER evaluation_run_snapshot_is_immutable
BEFORE UPDATE ON lexsond.evaluation_runs
FOR EACH ROW EXECUTE FUNCTION lexsond.protect_evaluation_run_snapshot();
CREATE TRIGGER evaluation_run_dataset_scope_is_valid
BEFORE INSERT OR UPDATE OF workspace_id, dataset_id, dataset_revision_id
ON lexsond.evaluation_runs
FOR EACH ROW EXECUTE FUNCTION lexsond.require_evaluation_run_dataset_scope();
CREATE TRIGGER evaluation_run_model_result_is_immutable
BEFORE UPDATE ON lexsond.evaluation_run_models
FOR EACH ROW EXECUTE FUNCTION lexsond.protect_evaluation_run_model();
CREATE TRIGGER evaluation_run_items_require_running_parent
BEFORE INSERT ON lexsond.evaluation_run_items
FOR EACH ROW EXECUTE FUNCTION lexsond.require_running_evaluation_parent();
CREATE TRIGGER evaluation_run_events_require_running_parent
BEFORE INSERT ON lexsond.evaluation_run_events
FOR EACH ROW EXECUTE FUNCTION lexsond.require_running_evaluation_parent();

CREATE OR REPLACE FUNCTION lexsond.purge_evaluation_dataset(
    p_workspace_id UUID,
    p_dataset_id UUID
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, lexsond
AS $$
DECLARE
    v_archived_at TIMESTAMPTZ;
BEGIN
    SELECT archived_at INTO v_archived_at
    FROM lexsond.evaluation_datasets
    WHERE dataset_id = p_dataset_id AND workspace_id = p_workspace_id
      AND scope = 'WORKSPACE'
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'workspace evaluation dataset was not found'
            USING ERRCODE = 'P0002';
    END IF;
    IF v_archived_at IS NULL THEN
        RAISE EXCEPTION 'evaluation dataset must be archived before purge'
            USING ERRCODE = '55000';
    END IF;
    IF EXISTS (
        SELECT 1 FROM lexsond.evaluation_runs
        WHERE dataset_id = p_dataset_id
    ) THEN
        RAISE EXCEPTION 'evaluation dataset is referenced by a retained run'
            USING ERRCODE = '23503';
    END IF;
    PERFORM set_config('lexsond.evaluation_purge', 'on', true);
    DELETE FROM lexsond.evaluation_dataset_items
    WHERE revision_id IN (
        SELECT revision_id FROM lexsond.evaluation_dataset_revisions
        WHERE dataset_id = p_dataset_id
    );
    UPDATE lexsond.evaluation_datasets SET latest_revision_id = NULL
    WHERE dataset_id = p_dataset_id;
    DELETE FROM lexsond.evaluation_dataset_revisions
    WHERE dataset_id = p_dataset_id;
    DELETE FROM lexsond.evaluation_datasets
    WHERE dataset_id = p_dataset_id AND workspace_id = p_workspace_id;
END;
$$;

CREATE OR REPLACE FUNCTION lexsond.purge_evaluation_run(
    p_workspace_id UUID,
    p_evaluation_run_id UUID
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, lexsond
AS $$
DECLARE
    v_archived_at TIMESTAMPTZ;
BEGIN
    SELECT archived_at INTO v_archived_at
    FROM lexsond.evaluation_runs
    WHERE evaluation_run_id = p_evaluation_run_id
      AND workspace_id = p_workspace_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'evaluation run was not found' USING ERRCODE = 'P0002';
    END IF;
    IF v_archived_at IS NULL THEN
        RAISE EXCEPTION 'evaluation run must be archived before purge'
            USING ERRCODE = '55000';
    END IF;
    PERFORM set_config('lexsond.evaluation_purge', 'on', true);
    DELETE FROM lexsond.evaluation_run_events
    WHERE evaluation_run_id = p_evaluation_run_id;
    DELETE FROM lexsond.evaluation_run_items
    WHERE evaluation_run_id = p_evaluation_run_id;
    DELETE FROM lexsond.evaluation_run_models
    WHERE evaluation_run_id = p_evaluation_run_id;
    DELETE FROM lexsond.evaluation_runs
    WHERE evaluation_run_id = p_evaluation_run_id
      AND workspace_id = p_workspace_id;
END;
$$;

ALTER FUNCTION lexsond.purge_evaluation_dataset(UUID, UUID)
    OWNER TO lexsond_runtime_owner;
ALTER FUNCTION lexsond.purge_evaluation_run(UUID, UUID)
    OWNER TO lexsond_runtime_owner;

REVOKE ALL ON lexsond.evaluation_datasets,
    lexsond.evaluation_dataset_revisions, lexsond.evaluation_dataset_items,
    lexsond.evaluation_runs, lexsond.evaluation_run_models,
    lexsond.evaluation_run_items, lexsond.evaluation_run_events FROM PUBLIC;
REVOKE ALL ON FUNCTION lexsond.is_safe_snapshot_base_url(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lexsond.is_safe_snapshot_base_url(TEXT) TO lexsond_control;
-- The credential binding is secret-derived and workspace scoped.  Reader
-- projections do not need it and must not gain a cross-resource correlation
-- surface through the underlying catalog table.
REVOKE SELECT ON lexsond.model_catalog_snapshots FROM lexsond_reader;
GRANT SELECT (
    snapshot_id, workspace_id, target_id, credential_profile_id,
    target_version, provider_id, models_json, model_count, status,
    content_sha256, fetched_at, expires_at, target_base_url,
    target_kind, protocol
) ON lexsond.model_catalog_snapshots TO lexsond_reader;

GRANT SELECT, INSERT, UPDATE ON lexsond.evaluation_datasets,
    lexsond.evaluation_runs TO lexsond_control;
-- Revisions need UPDATE only for the guarded NULL -> sealed_at transition.
-- Items remain append-only for the control role.
GRANT SELECT, INSERT, UPDATE ON lexsond.evaluation_dataset_revisions TO lexsond_control;
GRANT SELECT, INSERT ON lexsond.evaluation_dataset_items, lexsond.evaluation_run_items,
    lexsond.evaluation_run_events TO lexsond_control;
GRANT SELECT, INSERT, UPDATE ON lexsond.evaluation_run_models TO lexsond_control;
GRANT SELECT, UPDATE, DELETE ON lexsond.evaluation_datasets,
    lexsond.evaluation_runs TO lexsond_runtime_owner;
GRANT SELECT, DELETE ON lexsond.evaluation_dataset_revisions,
    lexsond.evaluation_dataset_items, lexsond.evaluation_run_models,
    lexsond.evaluation_run_items, lexsond.evaluation_run_events
    TO lexsond_runtime_owner;
REVOKE ALL ON FUNCTION lexsond.purge_evaluation_dataset(UUID, UUID),
    lexsond.purge_evaluation_run(UUID, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lexsond.purge_evaluation_dataset(UUID, UUID),
    lexsond.purge_evaluation_run(UUID, UUID) TO lexsond_control;

-- Readers can see catalog metadata and sanitized metrics, but not private
-- prompt/reference bodies in evaluation_dataset_items.
GRANT SELECT ON lexsond.evaluation_datasets,
    lexsond.evaluation_dataset_revisions, lexsond.evaluation_runs,
    lexsond.evaluation_run_models, lexsond.evaluation_run_items,
    lexsond.evaluation_run_events TO lexsond_reader;

COMMIT;
