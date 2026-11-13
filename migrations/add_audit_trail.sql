-- Audit trail + response traceability.
-- Mirrors what app/main.py::ensure_frontend_schema applies idempotently at
-- startup (tables via metadata.create_all, the rest below).

-- Tables (create_all equivalent) ---------------------------------------------
CREATE TABLE IF NOT EXISTS audit_logs (
  id             UUID PRIMARY KEY,
  tenant_id      UUID REFERENCES tenants(id) ON DELETE CASCADE,
  actor_user_id  UUID,            -- not an FK: SET NULL would be an UPDATE on an append-only table
  actor_email    VARCHAR(320),
  actor_role     VARCHAR(16),
  action         VARCHAR(64)  NOT NULL,
  resource_type  VARCHAR(32),
  resource_id    VARCHAR(64),
  resource_label VARCHAR(255),
  outcome        VARCHAR(8)   NOT NULL,
  status_code    INTEGER,
  method         VARCHAR(8),
  path           VARCHAR(255),
  ip             VARCHAR(64),
  user_agent     VARCHAR(255),
  request_id     VARCHAR(36),
  details        JSONB,
  seq            BIGINT,          -- per-tenant chain order, assigned under the append lock
  prev_hash      VARCHAR(64),
  entry_hash     VARCHAR(64)  NOT NULL,
  hash_version   SMALLINT     NOT NULL DEFAULT 1,  -- canonicalisation scheme of entry_hash
  created_at     TIMESTAMPTZ  NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_logs_tenant_id ON audit_logs (tenant_id);
CREATE INDEX IF NOT EXISTS ix_audit_logs_created_at ON audit_logs (created_at);
CREATE INDEX IF NOT EXISTS ix_audit_logs_tenant_created ON audit_logs (tenant_id, created_at);
CREATE INDEX IF NOT EXISTS ix_audit_logs_tenant_action ON audit_logs (tenant_id, action);
CREATE INDEX IF NOT EXISTS ix_audit_logs_tenant_seq ON audit_logs (tenant_id, seq);

CREATE TABLE IF NOT EXISTS answer_traces (
  id                UUID PRIMARY KEY,
  tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  conversation_id   UUID REFERENCES conversations(id) ON DELETE CASCADE,  -- traces die with the conversation
  message_id        UUID,
  channel           VARCHAR(16) NOT NULL,
  question          TEXT NOT NULL,
  answer            TEXT NOT NULL,
  decision          VARCHAR(24) NOT NULL,
  confidence        INTEGER,
  provider          VARCHAR(20),
  model             VARCHAR(100),
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  total_tokens      INTEGER,
  latency_ms        INTEGER,
  retrieval         JSONB,
  chunks            JSONB,
  conflicts         JSONB,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_answer_traces_tenant_id ON answer_traces (tenant_id);
CREATE INDEX IF NOT EXISTS ix_answer_traces_conversation_id ON answer_traces (conversation_id);
CREATE INDEX IF NOT EXISTS ix_answer_traces_message_id ON answer_traces (message_id);
CREATE INDEX IF NOT EXISTS ix_answer_traces_created_at ON answer_traces (created_at);
CREATE INDEX IF NOT EXISTS ix_answer_traces_tenant_created ON answer_traces (tenant_id, created_at);

-- Link a stored bot message to its trace ------------------------------------
ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS trace_id UUID;

-- Upgrades for databases bootstrapped by an earlier revision ------------------
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS seq BIGINT;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS hash_version SMALLINT NOT NULL DEFAULT 1;
ALTER TABLE audit_logs DROP CONSTRAINT IF EXISTS audit_logs_actor_user_id_fkey;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'answer_traces_conversation_id_fkey' AND confdeltype <> 'c') THEN
    ALTER TABLE answer_traces DROP CONSTRAINT answer_traces_conversation_id_fkey;
    ALTER TABLE answer_traces ADD CONSTRAINT answer_traces_conversation_id_fkey
      FOREIGN KEY (conversation_id) REFERENCES conversations (id) ON DELETE CASCADE;
  END IF;
END $$;
DROP TRIGGER IF EXISTS trg_audit_logs_immutable ON audit_logs;  -- the backfill below is an UPDATE
UPDATE audit_logs a SET seq = s.rn + COALESCE((SELECT MAX(seq) FROM audit_logs b WHERE b.tenant_id IS NOT DISTINCT FROM a.tenant_id), 0)
  FROM (SELECT id, row_number() OVER (PARTITION BY tenant_id ORDER BY created_at, id) AS rn FROM audit_logs WHERE seq IS NULL) s
  WHERE a.id = s.id AND a.seq IS NULL;

-- Append-only guarantee ------------------------------------------------------
-- UPDATE is always rejected. DELETE is allowed only for the retention sweep
-- (which sets the session flag nexus.audit_purge) or when the owning tenant
-- row no longer exists (FK cascade on tenant deletion). This guards against
-- accidental/application-level edits; a compromised DB credential is out of scope.
CREATE OR REPLACE FUNCTION audit_logs_immutable() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' AND current_setting('nexus.audit_purge', true) = '1' THEN RETURN OLD; END IF;
  IF TG_OP = 'DELETE' AND OLD.tenant_id IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM tenants t WHERE t.id = OLD.tenant_id) THEN RETURN OLD; END IF;
  RAISE EXCEPTION 'audit_logs is append-only (% rejected)', TG_OP;
END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_audit_logs_immutable ON audit_logs;
CREATE TRIGGER trg_audit_logs_immutable BEFORE UPDATE OR DELETE ON audit_logs
  FOR EACH ROW EXECUTE FUNCTION audit_logs_immutable();
