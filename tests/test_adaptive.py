"""Tests for AdaptiveMMStrategy — regime detection, spread adjustment, fill rate, inventory decay."""

import pytest
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

from bot_mm.config import AssetMMConfig, QuoteParams, RiskLimits
from bot_mm.core.quoter import Quote
from bot_mm.strategies.adaptive_mm import (
    AdaptiveMMStrategy,
    VolRegime,
    FILL_RATE_SPREAD_ADJ,
    FILL_RATE_TOO_HIGH,
    FILL_RATE_TOO_LOW,
    FILL_TRACK_WINDOW,
    INVENTORY_DECAY_MAX_MULT,
    LOW_VOL_RATIO,
    HIGH_VOL_RATIO,
    REGIME_SPREAD_MULT,
    REGIME_SIZE_MULT,
)


def make_config(**overrides) -> AssetMMConfig:
    quote_kw = overrides.pop("quote", {})
    risk_kw = overrides.pop("risk", {})
    return AssetMMConfig(
        symbol="BTCUSDT",
        quote=QuoteParams(**quote_kw),
        risk=RiskLimits(**risk_kw),
        **overrides,
    )


def make_strategy(
    config: AssetMMConfig = None,
    vol_short: int = 5,
    vol_medium: int = 20,
    vol_long: int = 50,
    decay_candles: int = 30,
) -> AdaptiveMMStrategy:
    """Build an AdaptiveMMStrategy with a mock exchange."""
    if config is None:
        config = make_config()
    exchange = AsyncMock()
    exchange.get_mid_price = AsyncMock(return_value=50000.0)
    exchange.get_position = AsyncMock(return_value={"size": 0.0, "side": "none"})
    exchange.batch_modify_orders = AsyncMock(return_value=[])
    exchange.cancel_all_orders = AsyncMock(return_value=0)

    strategy = AdaptiveMMStrategy(
        exchange=exchange,
        config=config,
        vol_window_short=vol_short,
        vol_window_medium=vol_medium,
        vol_window_long=vol_long,
        inventory_decay_candles=decay_candles,
    )
    return strategy


def _feed_prices(strategy: AdaptiveMMStrategy, prices: list):
    """Feed a sequence of prices to record returns and update _last_mid."""
    for p in prices:
        strategy._record_return(p)
        strategy._last_mid = p


# ═══════════════════════════════════════════════════════════════
# 1. Regime detection
# ═══════════════════════════════════════════════════════════════


class TestRegimeDetection:

    def test_normal_regime_insufficient_data(self):
        """With insufficient data, regime defaults to NORMAL."""
        s = make_strategy()
        assert s.detect_regime() == VolRegime.NORMAL

    def test_normal_regime_stable_prices(self):
        """Stable returns → short_vol ≈ long_vol → NORMAL."""
        s = make_strategy(vol_short=5, vol_long=50)
        # Feed uniform random-ish returns across full long window
        import random
        random.seed(42)
        prices = [50000 + random.gauss(0, 50) for _ in range(60)]
        _feed_prices(s, prices)
        regime = s.detect_regime()
        assert regime == VolRegime.NORMAL

    def test_low_vol_regime(self):
        """Short vol much lower than long vol → LOW regime."""
        s = make_strategy(vol_short=5, vol_long=10)
        # First: volatile period (big swings for long window)
        volatile = []
        for i in range(15):
            volatile.append(50000 + (500 if i % 2 == 0 else -500))
        _feed_prices(s, volatile)
        # Then: calm period (tiny moves for short window)
        calm = [50000 + i * 0.1 for i in range(8)]
        _feed_prices(s, calm)

        regime = s.detect_regime()
        assert regime == VolRegime.LOW

    def test_high_vol_regime(self):
        """Short vol much higher than long vol → HIGH regime."""
        s = make_strategy(vol_short=5, vol_long=50)
        # Long calm period to fill long window with low vol
        calm = [50000 + i * 0.1 for i in range(55)]
        _feed_prices(s, calm)
        # Then: volatile spike in the short window
        spike = []
        for i in range(8):
            spike.append(50000 + (2000 if i % 2 == 0 else -2000))
        _feed_prices(s, spike)

        regime = s.detect_regime()
        assert regime == VolRegime.HIGH


# ═══════════════════════════════════════════════════════════════
# 2. Spread adjustment per regime
# ═══════════════════════════════════════════════════════════════


class TestSpreadAdjustment:

    def _make_quotes(self, mid: float = 50000.0) -> list:
        """Make a simple bid+ask pair around mid."""
        spread_offset = mid * 0.0002  # 2 bps each side
        return [
            Quote(price=mid - spread_offset, size=0.01, side="buy", level=0),
            Quote(price=mid + spread_offset, size=0.01, side="sell", level=0),
        ]

    def test_normal_regime_no_change(self):
        """NORMAL regime doesn't alter spread or size."""
        s = make_strategy()
        s._regime = VolRegime.NORMAL
        mid = 50000.0
        quotes = self._make_quotes(mid)
        adjusted = s.adjust_quotes(quotes, mid, inventory_usd=0.0)

        for orig, adj in zip(quotes, adjusted):
            assert adj.price == pytest.approx(orig.price, rel=1e-9)
            assert adj.size == pytest.approx(orig.size, rel=1e-9)

    def test_low_vol_tightens_spread(self):
        """LOW regime tightens spread (quotes closer to mid)."""
        s = make_strategy()
        s._regime = VolRegime.LOW
        mid = 50000.0
        quotes = self._make_quotes(mid)
        adjusted = s.adjust_quotes(quotes, mid, inventory_usd=0.0)

        bid_orig = quotes[0].price
        bid_adj = adjusted[0].price
        ask_orig = quotes[1].price
        ask_adj = adjusted[1].price

        # Adjusted bid should be closer to mid (higher)
        assert bid_adj > bid_orig
        # Adjusted ask should be closer to mid (lower)
        assert ask_adj < ask_orig

        # Size should be larger
        assert adjusted[0].size > quotes[0].size
        assert adjusted[0].size == pytest.approx(
            quotes[0].size * REGIME_SIZE_MULT[VolRegime.LOW], rel=1e-6
        )

    def test_high_vol_widens_spread(self):
        """HIGH regime widens spread (quotes further from mid)."""
        s = make_strategy()
        s._regime = VolRegime.HIGH
        mid = 50000.0
        quotes = self._make_quotes(mid)
        adjusted = s.adjust_quotes(quotes, mid, inventory_usd=0.0)

        bid_orig = quotes[0].price
        bid_adj = adjusted[0].price
        ask_orig = quotes[1].price
        ask_adj = adjusted[1].price

        # Adjusted bid should be further from mid (lower)
        assert bid_adj < bid_orig
        # Adjusted ask should be further from mid (higher)
        assert ask_adj > ask_orig

        # Size should be smaller
        assert adjusted[0].size < quotes[0].size
        assert adjusted[0].size == pytest.approx(
            quotes[0].size * REGIME_SIZE_MULT[VolRegime.HIGH], rel=1e-6
        )

    def test_spread_multipliers_correct(self):
        """Verify exact spread multiplier values per regime."""
        s = make_strategy()
        mid = 50000.0
        offset = 10.0  # 2 bps
        quotes = [
            Quote(price=mid - offset, size=0.01, side="buy", level=0),
            Quote(price=mid + offset, size=0.01, side="sell", level=0),
        ]

        for regime in VolRegime:
            s._regime = regime
            adjusted = s.adjust_quotes(quotes, mid, inventory_usd=0.0)
            expected_bid = mid - offset * REGIME_SPREAD_MULT[regime]
            expected_ask = mid + offset * REGIME_SPREAD_MULT[regime]
            assert adjusted[0].price == pytest.approx(expected_bid, rel=1e-9), f"Failed for {regime}"
            assert adjusted[1].price == pytest.approx(expected_ask, rel=1e-9), f"Failed for {regime}"


# ═══════════════════════════════════════════════════════════════
# 3. Fill rate tracking
# ═══════════════════════════════════════════════════════════════


class TestFillRateTracking:

    def test_fill_rate_zero_initially(self):
        """Fill rate is 0 with no data."""
        s = make_strategy()
        assert s.fill_rate == 0.0

    def test_fill_rate_calculation(self):
        """Fill rate = total_fills / total_quotes."""
        s = make_strategy()
        # 10 cycles: 1 fill per 2 quotes placed = 50%
        for _ in range(10):
            s.record_fills(num_fills=1, num_quotes=2)
        assert s.fill_rate == pytest.approx(0.5, rel=1e-6)

    def test_fill_rate_low_tightens(self):
        """Fill rate < 20% → spread adj < 1.0 (tighten)."""
        s = make_strategy()
        # 10 cycles: 0 fills, 10 quotes each → 0% fill rate
        for _ in range(10):
            s.record_fills(num_fills=0, num_quotes=10)
        adj = s._fill_rate_spread_adj()
        assert adj < 1.0
        assert adj == pytest.approx(1.0 - FILL_RATE_SPREAD_ADJ, rel=1e-6)

    def test_fill_rate_high_widens(self):
        """Fill rate > 60% → spread adj > 1.0 (widen)."""
        s = make_strategy()
        # 10 cycles: 8 fills, 10 quotes each → 80% fill rate
        for _ in range(10):
            s.record_fills(num_fills=8, num_quotes=10)
        adj = s._fill_rate_spread_adj()
        assert adj > 1.0
        assert adj == pytest.approx(1.0 + FILL_RATE_SPREAD_ADJ, rel=1e-6)

    def test_fill_rate_normal_no_adj(self):
        """Fill rate between 20-60% → no adjustment."""
        s = make_strategy()
        # 10 cycles: 4 fills, 10 quotes each → 40% fill rate
        for _ in range(10):
            s.record_fills(num_fills=4, num_quotes=10)
        adj = s._fill_rate_spread_adj()
        assert adj == 1.0

    def test_fill_rate_insufficient_data(self):
        """With < 5 data points, no adjustment."""
        s = make_strategy()
        for _ in range(3):
            s.record_fills(num_fills=0, num_quotes=10)
        adj = s._fill_rate_spread_adj()
        assert adj == 1.0

    def test_fill_rate_rolling_window(self):
        """Fill rate uses a rolling window of FILL_TRACK_WINDOW cycles."""
        s = make_strategy()
        # Fill entire window with high fill rate
        for _ in range(FILL_TRACK_WINDOW):
            s.record_fills(num_fills=9, num_quotes=10)
        assert s.fill_rate > FILL_RATE_TOO_HIGH

        # Now push low fill rate, old data falls off
        for _ in range(FILL_TRACK_WINDOW):
            s.record_fills(num_fills=0, num_quotes=10)
        assert s.fill_rate < FILL_RATE_TOO_LOW


# ═══════════════════════════════════════════════════════════════
# 4. Inventory decay
# ═══════════════════════════════════════════════════════════════


class TestInventoryDecay:

    def test_no_decay_when_flat(self):
        """No decay multiplier when inventory is zero."""
        s = make_strategy(decay_candles=10)
        for _ in range(50):
            s._update_inventory_age(0.0)
        assert s._inventory_decay_mult("buy", 0.0) == 1.0
        assert s._inventory_decay_mult("sell", 0.0) == 1.0

    def test_no_decay_before_threshold(self):
        """No decay before inventory_decay_candles exceeded."""
        s = make_strategy(decay_candles=10)
        # First call sets the sign (count stays 0), subsequent calls increment
        for _ in range(10):
            s._update_inventory_age(100.0)
        assert s._inventory_unchanged_count == 9
        assert s._inventory_decay_mult("buy", 100.0) == 1.0

    def test_decay_after_threshold_long(self):
        """Long inventory past threshold → buy side widens."""
        s = make_strategy(decay_candles=10)
        for _ in range(20):
            s._update_inventory_age(100.0)

        # Buy side should be widened (discourage adding to long)
        buy_mult = s._inventory_decay_mult("buy", 100.0)
        assert buy_mult > 1.0

        # Sell side stays normal (encourage reducing long)
        sell_mult = s._inventory_decay_mult("sell", 100.0)
        assert sell_mult == 1.0

    def test_decay_after_threshold_short(self):
        """Short inventory past threshold → sell side widens."""
        s = make_strategy(decay_candles=10)
        for _ in range(20):
            s._update_inventory_age(-100.0)

        sell_mult = s._inventory_decay_mult("sell", -100.0)
        assert sell_mult > 1.0

        buy_mult = s._inventory_decay_mult("buy", -100.0)
        assert buy_mult == 1.0

    def test_decay_resets_on_side_change(self):
        """Inventory age resets when position changes sign."""
        s = make_strategy(decay_candles=5)
        # First call sets sign (count=0), next 9 increment → count=9
        for _ in range(10):
            s._update_inventory_age(100.0)
        assert s._inventory_unchanged_count == 9

        # Flip to short — resets count and sets new sign
        s._update_inventory_age(-100.0)
        assert s._inventory_unchanged_count == 0

    def test_decay_max_multiplier(self):
        """Decay multiplier is capped at INVENTORY_DECAY_MAX_MULT."""
        s = make_strategy(decay_candles=10)
        for _ in range(1000):
            s._update_inventory_age(100.0)

        buy_mult = s._inventory_decay_mult("buy", 100.0)
        assert buy_mult == pytest.approx(INVENTORY_DECAY_MAX_MULT, rel=1e-6)

    def test_decay_gradual_progression(self):
        """Decay multiplier increases gradually after threshold."""
        s = make_strategy(decay_candles=10)
        mults = []
        for i in range(25):
            s._update_inventory_age(100.0)
            mults.append(s._inventory_decay_mult("buy", 100.0))

        # First 10 should all be 1.0
        for m in mults[:10]:
            assert m == 1.0

        # After 10, should increase monotonically
        for j in range(11, len(mults) - 1):
            assert mults[j + 1] >= mults[j]


# ═══════════════════════════════════════════════════════════════
# 5. Integration — adjust_quotes combines all effects
# ═══════════════════════════════════════════════════════════════


class TestAdjustQuotesIntegration:

    def test_combined_regime_and_fill_rate(self):
        """HIGH regime + high fill rate → double-widen effect."""
        s = make_strategy()
        s._regime = VolRegime.HIGH
        for _ in range(10):
            s.record_fills(num_fills=8, num_quotes=10)

        mid = 50000.0
        offset = 10.0
        quotes = [
            Quote(price=mid - offset, size=0.01, side="buy", level=0),
            Quote(price=mid + offset, size=0.01, side="sell", level=0),
        ]
        adjusted = s.adjust_quotes(quotes, mid, inventory_usd=0.0)

        # Both regime (1.5) and fill rate (1.15) widen
        expected_mult = REGIME_SPREAD_MULT[VolRegime.HIGH] * (1.0 + FILL_RATE_SPREAD_ADJ)
        expected_bid = mid - offset * expected_mult
        assert adjusted[0].price == pytest.approx(expected_bid, rel=1e-6)

    def test_combined_with_inventory_decay(self):
        """Regime + fill rate + inventory decay all stack."""
        s = make_strategy(decay_candles=5)
        s._regime = VolRegime.NORMAL
        # Enough fill data for neutral adjustment
        for _ in range(10):
            s.record_fills(num_fills=4, num_quotes=10)
        # Stale long inventory
        for _ in range(15):
            s._update_inventory_age(500.0)

        mid = 50000.0
        offset = 10.0
        quotes = [
            Quote(price=mid - offset, size=0.01, side="buy", level=0),
            Quote(price=mid + offset, size=0.01, side="sell", level=0),
        ]
        adjusted = s.adjust_quotes(quotes, mid, inventory_usd=500.0)

        # Buy side should be widened (decay), sell should not
        buy_offset = mid - adjusted[0].price
        sell_offset = adjusted[1].price - mid
        assert buy_offset > sell_offset  # Buy widened more due to decay
