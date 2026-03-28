"""
config/settings.py

Central configuration for the Polymarket Alpha system.
All strategy toggles live here. Set values via environment variables or .env file.

Usage:
    from config.settings import settings
    if settings.ENABLE_RESOLUTION_CRITERIA:
        ...
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ── Strategy Toggles ────────────────────────────────────────────────────
    # Each strategy is independently toggleable at runtime.
    # Disabling a strategy stops its compute and API calls but not data ingestion.

    ENABLE_RESOLUTION_CRITERIA: bool = Field(
        default=True,
        description=(
            "Use Claude to parse exact resolution conditions and find where "
            "traders misread the literal criteria. Incurs Anthropic API cost. "
            "Cached per market — only re-runs when market description changes."
        ),
    )

    ENABLE_BASE_RATES: bool = Field(
        default=True,
        description=(
            "Compare market prices to historical base rates for similar event "
            "types (elections, FDA approvals, macro events, etc). Pure DB "
            "lookups — no external API cost."
        ),
    )

    ENABLE_LATENCY_ARB: bool = Field(
        default=True,
        description=(
            "Monitor Metaculus, Manifold, and Kalshi for price discrepancies "
            "vs Polymarket. Polls external platforms at a rate-limited pace. "
            "Disable to reduce external API calls."
        ),
    )

    # ── Anthropic / LLM ─────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str = Field(default="", description="Anthropic API key")
    ANTHROPIC_MODEL: str = Field(
        default="claude-sonnet-4-5",
        description="Model to use for resolution criteria analysis",
    )
    ANTHROPIC_MAX_TOKENS: int = Field(default=2048)
    # Cache resolution analysis for this many hours before re-running
    RESOLUTION_CACHE_TTL_HOURS: int = Field(default=24)

    # ── Database ─────────────────────────────────────────────────────────────
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://user:password@localhost:5432/polymarket_alpha"
    )
    DB_POOL_SIZE: int = Field(default=10)
    DB_MAX_OVERFLOW: int = Field(default=20)

    # ── Polymarket CLOB ──────────────────────────────────────────────────────
    CLOB_BASE_URL: str = Field(default="https://clob.polymarket.com")
    CLOB_WS_URL: str = Field(
        default="wss://ws-subscriptions-clob.polymarket.com/ws/market"
    )
    GAMMA_API_URL: str = Field(default="https://gamma-api.polymarket.com")
    # Optional — only needed for order placement
    POLYMARKET_PRIVATE_KEY: str = Field(default="")
    POLYMARKET_API_KEY: str = Field(default="")
    POLYMARKET_API_SECRET: str = Field(default="")
    POLYMARKET_API_PASSPHRASE: str = Field(default="")

    # ── External Platform APIs ───────────────────────────────────────────────
    METACULUS_BASE_URL: str = Field(default="https://www.metaculus.com/api2")
    MANIFOLD_BASE_URL: str = Field(default="https://api.manifold.markets/v0")
    MANIFOLD_API_KEY: str = Field(default="")
    KALSHI_BASE_URL: str = Field(
        default="https://trading-api.kalshi.com/trade-api/v2"
    )
    KALSHI_API_KEY: str = Field(default="")

    # Rate limiting for external platform polling (requests per second)
    EXTERNAL_PLATFORM_RPS: float = Field(default=1.0)
    # How often to poll external platforms for latency arb (seconds)
    LATENCY_ARB_POLL_INTERVAL_SECONDS: int = Field(default=30)

    # ── Signal Engine ────────────────────────────────────────────────────────
    # Minimum edge (in probability points) to generate a signal
    MIN_EDGE_THRESHOLD: float = Field(default=0.04)
    # Kelly fraction multiplier (0.25 = quarter-Kelly for safety)
    KELLY_FRACTION: float = Field(default=0.25)
    # Maximum position size in USD
    MAX_POSITION_USD: float = Field(default=100.0)
    # Require human confirmation for orders above this size
    HUMAN_CONFIRMATION_THRESHOLD_USD: float = Field(default=50.0)
    REQUIRE_HUMAN_CONFIRMATION: bool = Field(default=True)

    # Weights for fair value synthesis (must sum to 1.0 across enabled strategies)
    # These are relative weights — the engine normalises based on which are enabled
    WEIGHT_RESOLUTION_CRITERIA: float = Field(default=0.45)
    WEIGHT_BASE_RATES: float = Field(default=0.35)
    WEIGHT_LATENCY_ARB: float = Field(default=0.20)

    # ── Dashboard ────────────────────────────────────────────────────────────
    DASHBOARD_HOST: str = Field(default="0.0.0.0")
    DASHBOARD_PORT: int = Field(default=8000)
    DASHBOARD_SECRET_KEY: str = Field(default="change-me-in-production")

    # ── Ingestion ────────────────────────────────────────────────────────────
    # How many markets to track simultaneously
    MAX_TRACKED_MARKETS: int = Field(default=500)
    # Minimum daily volume (USD) for a market to be tracked
    MIN_MARKET_VOLUME_USD: float = Field(default=1000.0)
    # Order book snapshot interval (seconds)
    ORDER_BOOK_SNAPSHOT_INTERVAL: int = Field(default=10)

    # ── Logging ──────────────────────────────────────────────────────────────
    LOG_LEVEL: str = Field(default="INFO")
    LOG_FORMAT: str = Field(default="json")  # "json" or "console"

    def active_strategies(self) -> list[str]:
        """Return list of currently enabled strategy names."""
        active = []
        if self.ENABLE_RESOLUTION_CRITERIA:
            active.append("resolution_criteria")
        if self.ENABLE_BASE_RATES:
            active.append("base_rates")
        if self.ENABLE_LATENCY_ARB:
            active.append("latency_arb")
        return active

    def strategy_weights(self) -> dict[str, float]:
        """
        Return normalised weights for enabled strategies.
        Disabled strategies are excluded and weights are renormalised to sum to 1.0.
        """
        raw = {}
        if self.ENABLE_RESOLUTION_CRITERIA:
            raw["resolution_criteria"] = self.WEIGHT_RESOLUTION_CRITERIA
        if self.ENABLE_BASE_RATES:
            raw["base_rates"] = self.WEIGHT_BASE_RATES
        if self.ENABLE_LATENCY_ARB:
            raw["latency_arb"] = self.WEIGHT_LATENCY_ARB

        if not raw:
            return {}

        total = sum(raw.values())
        return {k: v / total for k, v in raw.items()}

    def validate_llm_config(self) -> None:
        """Raise if resolution criteria is enabled but API key is missing."""
        if self.ENABLE_RESOLUTION_CRITERIA and not self.ANTHROPIC_API_KEY:
            raise ValueError(
                "ENABLE_RESOLUTION_CRITERIA=True but ANTHROPIC_API_KEY is not set. "
                "Either set the API key or disable the strategy."
            )


settings = Settings()
