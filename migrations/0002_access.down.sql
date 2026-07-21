BEGIN;

REVOKE ALL ON SCHEMA lexsond
    FROM lexsond_control, lexsond_worker, lexsond_reader,
         lexsond_runtime_owner;
REVOKE ALL ON ALL TABLES IN SCHEMA lexsond
    FROM lexsond_control, lexsond_worker, lexsond_reader,
         lexsond_runtime_owner;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA lexsond
    FROM lexsond_control, lexsond_worker, lexsond_reader;

-- Roles are cluster-global and may have grants in other databases. They are
-- intentionally retained; platform provisioning owns their final removal.

COMMIT;
