"""
Generates labeled training data for FillPredictor from historical candles.

For each candle, simulates quotes at various distances from mid,
and labels whether they would have been filled and whether the fill
was adverse (price moved against us after fill).
"""

import math
from typing import List, Tuple

import numpy as np

from bot_mm.ml.fill_predictor import FillPredictor, FEATURE_NAMES


class FillDataGenerator:
    """
    Generates training data from historical candles.

    For each candle, simulates quotes at various distances from mid,
    and labels whether they would have been filled and whether the fill
    was adverse (price moved against us after fill).
    """

    def __init__(self, atr_period: int = 14):
        self.atr_period = atr_period
        self._predictor = FillPredictor.__new__(FillPredictor)

    def generate(
        self,
        candles,
        quote_distances_bps: List[float] = None,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Generate labeled dataset from candle history.

        For each candle i (from warmup to end):
          For each distance d in quote_distances_bps:
            For each side (buy, sell):
              - Compute features
              - Label fill: bid filled if low <= bid_price, ask filled if high >= ask_price
              - Label adverse: bid adverse if filled AND close < bid_price,
                               ask adverse if filled AND close > ask_price

        Args:
            candles: List of Candle objects (need .open, .high, .low, .close, .volume, .timestamp).
            quote_distances_bps: Distances from mid to simulate (default: [1,2,3,5,8,10,15,20]).

        Returns:
            (X, y_fill, y_adverse):
              X: np.ndarray shape (n_samples, n_features)
              y_fill: np.ndarray shape (n_samples,) — 0 or 1
              y_adverse: np.ndarray shape (n_samples,) — 0 or 1
        """
        if quote_distances_bps is None:
            quote_distances_bps = [1, 2, 3, 5, 8, 10, 15, 20]

        # Minimum warmup: atr_period + 20 (for momentum_20)
        warmup = max(self.atr_period, 21)
        if len(candles) <= warmup:
            raise ValueError(f"Need at least {warmup + 1} candles, got {len(candles)}")

        # Pre-compute ATR
        atrs = self._compute_atr(candles)

        # Pre-compute close array for momentum
        closes = np.array([c.close for c in candles], dtype=np.float64)

        # Pre-compute volume stats (rolling mean/std, window=50)
        volumes = np.array([c.volume for c in candles], dtype=np.float64)
        vol_window = 50
        vol_means = np.zeros(len(candles))
        vol_stds = np.ones(len(candles))
        for i in range(vol_window, len(candles)):
            window = volumes[i - vol_window : i]
            vol_means[i] = np.mean(window)
            vol_stds[i] = max(np.std(window), 1e-10)

        # Pre-compute short/long vol ratio (vol_regime)
        short_window = 5
        long_window = 20

        n_candles_usable = len(candles) - warmup
        n_distances = len(quote_distances_bps)
        n_sides = 2
        n_samples = n_candles_usable * n_distances * n_sides

        X = np.zeros((n_samples, len(FEATURE_NAMES)), dtype=np.float64)
        y_fill = np.zeros(n_samples, dtype=np.int32)
        y_adverse = np.zeros(n_samples, dtype=np.int32)

        sample_idx = 0

        for i in range(warmup, len(candles)):
            candle = candles[i]
            prev_candle = candles[i - 1]
            atr = atrs[i]
            mid_price = (candle.high + candle.low) / 2.0

            if mid_price <= 0 or atr <= 0:
                # Skip degenerate candles, fill with zeros
                for _ in range(n_distances * n_sides):
                    sample_idx += 1
                continue

            volatility_pct = atr / mid_price

            # Vol regime: short ATR / long ATR
            if i >= long_window:
                short_vol = np.mean(
                    [abs(closes[j] / closes[j - 1] - 1) for j in range(i - short_window + 1, i + 1)]
                ) if closes[i - short_window] > 0 else volatility_pct
                long_vol = np.mean(
                    [abs(closes[j] / closes[j - 1] - 1) for j in range(i - long_window + 1, i + 1)]
                ) if closes[i - long_window] > 0 else volatility_pct
                vol_regime = short_vol / long_vol if long_vol > 0 else 1.0
            else:
                vol_regime = 1.0

            for d in quote_distances_bps:
                for side in ("buy", "sell"):
                    if side == "buy":
                        quote_price = mid_price * (1 - d / 10000)
                        filled = 1 if candle.low <= quote_price else 0
                        adverse = 1 if filled and candle.close < quote_price else 0
                    else:
                        quote_price = mid_price * (1 + d / 10000)
                        filled = 1 if candle.high >= quote_price else 0
                        adverse = 1 if filled and candle.close > quote_price else 0

                    features = self._predictor.extract_features(
                        candle=candle,
                        prev_candle=prev_candle,
                        mid_price=mid_price,
                        quote_price=quote_price,
                        quote_side=side,
                        volatility_pct=volatility_pct,
                        inventory_ratio=0.0,  # no inventory during data generation
                        vol_regime=vol_regime,
                        candle_idx=i,
                        volume_mean=vol_means[i],
                        volume_std=vol_stds[i],
                        closes=closes,
                    )

                    X[sample_idx] = [features[name] for name in FEATURE_NAMES]
                    y_fill[sample_idx] = filled
                    y_adverse[sample_idx] = adverse
                    sample_idx += 1

        # Trim if we skipped any degenerate candles
        X = X[:sample_idx]
        y_fill = y_fill[:sample_idx]
        y_adverse = y_adverse[:sample_idx]

        return X, y_fill, y_adverse

    def _compute_atr(self, candles) -> List[float]:
        """Compute ATR for each candle (same logic as backtester)."""
        atrs = [0.0] * len(candles)
        if len(candles) < 2:
            return atrs

        trs = []
        for i in range(1, len(candles)):
            tr = max(
                candles[i].high - candles[i].low,
                abs(candles[i].high - candles[i - 1].close),
                abs(candles[i].low - candles[i - 1].close),
            )
            trs.append(tr)

        if len(trs) >= self.atr_period:
            atr = sum(trs[: self.atr_period]) / self.atr_period
            atrs[self.atr_period] = atr

            for i in range(self.atr_period + 1, len(candles)):
                atr = (atr * (self.atr_period - 1) + trs[i - 1]) / self.atr_period
                atrs[i] = atr

        return atrs
