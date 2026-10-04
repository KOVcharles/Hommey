-- Imported baseline: 0001_users.sql
-- Hommey 鉴权系统 v1.0：用户表
-- 幂等：可重复执行；字段与 PRD §3.1 一致。
-- 该 DDL 同时由 webui_new/auth/storage.py::apply_migration 在代码内执行，
-- 本文件供运维/CI 手工独立执行（见 design.md §2.3）。
CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL    PRIMARY KEY,
    email         TEXT         UNIQUE NOT NULL,
    password_hash TEXT         NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- 可选：便于登录按邮箱查重的索引（UNIQUE 约束已隐式建索引，此处显式注释供运维参考）
-- CREATE UNIQUE INDEX IF NOT EXISTS users_email_uidx ON users (email);


-- Imported baseline: 0002_skill_platform.sql
ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'user';

CREATE TABLE IF NOT EXISTS active_trip_contexts (
    user_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active',
    context_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS skill_settings (
    skill_name TEXT PRIMARY KEY,
    enabled BOOLEAN NOT NULL,
    config_overrides JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_by TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS skill_execution_runs (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    skill_version TEXT NOT NULL,
    status TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    input_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    parent_run_id TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_skill_runs_started_at
    ON skill_execution_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_skill_runs_skill_started
    ON skill_execution_runs (skill_name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_skill_runs_request
    ON skill_execution_runs (request_id);


-- Imported baseline: 0003_memory_p0.sql
CREATE TABLE IF NOT EXISTS chat_history (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    request_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trip_history (
    id BIGSERIAL PRIMARY KEY,
    trip_id TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    origin TEXT,
    destination TEXT,
    start_date TEXT,
    end_date TEXT,
    purpose TEXT,
    request_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE chat_history ADD COLUMN IF NOT EXISTS request_id TEXT;
ALTER TABLE trip_history ADD COLUMN IF NOT EXISTS request_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_history_request_role
    ON chat_history (user_id, request_id, role)
    WHERE request_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_trip_history_request
    ON trip_history (user_id, request_id)
    WHERE request_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_chat_history_user_created
    ON chat_history (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_chat_history_user_session_created
    ON chat_history (user_id, session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_trip_history_user_created
    ON trip_history (user_id, created_at DESC);


-- Imported baseline: 0004_chat_session_titles.sql
CREATE TABLE IF NOT EXISTS chat_session_titles (
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_session_titles_user_updated
    ON chat_session_titles (user_id, updated_at DESC);


-- Imported baseline: 0005_multimodal_attachments.sql
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


-- Imported baseline: 0006_memory_stage1.sql
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


-- Imported baseline: 0007_conversation_message_attachments.sql
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


-- Imported baseline: 0008_memory_profile_stage2a.sql
-- Stage 2A memory foundation: versioned profile facts and explicit conflict requests.
-- This migration is additive; legacy user_preferences remains the active compatibility path.

CREATE TABLE IF NOT EXISTS user_profile_facts (
    fact_id UUID PRIMARY KEY,
    user_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    fact_value JSONB NOT NULL,
    normalized_value TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'rejected')),
    confidence NUMERIC(4, 3) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    write_mode TEXT NOT NULL CHECK (write_mode IN ('auto_explicit', 'user_confirmed', 'migration')),
    source_turn_id UUID,
    source_excerpt TEXT,
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('normal', 'restricted')),
    valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_to TIMESTAMPTZ,
    version INTEGER NOT NULL CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, namespace, fact_key, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_user_profile_facts_active
    ON user_profile_facts (user_id, namespace, fact_key)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_user_profile_facts_user_active
    ON user_profile_facts (user_id, namespace, fact_key)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS memory_change_requests (
    change_id UUID PRIMARY KEY,
    user_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    old_fact_id UUID REFERENCES user_profile_facts(fact_id),
    proposed_value JSONB NOT NULL,
    proposed_normalized_value TEXT NOT NULL,
    reason TEXT,
    source_turn_id UUID NOT NULL,
    source_excerpt TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'rejected', 'expired')),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);

-- One unresolved proposal per field keeps confirmation routing deterministic.
CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_change_requests_pending
    ON memory_change_requests (user_id, namespace, fact_key)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_memory_change_requests_user_pending
    ON memory_change_requests (user_id, expires_at)
    WHERE status = 'pending';


-- Imported baseline: 0009_answer_documents.sql
ALTER TABLE chat_history
ADD COLUMN IF NOT EXISTS answer_document JSONB;


-- Imported baseline: 0010_presentation_documents.sql
ALTER TABLE chat_history
ADD COLUMN IF NOT EXISTS presentation_document JSONB;


-- Imported baseline: 0011_user_travel_preferences.sql
CREATE TABLE IF NOT EXISTS user_travel_preferences (
    user_id TEXT PRIMARY KEY,
    home_location TEXT,
    transportation_preference TEXT,
    hotel_brands JSONB NOT NULL DEFAULT '[]'::jsonb,
    airlines JSONB NOT NULL DEFAULT '[]'::jsonb,
    seat_preference TEXT,
    meal_preference TEXT,
    budget_level TEXT,
    extra_preferences JSONB NOT NULL DEFAULT '{}'::jsonb,
    preference_updated_at JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_user_travel_preferences_hotel_brands_array
        CHECK (jsonb_typeof(hotel_brands) = 'array'),
    CONSTRAINT ck_user_travel_preferences_airlines_array
        CHECK (jsonb_typeof(airlines) = 'array'),
    CONSTRAINT ck_user_travel_preferences_extra_object
        CHECK (jsonb_typeof(extra_preferences) = 'object'),
    CONSTRAINT ck_user_travel_preferences_timestamps_object
        CHECK (jsonb_typeof(preference_updated_at) = 'object')
);

WITH aggregated AS (
    SELECT
        user_id,
        jsonb_object_agg(pref_type, pref_value) AS preferences,
        COALESCE(
            jsonb_object_agg(pref_type, to_jsonb(updated_at)),
            '{}'::jsonb
        ) AS preference_updated_at,
        MIN(updated_at) AS created_at,
        MAX(updated_at) AS updated_at
    FROM user_preferences
    GROUP BY user_id
)
INSERT INTO user_travel_preferences (
    user_id,
    home_location,
    transportation_preference,
    hotel_brands,
    airlines,
    seat_preference,
    meal_preference,
    budget_level,
    extra_preferences,
    preference_updated_at,
    created_at,
    updated_at
)
SELECT
    user_id,
    preferences ->> 'home_location',
    preferences ->> 'transportation_preference',
    CASE
        WHEN preferences -> 'hotel_brands' IS NULL THEN '[]'::jsonb
        WHEN jsonb_typeof(preferences -> 'hotel_brands') = 'array'
            THEN preferences -> 'hotel_brands'
        ELSE jsonb_build_array(preferences -> 'hotel_brands')
    END,
    CASE
        WHEN preferences -> 'airlines' IS NULL THEN '[]'::jsonb
        WHEN jsonb_typeof(preferences -> 'airlines') = 'array'
            THEN preferences -> 'airlines'
        ELSE jsonb_build_array(preferences -> 'airlines')
    END,
    preferences ->> 'seat_preference',
    preferences ->> 'meal_preference',
    preferences ->> 'budget_level',
    preferences - ARRAY[
        'home_location', 'transportation_preference', 'hotel_brands',
        'airlines', 'seat_preference', 'meal_preference', 'budget_level'
    ],
    preference_updated_at,
    created_at,
    updated_at
FROM aggregated
ON CONFLICT (user_id) DO NOTHING;

COMMENT ON TABLE user_preferences IS
    'Deprecated EAV compatibility mirror. Runtime reads user_travel_preferences.';
COMMENT ON TABLE user_travel_preferences IS
    'One row per user: typed core business-travel preferences plus JSONB extensions.';


-- Imported baseline: 0012_session_summaries.sql
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


-- Imported baseline: 0013_orchestration_checkpoints.sql
-- 跨轮"收集→暂停→续跑"检查点：plan-trip 信息不全时保存步骤剩余与已收集事实。
-- active_trip_contexts 仍是行程事实源；此表只存"下一步该从哪继续"的现场状态。
CREATE TABLE IF NOT EXISTS orchestration_checkpoints (
    user_id TEXT PRIMARY KEY,
    skill TEXT NOT NULL,
    request_id TEXT NOT NULL,
    pause_agent TEXT NOT NULL DEFAULT '',
    pause_field TEXT NOT NULL DEFAULT 'planning_ready',
    steps_remaining JSONB NOT NULL DEFAULT '[]'::jsonb,
    collected_facts JSONB NOT NULL DEFAULT '{}'::jsonb,
    entities JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- Imported baseline: 0014_orchestration_runs.sql
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


-- Imported baseline: 0015_orchestration_state_v2.sql
-- New runs use Goal-aware orchestration state v2.  Existing v1 snapshots keep
-- their version and are upgraded transactionally by the resume path, because
-- changing only this relational column would make it disagree with JSONB.
ALTER TABLE orchestration_runs
    ALTER COLUMN schema_version SET DEFAULT 2;


-- Imported baseline: 0016_conversation_message_documents.sql
-- Structured cards belong to the canonical conversation message, not only to
-- the legacy chat_history mirror.  Keeping both payloads with the assistant
-- message makes history replay render exactly like the live stream.
ALTER TABLE conversation_messages
    ADD COLUMN IF NOT EXISTS answer_document JSONB,
    ADD COLUMN IF NOT EXISTS presentation_document JSONB;


-- Imported baseline: 0017_attachment_reuse.sql
-- Allow an attachment to be referenced by more than one message (re-attach from
-- the attachment panel). The per-binding ownership / ready / expiry checks in
-- _bind_ready_attachments remain the security boundary; this only relaxes the
-- one-attachment-one-message uniqueness invariant.
ALTER TABLE conversation_message_attachments
    DROP CONSTRAINT IF EXISTS conversation_message_attachments_attachment_id_key;


-- Imported baseline: 0018_agent_evaluation.sql
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


-- Imported baseline: 0019_session_scoped_active_trips.sql
-- Current-trip state belongs to one conversation.  Legacy user-global rows are
-- retained under the isolated "legacy" session and are never auto-inherited by
-- a real conversation session.

ALTER TABLE active_trip_contexts
    ADD COLUMN IF NOT EXISTS session_id TEXT;

UPDATE active_trip_contexts
SET session_id = 'legacy'
WHERE session_id IS NULL;

ALTER TABLE active_trip_contexts
    ALTER COLUMN session_id SET NOT NULL;

ALTER TABLE active_trip_contexts
    DROP CONSTRAINT IF EXISTS active_trip_contexts_pkey;

ALTER TABLE active_trip_contexts
    ADD CONSTRAINT active_trip_contexts_pkey
    PRIMARY KEY (user_id, session_id);

CREATE INDEX IF NOT EXISTS idx_active_trip_contexts_session_status
    ON active_trip_contexts (user_id, session_id, status, updated_at DESC);


-- Imported baseline: 0020_rag_pgvector.sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS rag_collections (
    collection_name TEXT PRIMARY KEY,
    active_version TEXT,
    embedding_model TEXT NOT NULL,
    embedding_dimension INTEGER NOT NULL CHECK (embedding_dimension > 0),
    index_fingerprint TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rag_index_versions (
    collection_name TEXT NOT NULL REFERENCES rag_collections(collection_name) ON DELETE CASCADE,
    version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('building', 'active', 'retired', 'failed')),
    index_fingerprint TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dimension INTEGER NOT NULL CHECK (embedding_dimension > 0),
    schema_version TEXT NOT NULL,
    build_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    chunk_count INTEGER NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    activated_at TIMESTAMPTZ,
    retired_at TIMESTAMPTZ,
    PRIMARY KEY (collection_name, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_rag_index_versions_one_active
    ON rag_index_versions(collection_name)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS rag_documents (
    collection_name TEXT NOT NULL,
    index_version TEXT NOT NULL,
    document_id TEXT NOT NULL,
    document_version TEXT NOT NULL,
    source_path TEXT,
    source_hash TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (collection_name, index_version, document_id, document_version),
    FOREIGN KEY (collection_name, index_version)
        REFERENCES rag_index_versions(collection_name, version) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rag_chunks (
    collection_name TEXT NOT NULL,
    index_version TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    document_version TEXT NOT NULL,
    chunk_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata JSONB NOT NULL,
    embedding VECTOR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (collection_name, index_version, chunk_id),
    UNIQUE (collection_name, index_version, document_id, document_version, chunk_hash),
    FOREIGN KEY (collection_name, index_version)
        REFERENCES rag_index_versions(collection_name, version) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_rag_chunks_active_lookup
    ON rag_chunks(collection_name, index_version);

CREATE INDEX IF NOT EXISTS ix_rag_chunks_document
    ON rag_chunks(collection_name, index_version, document_id);

ALTER TABLE rag_collections
    DROP CONSTRAINT IF EXISTS fk_rag_collections_active_version;

ALTER TABLE rag_collections
    ADD CONSTRAINT fk_rag_collections_active_version
    FOREIGN KEY (collection_name, active_version)
    REFERENCES rag_index_versions(collection_name, version)
    DEFERRABLE INITIALLY DEFERRED;


-- Imported baseline: 0021_rag_refresh_control_plane.sql
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


-- Imported baseline: 0022_supervisor_runs.sql
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


-- Imported baseline: 0023_retire_dag_runs.sql
-- One-time retirement, not a second execution path. Preserve snapshots for
-- audit; new requests retain existing trip/message data and use the supervisor.
UPDATE orchestration_runs
SET status = 'ABANDONED', revision = revision + 1, updated_at = NOW(),
    state = jsonb_set(
        jsonb_set(state, '{status}', '"ABANDONED"'::jsonb),
        '{retirement_reason}', '"supervisor_only_runtime"'::jsonb
    )
WHERE status IN ('ACTIVE', 'WAITING_USER', 'INTERRUPTING', 'INTERRUPTED');

UPDATE orchestration_turns AS t
SET status = 'ABANDONED', updated_at = NOW()
FROM orchestration_runs AS r
WHERE t.run_id = r.run_id
  AND r.state->>'retirement_reason' = 'supervisor_only_runtime'
  AND t.status NOT IN ('COMPLETED', 'ABANDONED');


-- Imported baseline: 0024_concurrent_sessions.sql
-- A user can keep several conversations open. Request-scoped session locks
-- serialize turns; an active session no longer means the user's selected tab.
DROP INDEX IF EXISTS uq_conversation_sessions_active_user;


-- Imported baseline: 0025_invite_codes.sql
-- One-time owner-issued invitation codes. Store only digests, never plaintext.
CREATE TABLE IF NOT EXISTS invite_codes (
    code_hash CHAR(64) PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    used_at TIMESTAMPTZ,
    used_by_user_id BIGINT REFERENCES users(id)
);


-- Imported baseline: 0026_user_personal_profiles.sql
CREATE TABLE IF NOT EXISTS user_personal_profiles (
    user_id TEXT PRIMARY KEY,
    profile JSONB NOT NULL CHECK (jsonb_typeof(profile) = 'object'),
    onboarding_status TEXT NOT NULL CHECK (onboarding_status IN ('pending', 'completed', 'skipped')),
    revision INTEGER NOT NULL CHECK (revision > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
