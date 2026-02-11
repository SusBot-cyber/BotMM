"""Tests for QuoteEngine — spread, skew, multi-level quoting."""

import pytest
from bot_mm.config import QuoteParams
from bot_mm.core.quoter import QuoteEngine, Quote


def make_engine(**overrides) -> QuoteEngine:
    params = QuoteParams(**overrides)
    return params, QuoteEngine(params)


# ── Spread calculation ──────────────────────────────────────


def test_spread_base_only():
    """Spread with zero vol and zero inventory equals base spread."""
    params, engine = make_engine(base_spread_bps=2.0)
    spread = engine._calc_spread(volatility_pct=0.0, inventory_usd=0.0, max_position_usd=500.0)
    assert spread == 2.0


def test_spread_vol_adjustment():
    """Volatility widens the spread."""
    params, engine = make_engine(base_spread_bps=2.0, vol_multiplier=1.5, max_spread_bps=100.0)
    spread = engine._calc_spread(volatility_pct=0.005, inventory_usd=0.0, max_position_usd=500.0)
    # base(2.0) + vol(0.005 * 10000 * 1.5 = 75.0) = 77.0
    assert spread == pytest.approx(77.0, abs=0.01)


def test_spread_inventory_penalty():
    """Holding inventory widens spread."""
    params, engine = make_engine(base_spread_bps=2.0)
    spread_flat = engine._calc_spread(0.0, inventory_usd=0.0, max_position_usd=500.0)
    spread_loaded = engine._calc_spread(0.0, inventory_usd=500.0, max_position_usd=500.0)
    # At max inventory: penalty = 1.0 * 2.0 = 2.0 bps
    assert spread_loaded > spread_flat
    assert spread_loaded == pytest.approx(4.0, abs=0.01)


def test_spread_clamped_to_min():
    """Spread never goes below min_spread_bps."""
    params, engine = make_engine(base_spread_bps=0.1, min_spread_bps=1.0, max_spread_bps=50.0)
    spread = engine._calc_spread(0.0, 0.0, 500.0)
    assert spread == 1.0


def test_spread_clamped_to_max():
    """Spread never exceeds max_spread_bps."""
    params, engine = make_engine(base_spread_bps=5.0, max_spread_bps=10.0, vol_multiplier=1.5)
    spread = engine._calc_spread(volatility_pct=0.01, inventory_usd=0.0, max_position_usd=500.0)
    # base(5) + vol(0.01*10000*1.5=150) = 155 → clamped to 10
    assert spread == 10.0


# ── Skew ────────────────────────────────────────────────────


def test_skew_zero_inventory():
    """No inventory → no skew."""
    _, engine = make_engine(inventory_skew_factor=0.5)
    skew = engine._calc_skew(inventory_usd=0.0, max_position_usd=500.0, volatility_pct=0.005)
    assert skew == 0.0


def test_skew_long_inventory():
    """Long inventory → positive skew → lower bid/ask (incentivize sells)."""
    _, engine = make_engine(inventory_skew_factor=0.5)
    skew = engine._calc_skew(inventory_usd=250.0, max_position_usd=500.0, volatility_pct=0.005)
    # inv_ratio=0.5, skew = 0.5 * 0.5 * 0.005 * 10000 = 12.5 bps
    assert skew == pytest.approx(12.5, abs=0.01)
    assert skew > 0  # positive = shift quotes down


def test_skew_short_inventory():
    """Short inventory → negative skew → higher bid/ask (incentivize buys)."""
    _, engine = make_engine(inventory_skew_factor=0.5)
    skew = engine._calc_skew(inventory_usd=-250.0, max_position_usd=500.0, volatility_pct=0.005)
    assert skew == pytest.approx(-12.5, abs=0.01)
    assert skew < 0


def test_skew_shifts_quotes():
    """Long inventory shifts both bid and ask down."""
    _, engine = make_engine(
        base_spread_bps=2.0, inventory_skew_factor=0.5,
        vol_multiplier=0.0, num_levels=1,
        order_size_usd=100.0,
    )
    mid = 100_000.0

    quotes_flat = engine.calculate_quotes(mid, 0.005, inventory_usd=0.0, max_position_usd=500.0)
    quotes_long = engine.calculate_quotes(mid, 0.005, inventory_usd=250.0, max_position_usd=500.0)

    bid_flat = [q for q in quotes_flat if q.side == "buy"][0].price
    ask_flat = [q for q in quotes_flat if q.side == "sell"][0].price
    bid_long = [q for q in quotes_long if q.side == "buy"][0].price
    ask_long = [q for q in quotes_long if q.side == "sell"][0].price

    # Long position → quotes shift down (skew subtracted from both)
    assert bid_long < bid_flat
    assert ask_long < ask_flat


# ── Multi-level quoting ─────────────────────────────────────


def test_multi_level_count():
    """3 levels produce 6 quotes (3 bids + 3 asks)."""
    _, engine = make_engine(num_levels=3, order_size_usd=100.0, level_spacing_bps=1.0)
    quotes = engine.calculate_quotes(100_000.0, 0.005, 0.0, 500.0)
    assert len(quotes) == 6


def test_multi_level_spacing():
    """Deeper levels have wider spread from mid."""
    _, engine = make_engine(
        num_levels=3, order_size_usd=100.0,
        level_spacing_bps=2.0, base_spread_bps=2.0,
        vol_multiplier=0.0, inventory_skew_factor=0.0,
    )
    mid = 100_000.0
    quotes = engine.calculate_quotes(mid, 0.0, 0.0, 500.0)
    bids = sorted([q for q in quotes if q.side == "buy"], key=lambda q: -q.price)
    asks = sorted([q for q in quotes if q.side == "sell"], key=lambda q: q.price)

    # Each level should be further from mid
    for i in range(len(bids) - 1):
        assert bids[i].price > bids[i + 1].price
    for i in range(len(asks) - 1):
        assert asks[i].price < asks[i + 1].price


def test_multi_level_sizes():
    """3-level weights: normalized from [50%, 30%, 15%] base weights."""
    _, engine = make_engine(num_levels=3, order_size_usd=100.0)
    mid = 50_000.0
    quotes = engine.calculate_quotes(mid, 0.005, 0.0, 500.0)
    bids = [q for q in quotes if q.side == "buy"]

    size_usd_0 = bids[0].size * mid
    size_usd_1 = bids[1].size * mid
    size_usd_2 = bids[2].size * mid

    # Normalized weights: 0.50/0.95, 0.30/0.95, 0.15/0.95
    assert size_usd_0 == pytest.approx(100.0 * 0.50 / 0.95, rel=0.01)
    assert size_usd_1 == pytest.approx(100.0 * 0.30 / 0.95, rel=0.01)
    assert size_usd_2 == pytest.approx(100.0 * 0.15 / 0.95, rel=0.01)


# ── Edge cases ──────────────────────────────────────────────


def test_zero_volatility():
    """Zero vol → spread equals base (clamped to min if needed)."""
    _, engine = make_engine(base_spread_bps=3.0, vol_multiplier=1.5, min_spread_bps=0.5)
    spread = engine._calc_spread(0.0, 0.0, 500.0)
    assert spread == 3.0


def test_max_inventory_widens_spread():
    """At max inventory, spread is wider than at zero."""
    _, engine = make_engine(base_spread_bps=2.0)
    spread_zero = engine._calc_spread(0.0, 0.0, 500.0)
    spread_max = engine._calc_spread(0.0, 500.0, 500.0)
    assert spread_max > spread_zero


def test_quotes_symmetric_at_zero_inventory():
    """With zero inventory and zero imbalance, bid/ask equidistant from mid."""
    _, engine = make_engine(
        base_spread_bps=4.0, vol_multiplier=0.0,
        inventory_skew_factor=0.0, num_levels=1,
        order_size_usd=100.0,
    )
    mid = 50_000.0
    quotes = engine.calculate_quotes(mid, 0.0, 0.0, 500.0)
    bid = [q for q in quotes if q.side == "buy"][0].price
    ask = [q for q in quotes if q.side == "sell"][0].price

    assert mid - bid == pytest.approx(ask - mid, rel=1e-6)
