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
