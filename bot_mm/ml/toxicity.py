from dataclasses import dataclass, field
from collections import deque
from typing import Optional, List
import math


@dataclass
class FillRecord:
    """Record of a fill for toxicity analysis."""
    timestamp: str
    side: str          # "buy" or "sell"
    fill_price: float
    mid_at_fill: float
    size: float
    # Filled after N bars:
    mid_after_1: Optional[float] = None
    mid_after_5: Optional[float] = None
    mid_after_10: Optional[float] = None
    toxicity_score: float = 0.0  # Computed post-fill


class ToxicityDetector:
    """
    Real-time toxicity measurement and spread adjustment.
    
    Measures toxicity by tracking how price moves AFTER fills:
    - If we bought and price drops -> toxic (informed seller hit us)
    - If we sold and price rises -> toxic (informed buyer hit us)
    
    Metrics:
    1. VPIN (Volume-synchronized Probability of Informed Trading):
       Ratio of volume that moves price vs total volume
       
    2. Fill toxicity score: 
       After each fill, measure price movement at +1, +5, +10 bars
       toxicity = adverse_move / ATR (normalized)
       
    3. Rolling toxicity average:
       EMA of recent fill toxicity scores
       
    Adaptation:
    - toxicity > 0.6 -> widen spread by 50%
    - toxicity > 0.4 -> widen spread by 25%
    - toxicity < 0.2 -> tighten spread by 10%
    - Separate tracking for buy-side and sell-side toxicity
    """
    
    def __init__(
        self,
        lookback_fills: int = 50,       # Rolling window of fills
        measurement_bars: int = 5,       # Bars after fill to measure move
        ema_alpha: float = 0.1,          # EMA smoothing for toxicity
        high_toxicity: float = 0.6,      # Threshold for high
        medium_toxicity: float = 0.4,    # Threshold for medium
        low_toxicity: float = 0.2,       # Below = low (tighten OK)
    ):
        self._lookback = lookback_fills
        self._measurement_bars = measurement_bars
        self._ema_alpha = ema_alpha
        self._high = high_toxicity
        self._medium = medium_toxicity
        self._low = low_toxicity
        
        # Fill records awaiting measurement
        self._pending_fills: List[FillRecord] = []
        
        # Completed fill records
        self._completed_fills: deque = deque(maxlen=lookback_fills)
        
        # Running toxicity scores (EMA)
        self._buy_toxicity: float = 0.3   # Start neutral
        self._sell_toxicity: float = 0.3
        self._overall_toxicity: float = 0.3
        
        # Bar counter for measurement
        self._bar_count: int = 0
        self._fills_measured: int = 0
        
    def on_fill(self, side: str, fill_price: float, mid_price: float, 
                size: float, timestamp: str = ""):
        """Record a new fill for future toxicity measurement."""
        record = FillRecord(
            timestamp=timestamp,
            side=side,
            fill_price=fill_price,
            mid_at_fill=mid_price,
            size=size,
        )
        record._fill_bar = self._bar_count
        self._pending_fills.append(record)
    
    def on_bar(self, mid_price: float, atr: float = 0.0):
        """
        Called on each new bar (candle close).
        Measures price movement for pending fills.
        """
        self._bar_count += 1
        
        still_pending = []
        for fill in self._pending_fills:
            bars_since = self._bar_count - fill._fill_bar
            
            if bars_since == 1:
                fill.mid_after_1 = mid_price
            elif bars_since == 5:
                fill.mid_after_5 = mid_price
            elif bars_since >= self._measurement_bars:
                # Fill is now fully measured
                fill.mid_after_10 = mid_price  # using measurement_bars
                fill.toxicity_score = self._compute_toxicity(fill, atr)
                self._completed_fills.append(fill)
                self._update_ema(fill)
                self._fills_measured += 1
                continue  # Don't re-add to pending
            
            still_pending.append(fill)
        
        self._pending_fills = still_pending
    
    def _compute_toxicity(self, fill: FillRecord, atr: float) -> float:
        """
        Compute toxicity score for a completed fill.
        
        Toxicity = how much price moved AGAINST us after fill, normalized by ATR.
        
        Buy fill: adverse if price dropped (mid_after < mid_at_fill)
        Sell fill: adverse if price rose (mid_after > mid_at_fill)
        
        Returns: 0-1 (0 = benign, 1 = very toxic)
        """
        if fill.mid_after_10 is None:
            return 0.0
            
        if fill.side == "buy":
            # We bought — adverse if price fell
            move = fill.mid_at_fill - fill.mid_after_10
        else:
            # We sold — adverse if price rose
            move = fill.mid_after_10 - fill.mid_at_fill
        
        # Normalize by ATR (or by mid price if no ATR)
        normalizer = atr if atr > 0 else fill.mid_at_fill * 0.001
        normalized_move = move / normalizer
        
        # Clamp to [0, 1] — only care about adverse moves
        return max(0.0, min(1.0, normalized_move))
    
    def _update_ema(self, fill: FillRecord):
        """Update rolling EMA toxicity scores."""
        a = self._ema_alpha
        score = fill.toxicity_score
        
        if fill.side == "buy":
            self._buy_toxicity = a * score + (1 - a) * self._buy_toxicity
        else:
            self._sell_toxicity = a * score + (1 - a) * self._sell_toxicity
        
        self._overall_toxicity = (self._buy_toxicity + self._sell_toxicity) / 2
    
    @property
    def overall_toxicity(self) -> float:
        return self._overall_toxicity
    
    @property
    def buy_toxicity(self) -> float:
        return self._buy_toxicity
    
    @property
    def sell_toxicity(self) -> float:
        return self._sell_toxicity
    
    @property
    def fills_measured(self) -> int:
        return self._fills_measured
    
    def get_spread_multiplier(self, side: str = "both") -> float:
        """
        Get spread multiplier based on current toxicity.
        
        Returns:
            float: multiplier (1.0 = no change, >1.0 = widen, <1.0 = tighten)
        """
        if side == "buy":
            tox = self._buy_toxicity
        elif side == "sell":
            tox = self._sell_toxicity
        else:
            tox = self._overall_toxicity
        
        if tox > 0.8:
            return 0.0   # Cancel quotes — extreme adverse selection
        elif tox > self._high:
            return 1.5   # Widen 50%
        elif tox > self._medium:
            return 1.25  # Widen 25%
        elif tox < self._low:
            return 0.9   # Tighten 10%
        else:
            return 1.0   # Neutral
    
    def get_side_multipliers(self) -> tuple:
        """Returns (buy_mult, sell_mult) for asymmetric adjustment."""
        return (
            self.get_spread_multiplier("buy"),
            self.get_spread_multiplier("sell"),
        )
    
    def summary(self) -> dict:
        """Return summary stats."""
        completed = list(self._completed_fills)
        return {
            "overall_toxicity": self._overall_toxicity,
            "buy_toxicity": self._buy_toxicity,
            "sell_toxicity": self._sell_toxicity,
            "fills_measured": self._fills_measured,
            "pending_fills": len(self._pending_fills),
            "avg_toxicity": (
                sum(f.toxicity_score for f in completed) / len(completed)
                if completed else 0.0
            ),
            "toxic_fills_pct": (
                sum(1 for f in completed if f.toxicity_score > 0.5) / len(completed) * 100
                if completed else 0.0
            ),
            "avg_spread_multiplier": (
                self.get_spread_multiplier("buy") + self.get_spread_multiplier("sell")
            ) / 2,
            "spread_mult_buy": self.get_spread_multiplier("buy"),
            "spread_mult_sell": self.get_spread_multiplier("sell"),
        }
