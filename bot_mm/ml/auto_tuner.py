"""
Runtime auto-parameter tuner for the market making bot.

Tracks live performance in rolling windows and periodically adjusts
strategy parameters (spread, skew, size) based on observed metrics.

Complements the offline optimizer (scripts/run_mm_optimizer.py) and the
regime-based adaptive_mm by doing continuous closed-loop tuning.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
import math
import time
import logging

from bot_mm.config import AssetMMConfig

logger = logging.getLogger(__name__)


@dataclass
class PerformanceWindow:
    """Rolling performance metrics for auto-tuning decisions."""
    start_time: float
    pnl_series: List[float] = field(default_factory=list)
    fills: int = 0
    quotes: int = 0
    max_inventory_pct: float = 0.0
    peak_equity: float = 0.0
    max_drawdown: float = 0.0

    @property
    def fill_rate(self) -> float:
        """Ratio of fills to quotes, 0.0 if no quotes."""
        return self.fills / self.quotes if self.quotes > 0 else 0.0

    @property
    def total_pnl(self) -> float:
        return sum(self.pnl_series)

    @property
    def sharpe(self) -> float:
        """Annualised Sharpe estimate from bar-level PnL."""
        if len(self.pnl_series) < 2:
            return 0.0
        mean = sum(self.pnl_series) / len(self.pnl_series)
        var = sum((p - mean) ** 2 for p in self.pnl_series) / (len(self.pnl_series) - 1)
        std = math.sqrt(var) if var > 0 else 1e-9
        # Rough annualisation assuming 1h bars → 8760 bars/year
        return (mean / std) * math.sqrt(8760)


@dataclass
class TuningState:
    """Current state of auto-tuned parameters."""
    base_spread_bps: float
    vol_multiplier: float
    inventory_skew_factor: float
    order_size_usd: float

    # Track original values for reset
    original_spread_bps: float = 0.0
    original_vol_multiplier: float = 0.0
    original_skew_factor: float = 0.0
    original_size_usd: float = 0.0

    adjustments_count: int = 0
    last_adjustment_time: float = 0.0

    def drift_pct(self, param: str) -> float:
        """How far a parameter has drifted from its original value (%)."""
        current = getattr(self, param)
        orig_map = {
            "base_spread_bps": self.original_spread_bps,
            "vol_multiplier": self.original_vol_multiplier,
            "inventory_skew_factor": self.original_skew_factor,
            "order_size_usd": self.original_size_usd,
        }
        orig = orig_map[param]
        if orig == 0:
            return 0.0
        return abs(current - orig) / orig * 100

    def max_drift_pct(self) -> float:
        """Maximum drift across all parameters."""
        return max(
            self.drift_pct("base_spread_bps"),
            self.drift_pct("vol_multiplier"),
            self.drift_pct("inventory_skew_factor"),
            self.drift_pct("order_size_usd"),
        )


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------
SIZE_MIN = 50.0
SIZE_MAX = 500.0
SKEW_MIN = 0.1
SKEW_MAX = 1.0
MAX_DRIFT_PCT = 70.0  # Reset if any param drifts > 70% from baseline


class AutoParameterTuner:
    """
    Runtime parameter self-adjustment based on live performance metrics.

    Evaluation rules (applied in order, at most one spread adjustment per eval):
      1. Sharpe < 0 for 2 consecutive windows → widen spread +10%
      2. fill_rate < 15% → tighten spread -10%
      3. fill_rate > 85% → widen spread +10%
      4. avg_inventory > 70% of max → increase skew +10%
      5. max_drawdown > 50% of daily limit → reduce size -20%

    If cumulative drift exceeds ±70%, all params reset to baseline.
    """

    def __init__(
        self,
        config: AssetMMConfig,
        evaluation_interval_hours: float = 4,
        window_hours: float = 24,
        *,
        _time_fn=None,
    ):
        self._config = config
        self._eval_interval_s = evaluation_interval_hours * 3600
        self._window_s = window_hours * 3600
        self._time_fn = _time_fn or time.time

        # Boundaries from config
        self._min_spread = config.quote.min_spread_bps
        self._max_spread = config.quote.max_spread_bps
        self._daily_loss_limit = config.risk.max_daily_loss_usd

        # Tuning state — initialised from config
        q = config.quote
        self._state = TuningState(
            base_spread_bps=q.base_spread_bps,
            vol_multiplier=q.vol_multiplier,
            inventory_skew_factor=q.inventory_skew_factor,
            order_size_usd=q.order_size_usd,
            original_spread_bps=q.base_spread_bps,
            original_vol_multiplier=q.vol_multiplier,
            original_skew_factor=q.inventory_skew_factor,
            original_size_usd=q.order_size_usd,
        )

        # Rolling windows
        self._current_window = PerformanceWindow(start_time=self._time_fn())
        self._prev_window: Optional[PerformanceWindow] = None
        self._last_eval_time = self._time_fn()

        # History
        self._adjustment_log: List[dict] = []

    # ------------------------------------------------------------------
    # Event recording
    # ------------------------------------------------------------------

    def on_fill(self, side: str, price: float, size: float, pnl: float):
        """Record a fill event."""
        self._maybe_rotate_window()
        self._current_window.fills += 1
        self._current_window.pnl_series.append(pnl)

    def on_quote(self, side: str, price: float, size: float):
        """Record a quote event."""
        self._maybe_rotate_window()
        self._current_window.quotes += 1

    def on_bar(self, equity: float, inventory_pct: float):
        """Record bar-level metrics (called every candle)."""
        self._maybe_rotate_window()
        w = self._current_window

        # Track inventory
        w.max_inventory_pct = max(w.max_inventory_pct, abs(inventory_pct))

        # Track drawdown
        if equity > w.peak_equity:
            w.peak_equity = equity
        dd = w.peak_equity - equity
        if dd > w.max_drawdown:
            w.max_drawdown = dd

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(self) -> Dict[str, float]:
        """
        Evaluate performance and adjust parameters if needed.

        Returns dict of param_name → new_value (only changed params).
        Called externally or by the bot's main loop.
        """
        now = self._time_fn()
        if now - self._last_eval_time < self._eval_interval_s:
            return {}

        self._last_eval_time = now
        changes: Dict[str, float] = {}
        w = self._current_window
        reasons: List[str] = []

        # --- Rule 1: consecutive negative Sharpe → widen spread ---
        spread_adjusted = False
        if self._prev_window is not None:
            if self._prev_window.sharpe < 0 and w.sharpe < 0:
                new_spread = self._clamp_spread(self._state.base_spread_bps * 1.10)
                if new_spread != self._state.base_spread_bps:
                    changes["base_spread_bps"] = new_spread
                    reasons.append(
                        f"Sharpe negative 2 consecutive windows "
                        f"({self._prev_window.sharpe:.2f}, {w.sharpe:.2f})"
                    )
                    spread_adjusted = True

        # --- Rule 2: fill rate too low → tighten spread ---
        if not spread_adjusted and w.quotes > 0:
            fr = w.fill_rate
            if fr < 0.15:
                new_spread = self._clamp_spread(self._state.base_spread_bps * 0.90)
                if new_spread != self._state.base_spread_bps:
                    changes["base_spread_bps"] = new_spread
                    reasons.append(f"Fill rate too low ({fr:.1%})")
                    spread_adjusted = True

        # --- Rule 3: fill rate too high → widen spread ---
        if not spread_adjusted and w.quotes > 0:
            fr = w.fill_rate
            if fr > 0.85:
                new_spread = self._clamp_spread(self._state.base_spread_bps * 1.10)
                if new_spread != self._state.base_spread_bps:
                    changes["base_spread_bps"] = new_spread
                    reasons.append(f"Fill rate too high ({fr:.1%})")

        # --- Rule 4: high inventory → increase skew by 10% ---
        if w.max_inventory_pct > 0.70:
            new_skew = min(SKEW_MAX, self._state.inventory_skew_factor * 1.10)
            if new_skew != self._state.inventory_skew_factor:
                changes["inventory_skew_factor"] = new_skew
                reasons.append(
                    f"High inventory ({w.max_inventory_pct:.0%} of max)"
                )

        # --- Rule 5: high drawdown → reduce size ---
        if self._daily_loss_limit > 0 and w.max_drawdown > 0.50 * self._daily_loss_limit:
            new_size = max(SIZE_MIN, self._state.order_size_usd * 0.80)
            if new_size != self._state.order_size_usd:
                changes["order_size_usd"] = new_size
                reasons.append(
                    f"Max drawdown ${w.max_drawdown:.2f} > 50% of daily limit "
                    f"${self._daily_loss_limit:.2f}"
                )

        # Apply changes
        if changes:
            self._apply(changes, reasons)

        # Check cumulative drift → reset if too far
        if self._state.max_drift_pct() > MAX_DRIFT_PCT:
            logger.warning(
                "AutoTuner: drift %.1f%% > %.0f%% threshold — resetting to baseline",
                self._state.max_drift_pct(),
                MAX_DRIFT_PCT,
            )
            self.reset_to_baseline()
            changes = {}  # Reset supersedes individual changes

        return changes

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_current_params(self) -> TuningState:
        """Get current tuned parameters."""
        return self._state

    def reset_to_baseline(self):
        """Reset all params to original values."""
        before = {
            "base_spread_bps": self._state.base_spread_bps,
            "vol_multiplier": self._state.vol_multiplier,
            "inventory_skew_factor": self._state.inventory_skew_factor,
            "order_size_usd": self._state.order_size_usd,
        }
        self._state.base_spread_bps = self._state.original_spread_bps
        self._state.vol_multiplier = self._state.original_vol_multiplier
        self._state.inventory_skew_factor = self._state.original_skew_factor
        self._state.order_size_usd = self._state.original_size_usd
        self._state.adjustments_count += 1
        self._state.last_adjustment_time = self._time_fn()
        logger.info(
            "AutoTuner RESET to baseline: %s → %s",
            before,
            {
                "base_spread_bps": self._state.base_spread_bps,
                "vol_multiplier": self._state.vol_multiplier,
                "inventory_skew_factor": self._state.inventory_skew_factor,
                "order_size_usd": self._state.order_size_usd,
            },
        )

    def summary(self) -> dict:
        """Return summary stats for logging/display."""
        w = self._current_window
        return {
            "base_spread_bps": self._state.base_spread_bps,
            "vol_multiplier": self._state.vol_multiplier,
            "inventory_skew_factor": self._state.inventory_skew_factor,
            "order_size_usd": self._state.order_size_usd,
            "adjustments_count": self._state.adjustments_count,
            "max_drift_pct": self._state.max_drift_pct(),
            "window_fills": w.fills,
            "window_quotes": w.quotes,
            "window_fill_rate": w.fill_rate,
            "window_sharpe": w.sharpe,
            "window_max_inventory_pct": w.max_inventory_pct,
            "window_max_drawdown": w.max_drawdown,
            "window_pnl": w.total_pnl,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _maybe_rotate_window(self):
        """Rotate to a new performance window when the current one expires."""
        now = self._time_fn()
        if now - self._current_window.start_time >= self._window_s:
            self._prev_window = self._current_window
            self._current_window = PerformanceWindow(start_time=now)

    def _clamp_spread(self, value: float) -> float:
        return max(self._min_spread, min(self._max_spread, value))

    def _apply(self, changes: Dict[str, float], reasons: List[str]):
        """Apply parameter changes and log them."""
        for param, new_val in changes.items():
            old_val = getattr(self._state, param)
            setattr(self._state, param, new_val)
            logger.info(
                "AutoTuner ADJUST %s: %.4f → %.4f  reason=%s",
                param, old_val, new_val, "; ".join(reasons),
            )
            self._adjustment_log.append({
                "time": self._time_fn(),
                "param": param,
                "old": old_val,
                "new": new_val,
                "reasons": list(reasons),
            })
        self._state.adjustments_count += 1
        self._state.last_adjustment_time = self._time_fn()
