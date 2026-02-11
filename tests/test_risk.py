"""Tests for RiskManager — circuit breakers and safety checks."""

import time
import pytest
from bot_mm.core.risk import RiskManager, RiskStatus


def make_risk(**overrides) -> RiskManager:
    defaults = dict(
        max_daily_loss_usd=50.0,
        max_drawdown_pct=5.0,
        volatility_pause_mult=3.0,
        capital_usd=1000.0,
    )
    defaults.update(overrides)
    return RiskManager(**defaults)


def check(rm, daily_pnl=0.0, equity=1000.0, vol=0.001, pos=0.0, max_pos=500.0):
    return rm.check_all(daily_pnl, equity, vol, pos, max_pos)


# ── Normal operation ─────────────────────────────────────────


def test_normal_status():
    """No risk triggers → NORMAL."""
    rm = make_risk()
    status = check(rm)
    assert status == RiskStatus.NORMAL
    assert rm.state.reason == ""


def test_normal_with_moderate_position():
    """Position at 50% → still NORMAL."""
    rm = make_risk()
    status = check(rm, pos=250.0, max_pos=500.0)
    assert status == RiskStatus.NORMAL


# ── Daily loss halt ──────────────────────────────────────────


def test_daily_loss_halt():
    """Exceeding daily loss limit → HALT."""
    rm = make_risk(max_daily_loss_usd=50.0)
    status = check(rm, daily_pnl=-51.0)
    assert status == RiskStatus.HALT
    assert "Daily loss" in rm.state.reason


def test_daily_loss_at_limit_ok():
    """Exactly at limit → still OK (check is strictly greater)."""
    rm = make_risk(max_daily_loss_usd=50.0)
    status = check(rm, daily_pnl=-50.0)
    assert status == RiskStatus.NORMAL


# ── Drawdown halt ────────────────────────────────────────────


def test_drawdown_halt():
    """Equity drops >5% from peak → HALT."""
    rm = make_risk(max_drawdown_pct=5.0, capital_usd=1000.0)
    # First call sets peak
    check(rm, equity=1100.0)
    assert rm.state.peak_equity == 1100.0
    # Drop to 1040 = 5.45% drawdown from 1100
    status = check(rm, equity=1040.0)
    assert status == RiskStatus.HALT
    assert "Drawdown" in rm.state.reason


def test_drawdown_within_limit():
    """Small drawdown → NORMAL."""
    rm = make_risk(max_drawdown_pct=5.0)
    check(rm, equity=1000.0)
    status = check(rm, equity=960.0)  # 4% drawdown
    assert status == RiskStatus.NORMAL


# ── Volatility pause ────────────────────────────────────────


def test_volatility_spike_critical():
    """Vol spike > 3× normal → CRITICAL."""
    rm = make_risk(volatility_pause_mult=3.0)
    # First call sets normal vol baseline
    check(rm, vol=0.001)
    assert rm._normal_vol == pytest.approx(0.001, abs=1e-6)
    # Spike to 4× normal
    status = check(rm, vol=0.004)
    assert status == RiskStatus.CRITICAL
    assert "Volatility" in rm.state.reason


def test_volatility_normal():
    """Vol within bounds → NORMAL."""
    rm = make_risk(volatility_pause_mult=3.0)
    check(rm, vol=0.001)
    status = check(rm, vol=0.002)  # 2× normal, under 3× threshold
    assert status == RiskStatus.NORMAL


# ── Position limits ──────────────────────────────────────────


def test_position_warning_at_70pct():
    """Position at 75% → WARNING."""
    rm = make_risk()
    # Set normal vol first so vol check doesn't interfere
    check(rm, vol=0.001)
    status = check(rm, vol=0.001, pos=375.0, max_pos=500.0)
    assert status == RiskStatus.WARNING
    assert "Position" in rm.state.reason


def test_position_critical_at_90pct():
    """Position at 95% → CRITICAL."""
    rm = make_risk()
    check(rm, vol=0.001)
    status = check(rm, vol=0.001, pos=475.0, max_pos=500.0)
    assert status == RiskStatus.CRITICAL
    assert "Position" in rm.state.reason


def test_position_ok_at_50pct():
    """Position at 50% → NORMAL."""
    rm = make_risk()
    check(rm, vol=0.001)
    status = check(rm, vol=0.001, pos=250.0, max_pos=500.0)
    assert status == RiskStatus.NORMAL


# ── API error tracking ──────────────────────────────────────


def test_api_errors_trigger_halt():
    """More than 5 API errors in 60s → HALT."""
    rm = make_risk()
    for _ in range(6):
        rm.on_api_error()
    assert rm.state.status == RiskStatus.HALT
    assert "API errors" in rm.state.reason


def test_api_errors_below_threshold():
    """5 or fewer errors → no halt."""
    rm = make_risk()
    for _ in range(5):
        rm.on_api_error()
    assert rm.state.api_errors_count == 5
    # Status not changed by on_api_error for ≤5
    assert rm.state.status != RiskStatus.HALT or "API" not in rm.state.reason


def test_api_errors_window_reset():
    """Errors from >60s ago are reset."""
    rm = make_risk()
    rm.on_api_error()
    # Simulate window expiry by backdating
    rm.state.api_errors_window_start = time.time() - 61
    rm.on_api_error()
    assert rm.state.api_errors_count == 1  # Reset to 1


# ── Large move pause ────────────────────────────────────────


def test_large_move_pauses():
    """Price move >1% triggers pause."""
    rm = make_risk()
    rm.on_large_move(1.5, pause_seconds=300)
    assert rm.state.status == RiskStatus.HALT
    assert "Large move" in rm.state.reason
    assert rm.state.paused_until > time.time()


def test_small_move_no_pause():
    """Price move <1% does nothing."""
    rm = make_risk()
    rm.on_large_move(0.5)
    assert rm.state.status != RiskStatus.HALT


# ── Update normal vol ───────────────────────────────────────


def test_normal_vol_ema_update():
    """Normal vol baseline updates with EMA."""
    rm = make_risk()
    rm.update_normal_vol(0.001)
    assert rm._normal_vol == 0.001
    rm.update_normal_vol(0.002, alpha=0.5)
    assert rm._normal_vol == pytest.approx(0.0015, abs=1e-6)
