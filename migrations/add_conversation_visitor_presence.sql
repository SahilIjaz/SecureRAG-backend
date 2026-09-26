-- =============================================================================
-- Add visitor_last_seen_at to conversations
-- =============================================================================
-- Applied automatically and idempotently by ensure_frontend_schema() in
-- app/main.py on startup — this file documents that same statement, it does
-- not need to be run manually.
--
-- Bumped on every widget request tied to a conversation while escalated
-- (escalate call, a message, or a live-status poll). Lets an owner's
-- "Join chat" click be refused if the visitor no longer appears to have the
-- chat open. See NexusContext/LIVE_AGENT_HANDOFF_PLAN.md.
-- =============================================================================

ALTER TABLE conversations ADD COLUMN IF NOT EXISTS visitor_last_seen_at TIMESTAMPTZ;
