"""
Centralized configuration. All settings from .env, single source of truth.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Database
    database_url: str = "postgresql+asyncpg://astra:astra_dev@localhost:5433/whatsapp_gateway"

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

    @property
    def meta_base_url(self) -> str:
        return f"https://graph.facebook.com/{self.meta_api_version}"


settings = Settings()
