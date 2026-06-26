-- =============================================================================
-- Add Documents and Sample Documents Tables
-- =============================================================================
-- Adds support for document uploads and web scraping with Crawl4AI
-- =============================================================================

-- Create ENUM types for document source and status
DO $$ BEGIN
    CREATE TYPE documentsource AS ENUM ('uploaded', 'sample', 'scraped');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE documentstatus AS ENUM ('pending', 'processing', 'ready', 'failed');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- =============================================================================
-- TABLE: documents
-- =============================================================================

CREATE TABLE IF NOT EXISTS documents (
    id                   UUID             PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            UUID             NOT NULL,
    original_filename    VARCHAR(500)     NOT NULL,
    file_path            VARCHAR(1000),   -- Cloudinary public_id or file path
    file_url             VARCHAR(2000),   -- HTTPS URL to file
    file_size_mb         FLOAT            NOT NULL DEFAULT 0.0,
    mime_type            VARCHAR(100)     NOT NULL DEFAULT 'application/pdf',
    source               documentsource   NOT NULL DEFAULT 'uploaded',
    source_url           VARCHAR(2048),   -- For scraped documents: original URL
    status               documentstatus   NOT NULL DEFAULT 'pending',
    sample_document_id   UUID,            -- FK to sample_documents
    is_active            BOOLEAN          NOT NULL DEFAULT TRUE,
    created_at           TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_documents_tenant_id
        FOREIGN KEY (tenant_id)
        REFERENCES tenants (id)
        ON DELETE CASCADE,

    CONSTRAINT fk_documents_sample_document_id
        FOREIGN KEY (sample_document_id)
        REFERENCES sample_documents (id)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_tenant_id ON documents (tenant_id);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents (source);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents (status);
CREATE INDEX IF NOT EXISTS idx_documents_is_active ON documents (is_active);

-- =============================================================================
-- TABLE: sample_documents
-- =============================================================================

CREATE TABLE IF NOT EXISTS sample_documents (
    id                   UUID             PRIMARY KEY DEFAULT gen_random_uuid(),
    business_category    VARCHAR(255)     NOT NULL,
    title                TEXT             NOT NULL,
    description          TEXT,
    filename             VARCHAR(255)     NOT NULL,
    file_path            VARCHAR(1000)    NOT NULL,
    file_size_mb         FLOAT            NOT NULL DEFAULT 0.0,
    is_active            BOOLEAN          NOT NULL DEFAULT TRUE,
    created_at           TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

    INDEX idx_sample_documents_category (business_category)
);

-- =============================================================================
-- TRIGGER: auto-update updated_at for documents
-- =============================================================================

DO $$ BEGIN
    CREATE TRIGGER set_documents_updated_at
        BEFORE UPDATE ON documents
        FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- =============================================================================
-- END OF MIGRATION
-- =============================================================================
