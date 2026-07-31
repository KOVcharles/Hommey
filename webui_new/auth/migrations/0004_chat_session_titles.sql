CREATE TABLE IF NOT EXISTS chat_session_titles (
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_session_titles_user_updated
    ON chat_session_titles (user_id, updated_at DESC);
