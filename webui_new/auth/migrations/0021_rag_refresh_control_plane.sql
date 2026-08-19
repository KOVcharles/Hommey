CREATE TABLE IF NOT EXISTS rag_source_generations (
    collection_name TEXT PRIMARY KEY,
    generation BIGINT NOT NULL DEFAULT 0 CHECK (generation >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rag_refresh_jobs (
    job_id UUID PRIMARY KEY,
    collection_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'success', 'partial_success', 'error')
    ),
    stage TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    requested_by TEXT NOT NULL,
    source_generation BIGINT NOT NULL CHECK (source_generation >= 0),
    source_manifest JSONB NOT NULL DEFAULT '[]'::jsonb,
    result_manifest JSONB,
    report JSONB,
    message TEXT,
    error TEXT,
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_rag_refresh_jobs_one_active
    ON rag_refresh_jobs(collection_name)
    WHERE status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS ix_rag_refresh_jobs_claim
    ON rag_refresh_jobs(status, next_attempt_at, created_at);

CREATE INDEX IF NOT EXISTS ix_rag_refresh_jobs_collection_history
    ON rag_refresh_jobs(collection_name, created_at DESC);

CREATE TABLE IF NOT EXISTS rag_worker_heartbeats (
    worker_id TEXT PRIMARY KEY,
    collection_name TEXT NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ix_rag_worker_heartbeats_collection
    ON rag_worker_heartbeats(collection_name, last_seen_at DESC);
