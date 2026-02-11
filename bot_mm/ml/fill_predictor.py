"""
ML model predicting fill probability and adverse selection risk for MM quotes.

Uses GradientBoosting classifiers trained on historical candle data.
Replaces/augments the simple penetration-based fill model in the backtester.
"""

import math
from typing import Dict, Optional, Tuple

import numpy as np

try:
    import joblib
    from sklearn.ensemble import GradientBoostingClassifier
except ImportError:
    joblib = None
    GradientBoostingClassifier = None


# Feature names in fixed order for consistent array construction
FEATURE_NAMES = [
    "distance_to_mid_bps",
    "side_is_buy",
    "volatility_pct",
    "vol_regime",
    "inventory_ratio",
    "candle_range_bps",
    "volume_zscore",
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
    "prev_candle_return",
    "prev_candle_range_bps",
    "momentum_5",
    "momentum_20",
]


class FillPredictor:
    """
    ML model predicting fill probability and adverse selection risk.

    Features per quote:
    - distance_to_mid_bps: how far quote is from mid (in bps)
    - side_is_buy: 1 for bid, 0 for ask
    - volatility_pct: current ATR/price
    - vol_regime: short_vol / long_vol ratio
    - inventory_ratio: abs(inventory) / max_position
    - candle_range_bps: (high-low)/mid in bps
    - volume_zscore: volume vs rolling avg
    - hour_of_day: 0-23 (cyclical encoded sin/cos)
    - day_of_week: 0-6 (cyclical encoded sin/cos)
    - prev_candle_return: close/open - 1 of previous candle
    - prev_candle_range_bps: range of previous candle in bps
    - momentum_5: 5-bar return
    - momentum_20: 20-bar return

    Two prediction heads:
    - fill_probability: float 0-1 (will this quote get filled?)
    - adverse_probability: float 0-1 (if filled, is it adverse selection?)
    """

    def __init__(self):
        if GradientBoostingClassifier is None:
            raise ImportError("scikit-learn is required: pip install scikit-learn>=1.3.0")

        self._fill_model: Optional[GradientBoostingClassifier] = None
        self._adverse_model: Optional[GradientBoostingClassifier] = None
        self._trained = False

    @property
    def is_trained(self) -> bool:
        return self._trained

    def extract_features(
        self,
        candle,
        prev_candle,
        mid_price: float,
        quote_price: float,
        quote_side: str,
        volatility_pct: float,
        inventory_ratio: float = 0.0,
        vol_regime: float = 1.0,
        candle_idx: int = 0,
        volume_mean: float = 0.0,
        volume_std: float = 1.0,
        closes: Optional[np.ndarray] = None,
    ) -> Dict[str, float]:
        """
        Extract feature dict for a single quote.

        Args:
            candle: Current candle (has open, high, low, close, volume, timestamp).
            prev_candle: Previous candle (same fields).
            mid_price: Current mid price.
            quote_price: Price of the quote.
            quote_side: "buy" or "sell".
            volatility_pct: ATR / price.
            inventory_ratio: abs(inventory) / max_position, 0-1.
            vol_regime: short_vol / long_vol ratio.
            candle_idx: Index of current candle in the series.
            volume_mean: Rolling mean of volume (for z-score).
            volume_std: Rolling std of volume (for z-score).
            closes: Array of close prices up to current candle (for momentum).

        Returns:
            Dict of feature_name -> value.
        """
        distance_bps = abs(quote_price - mid_price) / mid_price * 10000 if mid_price > 0 else 0.0

        candle_range_bps = (candle.high - candle.low) / mid_price * 10000 if mid_price > 0 else 0.0

        # Volume z-score
        vol_z = (candle.volume - volume_mean) / volume_std if volume_std > 0 else 0.0

        # Time features (cyclical encoding)
        hour = _parse_hour(candle.timestamp)
        dow = _parse_dow(candle.timestamp)
        hour_sin = math.sin(2 * math.pi * hour / 24)
        hour_cos = math.cos(2 * math.pi * hour / 24)
        dow_sin = math.sin(2 * math.pi * dow / 7)
        dow_cos = math.cos(2 * math.pi * dow / 7)

        # Previous candle features
        prev_return = (prev_candle.close / prev_candle.open - 1) if prev_candle.open > 0 else 0.0
        prev_range_bps = (
            (prev_candle.high - prev_candle.low) / mid_price * 10000 if mid_price > 0 else 0.0
        )

        # Momentum
        mom5 = 0.0
        mom20 = 0.0
        if closes is not None and len(closes) > 0:
            idx = min(candle_idx, len(closes) - 1)
            if idx >= 5 and closes[idx - 5] > 0:
                mom5 = closes[idx] / closes[idx - 5] - 1
            if idx >= 20 and closes[idx - 20] > 0:
                mom20 = closes[idx] / closes[idx - 20] - 1

        return {
            "distance_to_mid_bps": distance_bps,
            "side_is_buy": 1.0 if quote_side == "buy" else 0.0,
            "volatility_pct": volatility_pct,
            "vol_regime": vol_regime,
            "inventory_ratio": inventory_ratio,
            "candle_range_bps": candle_range_bps,
            "volume_zscore": vol_z,
            "hour_sin": hour_sin,
            "hour_cos": hour_cos,
            "dow_sin": dow_sin,
            "dow_cos": dow_cos,
            "prev_candle_return": prev_return,
            "prev_candle_range_bps": prev_range_bps,
            "momentum_5": mom5,
            "momentum_20": mom20,
        }

    def predict(self, features: Dict[str, float]) -> Tuple[float, float]:
        """
        Predict fill probability and adverse selection probability.

        Args:
            features: Dict from extract_features().

        Returns:
            (fill_prob, adverse_prob) — both floats in [0, 1].

        Raises:
            RuntimeError: If model is not trained.
        """
        if not self._trained:
            raise RuntimeError("Model not trained. Call train() or load() first.")

        x = self._features_to_array(features)
        fill_prob = float(self._fill_model.predict_proba(x)[:, 1][0])
        adverse_prob = float(self._adverse_model.predict_proba(x)[:, 1][0])
        return fill_prob, adverse_prob

    def predict_batch(self, X: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """
        Batch prediction on feature array.

        Args:
            X: 2D array of shape (n_samples, n_features).

        Returns:
            (fill_probs, adverse_probs) — arrays of shape (n_samples,).
        """
        if not self._trained:
            raise RuntimeError("Model not trained. Call train() or load() first.")

        fill_probs = self._fill_model.predict_proba(X)[:, 1]
        adverse_probs = self._adverse_model.predict_proba(X)[:, 1]
        return fill_probs, adverse_probs

    def train(
        self,
        X: np.ndarray,
        y_fill: np.ndarray,
        y_adverse: np.ndarray,
        fill_params: Optional[dict] = None,
        adverse_params: Optional[dict] = None,
    ) -> None:
        """
        Train both fill and adverse selection models.

        Args:
            X: Feature array, shape (n_samples, n_features).
            y_fill: Fill labels, shape (n_samples,), values 0 or 1.
            y_adverse: Adverse selection labels, shape (n_samples,), values 0 or 1.
            fill_params: Optional sklearn kwargs for fill model.
            adverse_params: Optional sklearn kwargs for adverse model.
        """
        default_params = dict(
            n_estimators=200,
            max_depth=5,
            learning_rate=0.1,
            subsample=0.8,
            min_samples_leaf=50,
            random_state=42,
        )

        fp = {**default_params, **(fill_params or {})}
        ap = {**default_params, **(adverse_params or {})}

        self._fill_model = GradientBoostingClassifier(**fp)
        self._fill_model.fit(X, y_fill)

        self._adverse_model = GradientBoostingClassifier(**ap)
        self._adverse_model.fit(X, y_adverse)

        self._trained = True

    def save(self, path: str) -> None:
        """Save both models to a single file with joblib."""
        if not self._trained:
            raise RuntimeError("Model not trained. Nothing to save.")
        joblib.dump(
            {"fill_model": self._fill_model, "adverse_model": self._adverse_model},
            path,
        )

    def load(self, path: str) -> None:
        """Load models from a joblib file."""
        data = joblib.load(path)
        self._fill_model = data["fill_model"]
        self._adverse_model = data["adverse_model"]
        self._trained = True

    @property
    def feature_names(self):
        return list(FEATURE_NAMES)

    def feature_importance(self) -> Dict[str, float]:
        """Return fill model feature importances as {name: importance}."""
        if not self._trained:
            raise RuntimeError("Model not trained.")
        importances = self._fill_model.feature_importances_
        return dict(zip(FEATURE_NAMES, importances))

    def adverse_feature_importance(self) -> Dict[str, float]:
        """Return adverse model feature importances."""
        if not self._trained:
            raise RuntimeError("Model not trained.")
        importances = self._adverse_model.feature_importances_
        return dict(zip(FEATURE_NAMES, importances))

    # ── Internal ────────────────────────────────────────────

    @staticmethod
    def _features_to_array(features: Dict[str, float]) -> np.ndarray:
        """Convert feature dict to 2D array in canonical order."""
        return np.array([[features[name] for name in FEATURE_NAMES]])


def _parse_hour(timestamp: str) -> int:
    """Extract hour from timestamp string 'YYYY-MM-DD HH:MM:SS'."""
    try:
        return int(timestamp[11:13])
    except (ValueError, IndexError):
        return 0


def _parse_dow(timestamp: str) -> int:
    """Extract day of week (0=Monday) from timestamp string."""
    try:
        from datetime import datetime
        dt = datetime.strptime(timestamp[:10], "%Y-%m-%d")
        return dt.weekday()
    except (ValueError, IndexError):
        return 0
