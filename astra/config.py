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

    # Autonomy. Three places used to disagree about what the default
    # should be on a fresh install: this constant (was "always_ask"),
    # the app_settings DB seed (`semi_auto`, per migration
    # m1f47g3e8f0b), and astra-web's /api/autonomy fallback
    # (`semi_auto`, when the DB row is missing). The misalignment
    # meant a cold-start UI showed "semi_auto" while the agent
    # enforced "always_ask" until the first DB read. Resolved by
    # picking `semi_auto` — the balanced behaviour that matches
    # both the seed and the UI's expectation. This value is now only
    # used as the cold-start fallback before refresh_from_db runs.
    default_autonomy_mode: str = "semi_auto"

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
    # Calibrated for all-MiniLM-L6-v2 (the embedding model above).
    # Empirically: legitimate semantic matches against memories of
    # 100-2000 chars score in the 0.25-0.40 range, not 0.50+. The
    # original 0.5 threshold rejected nearly every real match —
    # users could ask "where were we on our Studio 375 analysis"
    # and get "No relevant memories found" while a procedural
    # memory titled "Top Studios website reference: 375.studio/en"
    # sat in the DB at importance 0.9 with similarity 0.28.
    # Down-stream importance re-ranking does the actual quality
    # ordering; this threshold's only job is filtering pure noise
    # (random text scores ~0.10 against this model). 0.2 keeps the
    # noise floor while letting real matches through.
    memory_relevance_threshold: float = 0.2

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
