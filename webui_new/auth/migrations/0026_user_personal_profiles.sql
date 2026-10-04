CREATE TABLE IF NOT EXISTS user_personal_profiles (
    user_id TEXT PRIMARY KEY,
    profile JSONB NOT NULL CHECK (jsonb_typeof(profile) = 'object'),
    onboarding_status TEXT NOT NULL CHECK (onboarding_status IN ('pending', 'completed', 'skipped')),
    revision INTEGER NOT NULL CHECK (revision > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
