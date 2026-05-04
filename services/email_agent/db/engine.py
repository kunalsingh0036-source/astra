"""Database engine and session factory."""

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from email_agent.config import settings


def _normalize_async_url(url: str) -> str:
    """Force the +asyncpg driver hint. Railway injects DATABASE_URL
    as bare `postgresql://` (no driver hint) and create_async_engine
    then defaults to psycopg2, which is sync and raises
    InvalidRequestError. Mirror of astra/db/engine.py's shim."""
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


engine = create_async_engine(
    _normalize_async_url(settings.database_url),
    echo=False,
    pool_size=10,
    max_overflow=5,
    # Railway's internal LB drops idle TCP connections silently.
    # pool_pre_ping rebuilds dead connections on checkout instead
    # of hanging on them; pool_recycle=300 rotates connections
    # older than 5 min before they go stale.
    pool_pre_ping=True,
    pool_recycle=300,
)

async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Base class for all models."""
    pass


async def get_session():
    """FastAPI dependency that yields a database session."""
    async with async_session() as session:
        yield session
