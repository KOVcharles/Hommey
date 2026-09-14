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
