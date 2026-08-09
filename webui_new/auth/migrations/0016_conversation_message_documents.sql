-- Structured cards belong to the canonical conversation message, not only to
-- the legacy chat_history mirror.  Keeping both payloads with the assistant
-- message makes history replay render exactly like the live stream.
ALTER TABLE conversation_messages
    ADD COLUMN IF NOT EXISTS answer_document JSONB,
    ADD COLUMN IF NOT EXISTS presentation_document JSONB;
