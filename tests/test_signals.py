"""Tests for DirectionalBias (Kalman + QQE) module."""

import numpy as np
import pytest

from bot_mm.core.signals import DirectionalBias, BiasResult, Regime


# ---------------------------------------------------------------------------
# Helpers — generate synthetic price series
# ---------------------------------------------------------------------------

def _uptrend(n: int = 80, start: float = 100_000, step: float = 100, seed: int = 42):
    rng = np.random.RandomState(seed)
    return [start + i * step + rng.normal(0, 10) for i in range(n)]


def _downtrend(n: int = 80, start: float = 100_000, step: float = 100, seed: int = 42):
    rng = np.random.RandomState(seed)
    return [start - i * step + rng.normal(0, 10) for i in range(n)]


def _range_market(n: int = 80, center: float = 100_000, amplitude: float = 1000, seed: int = 42):
    rng = np.random.RandomState(seed)
    return [center + amplitude * np.sin(2 * np.pi * i / 20) + rng.normal(0, 50) for i in range(n)]


def _feed(db: DirectionalBias, prices: list) -> BiasResult:
    """Feed all prices and return the last BiasResult."""
    result = None
    for p in prices:
        r = db.update(p)
        if r is not None:
            result = r
    return result


# ===================================================================
# 1. Warmup tests
# ===================================================================

class TestWarmup:

    def test_not_ready_during_warmup(self):
        """Returns None until enough bars have been fed."""
        db = DirectionalBias()
        warmup = db._warmup_bars
        prices = _uptrend(n=warmup - 1)
        for p in prices:
            result = db.update(p)
            assert result is None, f"Expected None during warmup, got {result}"
        assert not db.is_ready

    def test_ready_after_warmup(self):
        """Returns a BiasResult once warmup_bars have been exceeded."""
        db = DirectionalBias()
        prices = _uptrend(n=60)
        result = _feed(db, prices)
        assert db.is_ready
        assert isinstance(result, BiasResult)
        assert isinstance(result.regime, Regime)


# ===================================================================
# 2. Kalman filter tests
# ===================================================================

class TestKalman:

    def test_kalman_tracks_price(self):
        """Filtered price follows close price within a reasonable margin."""
        db = DirectionalBias()
        prices = _uptrend(n=80)
        result = _feed(db, prices)
        # Kalman price should be close to last raw price (within 2% for a trend)
        assert abs(result.kalman_price - prices[-1]) / prices[-1] < 0.02

    def test_kalman_smoother_than_raw(self):
        """Kalman-filtered series has lower variance than raw prices."""
        db = DirectionalBias()
        prices = _range_market(n=80)
        for p in prices:
            db.update(p)
        raw_var = np.var(prices)
        kalman_var = np.var(db._kalman_history)
        assert kalman_var < raw_var, "Kalman should smooth out noise"

    def test_kalman_slope_positive_uptrend(self):
        """Kalman slope is positive when prices are steadily rising."""
        db = DirectionalBias()
        prices = _uptrend(n=80)
        result = _feed(db, prices)
        assert result.kalman_slope > 0, f"Expected positive slope, got {result.kalman_slope}"

    def test_kalman_slope_negative_downtrend(self):
        """Kalman slope is negative when prices are steadily falling."""
        db = DirectionalBias()
        prices = _downtrend(n=80)
        result = _feed(db, prices)
        assert result.kalman_slope < 0, f"Expected negative slope, got {result.kalman_slope}"


# ===================================================================
# 3. QQE tests
# ===================================================================

class TestQQE:

    def test_qqe_bullish_in_uptrend(self):
        """QQE smoothed RSI > 50 in a strong uptrend."""
        db = DirectionalBias()
        prices = _uptrend(n=80, step=150)
        result = _feed(db, prices)
        assert result.qqe_value > 50, f"Expected QQE > 50 in uptrend, got {result.qqe_value}"

    def test_qqe_bearish_in_downtrend(self):
        """QQE smoothed RSI < 50 in a strong downtrend."""
        db = DirectionalBias()
        prices = _downtrend(n=80, step=150)
        result = _feed(db, prices)
        assert result.qqe_value < 50, f"Expected QQE < 50 in downtrend, got {result.qqe_value}"

    def test_qqe_value_bounded(self):
        """QQE smoothed RSI stays within 0-100 range."""
        db = DirectionalBias()
        prices = _uptrend(n=80, step=200) + _downtrend(n=80, start=116_000, step=200)
        for p in prices:
            r = db.update(p)
            if r is not None:
                assert 0 <= r.qqe_value <= 100, f"QQE out of bounds: {r.qqe_value}"


# ===================================================================
# 4. Bias calculation tests
# ===================================================================

class TestBias:

    def test_bias_positive_in_uptrend(self):
        """Bias is positive when all signals are bullish."""
        db = DirectionalBias(bias_strength=1.0)
        prices = _uptrend(n=80, step=150)
        result = _feed(db, prices)
        assert result.bias > 0, f"Expected positive bias in uptrend, got {result.bias}"

    def test_bias_negative_in_downtrend(self):
        """Bias is negative when all signals are bearish."""
        db = DirectionalBias(bias_strength=1.0)
        prices = _downtrend(n=80, step=150)
        result = _feed(db, prices)
        assert result.bias < 0, f"Expected negative bias in downtrend, got {result.bias}"

    def test_bias_near_zero_in_range(self):
        """Bias is close to 0 in a sideways / ranging market."""
        db = DirectionalBias(bias_strength=0.5)
        prices = _range_market(n=120, amplitude=200, seed=99)
        result = _feed(db, prices)
        assert abs(result.bias) < 0.35, f"Expected near-zero bias in range, got {result.bias}"

    def test_bias_strength_scaling(self):
        """Higher bias_strength produces a larger absolute bias."""
        prices = _uptrend(n=80, step=150)

        db_low = DirectionalBias(bias_strength=0.3)
        result_low = _feed(db_low, prices)

        db_high = DirectionalBias(bias_strength=1.0)
        result_high = _feed(db_high, prices)

        assert abs(result_high.bias) > abs(result_low.bias), (
            f"Higher strength should give larger bias: "
            f"high={result_high.bias}, low={result_low.bias}"
        )


# ===================================================================
# 5. Regime tests
# ===================================================================

class TestRegime:

    def test_regime_bullish(self):
        """Regime is BULLISH when bias > 0.15."""
        db = DirectionalBias(bias_strength=1.0)
        prices = _uptrend(n=80, step=150)
        result = _feed(db, prices)
        assert result.bias > 0.15, f"Precondition: bias should be > 0.15, got {result.bias}"
        assert result.regime == Regime.BULLISH

    def test_regime_bearish(self):
        """Regime is BEARISH when bias < -0.15."""
        db = DirectionalBias(bias_strength=1.0)
        prices = _downtrend(n=80, step=150)
        result = _feed(db, prices)
        assert result.bias < -0.15, f"Precondition: bias should be < -0.15, got {result.bias}"
        assert result.regime == Regime.BEARISH

    def test_regime_neutral_in_range(self):
        """Regime is NEUTRAL in a choppy / sideways market."""
        db = DirectionalBias(bias_strength=0.3)
        prices = _range_market(n=120, amplitude=200, seed=99)
        result = _feed(db, prices)
        assert result.regime == Regime.NEUTRAL, (
            f"Expected NEUTRAL regime, got {result.regime} (bias={result.bias})"
        )
