-- =============================================================================
-- SecureRAG++ — Initial Database Schema
-- =============================================================================
-- Description : Creates all tables, types, indexes, and constraints required
--               by the SecureRAG++ multi-tenant RAG platform.
-- PostgreSQL   : 14+
-- Convention   : UUIDs generated via gen_random_uuid(); timestamps stored as
--               TIMESTAMPTZ (UTC).  A value of -1 in quota columns means
--               "unlimited".
-- =============================================================================

-- Enable the pgcrypto extension so gen_random_uuid() is available on older PG.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- =============================================================================
-- ENUM TYPES
-- =============================================================================

DO $$ BEGIN
    CREATE TYPE planname AS ENUM ('free', 'pro', 'pro_plus');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE billingcycle AS ENUM ('monthly', 'yearly');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE subscriptionstatus AS ENUM ('active', 'expired', 'cancelled', 'trial');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- =============================================================================
-- TABLE: tenants
-- =============================================================================

CREATE TABLE IF NOT EXISTS tenants (
    id                   UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_name       VARCHAR(255)  NOT NULL,
    slug                 VARCHAR(255)  NOT NULL,
    business_category    VARCHAR(255),
    employee_count_range VARCHAR(50)
        CHECK (
            employee_count_range IS NULL OR
            employee_count_range IN ('1-15', '16-49', '50-249', '250+')
        ),
    is_active            BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at           TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_tenants_slug UNIQUE (slug)
);

CREATE INDEX IF NOT EXISTS idx_tenants_slug     ON tenants (slug);
CREATE INDEX IF NOT EXISTS idx_tenants_is_active ON tenants (is_active);

-- =============================================================================
-- TABLE: users
-- =============================================================================

CREATE TABLE IF NOT EXISTS users (
    id                  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID         NOT NULL,
    full_name           VARCHAR(255) NOT NULL,
    email               VARCHAR(320) NOT NULL,
    password_hash       VARCHAR(255) NOT NULL,
    is_email_verified   BOOLEAN      NOT NULL DEFAULT FALSE,
    is_active           BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_users_email     UNIQUE (email),
    CONSTRAINT uq_users_tenant_id UNIQUE (tenant_id),

    CONSTRAINT fk_users_tenant_id
        FOREIGN KEY (tenant_id)
        REFERENCES tenants (id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_users_email     ON users (email);
CREATE INDEX IF NOT EXISTS idx_users_tenant_id ON users (tenant_id);

-- =============================================================================
-- TABLE: email_verifications
-- =============================================================================

CREATE TABLE IF NOT EXISTS email_verifications (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID        NOT NULL,
    otp_code   VARCHAR(255) NOT NULL,  -- bcrypt-hashed OTP
    expires_at TIMESTAMPTZ NOT NULL,
    is_used    BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_email_verifications_user_id
        FOREIGN KEY (user_id)
        REFERENCES users (id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_email_verifications_user_id ON email_verifications (user_id);
CREATE INDEX IF NOT EXISTS idx_email_verifications_expires_at ON email_verifications (expires_at);

-- =============================================================================
-- TABLE: subscriptions
-- =============================================================================

CREATE TABLE IF NOT EXISTS subscriptions (
    id            UUID               PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID               NOT NULL,
    plan_name     planname           NOT NULL DEFAULT 'free',
    billing_cycle billingcycle,
    status        subscriptionstatus NOT NULL DEFAULT 'active',
    started_at    TIMESTAMPTZ        NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ        NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_subscriptions_tenant_id UNIQUE (tenant_id),

    CONSTRAINT fk_subscriptions_tenant_id
        FOREIGN KEY (tenant_id)
        REFERENCES tenants (id)
        ON DELETE CASCADE,

    -- billing_cycle must be set when a paid plan is chosen
    CONSTRAINT chk_subscriptions_billing_cycle
        CHECK (
            plan_name = 'free' OR billing_cycle IS NOT NULL
        )
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_tenant_id ON subscriptions (tenant_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_status    ON subscriptions (status);

-- =============================================================================
-- TABLE: tenant_quotas
-- =============================================================================

CREATE TABLE IF NOT EXISTS tenant_quotas (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID        NOT NULL,
    subscription_id         UUID        NOT NULL,
    max_documents           INTEGER     NOT NULL DEFAULT 10
        CHECK (max_documents = -1 OR max_documents >= 0),
    max_file_size_mb        INTEGER     NOT NULL DEFAULT 15
        CHECK (max_file_size_mb = -1 OR max_file_size_mb > 0),
    max_questions_per_month INTEGER     NOT NULL DEFAULT 50
        CHECK (max_questions_per_month = -1 OR max_questions_per_month >= 0),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_tenant_quotas_tenant_id UNIQUE (tenant_id),

    CONSTRAINT fk_tenant_quotas_tenant_id
        FOREIGN KEY (tenant_id)
        REFERENCES tenants (id)
        ON DELETE CASCADE,

    CONSTRAINT fk_tenant_quotas_subscription_id
        FOREIGN KEY (subscription_id)
        REFERENCES subscriptions (id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tenant_quotas_tenant_id       ON tenant_quotas (tenant_id);
CREATE INDEX IF NOT EXISTS idx_tenant_quotas_subscription_id ON tenant_quotas (subscription_id);

-- =============================================================================
-- TABLE: usage_counts
-- =============================================================================

CREATE TABLE IF NOT EXISTS usage_counts (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID        NOT NULL,
    period_month     DATE        NOT NULL,   -- always the 1st of the month
    questions_used   INTEGER     NOT NULL DEFAULT 0
        CHECK (questions_used >= 0),
    documents_count  INTEGER     NOT NULL DEFAULT 0
        CHECK (documents_count >= 0),
    storage_used_mb  FLOAT       NOT NULL DEFAULT 0.0
        CHECK (storage_used_mb >= 0),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_usage_tenant_period
        UNIQUE (tenant_id, period_month),

    CONSTRAINT fk_usage_counts_tenant_id
        FOREIGN KEY (tenant_id)
        REFERENCES tenants (id)
        ON DELETE CASCADE,

    -- Enforce that period_month is always the first day of a month
    CONSTRAINT chk_usage_period_first_of_month
        CHECK (EXTRACT(DAY FROM period_month) = 1)
);

CREATE INDEX IF NOT EXISTS idx_usage_counts_tenant_id    ON usage_counts (tenant_id);
CREATE INDEX IF NOT EXISTS idx_usage_counts_period_month ON usage_counts (period_month);

-- =============================================================================
-- TRIGGER: auto-update updated_at columns
-- =============================================================================

CREATE OR REPLACE FUNCTION trigger_set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

-- Attach the trigger to each table that has an updated_at column.

DO $$ BEGIN
    CREATE TRIGGER set_tenants_updated_at
        BEFORE UPDATE ON tenants
        FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER set_users_updated_at
        BEFORE UPDATE ON users
        FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER set_tenant_quotas_updated_at
        BEFORE UPDATE ON tenant_quotas
        FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER set_usage_counts_updated_at
        BEFORE UPDATE ON usage_counts
        FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- =============================================================================
-- END OF SCHEMA
-- =============================================================================
