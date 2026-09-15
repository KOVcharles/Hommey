-- A user can keep several conversations open. Request-scoped session locks
-- serialize turns; an active session no longer means the user's selected tab.
DROP INDEX IF EXISTS uq_conversation_sessions_active_user;
