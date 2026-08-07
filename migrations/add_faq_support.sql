-- =============================================================================
-- Add FAQ Support
-- =============================================================================
-- Adds a third knowledge-base source type: a manually-entered question +
-- answer pair, alongside the existing 'uploaded'/'sample'/'scraped' sources.
--
-- IMPORTANT — run this file's ALTER TYPE statement as its own transaction,
-- separate from anything that inserts a 'faq' row. Postgres does not allow a
-- newly-added enum value to be used until the ALTER TYPE that added it has
-- committed. (app/main.py's ensure_frontend_schema() mirrors this by running
-- the enum alteration in its own dedicated transaction, before the rest of
-- its schema-sync batch — see that function for the runtime equivalent of
-- this migration.)
-- =============================================================================

ALTER TYPE documentsource ADD VALUE IF NOT EXISTS 'faq';

-- =============================================================================
-- END OF (enum) MIGRATION — run the column additions below separately.
-- =============================================================================

ALTER TABLE documents ADD COLUMN IF NOT EXISTS question TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS answer TEXT;
