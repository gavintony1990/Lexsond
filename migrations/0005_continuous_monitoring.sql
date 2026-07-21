BEGIN;

CREATE TABLE lexsond.monitor_policies (
    policy_id UUID PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    target_id UUID NOT NULL REFERENCES lexsond.targets(target_id) ON DELETE RESTRICT,
    suite_revision_id UUID REFERENCES lexsond.suite_revisions(revision_id) ON DELETE RESTRICT,
    run_kind TEXT NOT NULL CHECK (run_kind IN ('component', 'suite')),
    probe_type TEXT NOT NULL CHECK (probe_type IN (
        'chat', 'vision', 'embedding', 'image_generation',
        'audio_speech', 'audio_transcription'
    )),
    execution_backend TEXT NOT NULL CHECK (execution_backend IN ('local', 'temporal')),
    model TEXT NOT NULL,
    streaming BOOLEAN NOT NULL,
    timeout_seconds DOUBLE PRECISION NOT NULL CHECK (timeout_seconds > 0 AND timeout_seconds <= 300),
    interval_seconds INTEGER NOT NULL CHECK (interval_seconds BETWEEN 60 AND 2592000),
    failure_threshold INTEGER NOT NULL CHECK (failure_threshold BETWEEN 1 AND 10),
    recovery_threshold INTEGER NOT NULL CHECK (recovery_threshold BETWEEN 1 AND 10),
    schedule_offset_seconds INTEGER NOT NULL CHECK (schedule_offset_seconds BETWEEN 0 AND 59),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    next_run_at TIMESTAMPTZ,
    last_run_at TIMESTAMPTZ,
    last_run_id UUID,
    last_dispatch_failure_code TEXT CHECK (
        last_dispatch_failure_code IS NULL
        OR last_dispatch_failure_code ~ '^[A-Z][A-Z0-9_]{2,127}$'
    ),
    lease_token UUID,
    lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    archived_at TIMESTAMPTZ,
    CHECK ((lease_token IS NULL) = (lease_until IS NULL)),
    CHECK ((run_kind = 'suite') = (suite_revision_id IS NOT NULL))
);

CREATE INDEX idx_monitor_policies_due
    ON lexsond.monitor_policies (next_run_at, policy_id)
    WHERE enabled AND archived_at IS NULL;

ALTER TABLE lexsond.probe_runs
    ADD COLUMN monitor_policy_id UUID
    REFERENCES lexsond.monitor_policies(policy_id) ON DELETE SET NULL;

CREATE INDEX idx_probe_runs_monitor_policy
    ON lexsond.probe_runs (monitor_policy_id, created_at DESC)
    WHERE monitor_policy_id IS NOT NULL;

CREATE TABLE lexsond.monitor_states (
    policy_id UUID PRIMARY KEY
        REFERENCES lexsond.monitor_policies(policy_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('UNKNOWN', 'UP', 'DEGRADED', 'DOWN')),
    consecutive_successes INTEGER NOT NULL CHECK (consecutive_successes >= 0),
    consecutive_failures INTEGER NOT NULL CHECK (consecutive_failures >= 0),
    last_observation TEXT NOT NULL CHECK (last_observation IN ('PASS', 'WARN', 'FAIL', 'UNKNOWN')),
    last_run_id UUID NOT NULL REFERENCES lexsond.probe_runs(run_id) ON DELETE RESTRICT,
    last_observed_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE lexsond.monitor_samples (
    sample_id UUID PRIMARY KEY,
    policy_id UUID NOT NULL
        REFERENCES lexsond.monitor_policies(policy_id) ON DELETE CASCADE,
    run_id UUID NOT NULL UNIQUE
        REFERENCES lexsond.probe_runs(run_id) ON DELETE CASCADE,
    observed_at TIMESTAMPTZ NOT NULL,
    observation TEXT NOT NULL CHECK (observation IN ('PASS', 'WARN', 'FAIL', 'UNKNOWN')),
    error_class TEXT CHECK (
        error_class IS NULL OR error_class ~ '^[A-Z][A-Z0-9_]{2,127}$'
    ),
    p95_e2e_ms DOUBLE PRECISION CHECK (p95_e2e_ms IS NULL OR p95_e2e_ms >= 0),
    p95_ttft_ms DOUBLE PRECISION CHECK (p95_ttft_ms IS NULL OR p95_ttft_ms >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX idx_monitor_samples_policy_observed
    ON lexsond.monitor_samples (policy_id, observed_at DESC);

CREATE TABLE lexsond.monitor_incident_events (
    incident_id UUID PRIMARY KEY,
    policy_id UUID NOT NULL
        REFERENCES lexsond.monitor_policies(policy_id) ON DELETE CASCADE,
    run_id UUID NOT NULL REFERENCES lexsond.probe_runs(run_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (event_type IN ('DOWN', 'DEGRADED', 'RECOVERED')),
    from_status TEXT NOT NULL CHECK (from_status IN ('UNKNOWN', 'UP', 'DEGRADED', 'DOWN')),
    to_status TEXT NOT NULL CHECK (to_status IN ('UNKNOWN', 'UP', 'DEGRADED', 'DOWN')),
    error_class TEXT CHECK (
        error_class IS NULL OR error_class ~ '^[A-Z][A-Z0-9_]{2,127}$'
    ),
    observed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (policy_id, run_id, event_type)
);

CREATE INDEX idx_monitor_incidents_observed
    ON lexsond.monitor_incident_events (observed_at DESC);

CREATE TRIGGER monitor_samples_are_immutable
BEFORE UPDATE ON lexsond.monitor_samples
FOR EACH ROW EXECUTE FUNCTION lexsond.reject_append_only_mutation();

CREATE TRIGGER monitor_incidents_are_immutable
BEFORE UPDATE ON lexsond.monitor_incident_events
FOR EACH ROW EXECUTE FUNCTION lexsond.reject_append_only_mutation();

REVOKE ALL ON lexsond.monitor_policies, lexsond.monitor_states,
    lexsond.monitor_samples, lexsond.monitor_incident_events FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE, DELETE ON lexsond.monitor_policies,
    lexsond.monitor_states TO lexsond_control;
GRANT SELECT, INSERT, DELETE ON lexsond.monitor_samples,
    lexsond.monitor_incident_events TO lexsond_control;
GRANT SELECT ON lexsond.monitor_policies, lexsond.monitor_states,
    lexsond.monitor_samples, lexsond.monitor_incident_events TO lexsond_reader;

COMMIT;
