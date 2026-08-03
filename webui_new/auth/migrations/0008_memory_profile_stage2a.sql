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
