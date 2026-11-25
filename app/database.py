from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from app.config import settings

def _build_database_url(raw_url: str) -> str:
    """
    Neon (and some other cloud PG providers) append ?sslmode=require&channel_binding=require
    to the connection string. asyncpg does not accept these as query params — it needs
    ssl passed via connect_args instead. Strip them from the URL here.
    """
    if "?" in raw_url:
        raw_url = raw_url.split("?")[0]
    return raw_url

_db_url = _build_database_url(settings.DATABASE_URL)

def _is_local_db(url: str) -> bool:
    """A Postgres on this machine / the Docker host has no TLS — forcing SSL
    against it makes asyncpg fail with "rejected SSL upgrade". Cloud hosts
    (Neon etc.) keep SSL required."""
    from sqlalchemy.engine import make_url

    return (make_url(url).host or "") in ("localhost", "127.0.0.1", "::1", "host.docker.internal", "postgres", "db")

_connect_args: dict = {}
if not _is_local_db(_db_url):
    # Remote pooled endpoint (PgBouncer): disable the client-side statement cache.
    _connect_args["statement_cache_size"] = 0
    _connect_args["ssl"] = "require"

async_engine = create_async_engine(
    _db_url,
    echo=settings.SQL_ECHO,
    pool_pre_ping=settings.DB_POOL_PRE_PING,
    pool_size=5,
    max_overflow=10,
    # statement_cache_size=0: DATABASE_URL points at Neon's pooled endpoint (PgBouncer,
    # transaction mode) — a prepared statement created on one physical backend connection
    # can vanish before asyncpg's next query if PgBouncer hands out a different backend,
    # surfacing as "prepared statement ... does not exist". Disabling asyncpg's client-side
    # statement cache avoids that; the pooler is what actually removes the connection-setup
    # latency, so losing local statement caching costs nothing extra here.
    connect_args=_connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

Base = declarative_base()

async def get_db() -> AsyncSession:
    """Yield an async DB session with automatic commit/rollback."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
