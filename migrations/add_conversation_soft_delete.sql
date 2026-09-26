-- =============================================================================
-- Add deleted_at to conversations (soft delete)
-- =============================================================================
-- Applied automatically and idempotently by ensure_frontend_schema() in
-- app/main.py on startup — this file documents that same statement, it does
-- not need to be run manually.
--
-- Lets an owner delete a conversation from the dashboard inbox without an
-- instant, unrecoverable hard delete. Soft-deleted conversations are
-- excluded from all normal list/get queries; conversation_service's
-- trash_purge_loop() hard-deletes (cascading to conversation_messages) any
-- row whose deleted_at is older than settings.CONVERSATION_TRASH_RETENTION_DAYS.
-- =============================================================================

ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS ix_conversations_deleted_at ON conversations (deleted_at);
