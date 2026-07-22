BEGIN;

CREATE TABLE lexsond.partner_applications (
    application_id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL
        REFERENCES lexsond.workspaces(workspace_id) ON DELETE RESTRICT,
    site_name TEXT NOT NULL CHECK (char_length(site_name) BETWEEN 1 AND 120)
        CHECK (NOT lexsond.contains_recognizable_secret_value(to_jsonb(site_name))),
    website_url TEXT NOT NULL CHECK (
        website_url ~ '^https://[^/?#@]+(/[^?#]*)?$'
        AND website_url !~ '://[^/]*@' AND website_url !~ '[?#]'
    ),
    terms_url TEXT NOT NULL CHECK (
        terms_url ~ '^https://[^/?#@]+(/[^?#]*)?$'
        AND terms_url !~ '://[^/]*@' AND terms_url !~ '[?#]'
    ),
    privacy_url TEXT NOT NULL CHECK (
        privacy_url ~ '^https://[^/?#@]+(/[^?#]*)?$'
        AND privacy_url !~ '://[^/]*@' AND privacy_url !~ '[?#]'
    ),
    contact_email TEXT NOT NULL CHECK (
        char_length(contact_email) BETWEEN 3 AND 320 AND contact_email LIKE '%@%'
    ),
    api_base_url TEXT NOT NULL CHECK (
        api_base_url ~ '^https://[^/?#@]+(/[^?#]*)?$'
        AND api_base_url !~ '://[^/]*@' AND api_base_url !~ '[?#]'
    ),
    protocol TEXT NOT NULL CHECK (protocol IN (
        'openai-compatible', 'anthropic-messages', 'gemini-native'
    )),
    region TEXT NOT NULL CHECK (char_length(region) BETWEEN 2 AND 64),
    model_claims JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(model_claims) = 'array')
        CHECK (jsonb_array_length(model_claims) BETWEEN 1 AND 100)
        CHECK (NOT lexsond.contains_forbidden_secret_key(model_claims))
        CHECK (NOT lexsond.contains_recognizable_secret_value(model_claims)),
    pricing_notes TEXT NOT NULL CHECK (char_length(pricing_notes) BETWEEN 1 AND 4000)
        CHECK (NOT lexsond.contains_recognizable_secret_value(to_jsonb(pricing_notes))),
    source_evidence_url TEXT NOT NULL CHECK (
        source_evidence_url ~ '^https://[^/?#@]+(/[^#]*)?$'
        AND source_evidence_url !~ '://[^/]*@'
    ),
    monitoring_credential_id UUID,
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN (
        'DRAFT', 'SUBMITTED', 'OWNERSHIP_PENDING', 'MANUAL_REVIEW',
        'BASELINE_TEST', 'PROBATION', 'APPROVED', 'REJECTED', 'PUBLISHED'
    )),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    idempotency_key UUID,
    request_sha256 CHAR(64) CHECK (
        request_sha256 IS NULL OR request_sha256 ~ '^[0-9a-f]{64}$'
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    submitted_at TIMESTAMPTZ,
    UNIQUE (workspace_id, application_id),
    UNIQUE (workspace_id, idempotency_key),
    FOREIGN KEY (workspace_id, monitoring_credential_id)
        REFERENCES lexsond.credential_profiles(workspace_id, credential_id)
        ON DELETE RESTRICT,
    CHECK ((idempotency_key IS NULL) = (request_sha256 IS NULL)),
    CHECK ((status = 'DRAFT') = (submitted_at IS NULL))
);

CREATE INDEX idx_partner_applications_workspace_updated
    ON lexsond.partner_applications (workspace_id, updated_at DESC, application_id);
CREATE INDEX idx_partner_applications_review_queue
    ON lexsond.partner_applications (status, submitted_at, application_id)
    WHERE status <> 'DRAFT';

CREATE TABLE lexsond.partner_application_revisions (
    revision_id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    application_id UUID NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    snapshot_sha256 CHAR(64) NOT NULL CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    snapshot_json JSONB NOT NULL
        CHECK (jsonb_typeof(snapshot_json) = 'object')
        CHECK (NOT lexsond.contains_forbidden_secret_key(snapshot_json))
        CHECK (NOT lexsond.contains_recognizable_secret_value(snapshot_json)),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (workspace_id, application_id)
        REFERENCES lexsond.partner_applications(workspace_id, application_id)
        ON DELETE RESTRICT,
    UNIQUE (workspace_id, application_id, revision),
    UNIQUE (workspace_id, application_id, snapshot_sha256)
);

CREATE TRIGGER partner_application_revisions_are_immutable
BEFORE UPDATE OR DELETE ON lexsond.partner_application_revisions
FOR EACH ROW EXECUTE FUNCTION lexsond.reject_append_only_mutation();

CREATE TABLE lexsond.partner_domain_challenges (
    challenge_id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    application_id UUID NOT NULL,
    method TEXT NOT NULL CHECK (method IN ('DNS_TXT', 'WELL_KNOWN')),
    token_hash BYTEA NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    verified_at TIMESTAMPTZ,
    FOREIGN KEY (workspace_id, application_id)
        REFERENCES lexsond.partner_applications(workspace_id, application_id)
        ON DELETE RESTRICT,
    CHECK (expires_at > created_at),
    CHECK (verified_at IS NULL OR verified_at >= created_at)
);

CREATE INDEX idx_partner_domain_challenges_pending
    ON lexsond.partner_domain_challenges (workspace_id, application_id, expires_at)
    WHERE verified_at IS NULL;

REVOKE ALL ON lexsond.partner_applications,
    lexsond.partner_application_revisions,
    lexsond.partner_domain_challenges FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON lexsond.partner_applications TO lexsond_control;
GRANT SELECT, INSERT ON lexsond.partner_application_revisions,
    lexsond.partner_domain_challenges TO lexsond_control;
GRANT SELECT ON lexsond.partner_applications,
    lexsond.partner_application_revisions TO lexsond_reader;

COMMIT;
