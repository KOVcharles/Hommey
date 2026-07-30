CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT,
    request_id TEXT,
    filename TEXT NOT NULL,
    mime_type TEXT,
    kind TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    sha256 TEXT,
    object_key TEXT NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_attachments_user_created
    ON attachments (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_attachments_status
    ON attachments (status)
    WHERE status IN ('uploaded', 'queued', 'processing');

CREATE INDEX IF NOT EXISTS idx_attachments_expires
    ON attachments (expires_at)
    WHERE expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS attachment_extractions (
    attachment_id TEXT PRIMARY KEY
        REFERENCES attachments(id) ON DELETE CASCADE,
    parser_version TEXT,
    language TEXT,
    content_text TEXT,
    structured JSONB NOT NULL DEFAULT '{}'::jsonb,
    char_count INTEGER NOT NULL DEFAULT 0,
    extracted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS chat_message_attachments (
    chat_history_id BIGINT NOT NULL
        REFERENCES chat_history(id) ON DELETE CASCADE,
    attachment_id TEXT NOT NULL
        REFERENCES attachments(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chat_history_id, attachment_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_message_attachments_attachment
    ON chat_message_attachments (attachment_id);
