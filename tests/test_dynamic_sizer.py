"""Tests for DynamicSizer — pure computation, no mocks needed."""

import pytest
from bot_mm.ml.dynamic_sizer import DynamicSizer


# Neutral inputs: vol=avg, fill_rate mid-range, no inventory/toxicity/drawdown
NEUTRAL = dict(
    current_vol=0.01,
    avg_vol=0.01,
    fill_rate=0.50,
    inventory_pct=0.0,
    toxicity_score=0.25,  # between 0.2 and 0.4 -> no factor
    drawdown_pct=0.0,
)


class TestBasicSizing:
    def test_base_size_no_conditions(self):
        """Neutral inputs -> returns base_size (clamped to capital*max_size_pct)."""
        ds = DynamicSizer(base_size_usd=100.0, capital_usd=10000.0)
        size = ds.compute_size(**NEUTRAL)
        assert size == 100.0

    def test_capital_proportional(self):
        """equity=10000 -> base size = 10000 * 0.15 = 1500."""
        ds = DynamicSizer(capital_usd=50000.0)
        size = ds.compute_size(**{**NEUTRAL, "equity": 10000.0})
        assert size == 1500.0


class TestVolatility:
    def test_low_vol_increases_size(self):
        """vol_ratio=0.5 -> size > base."""
        ds = DynamicSizer(base_size_usd=100.0, capital_usd=10000.0)
        size = ds.compute_size(
            current_vol=0.005, avg_vol=0.01,
            fill_rate=0.50, inventory_pct=0.0,
            toxicity_score=0.25,
        )
        assert size > 100.0

    def test_high_vol_decreases_size(self):
        """vol_ratio=2.0 -> size < base."""
        ds = DynamicSizer(base_size_usd=100.0, capital_usd=10000.0)
        size = ds.compute_size(
            current_vol=0.02, avg_vol=0.01,
            fill_rate=0.50, inventory_pct=0.0,
            toxicity_score=0.25,
        )
        assert size < 100.0

    def test_vol_scaling_disabled(self):
        """vol_scale=False -> vol has no effect."""
        ds = DynamicSizer(base_size_usd=100.0, capital_usd=10000.0, vol_scale=False)
        size_low = ds.compute_size(
            current_vol=0.005, avg_vol=0.01,
            fill_rate=0.50, inventory_pct=0.0,
            toxicity_score=0.25,
        )
        size_high = ds.compute_size(
            current_vol=0.02, avg_vol=0.01,
            fill_rate=0.50, inventory_pct=0.0,
            toxicity_score=0.25,
        )
        assert size_low == size_high


class TestFillRate:
    def test_high_fill_rate_bonus(self):
        """fill_rate=0.9 -> size slightly larger."""
        ds = DynamicSizer(base_size_usd=100.0, capital_usd=10000.0)
        size = ds.compute_size(**{**NEUTRAL, "fill_rate": 0.9})
        assert size > 100.0

    def test_low_fill_rate_penalty(self):
        """fill_rate=0.1 -> size smaller."""
        ds = DynamicSizer(base_size_usd=100.0, capital_usd=10000.0)
        size = ds.compute_size(**{**NEUTRAL, "fill_rate": 0.1})
        assert size < 100.0


class TestInventory:
    def test_heavy_inventory_reduces(self):
        """inventory_pct=0.8 -> size * 0.70."""
        ds = DynamicSizer(base_size_usd=100.0, capital_usd=10000.0)
        size = ds.compute_size(**{**NEUTRAL, "inventory_pct": 0.8})
        assert size == round(100.0 * 0.70, 2)

    def test_moderate_inventory(self):
        """inventory_pct=0.6 -> size * 0.85."""
        ds = DynamicSizer(base_size_usd=100.0, capital_usd=10000.0)
        size = ds.compute_size(**{**NEUTRAL, "inventory_pct": 0.6})
        assert size == round(100.0 * 0.85, 2)


class TestToxicity:
    def test_high_toxicity_reduces(self):
        """toxicity=0.7 -> size * 0.70."""
        ds = DynamicSizer(base_size_usd=100.0, capital_usd=10000.0)
        size = ds.compute_size(**{**NEUTRAL, "toxicity_score": 0.7})
        assert size == round(100.0 * 0.70, 2)

    def test_low_toxicity_bonus(self):
        """toxicity=0.1 -> size * 1.05."""
        ds = DynamicSizer(base_size_usd=100.0, capital_usd=10000.0)
        size = ds.compute_size(**{**NEUTRAL, "toxicity_score": 0.1})
        assert size == round(100.0 * 1.05, 2)


class TestDrawdown:
    def test_severe_drawdown_halves(self):
        """drawdown_pct=0.8 -> size * 0.50."""
        ds = DynamicSizer(base_size_usd=100.0, capital_usd=10000.0)
        size = ds.compute_size(**{**NEUTRAL, "drawdown_pct": 0.8})
        assert size == round(100.0 * 0.50, 2)


class TestStreaks:
    def test_win_streak_bonus(self):
        """5 wins -> size * 1.15."""
        ds = DynamicSizer(base_size_usd=100.0, capital_usd=10000.0)
        for _ in range(5):
            ds.record_fill(0.5)
        size = ds.compute_size(**NEUTRAL)
        assert size == round(100.0 * 1.15, 2)

    def test_lose_streak_penalty(self):
        """5 losses -> size * 0.70."""
        ds = DynamicSizer(base_size_usd=100.0, capital_usd=10000.0)
        for _ in range(5):
            ds.record_fill(-0.5)
        size = ds.compute_size(**NEUTRAL)
        assert size == round(100.0 * 0.70, 2)

    def test_record_fill_updates_streaks(self):
        """Verify streak tracking logic."""
        ds = DynamicSizer()
        ds.record_fill(1.0)
        ds.record_fill(1.0)
        assert ds._win_streak == 2
        assert ds._lose_streak == 0

        ds.record_fill(-1.0)
        assert ds._win_streak == 0
        assert ds._lose_streak == 1

        ds.record_fill(-1.0)
        ds.record_fill(-1.0)
        assert ds._lose_streak == 3

        ds.record_fill(0.0)  # zero PnL counts as win
        assert ds._win_streak == 1
        assert ds._lose_streak == 0


class TestClamping:
    def test_clamp_min(self):
        """Extreme conditions don't go below min_size."""
        ds = DynamicSizer(
            base_size_usd=25.0,
            capital_usd=100000.0,
            min_size_usd=20.0,
        )
        # Stack worst conditions: high vol, low fill, heavy inventory,
        # high toxicity, severe drawdown, losing streak
        for _ in range(5):
            ds.record_fill(-1.0)
        size = ds.compute_size(
            current_vol=0.02, avg_vol=0.01,
            fill_rate=0.05, inventory_pct=0.9,
            toxicity_score=0.9, drawdown_pct=0.9,
        )
        assert size == 20.0

    def test_clamp_max(self):
        """Extreme conditions don't exceed max_size_pct * capital."""
        ds = DynamicSizer(
            base_size_usd=500.0,
            capital_usd=1000.0,  # max = 1000*0.15 = 150
        )
        # Stack best conditions
        for _ in range(5):
            ds.record_fill(1.0)
        size = ds.compute_size(
            current_vol=0.005, avg_vol=0.01,
            fill_rate=0.95, inventory_pct=0.0,
            toxicity_score=0.0, drawdown_pct=0.0,
        )
        assert size == 150.0  # clamped to capital * max_size_pct


class TestCompound:
    def test_compound_factors(self):
        """Multiple bad conditions combine multiplicatively."""
        ds = DynamicSizer(base_size_usd=100.0, capital_usd=10000.0)
        # High vol (factor ~0.7) + heavy inventory (0.70) + high toxicity (0.70)
        size = ds.compute_size(
            current_vol=0.02, avg_vol=0.01,
            fill_rate=0.50, inventory_pct=0.8,
            toxicity_score=0.7, drawdown_pct=0.0,
        )
        # Each factor multiplies: 100 * 0.7 * 0.70 * 0.70 ≈ 34.3
        assert size < 50.0  # well below base
        assert size >= 20.0  # above min


class TestCapitalUpdates:
    def test_update_capital(self):
        """Changing capital updates bounds."""
        ds = DynamicSizer(base_size_usd=100.0, capital_usd=1000.0)
        # Max is 1000*0.15 = 150, base=100 fits
        size_before = ds.compute_size(**NEUTRAL)
        assert size_before == 100.0

        ds.update_capital(500.0)
        # Now max is 500*0.15 = 75, base=100 gets clamped
        size_after = ds.compute_size(**NEUTRAL)
        assert size_after == 75.0


class TestSummary:
    def test_summary(self):
        """Check summary dict keys and values."""
        ds = DynamicSizer(base_size_usd=100.0, capital_usd=5000.0)
        ds.record_fill(1.0)
        ds.record_fill(1.0)
        ds.record_fill(-0.5)

        s = ds.summary()
        assert set(s.keys()) == {
            "base_size", "capital", "win_streak", "lose_streak",
            "win_rate", "recent_fills",
        }
        assert s["base_size"] == 100.0
        assert s["capital"] == 5000.0
        assert s["win_streak"] == 0
        assert s["lose_streak"] == 1
        assert s["win_rate"] == round(2 / 3, 3)
        assert s["recent_fills"] == 3
