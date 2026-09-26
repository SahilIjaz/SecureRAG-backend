-- =============================================================================
-- Add presence tracking to tenants
-- =============================================================================
-- Applied automatically and idempotently by ensure_frontend_schema() in
-- app/main.py on startup — this file documents that same statement, it does
-- not need to be run manually.
--
-- Live-agent-handoff plan (NexusContext/LIVE_AGENT_HANDOFF_PLAN.md §2).
-- is_owner_online is a cache updated by POST /api/presence/ping;
-- last_seen_at is the source of truth for actually computing "online" —
-- see app/api/frontend/helpers.py:is_owner_currently_online.
-- =============================================================================

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS is_owner_online BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;
