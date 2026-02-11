"""Tests for FillPredictor and FillDataGenerator."""

import math
import os
import tempfile
from dataclasses import dataclass

import numpy as np
import pytest

from bot_mm.ml.fill_predictor import FillPredictor, FEATURE_NAMES
from bot_mm.ml.data_generator import FillDataGenerator


# ── Helpers ─────────────────────────────────────────────────


@dataclass
class FakeCandle:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float


def make_candles(n: int = 100, base_price: float = 50000.0, seed: int = 42) -> list:
    """Generate synthetic candles for testing."""
    rng = np.random.RandomState(seed)
    candles = []
    price = base_price
    for i in range(n):
        ret = rng.normal(0, 0.005)
        o = price
        c = price * (1 + ret)
        h = max(o, c) * (1 + abs(rng.normal(0, 0.002)))
        l = min(o, c) * (1 - abs(rng.normal(0, 0.002)))
        vol = rng.uniform(100, 1000)
        ts = f"2025-01-{(i // 24) + 1:02d} {i % 24:02d}:00:00"
        candles.append(FakeCandle(timestamp=ts, open=o, high=h, low=l, close=c, volume=vol))
        price = c
    return candles


def _make_features(predictor: FillPredictor, **overrides) -> dict:
    """Make features with defaults, allowing overrides."""
    candle = FakeCandle("2025-01-05 12:00:00", 50000, 50200, 49800, 50100, 500)
    prev = FakeCandle("2025-01-05 11:00:00", 49900, 50050, 49850, 50000, 450)
    defaults = dict(
        candle=candle,
        prev_candle=prev,
        mid_price=50000.0,
        quote_price=49990.0,
        quote_side="buy",
        volatility_pct=0.005,
        inventory_ratio=0.0,
        vol_regime=1.0,
        candle_idx=25,
        volume_mean=500.0,
        volume_std=100.0,
        closes=np.array([50000.0] * 30),
    )
    defaults.update(overrides)
    return predictor.extract_features(**defaults)


def _train_predictor(n_samples: int = 500) -> FillPredictor:
    """Create and train a predictor on synthetic data."""
    rng = np.random.RandomState(42)
    n_features = len(FEATURE_NAMES)
    X = rng.randn(n_samples, n_features)
    # distance feature (col 0): larger distance → less likely to fill
    X[:, 0] = rng.uniform(0, 30, n_samples)
    y_fill = (X[:, 0] < 10).astype(int) | (rng.random(n_samples) < 0.1).astype(int)
    y_adverse = (y_fill & (rng.random(n_samples) < 0.3)).astype(int)

    predictor = FillPredictor()
    predictor.train(X, y_fill, y_adverse)
    return predictor


# ── Tests ───────────────────────────────────────────────────


class TestFeatureExtraction:
    def test_feature_extraction_shape(self):
        """extract_features returns dict with correct number of features."""
        predictor = FillPredictor()
        features = _make_features(predictor)
        assert len(features) == len(FEATURE_NAMES)
        for name in FEATURE_NAMES:
            assert name in features

    def test_feature_extraction_values(self):
        """Known inputs produce expected feature values."""
        predictor = FillPredictor()
        candle = FakeCandle("2025-01-05 12:00:00", 50000, 50200, 49800, 50100, 500)
        prev = FakeCandle("2025-01-05 11:00:00", 49900, 50050, 49850, 50000, 450)

        features = predictor.extract_features(
            candle=candle,
            prev_candle=prev,
            mid_price=50000.0,
            quote_price=49950.0,  # 10 bps below mid
            quote_side="buy",
            volatility_pct=0.005,
            inventory_ratio=0.3,
            vol_regime=1.2,
            candle_idx=25,
            volume_mean=500.0,
            volume_std=100.0,
            closes=np.array([50000.0] * 30),
        )

        assert features["distance_to_mid_bps"] == pytest.approx(10.0, abs=0.01)
        assert features["side_is_buy"] == 1.0
        assert features["volatility_pct"] == 0.005
        assert features["inventory_ratio"] == 0.3
        assert features["vol_regime"] == 1.2
        # candle range: (50200-49800)/50000 * 10000 = 80 bps
        assert features["candle_range_bps"] == pytest.approx(80.0, abs=0.01)
        # volume zscore: (500-500)/100 = 0
        assert features["volume_zscore"] == pytest.approx(0.0, abs=0.01)
        # prev return: 50000/49900 - 1 ≈ 0.002
        assert features["prev_candle_return"] == pytest.approx(0.002004, abs=0.001)
        # hour=12 → sin(2π*12/24) = sin(π) ≈ 0
        assert features["hour_sin"] == pytest.approx(0.0, abs=0.01)
        # hour=12 → cos(2π*12/24) = cos(π) = -1
        assert features["hour_cos"] == pytest.approx(-1.0, abs=0.01)

    def test_sell_side_feature(self):
        """Side feature is 0 for sell quotes."""
        predictor = FillPredictor()
        features = _make_features(predictor, quote_side="sell")
        assert features["side_is_buy"] == 0.0


class TestPrediction:
    def test_untrained_raises(self):
        """Predict before training raises RuntimeError."""
        predictor = FillPredictor()
        features = _make_features(predictor)
        with pytest.raises(RuntimeError, match="not trained"):
            predictor.predict(features)

    def test_train_and_predict(self):
        """Train on synthetic data, predict returns tuple of two floats."""
        predictor = _train_predictor()
        features = _make_features(predictor)
        fill_prob, adverse_prob = predictor.predict(features)
        assert isinstance(fill_prob, float)
        assert isinstance(adverse_prob, float)

    def test_fill_prob_range(self):
        """Fill probability is between 0 and 1."""
        predictor = _train_predictor()
        features = _make_features(predictor)
        fill_prob, _ = predictor.predict(features)
        assert 0.0 <= fill_prob <= 1.0

    def test_adverse_prob_range(self):
        """Adverse probability is between 0 and 1."""
        predictor = _train_predictor()
        features = _make_features(predictor)
        _, adverse_prob = predictor.predict(features)
        assert 0.0 <= adverse_prob <= 1.0

    def test_closer_quotes_higher_fill(self):
        """Quotes closer to mid should have higher fill probability (on average)."""
        predictor = _train_predictor(n_samples=2000)

        # Close quote (2 bps)
        feat_close = _make_features(predictor, quote_price=50000 * (1 - 2 / 10000))
        fill_close, _ = predictor.predict(feat_close)

        # Far quote (25 bps)
        feat_far = _make_features(predictor, quote_price=50000 * (1 - 25 / 10000))
        fill_far, _ = predictor.predict(feat_far)

        # Close quote should have higher fill probability
        assert fill_close > fill_far

    def test_is_trained_property(self):
        """is_trained is False before training, True after."""
        predictor = FillPredictor()
        assert predictor.is_trained is False
        predictor = _train_predictor()
        assert predictor.is_trained is True


class TestSaveLoad:
    def test_save_load(self):
        """Save model, load it, predictions match."""
        predictor = _train_predictor()
        features = _make_features(predictor)
        fill_orig, adverse_orig = predictor.predict(features)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test_model.joblib")
            predictor.save(path)
            assert os.path.exists(path)

            loaded = FillPredictor()
            loaded.load(path)
            assert loaded.is_trained

            fill_loaded, adverse_loaded = loaded.predict(features)
            assert fill_loaded == pytest.approx(fill_orig, abs=1e-10)
            assert adverse_loaded == pytest.approx(adverse_orig, abs=1e-10)

    def test_save_untrained_raises(self):
        """Saving untrained model raises RuntimeError."""
        predictor = FillPredictor()
        with pytest.raises(RuntimeError):
            predictor.save("dummy.joblib")


class TestDataGenerator:
    def test_data_generator_shape(self):
        """Output arrays have correct dimensions."""
        candles = make_candles(100)
        gen = FillDataGenerator()
        X, y_fill, y_adverse = gen.generate(candles, quote_distances_bps=[5, 10])

        usable = 100 - max(gen.atr_period, 21)
        expected_samples = usable * 2 * 2  # 2 distances × 2 sides
        assert X.shape == (expected_samples, len(FEATURE_NAMES))
        assert y_fill.shape == (expected_samples,)
        assert y_adverse.shape == (expected_samples,)

    def test_data_generator_labels(self):
        """Labels are binary (0 or 1)."""
        candles = make_candles(60)
        gen = FillDataGenerator()
        X, y_fill, y_adverse = gen.generate(candles, quote_distances_bps=[5])

        assert set(np.unique(y_fill)).issubset({0, 1})
        assert set(np.unique(y_adverse)).issubset({0, 1})

    def test_data_generator_adverse_subset_of_fill(self):
        """Adverse selection can only happen on filled quotes."""
        candles = make_candles(100)
        gen = FillDataGenerator()
        X, y_fill, y_adverse = gen.generate(candles)

        # Every adverse sample must also be a fill
        assert np.all(y_adverse <= y_fill)

    def test_data_generator_too_few_candles(self):
        """Raises ValueError if not enough candles."""
        candles = make_candles(10)
        gen = FillDataGenerator()
        with pytest.raises(ValueError, match="Need at least"):
            gen.generate(candles)

    def test_feature_importance(self):
        """Feature importance dict has correct keys after training."""
        predictor = _train_predictor()
        fi = predictor.feature_importance()
        assert set(fi.keys()) == set(FEATURE_NAMES)
        assert all(v >= 0 for v in fi.values())
        assert abs(sum(fi.values()) - 1.0) < 0.01
