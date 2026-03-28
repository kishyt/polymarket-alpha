"""
ingestion/platform_clients.py

Clients for external prediction market platforms used in cross-platform latency arb.

Only active when settings.ENABLE_LATENCY_ARB = True.

Platforms:
- Metaculus: community forecasting, free public API
- Manifold: play money + real money markets, public API
- Kalshi: US regulated exchange, requires auth

The latency arb strategy works by:
1. Finding matching or highly similar markets across platforms
2. Comparing prices after normalisation
3. Flagging divergences above threshold as signals
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class ExternalMarketPrice:
    platform: str
    external_id: str
    question: str
    yes_price: float
    no_price: float
    volume: Optional[float]
    fetched_at: float


@dataclass
class PriceDiscrepancy:
    polymarket_condition_id: str
    polymarket_price: float
    external_platform: str
    external_price: float
    discrepancy: float              # external - polymarket (signed)
    abs_discrepancy: float
    signal_direction: str           # "buy_polymarket", "sell_polymarket", "neutral"


class RateLimiter:
    """Simple token bucket rate limiter."""
    def __init__(self, rps: float):
        self._interval = 1.0 / rps
        self._last_call = 0.0

    async def wait(self):
        now = time.monotonic()
        elapsed = now - self._last_call
        if elapsed < self._interval:
            await asyncio.sleep(self._interval - elapsed)
        self._last_call = time.monotonic()


class MetaculusClient:
    """Fetches question probabilities from the Metaculus public API."""

    def __init__(self, http: httpx.AsyncClient, limiter: RateLimiter):
        self._http = http
        self._limiter = limiter

    async def fetch_questions(self, limit: int = 100) -> list[ExternalMarketPrice]:
        """Fetch open binary questions with community forecasts."""
        await self._limiter.wait()
        try:
            resp = await self._http.get(
                f"{settings.METACULUS_BASE_URL}/questions/",
                params={
                    "type": "forecast",
                    "status": "open",
                    "limit": limit,
                    "order_by": "-activity",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("Metaculus fetch failed", error=str(e))
            return []

        prices = []
        for q in data.get("results", []):
            resolution_criteria = q.get("resolution_criteria", "")
            # Only use binary (yes/no) questions with community predictions
            cp = q.get("community_prediction", {})
            if not cp or q.get("possibilities", {}).get("type") != "binary":
                continue

            yes_price = float(cp.get("q2", 0.5) or 0.5)
            prices.append(ExternalMarketPrice(
                platform="metaculus",
                external_id=str(q["id"]),
                question=q.get("title", ""),
                yes_price=yes_price,
                no_price=1 - yes_price,
                volume=None,  # Metaculus doesn't expose trading volume
                fetched_at=time.time(),
            ))
        return prices


class ManifoldClient:
    """Fetches market prices from the Manifold Markets API."""

    def __init__(self, http: httpx.AsyncClient, limiter: RateLimiter):
        self._http = http
        self._limiter = limiter

    async def fetch_markets(self, limit: int = 100) -> list[ExternalMarketPrice]:
        """Fetch open binary markets sorted by recent activity."""
        await self._limiter.wait()
        try:
            resp = await self._http.get(
                f"{settings.MANIFOLD_BASE_URL}/markets",
                params={"limit": limit, "sort": "last-updated", "filter": "open"},
            )
            resp.raise_for_status()
            markets = resp.json()
        except Exception as e:
            logger.error("Manifold fetch failed", error=str(e))
            return []

        prices = []
        for m in markets:
            if m.get("outcomeType") != "BINARY":
                continue
            yes_price = float(m.get("probability", 0.5))
            prices.append(ExternalMarketPrice(
                platform="manifold",
                external_id=m["id"],
                question=m.get("question", ""),
                yes_price=yes_price,
                no_price=1 - yes_price,
                volume=float(m.get("volume", 0)),
                fetched_at=time.time(),
            ))
        return prices


class KalshiClient:
    """Fetches market prices from the Kalshi regulated exchange."""

    def __init__(self, http: httpx.AsyncClient, limiter: RateLimiter):
        self._http = http
        self._limiter = limiter
        self._auth_headers = (
            {"Authorization": f"Bearer {settings.KALSHI_API_KEY}"}
            if settings.KALSHI_API_KEY
            else {}
        )

    async def fetch_markets(self, limit: int = 100) -> list[ExternalMarketPrice]:
        """Fetch active markets from Kalshi."""
        if not settings.KALSHI_API_KEY:
            logger.debug("Kalshi API key not set, skipping")
            return []

        await self._limiter.wait()
        try:
            resp = await self._http.get(
                f"{settings.KALSHI_BASE_URL}/markets",
                params={"limit": limit, "status": "open"},
                headers=self._auth_headers,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("Kalshi fetch failed", error=str(e))
            return []

        prices = []
        for m in data.get("markets", []):
            # Kalshi prices are in cents (0-100)
            yes_price = float(m.get("yes_bid", 50)) / 100
            prices.append(ExternalMarketPrice(
                platform="kalshi",
                external_id=m.get("ticker", ""),
                question=m.get("title", ""),
                yes_price=yes_price,
                no_price=1 - yes_price,
                volume=float(m.get("volume", 0)),
                fetched_at=time.time(),
            ))
        return prices


# ── Latency arb engine ────────────────────────────────────────────────────────

class LatencyArbEngine:
    """
    Polls external platforms and matches markets against Polymarket prices.
    Uses fuzzy string matching to correlate markets across platforms.
    """

    MIN_DISCREPANCY = 0.04          # 4 percentage points minimum to flag
    MIN_VOLUME_FILTER = 500.0       # ignore tiny Manifold markets

    def __init__(self):
        self._http = httpx.AsyncClient(timeout=20.0)
        self._limiter = RateLimiter(settings.EXTERNAL_PLATFORM_RPS)
        self._metaculus = MetaculusClient(self._http, self._limiter)
        self._manifold = ManifoldClient(self._http, self._limiter)
        self._kalshi = KalshiClient(self._http, self._limiter)

        # Cache of latest external prices: platform -> [ExternalMarketPrice]
        self._latest_prices: dict[str, list[ExternalMarketPrice]] = {}

    async def refresh_all(self) -> dict[str, list[ExternalMarketPrice]]:
        """
        Fetch fresh prices from all external platforms.
        Returns None if strategy is disabled.
        """
        if not settings.ENABLE_LATENCY_ARB:
            logger.debug("Latency arb strategy disabled")
            return {}

        logger.info("Refreshing external platform prices")

        results = await asyncio.gather(
            self._metaculus.fetch_questions(),
            self._manifold.fetch_markets(),
            self._kalshi.fetch_markets(),
            return_exceptions=True,
        )

        for platform, result in zip(["metaculus", "manifold", "kalshi"], results):
            if isinstance(result, Exception):
                logger.error(f"{platform} refresh failed", error=str(result))
            else:
                self._latest_prices[platform] = result
                logger.info(f"{platform} prices refreshed", count=len(result))

        return self._latest_prices

    def find_discrepancies(
        self,
        polymarket_question: str,
        polymarket_condition_id: str,
        polymarket_price: float,
    ) -> list[PriceDiscrepancy]:
        """
        Find significant price discrepancies between Polymarket and external platforms.
        Uses simple keyword overlap for market matching — replace with embeddings for production.
        """
        if not settings.ENABLE_LATENCY_ARB:
            return []

        discrepancies = []
        poly_words = set(polymarket_question.lower().split())

        for platform, prices in self._latest_prices.items():
            for ext_price in prices:
                # Volume filter for Manifold
                if platform == "manifold" and (ext_price.volume or 0) < self.MIN_VOLUME_FILTER:
                    continue

                ext_words = set(ext_price.question.lower().split())
                overlap = len(poly_words & ext_words) / max(len(poly_words | ext_words), 1)

                # Require >40% word overlap to consider a match
                if overlap < 0.40:
                    continue

                discrepancy = ext_price.yes_price - polymarket_price
                abs_disc = abs(discrepancy)

                if abs_disc < self.MIN_DISCREPANCY:
                    continue

                signal_direction = (
                    "buy_polymarket" if discrepancy > 0     # external higher → Poly underpriced
                    else "sell_polymarket"                   # external lower → Poly overpriced
                )

                discrepancies.append(PriceDiscrepancy(
                    polymarket_condition_id=polymarket_condition_id,
                    polymarket_price=polymarket_price,
                    external_platform=platform,
                    external_price=ext_price.yes_price,
                    discrepancy=discrepancy,
                    abs_discrepancy=abs_disc,
                    signal_direction=signal_direction,
                ))

        return sorted(discrepancies, key=lambda d: d.abs_discrepancy, reverse=True)

    def fair_value_delta(self, discrepancies: list[PriceDiscrepancy]) -> float:
        """
        Aggregate discrepancies into a single fair value delta.
        Weights platforms by reliability (Kalshi > Metaculus > Manifold).
        """
        if not discrepancies:
            return 0.0

        platform_weights = {"kalshi": 1.0, "metaculus": 0.7, "manifold": 0.4}

        weighted_sum = 0.0
        weight_total = 0.0

        for d in discrepancies:
            w = platform_weights.get(d.external_platform, 0.5)
            # discrepancy > 0 means external > poly → poly is underpriced → positive delta
            weighted_sum += d.discrepancy * w
            weight_total += w

        if weight_total == 0:
            return 0.0

        delta = weighted_sum / weight_total
        return max(-0.15, min(0.15, delta))

    async def close(self):
        await self._http.aclose()
