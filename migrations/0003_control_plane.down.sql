BEGIN;

DROP TRIGGER IF EXISTS suite_revisions_are_immutable ON lexsond.suite_revisions;
ALTER TABLE IF EXISTS lexsond.suites DROP CONSTRAINT IF EXISTS suites_latest_revision_fk;
DROP TABLE IF EXISTS lexsond.probe_run_events;
DROP TABLE IF EXISTS lexsond.probe_runs;
DROP TABLE IF EXISTS lexsond.suite_revisions;
DROP TABLE IF EXISTS lexsond.suites;
DROP TABLE IF EXISTS lexsond.targets;

COMMIT;
