-- Link ready attachments to the UUID message fact source used by stage-1 memory.
-- The legacy BIGINT link table remains untouched for safe upgrades.

CREATE TABLE IF NOT EXISTS conversation_message_attachments (
    message_id UUID NOT NULL
        REFERENCES conversation_messages(message_id) ON DELETE CASCADE,
    attachment_id TEXT NOT NULL
        REFERENCES attachments(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (message_id, attachment_id),
    UNIQUE (attachment_id)
);

CREATE INDEX IF NOT EXISTS idx_conversation_message_attachments_attachment
    ON conversation_message_attachments (attachment_id);
