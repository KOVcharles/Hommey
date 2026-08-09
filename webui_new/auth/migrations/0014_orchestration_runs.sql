-- Replace the user-scoped legacy checkpoint with session-scoped durable runs.
-- Old rows are intentionally discarded: they cannot be mapped safely to a session/run.
DROP TABLE IF EXISTS orchestration_checkpoints;

CREATE TABLE IF NOT EXISTS orchestration_runs (
    run_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    revision BIGINT NOT NULL DEFAULT 0,
    schema_version INTEGER NOT NULL DEFAULT 1,
    focused_goal_id TEXT,
    graph_hash TEXT NOT NULL DEFAULT '',
    state JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orchestration_runs_user_session_updated
    ON orchestration_runs (user_id, session_id, updated_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_orchestration_runs_active_session
    ON orchestration_runs (user_id, session_id)
    WHERE status IN ('ACTIVE', 'WAITING_USER', 'INTERRUPTING', 'INTERRUPTED');

CREATE TABLE IF NOT EXISTS orchestration_turns (
    turn_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES orchestration_runs(run_id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    status TEXT NOT NULL,
    input TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    interrupted_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    UNIQUE (run_id, request_id)
);

CREATE INDEX IF NOT EXISTS idx_orchestration_turns_run_created
    ON orchestration_turns (run_id, created_at DESC);
