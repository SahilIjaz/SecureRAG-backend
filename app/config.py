from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Every authenticated request re-derives its User row from the DB (see
    # auth_service._load_user) — on a remote/high-latency Postgres that round
    # trip alone can dominate a request. Cache it in-process for a few
    # seconds: short enough that a deactivation/profile change is picked up
    # almost immediately (and those mutation sites proactively invalidate
    # their own entry anyway — see auth_service.invalidate_user_cache), long
    # enough to collapse the burst of calls a single onboarding wizard makes.
    AUTH_USER_CACHE_TTL_SECONDS: float = 15.0

    # SQLAlchemy issues a liveness check on every pooled-connection checkout
    # when this is True. Confirmed (2026-08-21 latency audit, live probe
    # against Neon) it adds ~1s on top of an already-expensive checkout in
    # this environment, without catching anything a checkout-time reconnect
    # wouldn't already handle. Keep it True in any environment where stale
    # connections actually surface as errors; False is an explicit,
    # measured trade favoring latency once that's been ruled out.
    DB_POOL_PRE_PING: bool = True

    # Same idea as AUTH_USER_CACHE_TTL_SECONDS, for the public widget's
    # tenant+config lookup by widget API key (app/api/frontend/helpers.py:
    # get_tenant_by_widget_api_key). That lookup sits in front of every
    # single widget request (config load, every chat message, escalate,
    # live-status poll) and measured ~1.5s in the 2026-08-17 latency audit —
    # the highest single fixed cost on the hot path, on a query whose result
    # (tenant + chatbot config) rarely changes between one visitor message
    # and the next.
    WIDGET_TENANT_CACHE_TTL_SECONDS: float = 10.0
    APP_NAME: str = "SecureRAG++"
    DEBUG: bool = False

    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    EMAILS_FROM_EMAIL: str = ""
    EMAILS_FROM_NAME: str = "SecureRAG++"

    BREVO_API_KEY: str = ""

    GOOGLE_CLIENT_ID: str = ""

    OTP_EXPIRE_MINUTES: int = 10
    # Wrong OTP guesses allowed before the code is locked and a new one is
    # required — caps brute-force at 5 tries per issued code.
    OTP_MAX_ATTEMPTS: int = 5
    # Reject widget API calls that carry no Origin header (non-browser callers).
    # Real embeds always send one; this closes the scraped-key quota-burning
    # bypass. Set false only for a trusted non-browser integration.
    WIDGET_REQUIRE_ORIGIN: bool = True

    FRONTEND_URL: str = "http://localhost:5173"
    # Extra browser origins allowed by CORS beyond FRONTEND_URL and the local
    # dev ports — comma-separated (e.g. "https://app.example.com,https://example.com").
    CORS_EXTRA_ORIGINS: str = ""

    @property
    def cors_allowed_origins(self) -> list[str]:
        """The full allow-list: the configured frontend origin, the common
        local dev ports, and any CORS_EXTRA_ORIGINS. Deduplicated, order
        preserved. Driving this from config means production no longer silently
        breaks on a hardcoded localhost-only list."""
        origins = [
            self.FRONTEND_URL,
            "http://localhost:5173",
            "http://localhost:5174",
            "http://localhost:5175",
            "http://localhost:3000",
        ]
        origins += [o.strip() for o in self.CORS_EXTRA_ORIGINS.split(",") if o.strip()]
        seen: set[str] = set()
        return [o for o in origins if o and not (o in seen or seen.add(o))]

    UPLOAD_DIR: str = "storage/uploads"
    MAX_UPLOAD_SIZE_MB: int = 50
    # Hard ceiling on a whole upload request body (multipart), rejected from
    # Content-Length before buffering. Must exceed the largest plan's per-file
    # cap (100MB) plus room for small batches + overhead.
    MAX_REQUEST_BODY_MB: int = 120
    ALLOWED_MIME_TYPES: str = (
        "application/pdf,"
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
        "text/plain,"
        "text/markdown"
    )

    CRAWL4AI_ENABLED: bool = True
    CRAWL4AI_TIMEOUT: int = 30
    CRAWL4AI_MAX_CONTENT_SIZE_MB: int = 50

    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""

    FILE_ENCRYPTION_KEY: str = ""

    PINECONE_API_KEY: str = ""
    PINECONE_INDEX_NAME: str = "securerag-documents"

    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_PRICE_STARTER: str = ""
    STRIPE_PRICE_GROWTH: str = ""
    STRIPE_PRICE_BUSINESS: str = ""

    # Kept even though the active answer_question() call now uses Gemini —
    # the Anthropic call is commented out in rag_service.py, not deleted, so
    # this setting stays valid if it's ever restored.
    ANTHROPIC_API_KEY: str = ""
    GEMINI_API_KEY: str = ""

    # Model names, not hardcoded in rag_service.py, so a Google-side deprecation
    # (has already happened twice) is an env change, not a code change +
    # redeploy. Both default to "-latest" aliases rather than dated pinned
    # versions (e.g. "gemini-2.5-flash") — on this project's free-tier API
    # key, every dated gemini-2.5-* model 404s as "no longer available to new
    # users" while the alias identifiers work fine, since Google moves what
    # they point to instead of retiring them outright.
    # 2026-08-21 latency audit: on this project's API key, gemini-flash-latest
    # (the heavier model) 503/429'd on ~78% of live calls, so most requests
    # were paying for a failed first attempt before falling back anyway.
    # gemini-flash-lite-latest answered fast and reliably in the same audit,
    # so it's primary now — flash-latest stays as the fallback rather than
    # being dropped, in case the lite model degrades or its quota tightens.
    GEMINI_ANSWER_MODEL: str = "gemini-flash-lite-latest"
    GEMINI_FALLBACK_MODELS: str = "gemini-flash-latest"  # comma-separated, tried in order

    # Fixed independently of GEMINI_ANSWER_MODEL/GEMINI_MODEL_CHAIN — the
    # background sentiment/topic classifier (rag_service.classify_
    # conversation_turn) always wants the lighter model regardless of which
    # one is primary for answering. Confirmed live: the heavier model spends
    # an unpredictable chunk of its output budget on internal "thinking"
    # tokens before any visible text, truncating this short reply at
    # MAX_TOKENS with empty/partial text. Previously this used
    # GEMINI_MODEL_CHAIN[-1] as a stand-in for "the lite model" — that broke
    # silently the moment the answer chain's order changed, which is exactly
    # what just happened above.
    GEMINI_CLASSIFICATION_MODEL: str = "gemini-flash-lite-latest"

    # Platform-owner-controlled provider fallback order — NOT per-tenant.
    # A tenant's own selected provider (app/models/tenant_settings.py) is
    # tried first; if every model in that provider's own chain fails, the
    # router falls through to this chain before giving up entirely. With
    # the default single entry, the router reproduces the pre-refactor
    # Gemini-only retry behavior exactly.
    LLM_PROVIDER_CHAIN: str = "gemini"  # comma-separated, e.g. "gemini,anthropic"

    # Externalized rather than hardcoded (the pre-refactor commented-out
    # Anthropic block had "claude-sonnet-5" baked into the code) — same
    # reasoning as GEMINI_ANSWER_MODEL: a provider-side rename shouldn't
    # need a redeploy.
    ANTHROPIC_ANSWER_MODEL: str = "claude-sonnet-5"
    ANTHROPIC_REQUEST_TIMEOUT_SECONDS: float = 20.0

    # $ per 1M tokens, "provider:model:input_price:output_price" entries,
    # comma-separated. Every provider/model this build can route to needs
    # a real entry here — there is no "$0, skip billing" branch in code;
    # the only free usage in the system is the signup trial (a separate
    # message/day counter, not a pricing lookup). Confirm these numbers
    # against each provider's current pricing page before relying on them
    # for real billing — they are config, not verified-at-write-time facts.
    LLM_PRICING_TABLE: str = (
        "gemini:gemini-flash-lite-latest:0.10:0.40,"
        "gemini:gemini-flash-latest:0.30:2.50,"
        "anthropic:claude-sonnet-5:3.00:15.00"
    )

    # ── Prepaid wallet + one-time signup trial ──────────────────────────
    # There is no permanent free tier — see NexusContext plan doc
    # i-want-to-implement-floofy-hickey.md section C. A new tenant gets a
    # small, one-time, non-renewing trial on the platform default provider
    # only (message count AND day window, whichever hits first); once it
    # ends, every provider — including the default — draws real $ from the
    # tenant's wallet at raw cost * WALLET_MARKUP_MULTIPLIER.
    TRIAL_MESSAGE_LIMIT: int = 50
    TRIAL_DURATION_DAYS: int = 3
    WALLET_MARKUP_MULTIPLIER: float = 1.30
    WALLET_LOW_BALANCE_ALERT_THRESHOLD_USD: float = 2.0

    # One-time rollout migration credit for tenants that already existed
    # before this feature shipped — without it, their trial's day-cap fails
    # on their very first post-rollout message (their signup date is
    # already older than TRIAL_DURATION_DAYS) with balance_usd=0, which
    # would silently break every existing tenant's chatbot the moment this
    # deploys. See ensure_frontend_schema()'s migration backfill, guarded by
    # LLM_BILLING_MIGRATION_CUTOFF so it only ever applies once, to tenants
    # that existed before that fixed date — never to a real new signup.
    EXISTING_TENANT_MIGRATION_CREDIT_USD: float = 5.0
    LLM_BILLING_MIGRATION_CUTOFF: str = "2026-08-25"

    # Hard per-attempt ceiling on a Gemini generation call. Without this, a
    # stalled upstream request blocks the whole retry chain from ever
    # reaching the fallback model within a reasonable time — confirmed live
    # (2026-08-17 latency audit): one gemini-flash-lite-latest call hung for
    # 90s with nothing timing it out. Comfortably above the ~5-15s a healthy
    # call actually takes (per that same audit) so this only fires on a
    # genuine stall, not normal variance.
    GEMINI_REQUEST_TIMEOUT_SECONDS: float = 20.0

    RAG_CHUNK_SIZE: int = 500
    RAG_CHUNK_OVERLAP: int = 50
    RAG_SEARCH_TOP_K: int = 5
    # A widget conversation that has been idle longer than this auto-splits: the
    # next message starts a fresh conversation instead of appending to the old
    # one. Prevents unrelated chats (and, on a shared device, different people)
    # from being merged into a single conversation thread.
    WIDGET_CONVERSATION_IDLE_MINUTES: int = 30
    # Minimum cosine score a dense match must clear inside hybrid retrieval's
    # vector branch before it's fused with keyword results (see hybrid_search).
    RAG_MIN_VECTOR_SCORE: float = 0.30
    # Toggle hybrid (vector + keyword) retrieval. When off, retrieval is the
    # original pure-vector path, which uses RAG_MIN_RELEVANCE_SCORE below.
    RAG_HYBRID_SEARCH: bool = True
    # Below this cosine similarity, the top retrieved chunk (pure-vector path) is
    # treated as "not actually relevant" — same fallback path as zero chunks
    # (see _prepare_generation) instead of handing the LLM a best-effort context
    # it has to independently judge as off-topic.
    RAG_MIN_RELEVANCE_SCORE: float = 0.75

    # Background-job recovery: how long a document may sit in "processing"
    # before a startup sweep assumes the worker died and reschedules it.
    INDEXING_STALE_MINUTES: int = 15

    # How long an Open/Handed-off conversation may sit with no new message
    # before a recurring sweep auto-closes it — otherwise nothing ever marks
    # a conversation Resolved except an agent doing it by hand, so an
    # abandoned/unanswered chat (or a stray test message) stays "open"
    # forever and the inbox only ever grows.
    CONVERSATION_AUTO_RESOLVE_DAYS: int = 7

    # How long a soft-deleted conversation stays recoverable before
    # trash_purge_loop() (app/services/conversation_service.py) hard-deletes it.
    CONVERSATION_TRASH_RETENTION_DAYS: int = 3

    # Presence (live-agent-handoff). The dashboard pings roughly every 20s
    # while open (Frontend/src/api/presence.api.ts) — this window needs
    # enough margin over that interval that one dropped ping doesn't flip an
    # owner to "offline" (a laptop going to sleep or losing network is what
    # should actually flip it, not one missed request).
    PRESENCE_ONLINE_WINDOW_SECONDS: int = 60

    # How long the widget shows "connecting..." after a visitor escalates
    # before falling back to "no one's available right now" — real people
    # don't respond in 20s, but a visitor shouldn't wait indefinitely either.
    LIVE_JOIN_TIMEOUT_SECONDS: int = 90

    # How recently the widget must have polled/messaged for the visitor to
    # still count as "here" — gates whether an owner is allowed to join a
    # waiting chat. Deliberately generous (minutes, not seconds): a
    # backgrounded browser tab (visitor switched to another tab) gets its
    # JS timers throttled or fully paused by the browser, so the poll that
    # refreshes this can silently stall for well over a minute during
    # completely normal tab-switching — not just when the chat is actually
    # abandoned. A short window (originally 45s) was mistaking that for the
    # visitor having left. See the widget's visibilitychange handler
    # (WidgetPage.tsx), which also fires an immediate poll the moment the
    # tab regains focus, to recover from this faster than waiting for the
    # next scheduled tick.
    VISITOR_PRESENCE_WINDOW_SECONDS: int = 180

    # Message-text retention (manual, admin-triggered — not a recurring
    # sweep; see NexusContext/LIVE_AGENT_HANDOFF_PLAN.md gap #4). Only
    # applies to Resolved conversations — Open/Handed-off ones are either
    # still active or will auto-resolve via CONVERSATION_AUTO_RESOLVE_DAYS
    # first, which is what actually makes them eligible. The parent
    # Conversation row (and its summary columns) is never deleted by this —
    # only the ConversationMessage rows, so Overview stats stay accurate
    # without keeping transcript text around indefinitely.
    MESSAGE_RETENTION_DAYS_BOT_ONLY: int = 30
    MESSAGE_RETENTION_DAYS_AGENT_INVOLVED: int = 365

    # FAQ entries (question + answer pairs added directly, no file involved).
    FAQ_QUESTION_MAX_LEN: int = 500
    FAQ_ANSWER_MAX_LEN: int = 5000

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

    @property
    def GEMINI_MODEL_CHAIN(self) -> list[str]:
        """GEMINI_ANSWER_MODEL first, then each GEMINI_FALLBACK_MODELS entry,
        skipping blanks/duplicates. Shared by rag_service.py's retry loop and
        its startup reachability check, so the parsing logic lives once."""
        chain = [self.GEMINI_ANSWER_MODEL]
        for name in self.GEMINI_FALLBACK_MODELS.split(","):
            name = name.strip()
            if name and name not in chain:
                chain.append(name)
        return chain

    @property
    def LLM_PROVIDER_CHAIN_LIST(self) -> list[str]:
        """LLM_PROVIDER_CHAIN parsed the same "skip blanks/dedupe" way as
        GEMINI_MODEL_CHAIN — shared by app/services/llm/router.py and its
        startup reachability check."""
        chain: list[str] = []
        for name in self.LLM_PROVIDER_CHAIN.split(","):
            name = name.strip()
            if name and name not in chain:
                chain.append(name)
        return chain

    @model_validator(mode="after")
    def _validate_chunk_overlap(self) -> "Settings":
        if self.RAG_CHUNK_OVERLAP >= self.RAG_CHUNK_SIZE:
            raise ValueError(
                f"RAG_CHUNK_OVERLAP ({self.RAG_CHUNK_OVERLAP}) must be smaller than "
                f"RAG_CHUNK_SIZE ({self.RAG_CHUNK_SIZE}); otherwise chunking degrades "
                "to near-1-token steps."
            )
        return self

settings = Settings()
