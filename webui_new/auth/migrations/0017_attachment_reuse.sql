-- Allow an attachment to be referenced by more than one message (re-attach from
-- the attachment panel). The per-binding ownership / ready / expiry checks in
-- _bind_ready_attachments remain the security boundary; this only relaxes the
-- one-attachment-one-message uniqueness invariant.
ALTER TABLE conversation_message_attachments
    DROP CONSTRAINT IF EXISTS conversation_message_attachments_attachment_id_key;
