-- Frontend-compat schema additions (Nexus dashboard API at /api/...).
-- NOTE: the app also applies all of this automatically at startup
-- (see ensure_frontend_schema() in app/main.py) — this file documents the
-- change and lets you apply it manually if preferred. Fully idempotent.

-- New columns on existing tables ------------------------------------------------

ALTER TABLE documents ADD COLUMN IF NOT EXISTS chunk_count INTEGER;
ALTER TABLE tenants   ADD COLUMN IF NOT EXISTS has_documents BOOLEAN;
ALTER TABLE tenants   ADD COLUMN IF NOT EXISTS onboarding_completed_at TIMESTAMPTZ;
ALTER TABLE users     ADD COLUMN IF NOT EXISTS avatar_url TEXT;







-- The dashboard sends free-form team sizes ("Just me", "2–10", ...) and business
-- categories ("SaaS", ...) — drop the old fixed-set constraints.
ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_employee_count_range_check;
ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_business_category_check;

-- Chatbot widget configuration (one JSONB blob per tenant) ----------------------

CREATE TABLE IF NOT EXISTS chatbot_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL UNIQUE REFERENCES tenants(id) ON DELETE CASCADE,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_chatbot_configs_tenant_id ON chatbot_configs(tenant_id);

-- Conversations + messages -------------------------------------------------------

CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    visitor_name VARCHAR(255) NOT NULL DEFAULT 'Visitor',
    visitor_email VARCHAR(320),
    status VARCHAR(20) NOT NULL DEFAULT 'Open',
    sentiment VARCHAR(20) NOT NULL DEFAULT 'Neutral',
    channel VARCHAR(20) NOT NULL DEFAULT 'Widget',
    topic VARCHAR(255),
    unresolved_reason VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_conversations_tenant_id  ON conversations(tenant_id);
CREATE INDEX IF NOT EXISTS ix_conversations_status     ON conversations(status);
CREATE INDEX IF NOT EXISTS ix_conversations_created_at ON conversations(created_at);

CREATE TABLE IF NOT EXISTS conversation_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role VARCHAR(10) NOT NULL DEFAULT 'user',
    text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_conversation_messages_conversation_id ON conversation_messages(conversation_id);
CREATE INDEX IF NOT EXISTS ix_conversation_messages_created_at      ON conversation_messages(created_at);

-- Settings: notifications, API keys, payment method ------------------------------

CREATE TABLE IF NOT EXISTS notification_settings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL UNIQUE REFERENCES tenants(id) ON DELETE CASCADE,
    unresolved_conversations BOOLEAN NOT NULL DEFAULT TRUE,
    weekly_summary BOOLEAN NOT NULL DEFAULT TRUE,
    knowledge_gaps_detected BOOLEAN NOT NULL DEFAULT TRUE,
    plan_usage_warnings BOOLEAN NOT NULL DEFAULT TRUE,
    product_updates BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_notification_settings_tenant_id ON notification_settings(tenant_id);

CREATE TABLE IF NOT EXISTS api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    label VARCHAR(100) NOT NULL,
    key_hash VARCHAR(255) NOT NULL,
    masked_key VARCHAR(64) NOT NULL,
    is_revoked BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_api_keys_tenant_id ON api_keys(tenant_id);

CREATE TABLE IF NOT EXISTS payment_methods (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL UNIQUE REFERENCES tenants(id) ON DELETE CASCADE,
    brand VARCHAR(20) NOT NULL DEFAULT 'VISA',
    last4 VARCHAR(4) NOT NULL DEFAULT '0000',
    expiry VARCHAR(7) NOT NULL DEFAULT '12/30',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_payment_methods_tenant_id ON payment_methods(tenant_id);
