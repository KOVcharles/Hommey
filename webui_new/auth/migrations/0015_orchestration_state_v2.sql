-- New runs use Goal-aware orchestration state v2.  Existing v1 snapshots keep
-- their version and are upgraded transactionally by the resume path, because
-- changing only this relational column would make it disagree with JSONB.
ALTER TABLE orchestration_runs
    ALTER COLUMN schema_version SET DEFAULT 2;
