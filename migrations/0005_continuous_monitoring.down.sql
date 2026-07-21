BEGIN;

DROP TRIGGER IF EXISTS monitor_incidents_are_immutable
    ON lexsond.monitor_incident_events;
DROP TRIGGER IF EXISTS monitor_samples_are_immutable
    ON lexsond.monitor_samples;
DROP TABLE IF EXISTS lexsond.monitor_incident_events;
DROP TABLE IF EXISTS lexsond.monitor_samples;
DROP TABLE IF EXISTS lexsond.monitor_states;
DROP INDEX IF EXISTS lexsond.idx_probe_runs_monitor_policy;
ALTER TABLE IF EXISTS lexsond.probe_runs
    DROP COLUMN IF EXISTS monitor_policy_id;
DROP TABLE IF EXISTS lexsond.monitor_policies;

COMMIT;
