"""Centralized configuration. All settings from .env."""

from pathlib import Path

from pydantic_settings import BaseSettings

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = {"env_file": str(_ENV_FILE), "env_file_encoding": "utf-8"}

    # Database
    database_url: str = "postgresql+asyncpg://astra:astra_dev@localhost:5433/finance_db"

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
