CREATE TABLE IF NOT EXISTS evaluation_subjects (
    subject_id UUID PRIMARY KEY,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('turn', 'session')),
    request_id TEXT,
    session_id TEXT NOT NULL,
    run_id TEXT,
    turn_id TEXT,
    capture_mode TEXT NOT NULL CHECK (capture_mode IN ('live', 'reconciled', 'offline')),
    schema_version TEXT NOT NULL,
    payload JSONB NOT NULL,
    producer_versions JSONB NOT NULL DEFAULT '{}'::jsonb,
    payload_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT evaluation_turn_request_required
        CHECK (subject_type <> 'turn' OR request_id IS NOT NULL),
    UNIQUE (subject_type, request_id)
);

CREATE TABLE IF NOT EXISTS evaluation_runs (
    evaluation_id UUID PRIMARY KEY,
    subject_id UUID NOT NULL REFERENCES evaluation_subjects(subject_id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed', 'skipped')),
    evaluator_version TEXT NOT NULL,
    judge_model TEXT NOT NULL DEFAULT '',
    judge_prompt_version TEXT NOT NULL DEFAULT '',
    rubric_version TEXT NOT NULL DEFAULT '',
    verdict TEXT CHECK (verdict IN ('pass', 'warning', 'fail', 'critical_fail', 'unscored')),
    score INTEGER CHECK (score BETWEEN 0 AND 100),
    dimension_scores JSONB NOT NULL DEFAULT '{}'::jsonb,
    reason_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
    critical_errors JSONB NOT NULL DEFAULT '[]'::jsonb,
    rule_results JSONB NOT NULL DEFAULT '[]'::jsonb,
    explanation TEXT,
    review_required BOOLEAN NOT NULL DEFAULT FALSE,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    token_usage JSONB NOT NULL DEFAULT '{}'::jsonb,
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    UNIQUE (subject_id, evaluator_version)
);

CREATE INDEX IF NOT EXISTS idx_evaluation_runs_pending
    ON evaluation_runs (created_at)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_evaluation_runs_expired_lease
    ON evaluation_runs (lease_expires_at)
    WHERE status = 'running';
CREATE INDEX IF NOT EXISTS idx_evaluation_subjects_session_created
    ON evaluation_subjects (session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS evaluation_reviews (
    review_id UUID PRIMARY KEY,
    evaluation_id UUID NOT NULL REFERENCES evaluation_runs(evaluation_id),
    reviewer TEXT NOT NULL,
    human_verdict TEXT NOT NULL CHECK (
        human_verdict IN ('pass', 'warning', 'fail', 'critical_fail', 'unscored')
    ),
    human_reason_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
    agrees_with_judge BOOLEAN,
    comment TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
