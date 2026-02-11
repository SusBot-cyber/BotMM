"""
Dynamic order size scaler based on market conditions.

Adjusts order_size_usd based on volatility regime, fill rate, inventory,
toxicity, drawdown, and winning/losing streaks. Kelly-criterion inspired.
"""


class DynamicSizer:
    """
    Calculates optimal order size based on real-time market conditions.

    Unlike auto_tuner (which adjusts spread/skew/size via rules every 4h),
    DynamicSizer recomputes size on EVERY quote cycle based on current state.
    """

    def __init__(
        self,
        base_size_usd: float = 150.0,
        capital_usd: float = 1000.0,
        max_size_pct: float = 0.15,
        min_size_usd: float = 20.0,
        max_size_usd: float = 5000.0,
        vol_scale: bool = True,
        kelly_fraction: float = 0.25,
    ):
        self.base_size_usd = base_size_usd
        self.capital_usd = capital_usd
        self.max_size_pct = max_size_pct
        self.min_size_usd = min_size_usd
        self.max_size_usd = max_size_usd
        self.vol_scale = vol_scale
        self.kelly_fraction = kelly_fraction

        # Internal state
        self._recent_pnls: list = []
        self._win_streak: int = 0
        self._lose_streak: int = 0

    def compute_size(
        self,
        current_vol: float,
        avg_vol: float,
        fill_rate: float,
        inventory_pct: float,
        toxicity_score: float = 0.0,
        drawdown_pct: float = 0.0,
        equity: float = 0.0,
    ) -> float:
        """
        Compute optimal order size given current market conditions.
        Returns size in USD.
        """
        size = self.base_size_usd

        # 1. Capital-proportional base (if equity provided)
        if equity > 0:
            size = equity * self.max_size_pct

        # 2. Volatility scaling: low vol -> bigger, high vol -> smaller
        if self.vol_scale and avg_vol > 0:
            vol_ratio = current_vol / avg_vol
            vol_ratio = max(0.5, min(2.0, vol_ratio))
            vol_factor = 1.0 / vol_ratio
            vol_factor = 0.4 + 0.6 * vol_factor  # scale to [0.4, 1.6] range
            size *= vol_factor

        # 3. Fill rate factor
        if fill_rate > 0.85:
            size *= 1.10
        elif fill_rate < 0.15:
            size *= 0.85

        # 4. Inventory penalty: reduce size when heavily loaded
        inv_abs = abs(inventory_pct)
        if inv_abs > 0.7:
            size *= 0.70
        elif inv_abs > 0.5:
            size *= 0.85

        # 5. Toxicity penalty
        if toxicity_score > 0.6:
            size *= 0.70
        elif toxicity_score > 0.4:
            size *= 0.85
        elif toxicity_score < 0.2:
            size *= 1.05

        # 6. Drawdown protection
        if drawdown_pct > 0.7:
            size *= 0.50
        elif drawdown_pct > 0.5:
            size *= 0.70
        elif drawdown_pct > 0.3:
            size *= 0.85

        # 7. Streak factor (momentum)
        if self._win_streak >= 5:
            size *= 1.15
        elif self._win_streak >= 3:
            size *= 1.08
        elif self._lose_streak >= 5:
            size *= 0.70
        elif self._lose_streak >= 3:
            size *= 0.85

        # Clamp to bounds
        abs_max = (
            self.capital_usd * self.max_size_pct
            if self.capital_usd > 0
            else self.max_size_usd
        )
        size = max(self.min_size_usd, min(min(self.max_size_usd, abs_max), size))

        return round(size, 2)

    def record_fill(self, pnl: float):
        """Record a fill outcome to update streak tracking."""
        self._recent_pnls.append(pnl)
        if len(self._recent_pnls) > 100:
            self._recent_pnls.pop(0)

        if pnl >= 0:
            self._win_streak += 1
            self._lose_streak = 0
        else:
            self._lose_streak += 1
            self._win_streak = 0

    def update_capital(self, new_capital: float):
        """Update capital for position sizing."""
        self.capital_usd = new_capital

    @property
    def win_rate(self) -> float:
        if not self._recent_pnls:
            return 0.5
        wins = sum(1 for p in self._recent_pnls if p >= 0)
        return wins / len(self._recent_pnls)

    def summary(self) -> dict:
        return {
            "base_size": self.base_size_usd,
            "capital": self.capital_usd,
            "win_streak": self._win_streak,
            "lose_streak": self._lose_streak,
            "win_rate": round(self.win_rate, 3),
            "recent_fills": len(self._recent_pnls),
        }
