"""
Quote Engine — Avellaneda-Stoikov based market making quotes.

Calculates optimal bid/ask prices based on:
- Current mid price
- Volatility (ATR-based)
- Inventory position (skew)
- Order book imbalance
"""

import math
from dataclasses import dataclass
from typing import List, Tuple

from bot_mm.config import QuoteParams


@dataclass
class Quote:
    """A single quote (one side)."""
    price: float
    size: float
    side: str  # "buy" or "sell"
    level: int = 0


class QuoteEngine:
    """Calculates MM quotes using simplified Avellaneda-Stoikov model."""

    def __init__(self, params: QuoteParams):
        self.params = params

    def calculate_quotes(
        self,
        mid_price: float,
        volatility_pct: float,
        inventory_usd: float,
        max_position_usd: float,
        book_imbalance: float = 0.0,
        directional_bias: float = 0.0,
        maker_fee: float = 0.0,
        skip_buy: bool = False,
        skip_sell: bool = False,
    ) -> List[Quote]:
        """
        Calculate bid and ask quotes.

        Args:
            mid_price: Current mid price
            volatility_pct: ATR as % of price (e.g., 0.005 = 0.5%)
            inventory_usd: Current inventory in USD (+ = long)
            max_position_usd: Maximum allowed position
            book_imbalance: -1 to +1 (positive = buy pressure)
            directional_bias: -1 to +1 from DirectionalBias (positive = bullish)
            maker_fee: Maker fee as fraction (e.g. 0.00015). Used for profitability gate.
            skip_buy: If True, don't generate buy quotes (one-sided mode)
            skip_sell: If True, don't generate sell quotes (one-sided mode)

        Returns:
            List of Quote objects (bids + asks)
        """
        # Directional bias shifts the effective mid price
        bias_shift = directional_bias * volatility_pct * 0.5
        effective_mid = mid_price * (1 + bias_shift)

        spread_bps = self._calc_spread(volatility_pct, inventory_usd, max_position_usd)
        skew_bps = self._calc_skew(inventory_usd, max_position_usd, volatility_pct)

        # Profitability gate: half-spread per side must exceed maker fee
        fee_bps = abs(maker_fee) * 10000.0
        if fee_bps > 0:
            min_profitable_spread = fee_bps * 2.0  # round-trip cost
            spread_bps = max(spread_bps, min_profitable_spread)

        # Add book imbalance effect (widen on heavy-flow side)
        imbalance_adj = book_imbalance * 0.3 * spread_bps

        spread_pct = spread_bps / 10000.0
        skew_pct = skew_bps / 10000.0
        imb_pct = imbalance_adj / 10000.0

        quotes = []
        for level in range(self.params.num_levels):
            level_offset = level * self.params.level_spacing_bps / 10000.0

            bid_price = effective_mid * (1 - spread_pct / 2 - skew_pct - level_offset + imb_pct)
            ask_price = effective_mid * (1 + spread_pct / 2 - skew_pct + level_offset + imb_pct)

            # Size decreases with level — dynamic weights for 1-5 levels
            weight = self._level_weight(level)
            size_usd = self.params.order_size_usd * weight

            size = size_usd / mid_price

            if not skip_buy:
                quotes.append(Quote(price=bid_price, size=size, side="buy", level=level))
            if not skip_sell:
                quotes.append(Quote(price=ask_price, size=size, side="sell", level=level))

        return quotes

    # Base weights for up to 5 levels (50%, 30%, 15%, 5%, ...)
    _BASE_WEIGHTS = [0.50, 0.30, 0.15, 0.05]

    def _level_weight(self, level: int) -> float:
        """Return normalized weight for a given level index."""
        n = self.params.num_levels
        # Build raw weights for N levels
        raw = []
        for i in range(n):
            if i < len(self._BASE_WEIGHTS):
                raw.append(self._BASE_WEIGHTS[i])
            else:
                raw.append(self._BASE_WEIGHTS[-1])
        total = sum(raw)
        return raw[level] / total if total > 0 else 1.0 / n

    def _calc_spread(
        self, volatility_pct: float, inventory_usd: float, max_position_usd: float
    ) -> float:
        """Calculate spread in basis points."""
        base = self.params.base_spread_bps

        # Volatility component
        vol_component = volatility_pct * 10000 * self.params.vol_multiplier

        # Inventory penalty (wider spread when loaded)
        inv_ratio = abs(inventory_usd) / max(max_position_usd, 1.0)
        inv_penalty = inv_ratio * 2.0  # Up to 2 bps penalty at max

        spread = base + vol_component + inv_penalty

        return max(self.params.min_spread_bps, min(spread, self.params.max_spread_bps))

    def _calc_skew(
        self, inventory_usd: float, max_position_usd: float, volatility_pct: float
    ) -> float:
        """Calculate inventory skew in basis points."""
        if max_position_usd == 0:
            return 0.0

        inv_ratio = inventory_usd / max_position_usd  # -1 to +1
        skew = inv_ratio * self.params.inventory_skew_factor * volatility_pct * 10000

        return skew
