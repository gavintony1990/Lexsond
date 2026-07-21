BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lexsond_control') THEN
        CREATE ROLE lexsond_control NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lexsond_worker') THEN
        CREATE ROLE lexsond_worker NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lexsond_reader') THEN
        CREATE ROLE lexsond_reader NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lexsond_runtime_owner') THEN
        CREATE ROLE lexsond_runtime_owner NOLOGIN;
    END IF;
END;
$$;

REVOKE ALL ON SCHEMA lexsond FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA lexsond FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA lexsond FROM PUBLIC;

GRANT USAGE ON SCHEMA lexsond
    TO lexsond_control, lexsond_worker, lexsond_reader,
       lexsond_runtime_owner;

GRANT SELECT ON lexsond.endpoint_snapshots,
    lexsond.probe_suite_snapshots,
    lexsond.workflow_runs,
    lexsond.workflow_events,
    lexsond.activity_executions,
    lexsond.probe_results TO lexsond_runtime_owner;
GRANT INSERT ON lexsond.workflow_runs,
    lexsond.workflow_events,
    lexsond.activity_executions,
    lexsond.probe_results TO lexsond_runtime_owner;
GRANT UPDATE ON lexsond.workflow_runs,
    lexsond.activity_executions TO lexsond_runtime_owner;

ALTER FUNCTION lexsond.contains_forbidden_secret_key(JSONB)
    OWNER TO lexsond_runtime_owner;
GRANT EXECUTE ON FUNCTION lexsond.contains_forbidden_secret_key(JSONB)
    TO lexsond_control, lexsond_runtime_owner;

ALTER FUNCTION lexsond.create_workflow_run(UUID, CHAR, JSONB)
    OWNER TO lexsond_runtime_owner;
ALTER FUNCTION lexsond.append_workflow_event(
    UUID, BIGINT, JSONB
) OWNER TO lexsond_runtime_owner;
ALTER FUNCTION lexsond.claim_activity_execution(
    TEXT, UUID, TEXT, TEXT, INTEGER, UUID, INTEGER
) OWNER TO lexsond_runtime_owner;
ALTER FUNCTION lexsond.renew_activity_execution(
    TEXT, UUID, TEXT, TEXT, INTEGER, UUID, INTEGER
) OWNER TO lexsond_runtime_owner;
ALTER FUNCTION lexsond.complete_activity_execution(
    TEXT, UUID, TEXT, TEXT, INTEGER, UUID, TEXT, TEXT
) OWNER TO lexsond_runtime_owner;
ALTER FUNCTION lexsond.fail_activity_execution(
    TEXT, UUID, TEXT, TEXT, INTEGER, UUID, TEXT, TEXT, BOOLEAN
) OWNER TO lexsond_runtime_owner;
ALTER FUNCTION lexsond.persist_probe_result(UUID, TEXT, CHAR, JSONB)
    OWNER TO lexsond_runtime_owner;

GRANT SELECT, INSERT ON lexsond.endpoint_snapshots,
    lexsond.probe_suite_snapshots TO lexsond_control;
GRANT SELECT ON ALL TABLES IN SCHEMA lexsond TO lexsond_control;

GRANT SELECT ON lexsond.endpoint_snapshots,
    lexsond.probe_suite_snapshots,
    lexsond.workflow_runs,
    lexsond.workflow_events,
    lexsond.probe_results,
    lexsond.evidence_objects,
    lexsond.activity_executions TO lexsond_worker;
GRANT INSERT ON lexsond.evidence_objects TO lexsond_worker;
GRANT EXECUTE ON FUNCTION lexsond.create_workflow_run(UUID, CHAR, JSONB),
    lexsond.append_workflow_event(UUID, BIGINT, JSONB),
    lexsond.claim_activity_execution(TEXT, UUID, TEXT, TEXT, INTEGER, UUID, INTEGER),
    lexsond.renew_activity_execution(TEXT, UUID, TEXT, TEXT, INTEGER, UUID, INTEGER),
    lexsond.complete_activity_execution(TEXT, UUID, TEXT, TEXT, INTEGER, UUID, TEXT, TEXT),
    lexsond.fail_activity_execution(TEXT, UUID, TEXT, TEXT, INTEGER, UUID, TEXT, TEXT, BOOLEAN),
    lexsond.persist_probe_result(UUID, TEXT, CHAR, JSONB)
    TO lexsond_worker;

GRANT SELECT ON lexsond.endpoint_snapshots,
    lexsond.probe_suite_snapshots,
    lexsond.workflow_runs,
    lexsond.workflow_events,
    lexsond.probe_results,
    lexsond.evidence_objects TO lexsond_reader;

ALTER DEFAULT PRIVILEGES IN SCHEMA lexsond
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

COMMIT;
