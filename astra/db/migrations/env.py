"""
Alembic migration environment.

Configured for async PostgreSQL via asyncpg.
Imports all models from astra.db.models so autogenerate
can detect schema changes.

URL resolution order (first wins):
  1. DATABASE_URL env var          ← used by Railway/cloud + ad-hoc migrations
  2. sqlalchemy.url in alembic.ini ← local dev fallback

Why env-first: the alembic.ini value is hard-coded to localhost:5433
for the developer-machine Docker Postgres. When we run alembic against
Railway (or any other Postgres), we set DATABASE_URL and want it to win
without editing committed files. Without this override the cloud
target was being silently ignored — alembic would connect to localhost,
see it was at head, and exit 0 having migrated nothing on the cloud DB.
"""

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# This is the Alembic Config object
config = context.config

# Honor DATABASE_URL from env if set. Normalize the postgresql+asyncpg
# scheme — accept either form so callers don't have to remember.
_env_url = os.environ.get("DATABASE_URL", "").strip()
if _env_url:
    if _env_url.startswith("postgresql://"):
        _env_url = _env_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    config.set_main_option("sqlalchemy.url", _env_url)

# Set up Python logging from the config file
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import all models so Alembic can see them
from astra.db.models import Base

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (generates SQL without connecting)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migrations — runs the async version."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
