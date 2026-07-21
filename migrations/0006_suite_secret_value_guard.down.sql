BEGIN;

ALTER TABLE IF EXISTS lexsond.suite_revisions
    DROP CONSTRAINT IF EXISTS suite_revisions_no_secret_values;
ALTER TABLE IF EXISTS lexsond.probe_suite_snapshots
    DROP CONSTRAINT IF EXISTS probe_suite_snapshots_no_secret_values;
DROP FUNCTION IF EXISTS lexsond.contains_recognizable_secret_value(JSONB);

COMMIT;
