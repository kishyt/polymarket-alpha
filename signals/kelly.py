"""
signals/kelly.py

Kelly criterion position sizing for trade signals.

Applies fractional Kelly to avoid overbetting.
All monetary values are in USD.
"""

from config.settings import settings


class KellySizer:
    """
    Computes position size in USD using the fractional Kelly criterion.

    Full Kelly: f = (edge) / (price * (1 - price))
    Applied fraction: settings.KELLY_FRACTION (default 0.25 = quarter-Kelly)
    Hard cap: settings.MAX_POSITION_USD
    """

    def position_size(self, edge: float, price: float) -> float:
        """
        Return recommended position size in USD.

        Args:
            edge:  abs(fair_value - market_price) — probability edge
            price: market price of the side being bet (0 < price < 1)

        Returns:
            Position size in USD, capped at MAX_POSITION_USD.
        """
        if price <= 0 or price >= 1 or edge <= 0:
            return 0.0

        # Kelly fraction for a binary bet: f = edge / (price * (1 - price))
        # Scaled by bankroll proxy ($1) then capped
        kelly_fraction = edge / (price * (1.0 - price))
        fractional = kelly_fraction * settings.KELLY_FRACTION

        # Cap to max position size
        return min(fractional * settings.MAX_POSITION_USD, settings.MAX_POSITION_USD)
