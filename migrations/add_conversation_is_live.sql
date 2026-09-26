-- =============================================================================
-- Add is_live to conversations
-- =============================================================================
-- Applied automatically and idempotently by ensure_frontend_schema() in
-- app/main.py on startup — this file documents that same statement, it does
-- not need to be run manually.
--
-- True only once an owner has actively "joined" a real-time exchange with a
-- visitor — see NexusContext/LIVE_AGENT_HANDOFF_PLAN.md. Not what silences
-- the bot (that's status = 'Handed off', set the moment escalation starts);
-- this only drives the live-polling UI once later steps of that plan wire it
-- up. Column added now so it's in place ahead of that.
-- =============================================================================

ALTER TABLE conversations ADD COLUMN IF NOT EXISTS is_live BOOLEAN NOT NULL DEFAULT FALSE;
