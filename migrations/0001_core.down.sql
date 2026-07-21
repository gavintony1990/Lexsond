BEGIN;

DROP TRIGGER IF EXISTS probe_results_are_append_only ON lexsond.probe_results;
DROP TRIGGER IF EXISTS probe_suite_snapshots_are_append_only ON lexsond.probe_suite_snapshots;
DROP TRIGGER IF EXISTS endpoint_snapshots_are_append_only ON lexsond.endpoint_snapshots;
DROP TRIGGER IF EXISTS workflow_events_are_append_only ON lexsond.workflow_events;
DROP FUNCTION IF EXISTS lexsond.reject_append_only_mutation();
DROP FUNCTION IF EXISTS lexsond.persist_probe_result(UUID, TEXT, CHAR, JSONB);
DROP FUNCTION IF EXISTS lexsond.renew_activity_execution(
    TEXT, UUID, TEXT, TEXT, INTEGER, UUID, INTEGER
);
DROP FUNCTION IF EXISTS lexsond.fail_activity_execution(
    TEXT, UUID, TEXT, TEXT, INTEGER, UUID, TEXT, TEXT, BOOLEAN
);
DROP FUNCTION IF EXISTS lexsond.complete_activity_execution(
    TEXT, UUID, TEXT, TEXT, INTEGER, UUID, TEXT, TEXT
);
DROP FUNCTION IF EXISTS lexsond.claim_activity_execution(
    TEXT, UUID, TEXT, TEXT, INTEGER, UUID, INTEGER
);
DROP FUNCTION IF EXISTS lexsond.append_workflow_event(
    UUID, BIGINT, JSONB
);
DROP FUNCTION IF EXISTS lexsond.create_workflow_run(UUID, CHAR, JSONB);
DROP TABLE IF EXISTS lexsond.activity_executions;
DROP TABLE IF EXISTS lexsond.evidence_objects;
DROP TABLE IF EXISTS lexsond.probe_results;
DROP TABLE IF EXISTS lexsond.workflow_events;
DROP TABLE IF EXISTS lexsond.workflow_runs;
DROP TABLE IF EXISTS lexsond.probe_suite_snapshots;
DROP TABLE IF EXISTS lexsond.endpoint_snapshots;
DROP FUNCTION IF EXISTS lexsond.contains_forbidden_secret_key(JSONB);
DROP SCHEMA IF EXISTS lexsond;

COMMIT;
