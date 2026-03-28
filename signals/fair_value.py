"""
signals/fair_value.py

Weighted synthesis of strategy contributions into a single fair value estimate.

Each enabled strategy produces a fair_value_delta — a signed probability
adjustment relative to the current market price. This module combines them
using normalised weights from settings.strategy_weights().
"""

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from signals.engine import StrategyContribution


class FairValueModel:
    """
    Synthesises multiple strategy contributions into a single probability estimate.

    Method: weighted average of (market_price + delta) across contributing strategies.
    Strategies that did not run (ran=False) are excluded from the weighted average.
    Returns None if no strategy produced a usable delta.
    """

    def synthesise(
        self,
        market_price: float,
        contributions: "dict[str, StrategyContribution]",
        weights: dict[str, float],
    ) -> Optional[float]:
        """
        Args:
            market_price:   Current Polymarket mid price for the YES token.
            contributions:  Map of strategy_name → StrategyContribution.
            weights:        Normalised weights from settings.strategy_weights().

        Returns:
            Probability estimate in [0, 1], or None if no strategies contributed.
        """
        weighted_sum = 0.0
        total_weight = 0.0

        for name, contrib in contributions.items():
            if not contrib.enabled or not contrib.ran:
                continue
            if contrib.fair_value_delta is None:
                continue

            w = weights.get(name, 0.0)
            if w <= 0:
                continue

            implied_price = market_price + contrib.fair_value_delta
            # Clamp to valid probability range
            implied_price = max(0.01, min(0.99, implied_price))

            weighted_sum += implied_price * w
            total_weight += w

        if total_weight == 0:
            return None

        fair_value = weighted_sum / total_weight
        return max(0.01, min(0.99, fair_value))
