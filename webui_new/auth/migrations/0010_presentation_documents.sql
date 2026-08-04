ALTER TABLE chat_history
ADD COLUMN IF NOT EXISTS presentation_document JSONB;
