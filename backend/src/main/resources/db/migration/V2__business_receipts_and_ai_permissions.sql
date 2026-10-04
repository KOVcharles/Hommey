CREATE TABLE business_operation_receipts (
    user_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    operation_id CHAR(64) NOT NULL,
    payload_hash CHAR(64) NOT NULL,
    receipt JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, request_id, operation_id)
);

-- The AI service may read session ownership for checkpoint fencing. It cannot
-- write users, profiles, sessions, messages, trips, preferences or receipts.
-- Attachments are AI asset metadata; only authenticated internal capability
-- routes expose them. RAG and evaluation data remain AI-owned.
DO $$
DECLARE table_name TEXT;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hommey_ai') THEN
        GRANT USAGE ON SCHEMA public TO hommey_ai;
        GRANT SELECT ON conversation_sessions, conversation_messages TO hommey_ai;
        FOREACH table_name IN ARRAY ARRAY[
            'supervisor_runs', 'attachments', 'attachment_extractions'
        ] LOOP
            IF to_regclass('public.' || table_name) IS NOT NULL THEN
                EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I TO hommey_ai', table_name);
            END IF;
        END LOOP;
        FOR table_name IN SELECT tablename FROM pg_tables WHERE schemaname='public'
            AND (tablename LIKE 'rag\_%' ESCAPE '\' OR tablename LIKE 'evaluation\_%' ESCAPE '\'
                 OR tablename LIKE 'agent\_evaluation%' ESCAPE '\')
        LOOP
            EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I TO hommey_ai', table_name);
        END LOOP;
        GRANT SELECT ON conversation_message_attachments TO hommey_ai;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO hommey_ai;
    END IF;
END $$;
