"""
signals/engine.py

The signal engine orchestrates all enabled strategies and synthesises
their outputs into a single ranked list of trade opportunities.

Architecture:
- Each strategy is checked via its feature flag before running
- Disabled strategies contribute None to the signal (not 0)
- Fair value is computed only from enabled strategy inputs
- Kelly sizing is applied to generate position recommendations

Run this as a background task:
    engine = SignalEngine()
    await engine.run()  # loops forever
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from config.settings import settings
from analysis.resolution_parser import ResolutionParser, ResolutionAnalysis
from analysis.base_rates import BaseRateEngine, BaseRateLookup
from ingestion.clob_client import CLOBClient, Market, OrderBookSnapshot
from ingestion.platform_clients import LatencyArbEngine, PriceDiscrepancy
from signals.kelly import KellySizer
from signals.fair_value import FairValueModel

logger = logging.getLogger(__name__)


@dataclass
class StrategyContribution:
    """Output from a single strategy for a single market."""
    strategy_name: str
    enabled: bool
    ran: bool                                   # False if skipped despite being enabled
    fair_value_delta: Optional[float]           # None if disabled or skipped
    raw_output: Optional[object]                # The strategy's native output object
    error: Optional[str] = None


@dataclass
class Signal:
    """
    A fully synthesised trade signal for a single Polymarket market.
    """
    market_id: str
    question: str
    generated_at: float

    # Current market state
    market_price: float                         # Current mid price (YES token)
    best_bid: Optional[float]
    best_ask: Optional[float]

    # Strategy contributions (None = strategy disabled)
    resolution_criteria: StrategyContribution
    base_rates: StrategyContribution
    latency_arb: StrategyContribution

    # Synthesised output
    fair_value: Optional[float]                 # Our probability estimate
    edge: Optional[float]                       # fair_value - market_price
    edge_significant: bool
    recommended_side: Optional[str]             # "YES" or "NO"
    kelly_position_usd: Optional[float]
    active_strategies: list[str]               # Which strategies contributed

    # Human-readable summary
    summary: str = ""
    priority_score: float = 0.0                # For ranking (higher = more urgent)


class SignalEngine:
    """
    Orchestrates all strategies and produces ranked signals.
    """

    SIGNIFICANT_EDGE_THRESHOLD = settings.MIN_EDGE_THRESHOLD

    def __init__(self):
        self._resolution_parser = ResolutionParser()
        self._base_rate_engine = BaseRateEngine()
        self._latency_arb_engine = LatencyArbEngine()
        self._kelly = KellySizer()
        self._fair_value_model = FairValueModel()
        self._clob = CLOBClient()
        self._signals: list[Signal] = []

    async def run(self):
        """Main loop — initialise markets, then run continuous signal updates."""
        logger.info(
            "Signal engine starting",
            active_strategies=settings.active_strategies(),
        )

        # Start external platform poller as background task
        if settings.ENABLE_LATENCY_ARB:
            asyncio.create_task(self._poll_external_platforms())

        # Fetch initial market list
        markets = await self._clob.initialise_markets()
        logger.info("Markets loaded", count=len(markets))

        # Initial signal run
        order_books = await self._clob.snapshot_all_order_books()
        book_by_token = {ob.token_id: ob for ob in order_books}

        self._signals = await self._process_all_markets(markets, book_by_token)
        logger.info("Initial signals generated", count=len(self._signals))

        # Continuous update loop
        while True:
            await asyncio.sleep(settings.ORDER_BOOK_SNAPSHOT_INTERVAL)
            order_books = await self._clob.snapshot_all_order_books()
            book_by_token = {ob.token_id: ob for ob in order_books}
            self._signals = await self._process_all_markets(markets, book_by_token)

    async def _poll_external_platforms(self):
        """Background task: refresh external platform prices periodically."""
        while True:
            try:
                await self._latency_arb_engine.refresh_all()
            except Exception as e:
                logger.error("External platform poll failed", error=str(e))
            await asyncio.sleep(settings.LATENCY_ARB_POLL_INTERVAL_SECONDS)

    async def _process_all_markets(
        self,
        markets: list[Market],
        book_by_token: dict[str, OrderBookSnapshot],
    ) -> list[Signal]:
        """Process all markets and return sorted signals."""
        tasks = [self._process_market(m, book_by_token) for m in markets]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        signals = []
        for r in results:
            if isinstance(r, Exception):
                logger.warning("Market processing failed", error=str(r))
            elif r is not None:
                signals.append(r)

        # Sort by priority score (edge * confidence proxy)
        signals.sort(key=lambda s: s.priority_score, reverse=True)
        return signals

    async def _process_market(
        self,
        market: Market,
        book_by_token: dict[str, OrderBookSnapshot],
    ) -> Optional[Signal]:
        """Run all enabled strategies for a single market and synthesise a signal."""
        # Get the YES token order book
        yes_token = next((t for t in market.tokens if t.get("outcome") == "Yes"), None)
        if not yes_token:
            return None

        ob = book_by_token.get(yes_token["token_id"])
        if not ob or ob.mid_price is None:
            return None

        market_price = ob.mid_price

        # ── Run each strategy in isolation ───────────────────────────────────

        resolution_contrib = await self._run_resolution_criteria(market, market_price)
        base_rate_contrib = self._run_base_rates(market, market_price)
        latency_contrib = self._run_latency_arb(market, market_price)

        contributions = [resolution_contrib, base_rate_contrib, latency_contrib]
        active_strategies = [
            c.strategy_name for c in contributions if c.enabled and c.ran
        ]

        # ── Synthesise fair value ─────────────────────────────────────────────
        weights = settings.strategy_weights()
        fair_value = self._fair_value_model.synthesise(
            market_price=market_price,
            contributions={c.strategy_name: c for c in contributions},
            weights=weights,
        )

        edge = (fair_value - market_price) if fair_value is not None else None
        edge_significant = edge is not None and abs(edge) >= self.SIGNIFICANT_EDGE_THRESHOLD

        # ── Kelly sizing ──────────────────────────────────────────────────────
        kelly_usd = None
        recommended_side = None
        if edge_significant and edge is not None:
            recommended_side = "YES" if edge > 0 else "NO"
            price_for_kelly = market_price if edge > 0 else (1 - market_price)
            kelly_usd = self._kelly.position_size(
                edge=abs(edge),
                price=price_for_kelly,
            )

        # ── Priority score ────────────────────────────────────────────────────
        priority = 0.0
        if edge_significant and edge is not None:
            strategy_count_bonus = len(active_strategies) / 3  # more strategies = more conviction
            priority = abs(edge) * strategy_count_bonus * 100

        signal = Signal(
            market_id=market.condition_id,
            question=market.question,
            generated_at=time.time(),
            market_price=market_price,
            best_bid=ob.best_bid,
            best_ask=ob.best_ask,
            resolution_criteria=resolution_contrib,
            base_rates=base_rate_contrib,
            latency_arb=latency_contrib,
            fair_value=fair_value,
            edge=edge,
            edge_significant=edge_significant,
            recommended_side=recommended_side,
            kelly_position_usd=kelly_usd,
            active_strategies=active_strategies,
            summary=self._build_summary(market, fair_value, edge, active_strategies),
            priority_score=priority,
        )

        if edge_significant:
            logger.info(
                "Signal generated",
                market_id=market.condition_id,
                question=market.question[:80],
                market_price=f"{market_price:.3f}",
                fair_value=f"{fair_value:.3f}" if fair_value else "N/A",
                edge=f"{edge:+.3f}" if edge else "N/A",
                side=recommended_side,
                kelly_usd=f"${kelly_usd:.2f}" if kelly_usd else "N/A",
                strategies=active_strategies,
            )

        return signal

    async def _run_resolution_criteria(
        self, market: Market, market_price: float
    ) -> StrategyContribution:
        """Run the resolution criteria strategy."""
        if not settings.ENABLE_RESOLUTION_CRITERIA:
            return StrategyContribution(
                strategy_name="resolution_criteria",
                enabled=False,
                ran=False,
                fair_value_delta=None,
                raw_output=None,
            )

        try:
            analysis = await self._resolution_parser.analyse(
                market_id=market.condition_id,
                question=market.question,
                description=market.description,
                resolution_criteria=market.resolution_criteria,
            )
            delta = self._resolution_parser.fair_value_delta(analysis) if analysis else None
            return StrategyContribution(
                strategy_name="resolution_criteria",
                enabled=True,
                ran=analysis is not None,
                fair_value_delta=delta,
                raw_output=analysis,
            )
        except Exception as e:
            return StrategyContribution(
                strategy_name="resolution_criteria",
                enabled=True,
                ran=False,
                fair_value_delta=None,
                raw_output=None,
                error=str(e),
            )

    def _run_base_rates(self, market: Market, market_price: float) -> StrategyContribution:
        """Run the base rate strategy (synchronous — pure DB lookups)."""
        if not settings.ENABLE_BASE_RATES:
            return StrategyContribution(
                strategy_name="base_rates",
                enabled=False,
                ran=False,
                fair_value_delta=None,
                raw_output=None,
            )

        try:
            lookup = self._base_rate_engine.lookup(
                question=market.question,
                market_price=market_price,
                description=market.description,
            )
            delta = self._base_rate_engine.fair_value_delta(lookup) if lookup else None
            return StrategyContribution(
                strategy_name="base_rates",
                enabled=True,
                ran=lookup is not None,
                fair_value_delta=delta,
                raw_output=lookup,
            )
        except Exception as e:
            return StrategyContribution(
                strategy_name="base_rates",
                enabled=True,
                ran=False,
                fair_value_delta=None,
                raw_output=None,
                error=str(e),
            )

    def _run_latency_arb(self, market: Market, market_price: float) -> StrategyContribution:
        """Run the latency arb strategy (uses cached external prices)."""
        if not settings.ENABLE_LATENCY_ARB:
            return StrategyContribution(
                strategy_name="latency_arb",
                enabled=False,
                ran=False,
                fair_value_delta=None,
                raw_output=None,
            )

        try:
            discrepancies = self._latency_arb_engine.find_discrepancies(
                polymarket_question=market.question,
                polymarket_condition_id=market.condition_id,
                polymarket_price=market_price,
            )
            delta = self._latency_arb_engine.fair_value_delta(discrepancies)
            return StrategyContribution(
                strategy_name="latency_arb",
                enabled=True,
                ran=len(discrepancies) > 0,
                fair_value_delta=delta if discrepancies else None,
                raw_output=discrepancies,
            )
        except Exception as e:
            return StrategyContribution(
                strategy_name="latency_arb",
                enabled=True,
                ran=False,
                fair_value_delta=None,
                raw_output=None,
                error=str(e),
            )

    def _build_summary(
        self,
        market: Market,
        fair_value: Optional[float],
        edge: Optional[float],
        active_strategies: list[str],
    ) -> str:
        if not edge or abs(edge) < self.SIGNIFICANT_EDGE_THRESHOLD:
            return "No significant edge detected."

        side = "YES" if edge > 0 else "NO"
        strategy_str = " + ".join(active_strategies) or "no strategies"
        return (
            f"{side} edge of {abs(edge):.1%} detected. "
            f"Fair value: {fair_value:.1%} vs market: {fair_value - edge:.1%}. "  # type: ignore
            f"Driven by: {strategy_str}."
        )

    def get_signals(self, only_significant: bool = True) -> list[Signal]:
        """Return current signals, optionally filtered to significant ones only."""
        if only_significant:
            return [s for s in self._signals if s.edge_significant]
        return self._signals
