BEGIN;

DROP FUNCTION IF EXISTS lexsond.purge_evaluation_run(UUID, UUID);
DROP FUNCTION IF EXISTS lexsond.purge_evaluation_dataset(UUID, UUID);

DROP TABLE IF EXISTS lexsond.evaluation_run_events;
DROP TABLE IF EXISTS lexsond.evaluation_run_items;
DROP TABLE IF EXISTS lexsond.evaluation_run_models;
DROP TABLE IF EXISTS lexsond.evaluation_runs;

ALTER TABLE IF EXISTS lexsond.evaluation_datasets
    DROP CONSTRAINT IF EXISTS evaluation_datasets_latest_revision_fkey;
DROP TABLE IF EXISTS lexsond.evaluation_dataset_items;
DROP TABLE IF EXISTS lexsond.evaluation_dataset_revisions;
DROP TABLE IF EXISTS lexsond.evaluation_datasets;
DROP FUNCTION IF EXISTS lexsond.require_running_evaluation_parent();
DROP FUNCTION IF EXISTS lexsond.require_sealed_evaluation_dataset_revision();
DROP FUNCTION IF EXISTS lexsond.require_open_evaluation_dataset_revision();
DROP FUNCTION IF EXISTS lexsond.protect_evaluation_dataset_revision();
DROP FUNCTION IF EXISTS lexsond.protect_evaluation_run_snapshot();
DROP FUNCTION IF EXISTS lexsond.require_evaluation_run_dataset_scope();
DROP FUNCTION IF EXISTS lexsond.protect_evaluation_run_model();
DROP FUNCTION IF EXISTS lexsond.reject_evaluation_append_only_mutation();

ALTER TABLE lexsond.model_catalog_snapshots
    DROP COLUMN IF EXISTS protocol,
    DROP COLUMN IF EXISTS target_kind,
    DROP COLUMN IF EXISTS target_base_url,
    DROP COLUMN IF EXISTS credential_version,
    DROP COLUMN IF EXISTS credential_fingerprint;

DROP FUNCTION IF EXISTS lexsond.is_safe_snapshot_base_url(TEXT);

GRANT SELECT ON lexsond.model_catalog_snapshots TO lexsond_reader;

COMMIT;
