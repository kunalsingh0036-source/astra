"""Database engine and session factory."""

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from finance.config import settings


def _normalize_async_url(url: str) -> str:
    """Force +asyncpg driver hint (Railway injects bare postgresql://).
    Mirror of astra/db/engine.py's shim."""
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
