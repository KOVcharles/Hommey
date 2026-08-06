-- 0012_session_summaries.sql
-- Incremental LLM conversation summaries, persisted per session and keyed by
-- conversation_sessions.summary_watermark. Created lazily on the read path.
-- One row per summarized contiguous sequence range of conversation_messages.
--
-- segment_no == source_sequence_from: deterministic, monotonic, unique among
-- successful inserts. No UNIQUE constraint on segment_no (a claim-computed value
-- races across transactions); the idempotency key is the sequence range.

CREATE TABLE IF NOT EXISTS session_summaries (
    summary_id UUID PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id UUID NOT NULL
        REFERENCES conversation_sessions(session_id) ON DELETE CASCADE,
    segment_no INTEGER NOT NULL CHECK (segment_no >= 1),
    summary_text TEXT,
    summary_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_sequence_from BIGINT NOT NULL CHECK (source_sequence_from >= 1),
    source_sequence_to BIGINT NOT NULL CHECK (source_sequence_to >= source_sequence_from),
    source_message_count INTEGER NOT NULL DEFAULT 0 CHECK (source_message_count >= 0),
    model_name TEXT,
    prompt_version TEXT,
    status TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('done', 'claimed')),
    retention_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (session_id, source_sequence_from, source_sequence_to)
);

CREATE INDEX IF NOT EXISTS idx_session_summaries_user_created
    ON session_summaries (user_id, created_at);

CREATE INDEX IF NOT EXISTS idx_session_summaries_session_seq
    ON session_summaries (session_id, source_sequence_to);
