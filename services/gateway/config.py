"""
Centralized configuration. All settings from .env, single source of truth.
"""

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # extra="ignore": the env_file is whatever .env exists in CWD —
    # on a laptop that's often a repo root with unrelated vars, and
    # pydantic-settings' default extra='forbid' turned each one into
    # an import-time ValidationError. Deployed containers never hit
    # this (no .env file; undeclared plain env vars are always
    # ignored), which is why it lurked until the test suite imported
    # gateway modules from the repo root. Also fixes the no-op
    # module docstring (an import preceded it — flagged in the
    # deep-scan P2s).
    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    # Database. Service-specific alias first (for a consolidated process),
    # DATABASE_URL fallback for standalone. See email_agent/config.py.
    database_url: str = Field(
        default="postgresql+asyncpg://astra:astra_dev@localhost:5433/whatsapp_gateway",
        validation_alias=AliasChoices("WHATSAPP_DATABASE_URL", "DATABASE_URL"),
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
    redis_url: str = "redis://localhost:6380/1"

    # Meta WhatsApp Business API
    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: str = ""
    whatsapp_business_account_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""
    meta_api_version: str = "v18.0"

    # Rate limits
    whatsapp_daily_limit: int = 300
    default_cooldown_hours: int = 4   # Between ANY agent to same contact
    agent_cooldown_hours: int = 24    # Same agent to same contact
    session_window_hours: int = 24    # Meta's 24hr session window

    # AI
    anthropic_api_key: str = ""
    model_haiku: str = "claude-haiku-4-5-20251001"

    # Server
    host: str = "0.0.0.0"
    port: int = 8600

    # Mesh auth — callers (astra-web, stream, scheduler, A2A client)
    # must send `x-astra-secret: <this value>` on every protected
    # request. Empty value = FAIL CLOSED (503), never open: this
    # service can send WhatsApp as Kunal's businesses and sat publicly
    # unauthenticated until 2026-06-11.
    agent_shared_secret: str = ""

    @property
    def meta_base_url(self) -> str:
        return f"https://graph.facebook.com/{self.meta_api_version}"


settings = Settings()
