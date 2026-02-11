import pytest
from bot_mm.ml.toxicity import ToxicityDetector, FillRecord


def _make_detector(**kwargs):
    """Helper to create detector with short measurement window for tests."""
    defaults = dict(
        lookback_fills=50,
        measurement_bars=5,
        ema_alpha=0.1,
        high_toxicity=0.6,
        medium_toxicity=0.4,
        low_toxicity=0.2,
    )
    defaults.update(kwargs)
    return ToxicityDetector(**defaults)


def _advance_bars(det, n, mid_price, atr=10.0):
    """Advance N bars at constant mid price."""
    for _ in range(n):
        det.on_bar(mid_price, atr=atr)


class TestInitialState:
    def test_initial_toxicity_neutral(self):
        det = _make_detector()
        assert det.overall_toxicity == 0.3
        assert det.buy_toxicity == 0.3
        assert det.sell_toxicity == 0.3
        assert det.fills_measured == 0


class TestFillRecording:
    def test_on_fill_creates_pending(self):
        det = _make_detector()
        det.on_fill("buy", 100.0, 100.0, 1.0, "t1")
        assert len(det._pending_fills) == 1
        assert det._pending_fills[0].side == "buy"
        assert det._pending_fills[0].fill_price == 100.0
        assert det.fills_measured == 0

    def test_on_bar_measures_pending(self):
        det = _make_detector(measurement_bars=3)
        det.on_fill("buy", 100.0, 100.0, 1.0)
        # Advance 3 bars — fill should complete at bar 3
        for i in range(3):
            det.on_bar(100.0, atr=10.0)
        assert det.fills_measured == 1
        assert len(det._pending_fills) == 0
        assert len(det._completed_fills) == 1


class TestToxicBuyFill:
    def test_toxic_buy_fill(self):
        """Price drops after buy = toxic (informed seller hit our bid)."""
        det = _make_detector(measurement_bars=3, ema_alpha=1.0)
        det.on_fill("buy", 100.0, 100.0, 1.0)
        # Price drops by 1 ATR after fill
        det.on_bar(99.0, atr=10.0)   # bar 1
        det.on_bar(95.0, atr=10.0)   # bar 2
        det.on_bar(90.0, atr=10.0)   # bar 3 — measurement
        assert det.fills_measured == 1
        fill = det._completed_fills[0]
        # adverse move = 100 - 90 = 10, normalized by ATR 10 = 1.0
        assert fill.toxicity_score == 1.0
        assert det.buy_toxicity == 1.0


class TestBenignBuyFill:
    def test_benign_buy_fill(self):
        """Price rises after buy = benign (we profit)."""
        det = _make_detector(measurement_bars=3, ema_alpha=1.0)
        det.on_fill("buy", 100.0, 100.0, 1.0)
        det.on_bar(101.0, atr=10.0)
        det.on_bar(103.0, atr=10.0)
        det.on_bar(105.0, atr=10.0)  # measurement
        assert det.fills_measured == 1
        fill = det._completed_fills[0]
        # move = 100 - 105 = -5 → clamped to 0
        assert fill.toxicity_score == 0.0
        assert det.buy_toxicity == 0.0


class TestToxicSellFill:
    def test_toxic_sell_fill(self):
        """Price rises after sell = toxic (informed buyer hit our ask)."""
        det = _make_detector(measurement_bars=3, ema_alpha=1.0)
        det.on_fill("sell", 100.0, 100.0, 1.0)
        det.on_bar(102.0, atr=10.0)
        det.on_bar(105.0, atr=10.0)
        det.on_bar(110.0, atr=10.0)  # measurement
        assert det.fills_measured == 1
        fill = det._completed_fills[0]
        # adverse move = 110 - 100 = 10, / ATR 10 = 1.0
        assert fill.toxicity_score == 1.0
        assert det.sell_toxicity == 1.0


class TestBenignSellFill:
    def test_benign_sell_fill(self):
        """Price drops after sell = benign (we profit)."""
        det = _make_detector(measurement_bars=3, ema_alpha=1.0)
        det.on_fill("sell", 100.0, 100.0, 1.0)
        det.on_bar(99.0, atr=10.0)
        det.on_bar(97.0, atr=10.0)
        det.on_bar(95.0, atr=10.0)  # measurement
        assert det.fills_measured == 1
        fill = det._completed_fills[0]
        # move = 95 - 100 = -5 → clamped to 0
        assert fill.toxicity_score == 0.0
        assert det.sell_toxicity == 0.0


class TestEmaUpdates:
    def test_ema_updates_on_measurement(self):
        """EMA should blend old and new toxicity scores."""
        det = _make_detector(measurement_bars=2, ema_alpha=0.5)
        # First fill: fully toxic
        det.on_fill("buy", 100.0, 100.0, 1.0)
        det.on_bar(95.0, atr=10.0)
        det.on_bar(90.0, atr=10.0)  # score = 1.0
        # EMA: 0.5 * 1.0 + 0.5 * 0.3 = 0.65
        assert det.buy_toxicity == pytest.approx(0.65, abs=1e-6)

        # Second fill: benign
        det.on_fill("buy", 100.0, 100.0, 1.0)
        det.on_bar(105.0, atr=10.0)
        det.on_bar(110.0, atr=10.0)  # score = 0.0
        # EMA: 0.5 * 0.0 + 0.5 * 0.65 = 0.325
        assert det.buy_toxicity == pytest.approx(0.325, abs=1e-6)


class TestSpreadMultipliers:
    def test_spread_multiplier_high_toxicity(self):
        det = _make_detector()
        det._overall_toxicity = 0.7
        assert det.get_spread_multiplier("both") == 1.5

    def test_spread_multiplier_low_toxicity(self):
        det = _make_detector()
        det._overall_toxicity = 0.1
        assert det.get_spread_multiplier("both") == 0.9

    def test_spread_multiplier_neutral(self):
        det = _make_detector()
        det._overall_toxicity = 0.3
        assert det.get_spread_multiplier("both") == 1.0

    def test_spread_multiplier_medium(self):
        det = _make_detector()
        det._overall_toxicity = 0.5
        assert det.get_spread_multiplier("both") == 1.25


class TestSideSpecific:
    def test_side_specific_multipliers(self):
        """Buy side toxic, sell side benign → asymmetric multipliers."""
        det = _make_detector()
        det._buy_toxicity = 0.7   # High → 1.5
        det._sell_toxicity = 0.1  # Low → 0.9
        buy_m, sell_m = det.get_side_multipliers()
        assert buy_m == 1.5
        assert sell_m == 0.9


class TestSummary:
    def test_summary_stats(self):
        det = _make_detector(measurement_bars=2, ema_alpha=1.0)
        # Add a toxic buy fill
        det.on_fill("buy", 100.0, 100.0, 1.0, "t1")
        det.on_bar(95.0, atr=10.0)
        det.on_bar(90.0, atr=10.0)  # score = 1.0
        # Add a benign sell fill
        det.on_fill("sell", 100.0, 100.0, 1.0, "t2")
        det.on_bar(95.0, atr=10.0)
        det.on_bar(90.0, atr=10.0)  # score = 0.0 (price dropped after sell)

        s = det.summary()
        assert s["fills_measured"] == 2
        assert s["pending_fills"] == 0
        assert s["avg_toxicity"] == pytest.approx(0.5, abs=1e-6)
        assert s["toxic_fills_pct"] == pytest.approx(50.0, abs=1e-6)
        assert "overall_toxicity" in s
        assert "buy_toxicity" in s
        assert "sell_toxicity" in s
        assert "spread_mult_buy" in s
        assert "spread_mult_sell" in s
