"""Tests for InventoryManager — position tracking, PnL, limits."""

import pytest
from bot_mm.core.inventory import InventoryManager


def make_mgr(max_pos=500.0) -> InventoryManager:
    return InventoryManager(symbol="BTCUSDT", max_position_usd=max_pos)


# ── Opening positions ───────────────────────────────────────


def test_buy_opens_long():
    mgr = make_mgr()
    pnl = mgr.on_fill("buy", price=100_000.0, size=0.01)
    assert pnl == 0.0
    assert mgr.state.position_size == 0.01
    assert mgr.state.avg_entry_price == 100_000.0
    assert mgr.state.num_buys == 1


def test_sell_opens_short():
    mgr = make_mgr()
    pnl = mgr.on_fill("sell", price=100_000.0, size=0.01)
    assert pnl == 0.0
    assert mgr.state.position_size == -0.01
    assert mgr.state.avg_entry_price == 100_000.0
    assert mgr.state.num_sells == 1


def test_add_to_position():
    """Buying twice averages entry price."""
    mgr = make_mgr()
    mgr.on_fill("buy", 100_000.0, 0.01)
    mgr.on_fill("buy", 102_000.0, 0.01)
    assert mgr.state.position_size == 0.02
    assert mgr.state.avg_entry_price == pytest.approx(101_000.0, rel=1e-6)


# ── Closing positions ───────────────────────────────────────


def test_close_long_profit():
    """Buy then sell at higher price → positive PnL."""
    mgr = make_mgr()
    mgr.on_fill("buy", 100_000.0, 0.01)
    pnl = mgr.on_fill("sell", 101_000.0, 0.01)
    assert pnl == pytest.approx(10.0, abs=0.01)  # (101k-100k)*0.01
    assert mgr.state.position_size == 0.0
    assert mgr.state.realized_pnl == pytest.approx(10.0, abs=0.01)


def test_close_long_loss():
    """Buy then sell at lower price → negative PnL."""
    mgr = make_mgr()
    mgr.on_fill("buy", 100_000.0, 0.01)
    pnl = mgr.on_fill("sell", 99_000.0, 0.01)
    assert pnl == pytest.approx(-10.0, abs=0.01)


def test_close_short_profit():
    """Sell then buy at lower price → positive PnL."""
    mgr = make_mgr()
    mgr.on_fill("sell", 100_000.0, 0.01)
    pnl = mgr.on_fill("buy", 99_000.0, 0.01)
    assert pnl == pytest.approx(10.0, abs=0.01)


# ── Partial close ────────────────────────────────────────────


def test_partial_close():
    """Close half the position, keep remaining."""
    mgr = make_mgr()
    mgr.on_fill("buy", 100_000.0, 0.02)
    pnl = mgr.on_fill("sell", 101_000.0, 0.01)
    assert pnl == pytest.approx(10.0, abs=0.01)
    assert mgr.state.position_size == pytest.approx(0.01, abs=1e-10)
    assert mgr.state.avg_entry_price == 100_000.0  # unchanged


# ── Position flip ────────────────────────────────────────────


def test_flip_long_to_short():
    """Sell more than position → flip to short."""
    mgr = make_mgr()
    mgr.on_fill("buy", 100_000.0, 0.01)
    pnl = mgr.on_fill("sell", 101_000.0, 0.02)
    # Close 0.01 at profit, then open 0.01 short
    assert pnl == pytest.approx(10.0, abs=0.01)
    assert mgr.state.position_size == pytest.approx(-0.01, abs=1e-10)
    assert mgr.state.avg_entry_price == 101_000.0  # new entry for flipped portion


# ── Fees tracking ────────────────────────────────────────────


def test_fees_accumulated():
    """Fees accumulate across fills."""
    mgr = make_mgr()
    mgr.on_fill("buy", 100_000.0, 0.01, fee=-0.015)  # maker rebate
    mgr.on_fill("sell", 100_100.0, 0.01, fee=0.045)   # taker fee
    assert mgr.state.total_fees == pytest.approx(-0.015 + 0.045, abs=1e-6)


def test_net_pnl_includes_fees():
    """Net PnL = realized - fees (positive fee = cost, negative = rebate)."""
    mgr = make_mgr()
    mgr.on_fill("buy", 100_000.0, 0.01, fee=-0.015)   # rebate
    mgr.on_fill("sell", 101_000.0, 0.01, fee=0.045)    # taker cost
    # realized=10, fees=-0.015+0.045=0.03 (net cost)
    # net_pnl = 10 - 0.03 = 9.97
    assert mgr.net_pnl == pytest.approx(10.0 - 0.03, abs=0.01)


# ── Volume and round trips ──────────────────────────────────


def test_round_trip_counter():
    """Each close increments round trip counter."""
    mgr = make_mgr()
    mgr.on_fill("buy", 100_000.0, 0.01)
    mgr.on_fill("sell", 100_100.0, 0.01)
    assert mgr.state.round_trips == 1

    mgr.on_fill("sell", 100_200.0, 0.01)
    mgr.on_fill("buy", 100_100.0, 0.01)
    assert mgr.state.round_trips == 2


def test_volume_tracked():
    """Volume accumulates across all fills."""
    mgr = make_mgr()
    mgr.on_fill("buy", 100_000.0, 0.01)   # 1000 USD
    mgr.on_fill("sell", 100_000.0, 0.01)   # 1000 USD
    assert mgr.state.volume_traded_usd == pytest.approx(2000.0, abs=0.01)


# ── Position limits ─────────────────────────────────────────


def test_should_pause_buy_when_too_long():
    """At >80% long, pause buy side."""
    mgr = make_mgr(max_pos=500.0)
    # Build 0.005 BTC at 100k = $500 position, but use price to calc pos_usd
    mgr.on_fill("buy", 100_000.0, 0.005)  # pos_usd = 0.005*100k = 500
    assert mgr.should_pause_side("buy") is True
    assert mgr.should_pause_side("sell") is False


def test_should_pause_sell_when_too_short():
    """At >80% short, pause sell side."""
    mgr = make_mgr(max_pos=500.0)
    mgr.on_fill("sell", 100_000.0, 0.005)
    assert mgr.should_pause_side("sell") is True
    assert mgr.should_pause_side("buy") is False


def test_should_not_pause_small_position():
    """Small position doesn't trigger pause."""
    mgr = make_mgr(max_pos=500.0)
    mgr.on_fill("buy", 100_000.0, 0.001)  # $100 = 20%
    assert mgr.should_pause_side("buy") is False
    assert mgr.should_pause_side("sell") is False


def test_should_hedge_at_90_pct():
    """Hedge triggered at >90% of max position."""
    mgr = make_mgr(max_pos=500.0)
    mgr.on_fill("buy", 100_000.0, 0.005)  # $500 = 100%
    assert mgr.should_hedge() is True


def test_should_not_hedge_below_threshold():
    """No hedge needed below 90%."""
    mgr = make_mgr(max_pos=500.0)
    mgr.on_fill("buy", 100_000.0, 0.002)  # $200 = 40%
    assert mgr.should_hedge() is False


# ── Unrealized PnL ───────────────────────────────────────────


def test_unrealized_pnl_long():
    mgr = make_mgr()
    mgr.on_fill("buy", 100_000.0, 0.01)
    mgr.update_unrealized(101_000.0)
    assert mgr.state.unrealized_pnl == pytest.approx(10.0, abs=0.01)


def test_unrealized_pnl_flat():
    mgr = make_mgr()
    mgr.update_unrealized(100_000.0)
    assert mgr.state.unrealized_pnl == 0.0


def test_total_pnl():
    """Total PnL = realized + unrealized - fees."""
    mgr = make_mgr()
    mgr.on_fill("buy", 100_000.0, 0.02, fee=-0.03)     # rebate
    mgr.on_fill("sell", 101_000.0, 0.01, fee=0.045)     # realize $10
    mgr.update_unrealized(102_000.0)  # unrealized on remaining 0.01 = $20
    # total = realized(10) + unrealized(20) - fees(-0.03+0.045=0.015)
    # = 10 + 20 - 0.015 = 29.985
    assert mgr.total_pnl == pytest.approx(29.985, abs=0.01)
