BEGIN;

CREATE OR REPLACE FUNCTION lexsond.contains_recognizable_secret_value(p_value JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    child JSONB;
    child_key TEXT;
    text_value TEXT;
BEGIN
    IF jsonb_typeof(p_value) = 'string' THEN
        text_value := p_value #>> '{}';
        RETURN text_value ~ '((sk-|gsk_|xai-|nvapi-|csk-|pplx-)[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{16,})'
            OR text_value ~* 'authorization[[:space:]]*[:=][[:space:]]*(bearer[[:space:]]+)?[^[:space:],;]+';
    ELSIF jsonb_typeof(p_value) = 'object' THEN
        FOR child_key, child IN SELECT key, value FROM jsonb_each(p_value) LOOP
            IF lexsond.contains_recognizable_secret_value(to_jsonb(child_key))
                OR lexsond.contains_recognizable_secret_value(child) THEN
                RETURN TRUE;
            END IF;
        END LOOP;
    ELSIF jsonb_typeof(p_value) = 'array' THEN
        FOR child IN SELECT value FROM jsonb_array_elements(p_value) LOOP
            IF lexsond.contains_recognizable_secret_value(child) THEN
                RETURN TRUE;
            END IF;
        END LOOP;
    END IF;
    RETURN FALSE;
END;
$$;

ALTER FUNCTION lexsond.contains_recognizable_secret_value(JSONB)
    OWNER TO lexsond_runtime_owner;
REVOKE ALL ON FUNCTION lexsond.contains_recognizable_secret_value(JSONB)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lexsond.contains_recognizable_secret_value(JSONB)
    TO lexsond_control, lexsond_runtime_owner;

ALTER TABLE lexsond.suite_revisions
    ADD CONSTRAINT suite_revisions_no_secret_values
    CHECK (NOT lexsond.contains_recognizable_secret_value(document_json));

ALTER TABLE lexsond.probe_suite_snapshots
    ADD CONSTRAINT probe_suite_snapshots_no_secret_values
    CHECK (NOT lexsond.contains_recognizable_secret_value(suite_json));

COMMIT;
