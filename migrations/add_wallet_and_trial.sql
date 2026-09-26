-- Prepaid wallet + one-time signup trial (see NexusContext plan doc
-- i-want-to-implement-floofy-hickey.md section C).
--
-- Applied automatically and idempotently by ensure_frontend_schema()
-- (app/main.py) on every app startup — this file documents that same
-- statement, it does not need to be run manually.

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS balance_usd NUMERIC(10,4) NOT NULL DEFAULT 0;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS trial_messages_used INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS trial_ended_at TIMESTAMPTZ;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS wallet_low_balance_warned BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS preferred_llm_provider VARCHAR(30);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS preferred_llm_model VARCHAR(100);

CREATE TABLE IF NOT EXISTS wallet_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    type VARCHAR(20) NOT NULL,
    amount_usd NUMERIC(10,4) NOT NULL,
    balance_after NUMERIC(10,4),
    related_usage_log_id UUID,
    stripe_payment_intent_id VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS llm_usage_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
    provider VARCHAR(20) NOT NULL,
    model VARCHAR(100) NOT NULL,
    call_type VARCHAR(20) NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    raw_cost_usd NUMERIC(10,6),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One-time rollout migration credit for tenants that already existed before
-- this feature shipped — see EXISTING_TENANT_MIGRATION_CREDIT_USD /
-- LLM_BILLING_MIGRATION_CUTOFF in app/config.py. Guarded by a fixed cutoff
-- date (not "now()") and balance_usd = 0, so it only ever applies once, to
-- tenants that predate the cutoff — never to a real new signup.
