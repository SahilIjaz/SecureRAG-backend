-- =============================================================================
-- Add live_wait_started_at to conversations
-- =============================================================================
-- Applied automatically and idempotently by ensure_frontend_schema() in
-- app/main.py on startup — this file documents that same statement, it does
-- not need to be run manually.
--
-- Set/reset whenever a visitor escalates or the automatic low-confidence
-- handoff fires — used to compute the connecting/unavailable state within
-- LIVE_JOIN_TIMEOUT_SECONDS. See NexusContext/LIVE_AGENT_HANDOFF_PLAN.md §3.
-- =============================================================================

ALTER TABLE conversations ADD COLUMN IF NOT EXISTS live_wait_started_at TIMESTAMPTZ;
