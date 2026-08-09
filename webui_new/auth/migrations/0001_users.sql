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
