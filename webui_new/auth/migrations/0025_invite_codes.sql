-- One-time owner-issued invitation codes. Store only digests, never plaintext.
CREATE TABLE IF NOT EXISTS invite_codes (
    code_hash CHAR(64) PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    used_at TIMESTAMPTZ,
    used_by_user_id BIGINT REFERENCES users(id)
);
