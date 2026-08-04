ALTER TABLE chat_history
ADD COLUMN IF NOT EXISTS answer_document JSONB;
