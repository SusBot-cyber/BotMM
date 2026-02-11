"""
Risk Manager — circuit breakers, position limits, and safety checks.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional
import time


class RiskStatus(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"
    HALT = "halt"


@dataclass
class RiskState:
    """Current risk state."""
    status: RiskStatus = RiskStatus.NORMAL
    reason: str = ""
    daily_pnl: float = 0.0
    peak_equity: float = 0.0
    current_drawdown_pct: float = 0.0
    last_price_move_pct: float = 0.0
    paused_until: float = 0.0
    api_errors_count: int = 0
    api_errors_window_start: float = 0.0


class RiskManager:
    """Manages risk limits and circuit breakers for MM bot."""

    def __init__(
        self,
        max_daily_loss_usd: float = 50.0,
        max_drawdown_pct: float = 5.0,
        volatility_pause_mult: float = 3.0,
        capital_usd: float = 1000.0,
    ):
        self.max_daily_loss = max_daily_loss_usd
        self.max_drawdown_pct = max_drawdown_pct
        self.vol_pause_mult = volatility_pause_mult
        self.capital = capital_usd
        self.state = RiskState(peak_equity=capital_usd)
        self._normal_vol: Optional[float] = None

    def check_all(
        self,
        daily_pnl: float,
        equity: float,
        current_vol: float,
        position_usd: float,
        max_position_usd: float,
    ) -> RiskStatus:
        """Run all risk checks. Returns overall status."""
        self.state.daily_pnl = daily_pnl
        self.state.peak_equity = max(self.state.peak_equity, equity)

        # Check pause timer
        if time.time() < self.state.paused_until:
            self.state.status = RiskStatus.HALT
            self.state.reason = "Paused (cooldown)"
            return self.state.status

        # Daily loss limit
        if daily_pnl < -self.max_daily_loss:
            self.state.status = RiskStatus.HALT
            self.state.reason = f"Daily loss ${daily_pnl:.2f} > limit ${self.max_daily_loss}"
            return self.state.status

        # Drawdown check
        if self.state.peak_equity > 0:
            dd = (self.state.peak_equity - equity) / self.state.peak_equity * 100
            self.state.current_drawdown_pct = dd
            if dd > self.max_drawdown_pct:
                self.state.status = RiskStatus.HALT
                self.state.reason = f"Drawdown {dd:.1f}% > limit {self.max_drawdown_pct}%"
                return self.state.status

        # Volatility spike
        if self._normal_vol is not None and current_vol > self._normal_vol * self.vol_pause_mult:
            self.state.status = RiskStatus.CRITICAL
            self.state.reason = f"Volatility spike: {current_vol:.4f} > {self._normal_vol * self.vol_pause_mult:.4f}"
            return self.state.status
        if self._normal_vol is None and current_vol > 0:
            self._normal_vol = current_vol

        # Position limit warning
        if max_position_usd > 0:
            pos_ratio = abs(position_usd) / max_position_usd
            if pos_ratio > 0.9:
                self.state.status = RiskStatus.CRITICAL
                self.state.reason = f"Position {pos_ratio:.0%} of max"
                return self.state.status
            elif pos_ratio > 0.7:
                self.state.status = RiskStatus.WARNING
                self.state.reason = f"Position {pos_ratio:.0%} of max"
                return self.state.status

        self.state.status = RiskStatus.NORMAL
        self.state.reason = ""
        return self.state.status

    def on_large_move(self, pct_move: float, pause_seconds: int = 300):
        """Called when significant price move detected."""
        self.state.last_price_move_pct = pct_move
        if abs(pct_move) > 1.0:  # >1% move
            self.state.paused_until = time.time() + pause_seconds
            self.state.status = RiskStatus.HALT
            self.state.reason = f"Large move {pct_move:+.2f}%, paused {pause_seconds}s"

    def on_api_error(self):
        """Track API errors for rate limiting."""
        now = time.time()
        if now - self.state.api_errors_window_start > 60:
            self.state.api_errors_count = 0
            self.state.api_errors_window_start = now
        self.state.api_errors_count += 1

        if self.state.api_errors_count > 5:
            self.state.paused_until = now + 120
            self.state.status = RiskStatus.HALT
            self.state.reason = "Too many API errors"

    def update_normal_vol(self, vol: float, alpha: float = 0.01):
        """Slowly update normal volatility baseline."""
        if self._normal_vol is None:
            self._normal_vol = vol
        else:
            self._normal_vol = self._normal_vol * (1 - alpha) + vol * alpha
