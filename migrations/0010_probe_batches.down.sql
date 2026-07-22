BEGIN;

DROP TABLE IF EXISTS lexsond.probe_batch_events;
DROP TABLE IF EXISTS lexsond.probe_batch_items;
DROP TABLE IF EXISTS lexsond.probe_batches;
DROP TABLE IF EXISTS lexsond.model_catalog_snapshots;

ALTER TABLE lexsond.probe_runs
    DROP CONSTRAINT IF EXISTS probe_runs_workspace_credential_fkey,
    DROP COLUMN IF EXISTS credential_profile_id,
    DROP COLUMN IF EXISTS max_output_tokens;

COMMIT;
