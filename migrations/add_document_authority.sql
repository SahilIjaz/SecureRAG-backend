-- Document authority metadata for evidence-based conflict resolution.
-- Mirrors the idempotent statements applied at startup by
-- app/main.py::ensure_frontend_schema (the app does not use Alembic).
--
-- All columns are backward compatible: existing rows become
-- "version 1, no effective date, unverified, supersedes nothing, rank 0",
-- which the resolver treats as "no authority evidence" (never auto-resolved).

ALTER TABLE documents ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS effective_date DATE;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS approval_status VARCHAR(16) NOT NULL DEFAULT 'unverified';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS supersedes_document_id UUID;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS authority_rank INTEGER NOT NULL DEFAULT 0;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS authority_updated_at TIMESTAMPTZ;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS authority_updated_by UUID;

-- (id, tenant_id) must be a unique key so the composite self-FK below can
-- reference it. That FK is what makes cross-tenant supersession impossible
-- at the database level, independent of application checks.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ux_documents_id_tenant') THEN
    ALTER TABLE documents ADD CONSTRAINT ux_documents_id_tenant UNIQUE (id, tenant_id);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_documents_supersedes_same_tenant') THEN
    ALTER TABLE documents ADD CONSTRAINT fk_documents_supersedes_same_tenant
      FOREIGN KEY (supersedes_document_id, tenant_id) REFERENCES documents (id, tenant_id);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_documents_not_self_superseding') THEN
    ALTER TABLE documents ADD CONSTRAINT ck_documents_not_self_superseding
      CHECK (supersedes_document_id IS DISTINCT FROM id);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_documents_supersedes ON documents (supersedes_document_id);
