"""
CLI script to train the FillPredictor ML model on historical candle data.

Usage:
    py scripts/train_fill_model.py --symbol BTCUSDT --days 365 --data-dir "C:\\_Code\\BotHL\\data\\cache"
"""

import argparse
import os
import sys
from pathlib import Path

import numpy as np

# Add project root for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.mm_backtester import load_candles_csv
from bot_mm.ml.fill_predictor import FillPredictor, FEATURE_NAMES
from bot_mm.ml.data_generator import FillDataGenerator


def main():
    parser = argparse.ArgumentParser(description="Train Fill Prediction Model")
    parser.add_argument("--symbol", default="BTCUSDT", help="Trading pair")
    parser.add_argument("--days", type=int, default=365, help="Days of data")
    parser.add_argument("--data-dir", default=None, help="Data cache directory")
    parser.add_argument("--output", default=None, help="Output model path")
    parser.add_argument("--test-split", type=float, default=0.2, help="Test split fraction")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    args = parser.parse_args()

    np.random.seed(args.seed)

    # Find data file
    data_dir = args.data_dir
    if data_dir is None:
        bothl_cache = Path(__file__).parent.parent.parent / "BotHL" / "data" / "cache"
        local_cache = Path(__file__).parent.parent / "data" / "cache"
        if bothl_cache.exists():
            data_dir = str(bothl_cache)
        elif local_cache.exists():
            data_dir = str(local_cache)
        else:
            print("ERROR: No data directory found. Provide --data-dir")
            sys.exit(1)

    csv_file = os.path.join(data_dir, f"{args.symbol}_1h.csv")
    if not os.path.exists(csv_file):
        print(f"ERROR: Data file not found: {csv_file}")
        sys.exit(1)

    # Output path
    model_dir = Path(__file__).parent.parent / "models"
    model_dir.mkdir(exist_ok=True)
    output_path = args.output or str(model_dir / "fill_model.joblib")

    print()
    print("=" * 60)
    print("  FILL PREDICTION MODEL — TRAINING")
    print("=" * 60)

    # Load candles
    print(f"\n  Loading {args.symbol} from {csv_file}...")
    candles = load_candles_csv(csv_file, args.days)
    print(f"  Candles:    {len(candles):,}")

    # Generate training data
    print("  Generating training samples...")
    generator = FillDataGenerator()
    X, y_fill, y_adverse = generator.generate(candles)

    n_distances = 8
    n_sides = 2
    usable = len(candles) - max(generator.atr_period, 21)
    print(f"  Samples:    {len(X):,} ({n_distances} distances x {n_sides} sides x {usable:,} candles)")

    fill_rate = y_fill.mean() * 100
    adverse_rate = y_adverse.mean() * 100
    adverse_given_fill = (y_adverse.sum() / max(y_fill.sum(), 1)) * 100
    print(f"  Fill rate:  {fill_rate:.1f}%")
    print(f"  Adverse rate (overall): {adverse_rate:.1f}%")
    print(f"  Adverse rate (given fill): {adverse_given_fill:.1f}%")

    # Train/test split (time-based: first 80% train, last 20% test)
    split_idx = int(len(X) * (1 - args.test_split))
    X_train, X_test = X[:split_idx], X[split_idx:]
    y_fill_train, y_fill_test = y_fill[:split_idx], y_fill[split_idx:]
    y_adverse_train, y_adverse_test = y_adverse[:split_idx], y_adverse[split_idx:]

    print(f"  Train/Test: {len(X_train):,}/{len(X_test):,} ({(1-args.test_split)*100:.0f}%/{args.test_split*100:.0f}%)")

    # Train
    print("\n  Training models...")
    predictor = FillPredictor()
    predictor.train(X_train, y_fill_train, y_adverse_train)

    # Evaluate
    from sklearn.metrics import roc_auc_score, accuracy_score, precision_score, recall_score

    # Fill model
    fill_train_probs = predictor._fill_model.predict_proba(X_train)[:, 1]
    fill_test_probs = predictor._fill_model.predict_proba(X_test)[:, 1]
    fill_test_preds = (fill_test_probs >= 0.5).astype(int)

    fill_train_auc = roc_auc_score(y_fill_train, fill_train_probs)
    fill_test_auc = roc_auc_score(y_fill_test, fill_test_probs)
    fill_test_acc = accuracy_score(y_fill_test, fill_test_preds)
    fill_test_prec = precision_score(y_fill_test, fill_test_preds, zero_division=0)
    fill_test_rec = recall_score(y_fill_test, fill_test_preds, zero_division=0)

    print(f"\n  FILL MODEL:")
    print(f"    Train AUC:     {fill_train_auc:.4f}")
    print(f"    Test AUC:      {fill_test_auc:.4f}")
    print(f"    Test Accuracy: {fill_test_acc:.1%}")
    print(f"    Test Precision:{fill_test_prec:.1%}")
    print(f"    Test Recall:   {fill_test_rec:.1%}")

    print(f"\n    Feature importance:")
    fi = predictor.feature_importance()
    for name, imp in sorted(fi.items(), key=lambda x: -x[1]):
        bar = "█" * int(imp * 50)
        print(f"      {name:<25} {imp:.4f}  {bar}")

    # Adverse model
    adv_train_probs = predictor._adverse_model.predict_proba(X_train)[:, 1]
    adv_test_probs = predictor._adverse_model.predict_proba(X_test)[:, 1]
    adv_test_preds = (adv_test_probs >= 0.5).astype(int)

    adv_train_auc = roc_auc_score(y_adverse_train, adv_train_probs)
    adv_test_auc = roc_auc_score(y_adverse_test, adv_test_probs)
    adv_test_acc = accuracy_score(y_adverse_test, adv_test_preds)
    adv_test_prec = precision_score(y_adverse_test, adv_test_preds, zero_division=0)
    adv_test_rec = recall_score(y_adverse_test, adv_test_preds, zero_division=0)

    print(f"\n  ADVERSE SELECTION MODEL:")
    print(f"    Train AUC:     {adv_train_auc:.4f}")
    print(f"    Test AUC:      {adv_test_auc:.4f}")
    print(f"    Test Accuracy: {adv_test_acc:.1%}")
    print(f"    Test Precision:{adv_test_prec:.1%}")
    print(f"    Test Recall:   {adv_test_rec:.1%}")

    print(f"\n    Feature importance:")
    afi = predictor.adverse_feature_importance()
    for name, imp in sorted(afi.items(), key=lambda x: -x[1]):
        bar = "█" * int(imp * 50)
        print(f"      {name:<25} {imp:.4f}  {bar}")

    # Save
    predictor.save(output_path)
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"\n  Model saved to: {output_path} ({size_mb:.1f} MB)")
    print()
    print("=" * 60)


if __name__ == "__main__":
    main()
