import pytest
from bot_mm.config import AssetMMConfig, QuoteParams, RiskLimits
from bot_mm.ml.auto_tuner import (
    AutoParameterTuner,
    PerformanceWindow,
    TuningState,
    SIZE_MIN,
    SIZE_MAX,
    SKEW_MIN,
    SKEW_MAX,
)


def _make_config(**overrides) -> AssetMMConfig:
    """Build an AssetMMConfig with sensible test defaults."""
    quote_kw = {}
    risk_kw = {}
    for k in list(overrides):
        if k in QuoteParams.__dataclass_fields__:
            quote_kw[k] = overrides.pop(k)
        elif k in RiskLimits.__dataclass_fields__:
            risk_kw[k] = overrides.pop(k)

    quote = QuoteParams(**{**dict(
        base_spread_bps=2.0,
        vol_multiplier=1.5,
        inventory_skew_factor=0.5,
        order_size_usd=100.0,
        min_spread_bps=0.5,
        max_spread_bps=20.0,
    ), **quote_kw})

    risk = RiskLimits(**{**dict(
        max_daily_loss_usd=50.0,
    ), **risk_kw})

    return AssetMMConfig(symbol="BTCUSDT", quote=quote, risk=risk, **overrides)


class FakeClock:
    """Deterministic clock for testing."""

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float):
        self.now += seconds


def _make_tuner(
    clock: FakeClock = None,
    eval_hours: float = 4,
    window_hours: float = 24,
    **config_kw,
) -> AutoParameterTuner:
    clock = clock or FakeClock()
    cfg = _make_config(**config_kw)
    return AutoParameterTuner(
        cfg,
        evaluation_interval_hours=eval_hours,
        window_hours=window_hours,
        _time_fn=clock,
    )


# ===================================================================
# Initial state
# ===================================================================
class TestInitialState:
    def test_params_match_config(self):
        tuner = _make_tuner(base_spread_bps=3.0, vol_multiplier=2.0,
                            inventory_skew_factor=0.6, order_size_usd=150.0)
        s = tuner.get_current_params()
        assert s.base_spread_bps == 3.0
        assert s.vol_multiplier == 2.0
        assert s.inventory_skew_factor == 0.6
        assert s.order_size_usd == 150.0

    def test_originals_stored(self):
        tuner = _make_tuner(base_spread_bps=3.0, order_size_usd=200.0)
        s = tuner.get_current_params()
        assert s.original_spread_bps == 3.0
        assert s.original_size_usd == 200.0

    def test_no_adjustments_initially(self):
        tuner = _make_tuner()
        assert tuner.get_current_params().adjustments_count == 0


# ===================================================================
# Fill / quote tracking
# ===================================================================
class TestTracking:
    def test_fill_increments_counter(self):
        tuner = _make_tuner()
        tuner.on_fill("buy", 100.0, 1.0, 0.5)
        tuner.on_fill("sell", 100.0, 1.0, -0.3)
        assert tuner._current_window.fills == 2

    def test_quote_increments_counter(self):
        tuner = _make_tuner()
        tuner.on_quote("buy", 99.0, 1.0)
        tuner.on_quote("sell", 101.0, 1.0)
        assert tuner._current_window.quotes == 2

    def test_fill_records_pnl(self):
        tuner = _make_tuner()
        tuner.on_fill("buy", 100.0, 1.0, 1.5)
        tuner.on_fill("sell", 100.0, 1.0, -0.5)
        assert tuner._current_window.total_pnl == pytest.approx(1.0)

    def test_on_bar_tracks_drawdown(self):
        tuner = _make_tuner()
        tuner.on_bar(1000.0, 0.1)
        tuner.on_bar(1050.0, 0.1)  # new peak
        tuner.on_bar(1020.0, 0.1)  # drawdown = 30
        assert tuner._current_window.max_drawdown == pytest.approx(30.0)

    def test_on_bar_tracks_inventory(self):
        tuner = _make_tuner()
        tuner.on_bar(1000.0, 0.3)
        tuner.on_bar(1000.0, 0.7)
        tuner.on_bar(1000.0, 0.5)
        assert tuner._current_window.max_inventory_pct == pytest.approx(0.7)

    def test_on_bar_uses_abs_inventory(self):
        """Negative inventory (short) should count by magnitude."""
        tuner = _make_tuner()
        tuner.on_bar(1000.0, -0.8)
        assert tuner._current_window.max_inventory_pct == pytest.approx(0.8)


# ===================================================================
# Window rotation
# ===================================================================
class TestWindowRotation:
    def test_window_rotates_after_expiry(self):
        clock = FakeClock(0)
        tuner = _make_tuner(clock=clock, window_hours=1)
        tuner.on_fill("buy", 100.0, 1.0, 0.5)
        assert tuner._current_window.fills == 1

        clock.advance(3601)  # past 1h window
        tuner.on_fill("buy", 100.0, 1.0, 0.5)
        assert tuner._current_window.fills == 1  # new window
        assert tuner._prev_window is not None
        assert tuner._prev_window.fills == 1


# ===================================================================
# Spread widens when Sharpe negative for 2 consecutive windows
# ===================================================================
class TestNegativeSharpe:
    def test_spread_widens_on_consecutive_negative_sharpe(self):
        clock = FakeClock(0)
        tuner = _make_tuner(clock=clock, eval_hours=0, window_hours=1,
                            base_spread_bps=2.0)

        # Window 1: negative PnL
        for _ in range(10):
            tuner.on_fill("buy", 100.0, 1.0, -1.0)
        clock.advance(3601)

        # Window 2: negative PnL
        for _ in range(10):
            tuner.on_fill("buy", 100.0, 1.0, -1.0)

        changes = tuner.evaluate()
        assert "base_spread_bps" in changes
        assert changes["base_spread_bps"] == pytest.approx(2.0 * 1.1)

    def test_no_widen_single_negative_window(self):
        """Only one negative window shouldn't trigger the rule."""
        clock = FakeClock(0)
        tuner = _make_tuner(clock=clock, eval_hours=0, window_hours=1,
                            base_spread_bps=2.0)

        # Window 1: positive PnL → becomes prev_window
        for _ in range(10):
            tuner.on_fill("buy", 100.0, 1.0, 1.0)
        clock.advance(3601)

        # Window 2: negative PnL
        for _ in range(10):
            tuner.on_fill("buy", 100.0, 1.0, -1.0)

        changes = tuner.evaluate()
        # Sharpe rule should NOT trigger (prev was positive)
        assert "base_spread_bps" not in changes or \
               changes.get("base_spread_bps", 0) == pytest.approx(2.0)


# ===================================================================
# Spread tightens when fill rate too low
# ===================================================================
class TestFillRateLow:
    def test_spread_tightens_on_low_fill_rate(self):
        clock = FakeClock(0)
        tuner = _make_tuner(clock=clock, eval_hours=0, base_spread_bps=5.0)

        # 100 quotes, 10 fills → 10% < 15%
        for _ in range(100):
            tuner.on_quote("buy", 99.0, 1.0)
        for _ in range(10):
            tuner.on_fill("buy", 99.0, 1.0, 0.1)

        changes = tuner.evaluate()
        assert "base_spread_bps" in changes
        assert changes["base_spread_bps"] == pytest.approx(5.0 * 0.9)


# ===================================================================
# Spread widens when fill rate too high
# ===================================================================
class TestFillRateHigh:
    def test_spread_widens_on_high_fill_rate(self):
        clock = FakeClock(0)
        tuner = _make_tuner(clock=clock, eval_hours=0, base_spread_bps=2.0)

        # 100 quotes, 90 fills → 90% > 85%
        for _ in range(100):
            tuner.on_quote("buy", 99.0, 1.0)
        for _ in range(90):
            tuner.on_fill("buy", 99.0, 1.0, 0.1)

        changes = tuner.evaluate()
        assert "base_spread_bps" in changes
        assert changes["base_spread_bps"] == pytest.approx(2.0 * 1.1)


# ===================================================================
# Skew increases when inventory too high
# ===================================================================
class TestHighInventory:
    def test_skew_increases_on_high_inventory(self):
        clock = FakeClock(0)
        tuner = _make_tuner(clock=clock, eval_hours=0,
                            inventory_skew_factor=0.5)

        tuner.on_bar(1000.0, 0.75)  # > 70%

        changes = tuner.evaluate()
        assert "inventory_skew_factor" in changes
        assert changes["inventory_skew_factor"] == pytest.approx(0.55)  # 0.5 * 1.10


# ===================================================================
# Size reduces when drawdown too high
# ===================================================================
class TestHighDrawdown:
    def test_size_reduces_on_high_drawdown(self):
        clock = FakeClock(0)
        tuner = _make_tuner(clock=clock, eval_hours=0,
                            order_size_usd=200.0, max_daily_loss_usd=50.0)

        # Drawdown of $30 > 50% of $50 limit = $25
        tuner.on_bar(1000.0, 0.0)
        tuner.on_bar(970.0, 0.0)

        changes = tuner.evaluate()
        assert "order_size_usd" in changes
        assert changes["order_size_usd"] == pytest.approx(200.0 * 0.8)


# ===================================================================
# Boundary enforcement
# ===================================================================
class TestBoundaries:
    def test_spread_never_below_min(self):
        clock = FakeClock(0)
        tuner = _make_tuner(clock=clock, eval_hours=0,
                            base_spread_bps=0.6, min_spread_bps=0.5)

        # Low fill rate → tighten, but should not go below min
        for _ in range(100):
            tuner.on_quote("buy", 99.0, 1.0)
        for _ in range(5):
            tuner.on_fill("buy", 99.0, 1.0, 0.1)

        changes = tuner.evaluate()
        if "base_spread_bps" in changes:
            assert changes["base_spread_bps"] >= 0.5

    def test_spread_never_above_max(self):
        clock = FakeClock(0)
        tuner = _make_tuner(clock=clock, eval_hours=0,
                            base_spread_bps=19.5, max_spread_bps=20.0)

        # High fill rate → widen, but should not go above max
        for _ in range(100):
            tuner.on_quote("buy", 99.0, 1.0)
        for _ in range(80):
            tuner.on_fill("buy", 99.0, 1.0, 0.1)

        changes = tuner.evaluate()
        if "base_spread_bps" in changes:
            assert changes["base_spread_bps"] <= 20.0

    def test_size_never_below_min(self):
        clock = FakeClock(0)
        tuner = _make_tuner(clock=clock, eval_hours=0,
                            order_size_usd=55.0, max_daily_loss_usd=50.0)

        tuner.on_bar(1000.0, 0.0)
        tuner.on_bar(960.0, 0.0)  # DD=40 > 25

        changes = tuner.evaluate()
        if "order_size_usd" in changes:
            assert changes["order_size_usd"] >= SIZE_MIN

    def test_skew_capped_at_max(self):
        clock = FakeClock(0)
        tuner = _make_tuner(clock=clock, eval_hours=0,
                            inventory_skew_factor=0.95)

        tuner.on_bar(1000.0, 0.75)

        changes = tuner.evaluate()
        if "inventory_skew_factor" in changes:
            assert changes["inventory_skew_factor"] <= SKEW_MAX


# ===================================================================
# Reset when drift exceeds 70%
# ===================================================================
class TestResetOnDrift:
    def test_reset_when_spread_drifts_too_far(self):
        clock = FakeClock(0)
        tuner = _make_tuner(clock=clock, eval_hours=0, window_hours=1,
                            base_spread_bps=2.0)

        # Manually push spread far from baseline (simulate many adjustments)
        tuner._state.base_spread_bps = 3.5  # 75% drift > 70%

        changes = tuner.evaluate()  # should trigger reset
        s = tuner.get_current_params()
        assert s.base_spread_bps == pytest.approx(2.0)

    def test_reset_restores_all_params(self):
        tuner = _make_tuner(base_spread_bps=2.0, inventory_skew_factor=0.5,
                            order_size_usd=100.0, vol_multiplier=1.5)

        # Push several params
        tuner._state.base_spread_bps = 4.0
        tuner._state.inventory_skew_factor = 0.9
        tuner._state.order_size_usd = 60.0

        tuner.reset_to_baseline()
        s = tuner.get_current_params()
        assert s.base_spread_bps == pytest.approx(2.0)
        assert s.inventory_skew_factor == pytest.approx(0.5)
        assert s.order_size_usd == pytest.approx(100.0)
        assert s.vol_multiplier == pytest.approx(1.5)


# ===================================================================
# No adjustment when metrics healthy
# ===================================================================
class TestNoAdjustment:
    def test_healthy_metrics_no_changes(self):
        clock = FakeClock(0)
        tuner = _make_tuner(clock=clock, eval_hours=0, base_spread_bps=2.0,
                            order_size_usd=100.0, inventory_skew_factor=0.5)

        # Fill rate 40% (healthy range), positive PnL, moderate inventory
        for _ in range(100):
            tuner.on_quote("buy", 99.0, 1.0)
        for _ in range(40):
            tuner.on_fill("buy", 99.0, 1.0, 0.5)
        tuner.on_bar(1000.0, 0.3)

        changes = tuner.evaluate()
        assert changes == {}


# ===================================================================
# Evaluation respects interval
# ===================================================================
class TestEvalInterval:
    def test_no_eval_before_interval(self):
        clock = FakeClock(0)
        tuner = _make_tuner(clock=clock, eval_hours=4, base_spread_bps=2.0)

        # Very bad metrics (90% fill rate > 85% threshold)
        for _ in range(100):
            tuner.on_quote("buy", 99.0, 1.0)
        for _ in range(90):
            tuner.on_fill("buy", 99.0, 1.0, 0.1)

        # Only 1 hour passed — too soon
        clock.advance(3600)
        changes = tuner.evaluate()
        assert changes == {}

        # 4+ hours passed — should evaluate
        clock.advance(3600 * 3 + 1)
        changes = tuner.evaluate()
        assert "base_spread_bps" in changes


# ===================================================================
# Summary
# ===================================================================
class TestSummary:
    def test_summary_contains_expected_keys(self):
        tuner = _make_tuner()
        tuner.on_fill("buy", 100.0, 1.0, 0.5)
        tuner.on_quote("buy", 99.0, 1.0)
        tuner.on_bar(1000.0, 0.2)

        s = tuner.summary()
        expected_keys = {
            "base_spread_bps", "vol_multiplier", "inventory_skew_factor",
            "order_size_usd", "adjustments_count", "max_drift_pct",
            "window_fills", "window_quotes", "window_fill_rate",
            "window_sharpe", "window_max_inventory_pct",
            "window_max_drawdown", "window_pnl",
        }
        assert expected_keys.issubset(s.keys())


# ===================================================================
# PerformanceWindow unit tests
# ===================================================================
class TestPerformanceWindow:
    def test_fill_rate_no_quotes(self):
        w = PerformanceWindow(start_time=0)
        assert w.fill_rate == 0.0

    def test_fill_rate_calculation(self):
        w = PerformanceWindow(start_time=0, fills=30, quotes=100)
        assert w.fill_rate == pytest.approx(0.3)

    def test_sharpe_insufficient_data(self):
        w = PerformanceWindow(start_time=0)
        w.pnl_series = [1.0]
        assert w.sharpe == 0.0

    def test_sharpe_positive(self):
        w = PerformanceWindow(start_time=0)
        w.pnl_series = [1.0] * 100  # constant positive → infinite Sharpe (very high)
        # std ≈ 0 → should not crash, uses 1e-9 floor
        assert w.sharpe > 0


# ===================================================================
# TuningState drift
# ===================================================================
class TestTuningStateDrift:
    def test_no_drift_at_start(self):
        s = TuningState(
            base_spread_bps=2.0, vol_multiplier=1.5,
            inventory_skew_factor=0.5, order_size_usd=100.0,
            original_spread_bps=2.0, original_vol_multiplier=1.5,
            original_skew_factor=0.5, original_size_usd=100.0,
        )
        assert s.max_drift_pct() == pytest.approx(0.0)

    def test_drift_calculation(self):
        s = TuningState(
            base_spread_bps=3.0, vol_multiplier=1.5,
            inventory_skew_factor=0.5, order_size_usd=100.0,
            original_spread_bps=2.0, original_vol_multiplier=1.5,
            original_skew_factor=0.5, original_size_usd=100.0,
        )
        assert s.drift_pct("base_spread_bps") == pytest.approx(50.0)
        assert s.max_drift_pct() == pytest.approx(50.0)
