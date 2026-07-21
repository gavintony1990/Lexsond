BEGIN;

CREATE TABLE lexsond.agent_sessions (
    session_id UUID PRIMARY KEY,
    title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 120)
        CHECK (title !~ '(sk-|gsk_|xai-|nvapi-|csk-|pplx-)[A-Za-z0-9_-]{8,}'),
    target_id UUID NOT NULL REFERENCES lexsond.targets(target_id) ON DELETE RESTRICT,
    target_version INTEGER NOT NULL CHECK (target_version >= 1),
    base_url TEXT NOT NULL CHECK (base_url !~ '[?#]' AND base_url !~ '://[^/]*@')
        CHECK (base_url !~ '(sk-|gsk_|xai-|nvapi-|csk-|pplx-)[A-Za-z0-9_-]{8,}'),
    target_kind TEXT NOT NULL CHECK (target_kind IN ('local', 'cloud')),
    provider_id TEXT,
    model TEXT NOT NULL CHECK (char_length(model) BETWEEN 1 AND 256)
        CHECK (model !~ '(sk-|gsk_|xai-|nvapi-|csk-|pplx-)[A-Za-z0-9_-]{8,}'),
    skill_id TEXT NOT NULL CHECK (skill_id ~ '^[a-z0-9][a-z0-9-]{0,63}$'),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    archived_at TIMESTAMPTZ,
    turn_lease_token UUID,
    turn_lease_until TIMESTAMPTZ,
    CHECK ((turn_lease_token IS NULL) = (turn_lease_until IS NULL))
);

CREATE INDEX idx_agent_sessions_updated
    ON lexsond.agent_sessions (updated_at DESC);
CREATE INDEX idx_agent_sessions_target
    ON lexsond.agent_sessions (target_id, updated_at DESC);

CREATE TABLE lexsond.agent_messages (
    session_id UUID NOT NULL REFERENCES lexsond.agent_sessions(session_id) ON DELETE CASCADE,
    sequence BIGINT NOT NULL CHECK (sequence >= 1),
    message_id UUID NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL CHECK (char_length(content) <= 12000),
    metadata_json JSONB NOT NULL DEFAULT '{}'::JSONB
        CHECK (jsonb_typeof(metadata_json) = 'object')
        CHECK (NOT lexsond.contains_forbidden_secret_key(metadata_json))
        CHECK (metadata_json::TEXT !~ '(sk-|gsk_|xai-|nvapi-|csk-|pplx-)[A-Za-z0-9_-]{8,}'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (session_id, sequence),
    CHECK (content !~* '(authorization[[:space:]]*[:=][[:space:]]*(bearer[[:space:]]+)?[^[:space:],;]+)'),
    CHECK (content !~ '(sk-|gsk_|xai-|nvapi-|csk-|pplx-)[A-Za-z0-9_-]{8,}')
);

CREATE TABLE lexsond.agent_events (
    session_id UUID NOT NULL REFERENCES lexsond.agent_sessions(session_id) ON DELETE CASCADE,
    sequence BIGINT NOT NULL CHECK (sequence >= 1),
    event_id UUID NOT NULL UNIQUE,
    event_type TEXT NOT NULL CHECK (event_type ~ '^[A-Z][A-Z0-9_]{2,127}$'),
    name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 128)
        CHECK (name !~ '(sk-|gsk_|xai-|nvapi-|csk-|pplx-)[A-Za-z0-9_-]{8,}'),
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'PASS', 'WARN', 'FAIL')),
    payload_json JSONB NOT NULL DEFAULT '{}'::JSONB
        CHECK (jsonb_typeof(payload_json) = 'object')
        CHECK (NOT lexsond.contains_forbidden_secret_key(payload_json))
        CHECK (payload_json::TEXT !~ '(sk-|gsk_|xai-|nvapi-|csk-|pplx-)[A-Za-z0-9_-]{8,}'),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (session_id, sequence)
);

REVOKE ALL ON lexsond.agent_sessions, lexsond.agent_messages,
    lexsond.agent_events FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE, DELETE ON lexsond.agent_sessions,
    lexsond.agent_messages, lexsond.agent_events TO lexsond_control;
GRANT SELECT ON lexsond.agent_sessions, lexsond.agent_messages,
    lexsond.agent_events TO lexsond_reader;

COMMIT;
