-- =============================================================================
-- Add content_hash to documents
-- =============================================================================
-- SHA-256 hex digest of the raw uploaded file's bytes, used by
-- document_service.upload_documents() to reject a byte-identical re-upload
-- with 409 instead of silently creating a second full set of chunks/vectors.
-- Nullable: existing rows (and non-`uploaded` sources) have no hash.
-- =============================================================================

ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64);
CREATE INDEX IF NOT EXISTS ix_documents_content_hash ON documents (content_hash);
