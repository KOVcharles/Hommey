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
