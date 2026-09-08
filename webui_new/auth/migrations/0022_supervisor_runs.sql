-- Additive only. Legacy workflow tables and historical data remain intact.
CREATE TABLE IF NOT EXISTS supervisor_runs (
    user_id TEXT NOT NULL,
    session_id UUID NOT NULL REFERENCES conversation_sessions(session_id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    owner TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'interrupted', 'failed')),
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    checkpoint JSONB NOT NULL DEFAULT '{}'::jsonb,
    response JSONB,
    mutations JSONB NOT NULL DEFAULT '{}'::jsonb,
    retention_until TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '14 days'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, request_id)
);
CREATE INDEX IF NOT EXISTS supervisor_session_recent
    ON supervisor_runs (user_id, session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS supervisor_trip_records (
    trip_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id UUID NOT NULL REFERENCES conversation_sessions(session_id) ON DELETE CASCADE,
    record_state TEXT NOT NULL CHECK (record_state IN ('planned', 'cancelled')),
    context_data JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS supervisor_trip_user_recent
    ON supervisor_trip_records (user_id, updated_at DESC);
