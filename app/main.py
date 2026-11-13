import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from sqlalchemy import text

from app.api.billing_webhook import router as billing_webhook_router
from app.api.frontend.router import router as frontend_router
from app.api.public.widget import widget_app
from app.api.ptt_ws import router as ptt_ws_router
from app.config import settings
from app.core import perf_timing
from app.core.rate_limit import limiter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)
perf_logger = logging.getLogger("perf")

async def ensure_frontend_schema() -> None:
    """
    Idempotently apply the frontend-compat schema additions
    (mirrors migrations/add_frontend_compat.sql): new tables via
    metadata.create_all + ADD COLUMN IF NOT EXISTS for existing tables.
    """
    import app.models  # noqa: F401 — register all models on Base.metadata
    from app.database import Base, async_engine

    # Must run in its own transaction, committed before anything else below
    # (including create_all) touches the `documentsource` enum — Postgres
    # will not let a newly-added enum value be read or written until the
    # ALTER TYPE that added it has actually committed. Keep this separate
    # from `alter_statements`, which all run inside one shared transaction;
    # bundling this in there too would make the enum value's availability
    # depend on that whole batch committing first, which is exactly the
    # fragile-by-accident ordering this split avoids.
    #
    # Only applies to an *existing* database being upgraded — `ALTER TYPE`
    # requires the type to already exist. On a genuinely fresh database
    # (nothing created yet), skip it entirely: create_all() below creates
    # `documentsource` from the Python enum, which already includes 'faq'
    # as a member, so there's nothing to alter.
    async with async_engine.begin() as conn:
        type_exists = await conn.scalar(
            text("SELECT 1 FROM pg_type WHERE typname = 'documentsource'")
        )
        if type_exists:
            await conn.execute(text("ALTER TYPE documentsource ADD VALUE IF NOT EXISTS 'faq'"))

    alter_statements = [
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS chunk_count INTEGER",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS question TEXT",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS answer TEXT",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64)",
        # Why the last indexing attempt failed — see app/models/document.py.
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS error_message VARCHAR(500)",
        "CREATE INDEX IF NOT EXISTS ix_documents_content_hash ON documents (content_hash)",
        # The Knowledge page's stats/documents/urls/faqs endpoints all filter
        # on exactly this pair (tenant_id, is_active) — covers the hottest
        # query on the table instead of scanning the tenant_id index and
        # filtering is_active row-by-row.
        "CREATE INDEX IF NOT EXISTS ix_documents_tenant_active ON documents (tenant_id, is_active)",
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS has_documents BOOLEAN",
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS onboarding_completed_at TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT",
        # The dashboard sends free-form team sizes ("Just me", "2–10", ...) and
        # business categories ("SaaS", ...) that predate-constraint sets reject.
        "ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_employee_count_range_check",
        "ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_business_category_check",
        # Public widget lead capture (Behavior tab "collect phone before chat").
        "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS visitor_phone VARCHAR(32)",
        # Soft delete — see migrations/add_conversation_soft_delete.sql.
        "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS ix_conversations_deleted_at ON conversations (deleted_at)",
        # Live-agent-handoff UI flag — see migrations/add_conversation_is_live.sql.
        "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS is_live BOOLEAN NOT NULL DEFAULT FALSE",
        # Presence tracking — see migrations/add_tenant_presence.sql.
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS is_owner_online BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ",
        # Escalation wait timer — see migrations/add_conversation_live_wait.sql.
        "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS live_wait_started_at TIMESTAMPTZ",
        # Visitor presence — see migrations/add_conversation_visitor_presence.sql.
        "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS visitor_last_seen_at TIMESTAMPTZ",
        # Plan-usage notification dedup — fire the 80%/100% warning once per
        # billing period_month, not on every message past the threshold.
        "ALTER TABLE usage_counts ADD COLUMN IF NOT EXISTS warned_80_percent BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE usage_counts ADD COLUMN IF NOT EXISTS warned_100_percent BOOLEAN NOT NULL DEFAULT FALSE",
        # Citation sources for a bot reply — see app/models/conversation.py.
        "ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS sources JSONB",
        # Stripe linkage — see app/models/subscription.py.
        "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(255)",
        "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR(255)",
        "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS stripe_price_id VARCHAR(255)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_subscriptions_stripe_subscription_id "
        "ON subscriptions (stripe_subscription_id)",
        # One-time trial guard — see app/models/subscription.py.
        "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS trial_used_at TIMESTAMPTZ",
        # Per-plan storage ceiling (Phase 3) and hybrid-search chunk store are
        # created by Base.metadata.create_all; this only patches columns onto
        # pre-existing tables.
        "ALTER TABLE tenant_quotas ADD COLUMN IF NOT EXISTS max_storage_mb INTEGER NOT NULL DEFAULT 200",
        # Document authority metadata for conflict resolution — see
        # migrations/add_document_authority.sql and app/models/document.py.
        # Columns first (all with backward-compatible defaults), then the
        # constraints that make cross-tenant / self supersession impossible at
        # the database level. Constraints are guarded by DO blocks because
        # Postgres has no ADD CONSTRAINT IF NOT EXISTS.
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS effective_date DATE",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS approval_status VARCHAR(16) NOT NULL DEFAULT 'unverified'",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS supersedes_document_id UUID",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS authority_rank INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS authority_updated_at TIMESTAMPTZ",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS authority_updated_by UUID",
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ux_documents_id_tenant') "
        "THEN ALTER TABLE documents ADD CONSTRAINT ux_documents_id_tenant UNIQUE (id, tenant_id); END IF; END $$",
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_documents_supersedes_same_tenant') "
        "THEN ALTER TABLE documents ADD CONSTRAINT fk_documents_supersedes_same_tenant "
        "FOREIGN KEY (supersedes_document_id, tenant_id) REFERENCES documents (id, tenant_id); END IF; END $$",
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_documents_not_self_superseding') "
        "THEN ALTER TABLE documents ADD CONSTRAINT ck_documents_not_self_superseding "
        "CHECK (supersedes_document_id IS DISTINCT FROM id); END IF; END $$",
        "CREATE INDEX IF NOT EXISTS ix_documents_supersedes ON documents (supersedes_document_id)",
        # Session revocation on password change (Phase 2).
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0",
        # OTP brute-force lockout (Phase 2).
        "ALTER TABLE email_verifications ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0",
        # RBAC: conversation assignee + resolver (Phases 6 / multi-agent). The
        # tenant_users and invites tables and the tenantrole enum are created by
        # Base.metadata.create_all above.
        "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS assigned_user_id UUID REFERENCES users(id) ON DELETE SET NULL",
        "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS resolved_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL",
        # Multi-user tenancy: a tenant may now have an owner plus invited agents,
        # so the one-user-per-tenant unique constraint must go. Postgres names a
        # column UNIQUE constraint <table>_<col>_key by default.
        "ALTER TABLE users DROP CONSTRAINT IF EXISTS users_tenant_id_key",
        # migrations/init.sql names it explicitly, so drop that spelling too —
        # without this, invited agents fail with a UniqueViolation on insert.
        "ALTER TABLE users DROP CONSTRAINT IF EXISTS uq_users_tenant_id",
        # Per-user presence (multi-agent): last_seen_at on the membership row.
        "ALTER TABLE tenant_users ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ",
        # Per-user notifications: NULL user_id = whole-tenant (preserves old rows).
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE",
        # Backfill: every existing user is the owner of their tenant, so seed an
        # owner membership row for any user that doesn't have one yet.
        "INSERT INTO tenant_users (id, tenant_id, user_id, role) "
        "SELECT gen_random_uuid(), u.tenant_id, u.id, 'owner' FROM users u "
        "WHERE NOT EXISTS (SELECT 1 FROM tenant_users tu WHERE tu.user_id = u.id) "
        "ON CONFLICT DO NOTHING",
        # Audit trail + response traceability — see migrations/add_audit_trail.sql.
        # audit_logs / answer_traces tables come from create_all; this links
        # bot messages to their trace and makes audit_logs append-only at the
        # database: UPDATE is always rejected, DELETE only when the retention
        # sweep has set the nexus.audit_purge session flag (or a tenant row
        # is being removed, which cascades).
        "ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS trace_id UUID",
        "CREATE OR REPLACE FUNCTION audit_logs_immutable() RETURNS trigger AS $$ "
        "BEGIN "
        "  IF TG_OP = 'DELETE' AND current_setting('nexus.audit_purge', true) = '1' THEN RETURN OLD; END IF; "
        "  IF TG_OP = 'DELETE' AND OLD.tenant_id IS NOT NULL "
        "     AND NOT EXISTS (SELECT 1 FROM tenants t WHERE t.id = OLD.tenant_id) THEN RETURN OLD; END IF; "
        "  RAISE EXCEPTION 'audit_logs is append-only (% rejected)', TG_OP; "
        "END; $$ LANGUAGE plpgsql",
        "DROP TRIGGER IF EXISTS trg_audit_logs_immutable ON audit_logs",
        "CREATE TRIGGER trg_audit_logs_immutable BEFORE UPDATE OR DELETE ON audit_logs "
        "FOR EACH ROW EXECUTE FUNCTION audit_logs_immutable()",
        # Prepaid wallet + one-time signup trial — see
        # app/services/wallet_service.py and NexusContext plan doc
        # i-want-to-implement-floofy-hickey.md section C.
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS balance_usd NUMERIC(10,4) NOT NULL DEFAULT 0",
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS trial_messages_used INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS trial_ended_at TIMESTAMPTZ",
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS wallet_low_balance_warned BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS preferred_llm_provider VARCHAR(30)",
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS preferred_llm_model VARCHAR(100)",
        # Voice-call transcripts — see app/services/call_transcript_service.py.
        "ALTER TABLE ptt_sessions ADD COLUMN IF NOT EXISTS transcript TEXT",
        "ALTER TABLE ptt_sessions ADD COLUMN IF NOT EXISTS transcript_status VARCHAR(16)",
    ]
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for stmt in alter_statements:
            await conn.execute(text(stmt))

        # One-time rollout migration credit — see EXISTING_TENANT_MIGRATION_CREDIT_USD's
        # docstring in config.py for why this is guarded by a fixed cutoff
        # date (never "now()", which would keep moving forward and wrongly
        # credit real new signups on every future restart) AND
        # balance_usd = 0 (so this is a no-op once a tenant is migrated or
        # has topped up for real — safe to run on every startup).
        migration_cutoff = datetime.strptime(settings.LLM_BILLING_MIGRATION_CUTOFF, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        await conn.execute(
            text(
                "INSERT INTO wallet_transactions (id, tenant_id, type, amount_usd, balance_after, created_at) "
                "SELECT gen_random_uuid(), id, 'topup', :credit, :credit, now() FROM tenants "
                "WHERE balance_usd = 0 AND created_at < :cutoff"
            ),
            {"credit": settings.EXISTING_TENANT_MIGRATION_CREDIT_USD, "cutoff": migration_cutoff},
        )
        await conn.execute(
            text("UPDATE tenants SET balance_usd = :credit WHERE balance_usd = 0 AND created_at < :cutoff"),
            {"credit": settings.EXISTING_TENANT_MIGRATION_CREDIT_USD, "cutoff": migration_cutoff},
        )

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await ensure_frontend_schema()
        logger.info("Frontend-compat schema is up to date")
    except Exception as e:
        logger.error("Failed to apply frontend-compat schema: %s", e)

    try:
        from app.services.rag_service import verify_llm_providers
        await verify_llm_providers()
    except Exception as e:
        logger.error("LLM provider reachability check crashed unexpectedly: %s", e)

    import asyncio

    from app.services.indexing_service import stale_document_sweep_loop
    asyncio.create_task(stale_document_sweep_loop())

    from app.services.notification_service import weekly_summary_loop
    from app.services.conversation_service import (
        live_flag_sweep_loop,
        stale_conversation_sweep_loop,
        trash_purge_loop,
        release_orphaned_live_chats_loop,
    )
    # Held on app.state so these can't be garbage-collected mid-sleep —
    # asyncio.create_task() only keeps a weak reference to its result.
    app.state.weekly_summary_task = asyncio.create_task(weekly_summary_loop())
    app.state.conversation_sweep_task = asyncio.create_task(stale_conversation_sweep_loop())
    app.state.trash_purge_task = asyncio.create_task(trash_purge_loop())
    app.state.release_orphaned_task = asyncio.create_task(release_orphaned_live_chats_loop())
    app.state.live_flag_sweep_task = asyncio.create_task(live_flag_sweep_loop())

    # Audit-log / answer-trace retention (daily). The sweep is the only code
    # path allowed to delete audit rows — see audit_logs_immutable() above.
    from app.services.audit_service import retention_sweep_loop
    app.state.audit_retention_task = asyncio.create_task(retention_sweep_loop())

    # Optional Redis (shared rate-limit + embedding cache). No-op if unset.
    from app.core.redis_client import init_redis, close_redis
    await init_redis()

    logger.info(
        "%s API is running (debug=%s)",
        settings.APP_NAME,
        settings.DEBUG,
    )
    yield

    await close_redis()

app = FastAPI(
    title="SecureRAG++ API",
    version="1.0.0",
    description=(
        "Multi-tenant Retrieval-Augmented Generation platform. "
        "Provides secure document ingestion, semantic search, and LLM-powered Q&A "
        "with per-tenant isolation, quota enforcement, and subscription management."
    ),
    lifespan=lifespan,
    # Interactive API docs expose the entire schema; only serve them in DEBUG
    # so they aren't a free recon surface in production.
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
)

app.state.limiter = limiter

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request, exc):
    return JSONResponse(
        status_code=429,
        content={"detail": "Rate limit exceeded. Please try again later."}
    )

@app.middleware("http")
async def perf_timing_middleware(request: Request, call_next):
    """
    Temporary latency-audit middleware — logs total wall time per request
    plus a phase-by-phase breakdown for any endpoint using perf_timing.timed().
    See app/core/perf_timing.py.
    """
    token = perf_timing.start()
    t0 = time.perf_counter()
    response = await call_next(request)
    total_ms = (time.perf_counter() - t0) * 1000
    segments = perf_timing.finish(token)
    if segments:
        accounted_ms = sum(ms for _, ms in segments)
        breakdown = " | ".join(f"{label}={ms}ms" for label, ms in segments)
        perf_logger.info(
            "%s %s -> %s total=%.2fms accounted=%.2fms unaccounted=%.2fms | %s",
            request.method, request.url.path, response.status_code,
            total_ms, accounted_ms, total_ms - accounted_ms, breakdown,
        )
    else:
        perf_logger.info(
            "%s %s -> %s total=%.2fms",
            request.method, request.url.path, response.status_code, total_ms,
        )
    response.headers["X-Process-Time-Ms"] = f"{total_ms:.2f}"
    return response

@app.middleware("http")
async def audit_trail_middleware(request: Request, call_next):
    """Every mutating dashboard/API request → one append-only audit_logs
    row (actor, action, resource, outcome, IP). See app/core/audit_middleware.py."""
    from app.core.audit_middleware import audit_middleware
    return await audit_middleware(request, call_next)

@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Baseline security response headers. Conservative set that's safe for a
    JSON API (no CSP, which is the frontend's concern) — prevents MIME sniffing,
    clickjacking, and referrer leakage, and asks browsers to stick to HTTPS."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if not settings.DEBUG:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["Authorization", "Content-Type", "X-Widget-Key"],
    max_age=3600, )

# Compresses JSON responses over ~1KB (dashboard widget payloads, wallet
# transaction lists, etc.) — previously every response went over the wire
# uncompressed regardless of size.
app.add_middleware(GZipMiddleware, minimum_size=1000)


class WidgetPreflightMiddleware:
    """Answers CORS preflights for the public widget sub-app before the main
    app's CORSMiddleware sees them.

    Starlette middleware wraps the whole app, mounts included, so the main
    CORSMiddleware (dashboard allow-list) answered every OPTIONS for
    /api/public/widget/* with 400 "Disallowed CORS origin" for any origin
    that wasn't the Nexus frontend — the widget sub-app's own
    allow_origins=["*"] never ran. Simple GETs/POSTs were unaffected (the
    sub-app still stamped ACAO:* on the response), which is why only direct
    customer-page calls with custom headers broke. Added last so it is the
    outermost layer.
    """

    PREFIX = "/api/public/widget"

    def __init__(self, app):
        # Starlette constructs middleware as cls(app=<next layer>).
        self.inner = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and scope["method"] == "OPTIONS"
            and scope["path"].startswith(self.PREFIX)
            and any(k == b"origin" for k, _ in scope["headers"])
        ):
            from starlette.responses import PlainTextResponse
            from app.api.public.widget import WIDGET_CORS_ALLOW_HEADERS, WIDGET_CORS_ALLOW_METHODS

            response = PlainTextResponse(
                "OK",
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": ", ".join(WIDGET_CORS_ALLOW_METHODS),
                    "Access-Control-Allow-Headers": ", ".join(WIDGET_CORS_ALLOW_HEADERS),
                    "Access-Control-Max-Age": "3600",
                    "Vary": "Origin",
                },
            )
            await response(scope, receive, send)
            return
        await self.inner(scope, receive, send)


app.add_middleware(WidgetPreflightMiddleware)

# Frontend-compat API — paths/shapes match Nexus-frontend/src/api/*.api.ts.
app.include_router(frontend_router, prefix="/api")

# Stripe webhook — unauthenticated (verified via signature, not a JWT), so it
# is mounted directly rather than going through frontend_router's Depends.
app.include_router(billing_webhook_router, prefix="/api")

# Public widget API — mounted as its own sub-app (own CORS: allow_origins=["*"],
# since it's embedded on arbitrary customer domains not known ahead of time).
# The real per-tenant domain allowlist is enforced inside app/api/public/widget.py.
app.mount("/api/public/widget", widget_app)

# Push-to-talk WebSocket signaling (agent <-> visitor live voice). Native
# FastAPI WebSocket at /ws/ptt — see app/api/ptt_ws.py.
app.include_router(ptt_ws_router)

@app.get("/health", tags=["health"], summary="Health check")
async def health_check() -> dict:
    """Return application health status."""
    return {"status": "ok", "app": settings.APP_NAME, "version": "1.0.0"}
