"""Centralized configuration. All settings from .env."""

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings

# Project root = parent of this package (…/email-agent/)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = {"env_file": str(_ENV_FILE), "env_file_encoding": "utf-8"}

    # Database
    database_url: str = "postgresql+asyncpg://astra:astra_dev@localhost:5433/email_db"

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
    redis_url: str = "redis://localhost:6380/3"

    # AI
    anthropic_api_key: str = ""
    model_haiku: str = "claude-haiku-4-5-20251001"
    model_sonnet: str = "claude-sonnet-4-6"

    # Gmail API — resolved to absolute paths relative to the project root so
    # the agent works regardless of CWD (agent can be launched from anywhere,
    # including scheduler subprocesses or launchd).
    gmail_credentials_path: str = str(_PROJECT_ROOT / "credentials" / "gmail_credentials.json")
    gmail_token_path: str = str(_PROJECT_ROOT / "credentials" / "gmail_token.json")

    # Gmail Push (Pub/Sub) — astra-493705 replaced retired bay-fundraiser
    pubsub_topic: str = "projects/astra-493705/topics/gmailpush"
    webhook_base_url: str = ""  # Set dynamically by ngrok or manually
    ngrok_authtoken: str = ""

    # Server
    host: str = "0.0.0.0"
    port: int = 8005

    @field_validator("gmail_credentials_path", "gmail_token_path", mode="after")
    @classmethod
    def _resolve_relative(cls, v: str) -> str:
        """If env supplies a relative path, anchor it at the project root."""
        p = Path(v)
        if not p.is_absolute():
            p = (_PROJECT_ROOT / p).resolve()
        return str(p)


settings = Settings()
