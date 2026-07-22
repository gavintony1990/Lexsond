BEGIN;

CREATE TABLE lexsond.credential_profiles (
    credential_id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL
        REFERENCES lexsond.workspaces(workspace_id) ON DELETE RESTRICT,
    label TEXT NOT NULL CHECK (char_length(label) BETWEEN 1 AND 120)
        CHECK (NOT lexsond.contains_recognizable_secret_value(to_jsonb(label))),
    provider_id TEXT NOT NULL
        CHECK (provider_id ~ '^[a-z0-9][a-z0-9-]{0,63}$'),
    storage_backend TEXT NOT NULL CHECK (storage_backend IN (
        'SYSTEM_KEYRING', 'EXTERNAL_SECRET_MANAGER'
    )),
    secret_locator UUID NOT NULL,
    masked_suffix TEXT NOT NULL DEFAULT ''
        CHECK (masked_suffix ~ '^[A-Za-z0-9_-]{0,8}$'),
    fingerprint CHAR(64) NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    idempotency_key UUID,
    request_sha256 CHAR(64) CHECK (
        request_sha256 IS NULL OR request_sha256 ~ '^[0-9a-f]{64}$'
    ),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN (
        'ACTIVE', 'AUTHENTICATION_FAILED', 'PERMISSION_DENIED',
        'BALANCE_UNAVAILABLE', 'RATE_LIMITED', 'CATALOG_UNAVAILABLE',
        'VAULT_UNAVAILABLE', 'DELETION_PENDING', 'ARCHIVED'
    )),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    last_verified_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    archived_at TIMESTAMPTZ,
    UNIQUE (workspace_id, credential_id),
    UNIQUE (workspace_id, fingerprint),
    UNIQUE (workspace_id, idempotency_key),
    CHECK ((idempotency_key IS NULL) = (request_sha256 IS NULL)),
    CHECK ((status = 'ARCHIVED') = (archived_at IS NOT NULL))
);

CREATE UNIQUE INDEX idx_credential_profiles_workspace_label_active
    ON lexsond.credential_profiles (workspace_id, label)
    WHERE archived_at IS NULL;
CREATE INDEX idx_credential_profiles_workspace_updated
    ON lexsond.credential_profiles (workspace_id, updated_at DESC, credential_id);

CREATE TABLE lexsond.target_credential_bindings (
    binding_id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    target_id UUID NOT NULL,
    credential_id UUID NOT NULL,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (workspace_id, target_id)
        REFERENCES lexsond.targets(workspace_id, target_id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, credential_id)
        REFERENCES lexsond.credential_profiles(workspace_id, credential_id)
        ON DELETE RESTRICT,
    UNIQUE (workspace_id, target_id, credential_id)
);

CREATE UNIQUE INDEX idx_target_credential_one_default
    ON lexsond.target_credential_bindings (workspace_id, target_id)
    WHERE is_default;
CREATE INDEX idx_target_credential_by_profile
    ON lexsond.target_credential_bindings (workspace_id, credential_id);

CREATE TABLE lexsond.credential_audit_events (
    event_id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL
        REFERENCES lexsond.workspaces(workspace_id) ON DELETE RESTRICT,
    credential_id UUID NOT NULL,
    actor_user_id UUID REFERENCES lexsond.users(user_id) ON DELETE SET NULL,
    action TEXT NOT NULL CHECK (action IN (
        'CREATE', 'RENAME', 'REPLACE', 'VERIFY', 'ARCHIVE', 'DELETE_SECRET'
    )),
    outcome TEXT NOT NULL CHECK (outcome IN ('SUCCESS', 'REJECTED', 'ERROR')),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (workspace_id, credential_id)
        REFERENCES lexsond.credential_profiles(workspace_id, credential_id)
        ON DELETE RESTRICT
);

CREATE INDEX idx_credential_audit_workspace_time
    ON lexsond.credential_audit_events (workspace_id, occurred_at DESC, event_id);

REVOKE ALL ON lexsond.credential_profiles,
    lexsond.target_credential_bindings,
    lexsond.credential_audit_events FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE, DELETE ON lexsond.credential_profiles,
    lexsond.target_credential_bindings TO lexsond_control;
GRANT SELECT, INSERT ON lexsond.credential_audit_events TO lexsond_control;
GRANT SELECT ON lexsond.credential_profiles,
    lexsond.target_credential_bindings,
    lexsond.credential_audit_events TO lexsond_reader;

COMMIT;
