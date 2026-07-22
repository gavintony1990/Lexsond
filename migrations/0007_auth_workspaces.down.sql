BEGIN;

DROP INDEX IF EXISTS lexsond.idx_monitor_policies_workspace_updated;
DROP INDEX IF EXISTS lexsond.idx_agent_sessions_workspace_updated;
DROP INDEX IF EXISTS lexsond.idx_probe_runs_workspace_created;
DROP INDEX IF EXISTS lexsond.idx_probe_runs_workspace_idempotency;
DROP INDEX IF EXISTS lexsond.idx_suites_workspace_updated;
DROP INDEX IF EXISTS lexsond.idx_targets_workspace_updated;

ALTER TABLE lexsond.probe_runs
    DROP CONSTRAINT IF EXISTS probe_runs_workspace_target_fkey,
    DROP CONSTRAINT IF EXISTS probe_runs_workspace_revision_fkey,
    DROP CONSTRAINT IF EXISTS probe_runs_workspace_monitor_policy_fkey,
    ADD FOREIGN KEY (target_id) REFERENCES lexsond.targets(target_id) ON DELETE RESTRICT,
    ADD FOREIGN KEY (suite_revision_id) REFERENCES lexsond.suite_revisions(revision_id) ON DELETE RESTRICT,
    ADD FOREIGN KEY (monitor_policy_id) REFERENCES lexsond.monitor_policies(policy_id) ON DELETE SET NULL;
ALTER TABLE lexsond.agent_sessions
    DROP CONSTRAINT IF EXISTS agent_sessions_workspace_target_fkey,
    ADD FOREIGN KEY (target_id) REFERENCES lexsond.targets(target_id) ON DELETE RESTRICT;
ALTER TABLE lexsond.monitor_policies
    DROP CONSTRAINT IF EXISTS monitor_policies_workspace_target_fkey,
    DROP CONSTRAINT IF EXISTS monitor_policies_workspace_revision_fkey,
    ADD FOREIGN KEY (target_id) REFERENCES lexsond.targets(target_id) ON DELETE RESTRICT,
    ADD FOREIGN KEY (suite_revision_id) REFERENCES lexsond.suite_revisions(revision_id) ON DELETE RESTRICT;
ALTER TABLE lexsond.suite_revisions
    DROP CONSTRAINT IF EXISTS suite_revisions_workspace_suite_fkey,
    ADD FOREIGN KEY (suite_id) REFERENCES lexsond.suites(suite_id) ON DELETE RESTRICT;

-- The multi-tenant schema intentionally permits names and idempotency values
-- that collide across workspaces. Preserve every row during rollback by
-- deterministically disambiguating only the later duplicates.
UPDATE lexsond.targets AS target
SET name = left(target.name, 80) || '-' || target.target_id::text
WHERE EXISTS (
    SELECT 1 FROM lexsond.targets
    GROUP BY name HAVING count(*) > 1
);

UPDATE lexsond.suites AS suite
SET name = left(suite.name, 80) || '-' || suite.suite_id::text
WHERE EXISTS (
    SELECT 1 FROM lexsond.suites
    GROUP BY name HAVING count(*) > 1
);

UPDATE lexsond.monitor_policies AS policy
SET name = left(policy.name, 80) || '-' || policy.policy_id::text
WHERE EXISTS (
    SELECT 1 FROM lexsond.monitor_policies
    GROUP BY name HAVING count(*) > 1
);

WITH duplicates AS (
    SELECT run_id, row_number() OVER (
        PARTITION BY idempotency_key ORDER BY run_id
    ) AS ordinal
    FROM lexsond.probe_runs
    WHERE idempotency_key IS NOT NULL
)
UPDATE lexsond.probe_runs AS run
SET idempotency_key = NULL
FROM duplicates
WHERE duplicates.run_id = run.run_id AND duplicates.ordinal > 1;

ALTER TABLE lexsond.targets ADD UNIQUE (name);
ALTER TABLE lexsond.suites ADD UNIQUE (name);
ALTER TABLE lexsond.monitor_policies ADD UNIQUE (name);
ALTER TABLE lexsond.probe_runs ADD UNIQUE (idempotency_key);

ALTER TABLE lexsond.monitor_policies DROP COLUMN workspace_id;
ALTER TABLE lexsond.agent_sessions DROP COLUMN workspace_id;
ALTER TABLE lexsond.probe_runs DROP COLUMN workspace_id;
ALTER TABLE lexsond.suite_revisions DROP COLUMN workspace_id;
ALTER TABLE lexsond.suites DROP COLUMN scope, DROP COLUMN workspace_id;
ALTER TABLE lexsond.targets DROP COLUMN workspace_id;

DROP TABLE IF EXISTS lexsond.auth_rate_limits;
DROP TABLE IF EXISTS lexsond.auth_audit_events;
DROP TABLE IF EXISTS lexsond.auth_action_tokens;
DROP TABLE IF EXISTS lexsond.auth_session_csrf_tokens;
DROP TABLE IF EXISTS lexsond.auth_sessions;
DROP TABLE IF EXISTS lexsond.oauth_identities;
DROP TABLE IF EXISTS lexsond.workspace_members;
DROP TABLE IF EXISTS lexsond.workspaces;
DROP TABLE IF EXISTS lexsond.users;

COMMIT;
