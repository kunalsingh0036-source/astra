from pydantic import AliasChoices, Field, field_validator
"""Centralized configuration. All settings from .env."""

from pathlib import Path

from pydantic_settings import BaseSettings

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = {"env_file": str(_ENV_FILE), "env_file_encoding": "utf-8"}

    # Database. Service-specific alias first (for a consolidated process),
    # DATABASE_URL fallback for standalone. See email_agent/config.py.
    database_url: str = Field(
        default="postgresql+asyncpg://astra:astra_dev@localhost:5433/finance_db",
        validation_alias=AliasChoices("FINANCE_DATABASE_URL", "DATABASE_URL"),
    )

    @field_validator("database_url", mode="after")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        """Railway sets DATABASE_URL=`postgresql://...` (bare, sync
        driver). create_async_engine needs `postgresql+asyncpg://...`
        — without that prefix every query 500s. Same pattern
        documented in learnings_railway_migration.md.
        """
        if not v:
            return v
        if v.startswith("postgresql://"):
            v = "postgresql+asyncpg://" + v[len("postgresql://"):]
        elif v.startswith("postgres://"):
            v = "postgresql+asyncpg://" + v[len("postgres://"):]
        if "?sslmode=" in v:
            head, _, tail = v.partition("?sslmode=")
            other = tail.split("&", 1)[1] if "&" in tail else ""
            v = head + (("?" + other) if other else "")
        return v

    # Redis
    redis_url: str = "redis://localhost:6380/2"

    # AI
    anthropic_api_key: str = ""
    model_haiku: str = "claude-haiku-4-5-20251001"
    model_sonnet: str = "claude-sonnet-4-6"

    # Bookkeeper integration
    bookkeeper_url: str = "http://localhost:8000"

    # Server
    host: str = "0.0.0.0"
    port: int = 8004


settings = Settings()
