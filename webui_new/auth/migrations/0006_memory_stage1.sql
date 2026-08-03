-- Stage-1 memory fact source. Legacy chat_history remains intact so existing
-- deployments can upgrade without destructive data loss.

CREATE TABLE IF NOT EXISTS conversation_sessions (
    session_id UUID PRIMARY KEY,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'closed')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ,
    close_reason TEXT,
    message_count INTEGER NOT NULL DEFAULT 0 CHECK (message_count >= 0),
    last_sequence BIGINT NOT NULL DEFAULT 0 CHECK (last_sequence >= 0),
    summary_watermark BIGINT NOT NULL DEFAULT 0 CHECK (summary_watermark >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_sessions_active_user
    ON conversation_sessions (user_id)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_conversation_sessions_user_activity
    ON conversation_sessions (user_id, last_active_at DESC);

CREATE INDEX IF NOT EXISTS idx_conversation_sessions_user_started
    ON conversation_sessions (user_id, started_at DESC);

CREATE TABLE IF NOT EXISTS conversation_messages (
    message_id UUID PRIMARY KEY,
    request_id UUID NOT NULL,
    turn_id UUID NOT NULL,
    session_id UUID NOT NULL REFERENCES conversation_sessions(session_id),
    user_id TEXT NOT NULL,
    sequence_no BIGINT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    content TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'text',
    token_count INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retention_until TIMESTAMPTZ NOT NULL,
    deleted_at TIMESTAMPTZ,
    UNIQUE (user_id, request_id, role),
    UNIQUE (session_id, sequence_no)
);

CREATE INDEX IF NOT EXISTS idx_conversation_messages_user_session_sequence
    ON conversation_messages (user_id, session_id, sequence_no DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_conversation_messages_user_created
    ON conversation_messages (user_id, created_at DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_conversation_messages_retention
    ON conversation_messages (retention_until)
    WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS memory_versions (
    user_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, namespace)
);

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id TEXT NOT NULL,
    pref_type TEXT NOT NULL,
    pref_value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, pref_type)
);

CREATE TABLE IF NOT EXISTS user_statistics (
    user_id TEXT PRIMARY KEY,
    total_trips INTEGER NOT NULL DEFAULT 0,
    total_messages INTEGER NOT NULL DEFAULT 0,
    total_queries INTEGER NOT NULL DEFAULT 0,
    frequent_destinations JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS active_trip_contexts (
    user_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active',
    context_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
