-- =============================================================================
-- Add visitor_phone to conversations
-- =============================================================================
-- Applied automatically and idempotently by ensure_frontend_schema() in
-- app/main.py on startup — this file documents that same statement, it does
-- not need to be run manually.
--
-- Lets the public chat widget's pre-chat lead-capture form ("collect phone
-- before chat" in the Behavior tab) store a visitor's phone number, matching
-- the existing visitor_name / visitor_email columns.
-- =============================================================================

ALTER TABLE conversations ADD COLUMN IF NOT EXISTS visitor_phone VARCHAR(32);
