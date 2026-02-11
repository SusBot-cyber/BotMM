"""
Directional Bias — Kalman Filter + QQE for MM quote skewing.

Provides directional intelligence from BotHL's proven indicators:
- Kalman Filter: trend direction (slope + price position)
- QQE Mega: momentum confirmation (trend + level)

Bias output:
  +1.0 = strong BULLISH → tighten bid, widen ask
  -1.0 = strong BEARISH → widen bid, tighten ask
   0.0 = NEUTRAL → symmetric quotes

The bias is applied as additional skew in the QuoteEngine,
allowing the MM bot to accumulate favorable inventory in trends
while staying market-neutral in ranging conditions.
"""

import logging
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional, List

logger = logging.getLogger(__name__)


class Regime(IntEnum):
    BEARISH = -1
    NEUTRAL = 0
    BULLISH = 1


@dataclass
class BiasResult:
    """Output of directional bias calculation."""
    regime: Regime
    bias: float          # -1.0 to +1.0 (continuous)
    kalman_price: float  # Filtered price
    kalman_slope: float  # Slope (positive = uptrend)
    qqe_value: float     # QQE line (>50 = bullish zone)
    qqe_trend: int       # 1 = bullish, -1 = bearish


class DirectionalBias:
    """
    Uses Kalman Filter + QQE from BotHL to bias MM quotes.

    If Kalman/QQE says BULLISH:
      → Tighten bid (buy more eagerly)
      → Widen ask (sell less eagerly)
      → Net effect: accumulate long inventory in uptrend

    If Kalman/QQE says BEARISH:
      → Tighten ask, widen bid → accumulate short

    If NEUTRAL:
      → Symmetric quotes (pure MM)
    """

    def __init__(
        self,
        kalman_process_noise: float = 0.005,
        kalman_measurement_noise: float = 0.1,
        qqe_rsi_period: int = 14,
        qqe_smoothing: int = 5,
        qqe_factor: float = 3.5,
        slope_window: int = 5,
        bias_strength: float = 0.5,
    ):
        """
        Args:
            kalman_process_noise: Q — higher = more responsive
            kalman_measurement_noise: R — higher = smoother
            qqe_rsi_period: RSI period for QQE
            qqe_smoothing: EMA smoothing for QQE
            qqe_factor: Band multiplier for QQE
            slope_window: Bars to measure Kalman slope
            bias_strength: 0-1, how strongly bias affects quotes
        """
        # Kalman state
        self._kalman_x: Optional[float] = None
        self._kalman_P: float = 1.0
        self._Q = kalman_process_noise
        self._R = kalman_measurement_noise
        self._kalman_history: List[float] = []
        self._slope_window = slope_window

        # QQE state
        self._qqe_rsi_period = qqe_rsi_period
        self._qqe_smoothing = qqe_smoothing
        self._qqe_factor = qqe_factor
        self._prices: List[float] = []
        self._rsi_ema_mult = 2.0 / (qqe_smoothing + 1)
        self._atr_ema_mult = 2.0 / (qqe_rsi_period + 1)
        self._smoothed_rsi: Optional[float] = None
        self._rsi_atr: Optional[float] = None
        self._long_band: float = 0.0
        self._short_band: float = 0.0
        self._qqe_trend: int = 0
        self._prev_smoothed_rsi: Optional[float] = None

        # Config
        self._bias_strength = bias_strength
        self._warmup_bars = max(qqe_rsi_period + qqe_smoothing + 5, slope_window + 2)
        self._bar_count = 0

        # Last result
        self._last: Optional[BiasResult] = None

    @property
    def is_ready(self) -> bool:
        return self._bar_count >= self._warmup_bars

    @property
    def last_result(self) -> Optional[BiasResult]:
        return self._last

    def update(self, close: float) -> Optional[BiasResult]:
        """
        Feed a new candle close price.

        Returns BiasResult once warmed up, None during warmup.
        """
        self._bar_count += 1
        self._prices.append(close)

        # --- Kalman update ---
        kalman_price = self._update_kalman(close)
        kalman_slope = self._calc_slope()

        # --- QQE update ---
        qqe_value, qqe_trend = self._update_qqe()

        if not self.is_ready or qqe_value is None:
            return None

        # --- Calculate bias ---
        bias, regime = self._calc_bias(close, kalman_price, kalman_slope, qqe_value, qqe_trend)

        result = BiasResult(
            regime=regime,
            bias=bias,
            kalman_price=kalman_price,
            kalman_slope=kalman_slope,
            qqe_value=qqe_value,
            qqe_trend=qqe_trend,
        )
        self._last = result
        return result

    def _update_kalman(self, measurement: float) -> float:
        """Run one Kalman filter step."""
        if self._kalman_x is None:
            self._kalman_x = measurement
            self._kalman_P = 1.0
            self._kalman_history.append(measurement)
            return measurement

        # Predict
        x_pred = self._kalman_x
        P_pred = self._kalman_P + self._Q

        # Update
        K = P_pred / (P_pred + self._R)
        self._kalman_x = x_pred + K * (measurement - x_pred)
        self._kalman_P = (1 - K) * P_pred

        self._kalman_history.append(self._kalman_x)
        return self._kalman_x

    def _calc_slope(self) -> float:
        """Calculate Kalman slope over slope_window bars."""
        if len(self._kalman_history) < self._slope_window + 1:
            return 0.0
        recent = self._kalman_history[-self._slope_window:]
        # Normalized slope: (current - N bars ago) / current
        return (recent[-1] - recent[0]) / recent[-1] if recent[-1] != 0 else 0.0

    def _update_qqe(self):
        """Run one QQE step. Returns (qqe_value, trend) or (None, 0)."""
        if len(self._prices) < self._qqe_rsi_period + 1:
            return None, 0

        # RSI
        changes = []
        for i in range(len(self._prices) - self._qqe_rsi_period, len(self._prices)):
            changes.append(self._prices[i] - self._prices[i - 1])

        gains = sum(max(0, c) for c in changes) / self._qqe_rsi_period
        losses = sum(max(0, -c) for c in changes) / self._qqe_rsi_period

        if losses == 0:
            rsi = 100.0
        else:
            rsi = 100.0 - 100.0 / (1.0 + gains / losses)

        # Smoothed RSI (EMA)
        if self._smoothed_rsi is None:
            self._smoothed_rsi = rsi
        else:
            self._smoothed_rsi = (rsi - self._smoothed_rsi) * self._rsi_ema_mult + self._smoothed_rsi

        # RSI ATR (EMA of |change|)
        if self._prev_smoothed_rsi is not None:
            rsi_change = abs(self._smoothed_rsi - self._prev_smoothed_rsi)
            if self._rsi_atr is None:
                self._rsi_atr = rsi_change
            else:
                self._rsi_atr = (rsi_change - self._rsi_atr) * self._atr_ema_mult + self._rsi_atr
        self._prev_smoothed_rsi = self._smoothed_rsi

        if self._rsi_atr is None:
            return None, 0

        # Dynamic bands
        dar = self._rsi_atr * self._qqe_factor

        new_long = self._smoothed_rsi - dar
        new_short = self._smoothed_rsi + dar

        if not hasattr(self, '_bands_initialized') or not self._bands_initialized:
            # First time: initialize bands, then let trend detection work
            self._long_band = new_long
            self._short_band = new_short
            self._bands_initialized = True
        else:
            # Ratchet bands (long only goes up, short only goes down)
            prev_s = self._prev_smoothed_rsi if self._prev_smoothed_rsi else self._smoothed_rsi
            if prev_s > self._long_band:
                self._long_band = max(new_long, self._long_band)
            else:
                self._long_band = new_long
            if prev_s < self._short_band:
                self._short_band = min(new_short, self._short_band)
            else:
                self._short_band = new_short

        # Trend
        if self._smoothed_rsi > self._short_band:
            self._qqe_trend = 1
        elif self._smoothed_rsi < self._long_band:
            self._qqe_trend = -1

        return self._smoothed_rsi, self._qqe_trend

    def _calc_bias(
        self,
        price: float,
        kalman_price: float,
        kalman_slope: float,
        qqe_value: float,
        qqe_trend: int,
    ) -> tuple:
        """
        Combine Kalman + QQE into a single bias score.

        Scoring:
          Kalman component (0 to ±0.5):
            - slope direction: +0.25 if positive, -0.25 if negative
            - price vs kalman: +0.25 if price > kalman (uptrend confirmation)

          QQE component (0 to ±0.5):
            - trend: +0.25 if bullish, -0.25 if bearish
            - level: +0.25 if > 55 (strong bullish), -0.25 if < 45

        Total: -1.0 to +1.0, scaled by bias_strength
        """
        score = 0.0

        # Kalman slope
        if kalman_slope > 0.0001:
            score += 0.25
        elif kalman_slope < -0.0001:
            score -= 0.25

        # Price vs Kalman
        if price > kalman_price * 1.0001:
            score += 0.25
        elif price < kalman_price * 0.9999:
            score -= 0.25

        # QQE trend
        if qqe_trend == 1:
            score += 0.25
        elif qqe_trend == -1:
            score -= 0.25

        # QQE level
        if qqe_value > 55:
            score += 0.25
        elif qqe_value < 45:
            score -= 0.25

        # Apply strength scaling
        bias = score * self._bias_strength

        # Determine regime
        if bias > 0.15:
            regime = Regime.BULLISH
        elif bias < -0.15:
            regime = Regime.BEARISH
        else:
            regime = Regime.NEUTRAL

        return bias, regime
