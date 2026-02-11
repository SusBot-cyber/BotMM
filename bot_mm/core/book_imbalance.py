"""
Book Imbalance Tracker — measures order book pressure from L2 data.

Positive imbalance = buy pressure (more bid volume).
Negative imbalance = sell pressure (more ask volume).
"""

from typing import List, Tuple


class BookImbalanceTracker:
    """Tracks order book imbalance from L2 data with EMA smoothing."""

    def __init__(self, ema_alpha: float = 0.3):
        """
        Args:
            ema_alpha: EMA smoothing factor (0-1). Higher = more responsive.
        """
        self.ema_alpha = ema_alpha
        self._smoothed: float = 0.0
        self._initialized: bool = False

    def update(self, bids: List[Tuple[float, float]], asks: List[Tuple[float, float]], depth: int = 5) -> float:
        """
        Calculate imbalance from top N levels of order book.

        Args:
            bids: [(price, size), ...] sorted desc by price
            asks: [(price, size), ...] sorted asc by price
            depth: how many levels to consider

        Returns:
            float: -1 to +1 (positive = buy pressure)
        """
        bid_vol = sum(size for _, size in bids[:depth])
        ask_vol = sum(size for _, size in asks[:depth])

        total = bid_vol + ask_vol
        if total == 0:
            raw = 0.0
        else:
            raw = (bid_vol - ask_vol) / total

        # EMA smoothing
        if not self._initialized:
            self._smoothed = raw
            self._initialized = True
        else:
            self._smoothed = self.ema_alpha * raw + (1 - self.ema_alpha) * self._smoothed

        return self._smoothed

    @property
    def imbalance(self) -> float:
        """Current smoothed imbalance value."""
        return self._smoothed

    def reset(self):
        """Reset tracker state."""
        self._smoothed = 0.0
        self._initialized = False
