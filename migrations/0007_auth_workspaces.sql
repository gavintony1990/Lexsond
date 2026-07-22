BEGIN;

CREATE TABLE lexsond.users (
    user_id UUID PRIMARY KEY,
    email_normalized TEXT NOT NULL UNIQUE
        CHECK (email_normalized = lower(btrim(email_normalized)))
        CHECK (char_length(email_normalized) BETWEEN 3 AND 320),
    email_display TEXT NOT NULL CHECK (char_length(email_display) BETWEEN 3 AND 320),
    password_hash TEXT
        CHECK (password_hash IS NULL OR password_hash ~ '^\$argon2id\$'),
    display_name TEXT NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 120),
    avatar_url TEXT CHECK (
        avatar_url IS NULL OR (
            avatar_url ~ '^https://[^/?#@]+(/[^#]*)?$'
            AND avatar_url !~ '://[^/]*@'
            AND char_length(avatar_url) <= 2048
        )
    ),
    status TEXT NOT NULL CHECK (status IN (
        'PENDING_VERIFICATION', 'ACTIVE', 'SUSPENDED', 'DELETED'
    )),
    system_role TEXT NOT NULL DEFAULT 'USER'
        CHECK (system_role IN ('USER', 'ADMIN')),
    email_verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    deleted_at TIMESTAMPTZ,
    CHECK ((status = 'DELETED') = (deleted_at IS NOT NULL)),
    CHECK (status <> 'ACTIVE' OR email_verified_at IS NOT NULL)
);

CREATE TABLE lexsond.workspaces (
    workspace_id UUID PRIMARY KEY,
    name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
    slug TEXT NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    workspace_kind TEXT NOT NULL CHECK (workspace_kind IN ('PERSONAL', 'TEAM', 'LEGACY')),
    owner_user_id UUID REFERENCES lexsond.users(user_id) ON DELETE RESTRICT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    deleted_at TIMESTAMPTZ,
    CHECK (workspace_kind <> 'PERSONAL' OR owner_user_id IS NOT NULL)
);

CREATE TABLE lexsond.workspace_members (
    workspace_id UUID NOT NULL
        REFERENCES lexsond.workspaces(workspace_id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES lexsond.users(user_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('OWNER', 'ADMIN', 'MEMBER', 'VIEWER')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (workspace_id, user_id)
);

CREATE UNIQUE INDEX idx_workspace_single_owner
    ON lexsond.workspace_members (workspace_id) WHERE role = 'OWNER';
CREATE INDEX idx_workspace_members_user
    ON lexsond.workspace_members (user_id, workspace_id);

CREATE TABLE lexsond.oauth_identities (
    identity_id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES lexsond.users(user_id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('github', 'google')),
    provider_subject TEXT NOT NULL CHECK (char_length(provider_subject) BETWEEN 1 AND 255),
    provider_email TEXT CHECK (char_length(provider_email) BETWEEN 3 AND 320),
    provider_email_verified BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_login_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (provider, provider_subject),
    UNIQUE (user_id, provider)
);

CREATE TABLE lexsond.auth_sessions (
    session_id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES lexsond.users(user_id) ON DELETE CASCADE,
    active_workspace_id UUID NOT NULL,
    token_hash BYTEA NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
    csrf_secret_hash BYTEA NOT NULL CHECK (octet_length(csrf_secret_hash) = 32),
    user_agent_hash BYTEA NOT NULL CHECK (octet_length(user_agent_hash) = 32),
    ip_prefix INET,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    idle_expires_at TIMESTAMPTZ NOT NULL,
    absolute_expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    FOREIGN KEY (active_workspace_id, user_id)
        REFERENCES lexsond.workspace_members(workspace_id, user_id) ON DELETE CASCADE,
    CHECK (idle_expires_at <= absolute_expires_at),
    CHECK (absolute_expires_at > created_at)
);

CREATE INDEX idx_auth_sessions_user_active
    ON lexsond.auth_sessions (user_id, absolute_expires_at DESC)
    WHERE revoked_at IS NULL;
CREATE INDEX idx_auth_sessions_expiry
    ON lexsond.auth_sessions (idle_expires_at, absolute_expires_at)
    WHERE revoked_at IS NULL;

CREATE TABLE lexsond.auth_session_csrf_tokens (
    session_id UUID NOT NULL
        REFERENCES lexsond.auth_sessions(session_id) ON DELETE CASCADE,
    csrf_secret_hash BYTEA NOT NULL CHECK (octet_length(csrf_secret_hash) = 32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (session_id, csrf_secret_hash)
);

CREATE INDEX idx_auth_session_csrf_tokens_created
    ON lexsond.auth_session_csrf_tokens (session_id, created_at DESC);

CREATE TABLE lexsond.auth_action_tokens (
    token_id UUID PRIMARY KEY,
    user_id UUID REFERENCES lexsond.users(user_id) ON DELETE CASCADE,
    purpose TEXT NOT NULL CHECK (purpose IN (
        'verify_email', 'reset_password', 'change_email', 'claim_legacy_workspace'
    )),
    token_hash BYTEA NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
    pending_email_normalized TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    CHECK (expires_at > created_at),
    CHECK ((purpose = 'claim_legacy_workspace') OR user_id IS NOT NULL),
    CHECK ((purpose = 'change_email') = (pending_email_normalized IS NOT NULL))
);

CREATE INDEX idx_auth_action_tokens_active
    ON lexsond.auth_action_tokens (purpose, expires_at)
    WHERE consumed_at IS NULL;

CREATE TABLE lexsond.auth_audit_events (
    event_id UUID PRIMARY KEY,
    user_id UUID REFERENCES lexsond.users(user_id) ON DELETE SET NULL,
    provider TEXT CHECK (provider IS NULL OR provider IN ('password', 'github', 'google', 'local')),
    category TEXT NOT NULL CHECK (category IN (
        'REGISTER', 'VERIFY_EMAIL', 'VERIFY_EMAIL_RESEND', 'LOGIN', 'LOGOUT', 'LOGOUT_ALL',
        'PASSWORD_RESET_REQUEST', 'PASSWORD_RESET', 'PASSWORD_CHANGE',
        'SESSION_REVOKE', 'OAUTH_START', 'OAUTH_CALLBACK', 'OAUTH_LINK',
        'OAUTH_UNLINK', 'WORKSPACE_SELECT', 'ACCOUNT_DELETE'
    )),
    outcome TEXT NOT NULL CHECK (outcome IN (
        'SUCCESS', 'INVALID_CREDENTIALS', 'INVALID_TOKEN', 'EXPIRED_TOKEN',
        'CSRF_REJECTED', 'RATE_LIMITED', 'CONFLICT', 'FORBIDDEN', 'ERROR'
    )),
    ip_prefix INET,
    user_agent_hash BYTEA CHECK (
        user_agent_hash IS NULL OR octet_length(user_agent_hash) = 32
    ),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX idx_auth_audit_user_time
    ON lexsond.auth_audit_events (user_id, occurred_at DESC);
CREATE INDEX idx_auth_audit_time
    ON lexsond.auth_audit_events (occurred_at DESC);

CREATE TABLE lexsond.auth_rate_limits (
    bucket TEXT NOT NULL CHECK (bucket ~ '^[A-Z][A-Z0-9_]{2,63}$'),
    subject_hash BYTEA NOT NULL CHECK (octet_length(subject_hash) = 32),
    window_started_at TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts >= 1),
    blocked_until TIMESTAMPTZ,
    PRIMARY KEY (bucket, subject_hash)
);

CREATE INDEX idx_auth_rate_limits_cleanup
    ON lexsond.auth_rate_limits (window_started_at, blocked_until);

INSERT INTO lexsond.workspaces (
    workspace_id, name, slug, workspace_kind, owner_user_id
) VALUES (
    '00000000-0000-4000-8000-000000000001',
    'Legacy Workspace',
    'legacy-workspace',
    'LEGACY',
    NULL
);

ALTER TABLE lexsond.targets ADD COLUMN workspace_id UUID;
ALTER TABLE lexsond.suites
    ADD COLUMN workspace_id UUID,
    ADD COLUMN scope TEXT NOT NULL DEFAULT 'WORKSPACE'
        CHECK (scope IN ('SYSTEM', 'WORKSPACE'));
ALTER TABLE lexsond.suite_revisions ADD COLUMN workspace_id UUID;
ALTER TABLE lexsond.probe_runs ADD COLUMN workspace_id UUID;
ALTER TABLE lexsond.agent_sessions ADD COLUMN workspace_id UUID;
ALTER TABLE lexsond.monitor_policies ADD COLUMN workspace_id UUID;

DO $$
DECLARE
    legacy_workspace UUID := '00000000-0000-4000-8000-000000000001';
BEGIN
    UPDATE lexsond.targets SET workspace_id = legacy_workspace WHERE workspace_id IS NULL;
    UPDATE lexsond.suites SET workspace_id = legacy_workspace WHERE workspace_id IS NULL;
    UPDATE lexsond.suite_revisions SET workspace_id = legacy_workspace WHERE workspace_id IS NULL;
    UPDATE lexsond.probe_runs SET workspace_id = legacy_workspace WHERE workspace_id IS NULL;
    UPDATE lexsond.agent_sessions SET workspace_id = legacy_workspace WHERE workspace_id IS NULL;
    UPDATE lexsond.monitor_policies SET workspace_id = legacy_workspace WHERE workspace_id IS NULL;
END;
$$;

ALTER TABLE lexsond.targets ALTER COLUMN workspace_id SET NOT NULL;
ALTER TABLE lexsond.suites ALTER COLUMN workspace_id SET NOT NULL;
ALTER TABLE lexsond.suite_revisions ALTER COLUMN workspace_id SET NOT NULL;
ALTER TABLE lexsond.probe_runs ALTER COLUMN workspace_id SET NOT NULL;
ALTER TABLE lexsond.agent_sessions ALTER COLUMN workspace_id SET NOT NULL;
ALTER TABLE lexsond.monitor_policies ALTER COLUMN workspace_id SET NOT NULL;

ALTER TABLE lexsond.targets DROP CONSTRAINT targets_name_key;
ALTER TABLE lexsond.suites DROP CONSTRAINT suites_name_key;
ALTER TABLE lexsond.monitor_policies DROP CONSTRAINT monitor_policies_name_key;
ALTER TABLE lexsond.probe_runs DROP CONSTRAINT probe_runs_idempotency_key_key;

ALTER TABLE lexsond.targets
    ADD FOREIGN KEY (workspace_id) REFERENCES lexsond.workspaces(workspace_id) ON DELETE RESTRICT,
    ADD UNIQUE (workspace_id, target_id),
    ADD UNIQUE (workspace_id, name);
ALTER TABLE lexsond.suites
    ADD FOREIGN KEY (workspace_id) REFERENCES lexsond.workspaces(workspace_id) ON DELETE RESTRICT,
    ADD UNIQUE (workspace_id, suite_id),
    ADD UNIQUE (workspace_id, name);
ALTER TABLE lexsond.suite_revisions
    ADD FOREIGN KEY (workspace_id) REFERENCES lexsond.workspaces(workspace_id) ON DELETE RESTRICT,
    ADD UNIQUE (workspace_id, revision_id);
ALTER TABLE lexsond.probe_runs
    ADD FOREIGN KEY (workspace_id) REFERENCES lexsond.workspaces(workspace_id) ON DELETE RESTRICT,
    ADD UNIQUE (workspace_id, run_id);
CREATE UNIQUE INDEX idx_probe_runs_workspace_idempotency
    ON lexsond.probe_runs (workspace_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
ALTER TABLE lexsond.agent_sessions
    ADD FOREIGN KEY (workspace_id) REFERENCES lexsond.workspaces(workspace_id) ON DELETE RESTRICT,
    ADD UNIQUE (workspace_id, session_id);
ALTER TABLE lexsond.monitor_policies
    ADD FOREIGN KEY (workspace_id) REFERENCES lexsond.workspaces(workspace_id) ON DELETE RESTRICT,
    ADD UNIQUE (workspace_id, policy_id),
    ADD UNIQUE (workspace_id, name);

ALTER TABLE lexsond.suite_revisions
    DROP CONSTRAINT suite_revisions_suite_id_fkey,
    ADD CONSTRAINT suite_revisions_workspace_suite_fkey
        FOREIGN KEY (workspace_id, suite_id)
        REFERENCES lexsond.suites(workspace_id, suite_id) ON DELETE RESTRICT;
ALTER TABLE lexsond.probe_runs
    DROP CONSTRAINT probe_runs_target_id_fkey,
    DROP CONSTRAINT probe_runs_suite_revision_id_fkey,
    DROP CONSTRAINT probe_runs_monitor_policy_id_fkey,
    ADD CONSTRAINT probe_runs_workspace_target_fkey
        FOREIGN KEY (workspace_id, target_id)
        REFERENCES lexsond.targets(workspace_id, target_id) ON DELETE RESTRICT,
    ADD CONSTRAINT probe_runs_workspace_revision_fkey
        FOREIGN KEY (workspace_id, suite_revision_id)
        REFERENCES lexsond.suite_revisions(workspace_id, revision_id) ON DELETE RESTRICT,
    ADD CONSTRAINT probe_runs_workspace_monitor_policy_fkey
        FOREIGN KEY (workspace_id, monitor_policy_id)
        REFERENCES lexsond.monitor_policies(workspace_id, policy_id)
        ON DELETE SET NULL (monitor_policy_id);
ALTER TABLE lexsond.agent_sessions
    DROP CONSTRAINT agent_sessions_target_id_fkey,
    ADD CONSTRAINT agent_sessions_workspace_target_fkey
        FOREIGN KEY (workspace_id, target_id)
        REFERENCES lexsond.targets(workspace_id, target_id) ON DELETE RESTRICT;
ALTER TABLE lexsond.monitor_policies
    DROP CONSTRAINT monitor_policies_target_id_fkey,
    DROP CONSTRAINT monitor_policies_suite_revision_id_fkey,
    ADD CONSTRAINT monitor_policies_workspace_target_fkey
        FOREIGN KEY (workspace_id, target_id)
        REFERENCES lexsond.targets(workspace_id, target_id) ON DELETE RESTRICT,
    ADD CONSTRAINT monitor_policies_workspace_revision_fkey
        FOREIGN KEY (workspace_id, suite_revision_id)
        REFERENCES lexsond.suite_revisions(workspace_id, revision_id) ON DELETE RESTRICT;

CREATE INDEX idx_targets_workspace_updated
    ON lexsond.targets (workspace_id, updated_at DESC, target_id);
CREATE INDEX idx_suites_workspace_updated
    ON lexsond.suites (workspace_id, updated_at DESC, suite_id);
CREATE INDEX idx_probe_runs_workspace_created
    ON lexsond.probe_runs (workspace_id, created_at DESC, run_id);
CREATE INDEX idx_agent_sessions_workspace_updated
    ON lexsond.agent_sessions (workspace_id, updated_at DESC, session_id);
CREATE INDEX idx_monitor_policies_workspace_updated
    ON lexsond.monitor_policies (workspace_id, updated_at DESC, policy_id);

REVOKE ALL ON lexsond.users, lexsond.oauth_identities,
    lexsond.auth_sessions, lexsond.auth_session_csrf_tokens,
    lexsond.auth_action_tokens,
    lexsond.workspaces, lexsond.workspace_members,
    lexsond.auth_audit_events, lexsond.auth_rate_limits FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE, DELETE ON lexsond.users,
    lexsond.oauth_identities, lexsond.auth_sessions,
    lexsond.auth_session_csrf_tokens,
    lexsond.auth_action_tokens, lexsond.workspaces,
    lexsond.workspace_members TO lexsond_control;
GRANT SELECT, INSERT ON lexsond.auth_audit_events TO lexsond_control;
GRANT SELECT, INSERT, UPDATE, DELETE ON lexsond.auth_rate_limits TO lexsond_control;

COMMIT;
