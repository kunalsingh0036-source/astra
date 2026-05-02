"""
Centralized configuration for Astra.

All settings are loaded from environment variables (via .env file).
This is the single source of truth for configuration — no settings
are scattered across modules.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Anthropic
    anthropic_api_key: str = ""

    # Database
    database_url: str = "postgresql+asyncpg://astra:astra_dev@localhost:5433/astra"

    # Redis
    redis_url: str = "redis://localhost:6380/0"

    # Models
    model_opus: str = "claude-opus-4-6"
    model_sonnet: str = "claude-sonnet-4-6"
    model_haiku: str = "claude-haiku-4-5-20251001"

    # Autonomy
    default_autonomy_mode: str = "always_ask"

    # Notes writeback — applies only to the Kunal training-counter note.
    #   "approval" — stage a pending row; require Kunal's Apply click
    #   "auto"     — write directly (no gate)
    #   "off"      — never touch the note; only compute + report
    notes_writeback_mode: str = "approval"

    # Briefing / catch-up delivery channel.
    #   "notification" — macOS notification → tap opens Astra web
    #   "email"        — Gmail only
    #   "both"         — notification primary, email as redundancy
    briefing_channel: str = "both"

    # Public URL for astra-web. Used by notifications and the
    # briefing email to link to /tonight, /briefing, /catchup/:id.
    astra_web_base_url: str = "http://localhost:3000"

    # Web Push (VAPID) — browsers subscribe with the public key; the
    # backend signs send requests with the private key. These are
    # stable for the lifetime of the deployment; rotating them
    # invalidates every existing subscription.
    vapid_private_key_path: str = ""
    vapid_public_key: str = ""
    # RFC 8292 requires a contact email or URL. Apple is strict about
    # this — without a valid mailto/https contact, their push gateway
    # returns 403 and iPhone notifications never fire.
    vapid_contact: str = ""

    # Embeddings
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dimension: int = 384

    # Memory retrieval defaults
    memory_top_k: int = 10
    memory_relevance_threshold: float = 0.5

    # Scheduler
    briefing_hour: int = 7
    briefing_minute: int = 30
    health_check_interval_seconds: int = 300
    consolidation_hour: int = 3

    # Memory consolidation
    consolidation_decay_factor: float = 0.95
    consolidation_prune_threshold: float = 0.1
    consolidation_min_age_days: int = 30
    consolidation_summary_min_age_days: int = 90
    consolidation_max_clusters: int = 3

    # Tunnel (webhook forwarding)
    tunnel_provider: str = "ngrok"  # "ngrok" or "cloudflared"
    tunnel_hostname: str = ""  # Custom domain for cloudflared
    ngrok_authtoken: str = ""


settings = Settings()
