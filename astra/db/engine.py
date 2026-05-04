"""
Database engine and session factory.

Uses SQLAlchemy 2.0 async engine with asyncpg driver for PostgreSQL.
All database access goes through the async session factory defined here.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from astra.config import settings


def _normalize_async_url(url: str) -> str:
    """Force the +asyncpg driver hint onto a Postgres URL.

    Railway (and most managed Postgres providers) inject DATABASE_URL
    as a bare `postgresql://` URL without a driver hint. SQLAlchemy's
    `create_async_engine` then defaults to psycopg2 (the sync driver),
    which (a) isn't installed in our image and (b) wouldn't work with
    `create_async_engine` even if it were. Normalizing here means our
    code works whether the env injects the bare scheme or the
    explicit one.
    """
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        # Heroku-style legacy alias — also bare; same fix.
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


engine = create_async_engine(
    _normalize_async_url(settings.database_url),
    echo=False,
    pool_size=5,
    max_overflow=10,
    # ── Connection health (CRITICAL on Railway) ──────────────
    # Railway's internal load balancer silently drops idle TCP
    # connections after a few minutes. Without these flags a
    # dead pool connection looks alive to SQLAlchemy → next
    # query hangs forever waiting for a server that's gone →
    # tool calls stall → SDK CLI throws "Stream closed" inside
    # its hook callback → the whole turn freezes with no
    # exception, no event, nothing.
    #
    # pool_pre_ping issues `SELECT 1` before checking out a
    # pooled connection. ~0.5ms cost per use; rebuilds dead
    # connections instead of hanging on them.
    #
    # pool_recycle=300 proactively re-creates connections older
    # than 5 min. Belt-and-braces with pre_ping: even if the
    # ping somehow misses, recycle catches it on a timer.
    pool_pre_ping=True,
    pool_recycle=300,
)

async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""

    pass


async def get_session() -> AsyncSession:
    """Yield an async database session."""
    async with async_session() as session:
        yield session
