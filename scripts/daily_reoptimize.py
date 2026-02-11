#!/usr/bin/env python3
"""
Daily Auto-Reoptimizer — runs nightly to find best params per asset.

Workflow:
1. Fetches latest candle data (or uses cached)
2. Runs quick optimizer for each asset
3. Compares new params vs current live params
4. Applies if improvement > threshold, with max drift safety
5. Saves history for tracking param evolution

Usage:
    # Run for all configured assets
    python scripts/daily_reoptimize.py

    # Run for specific assets
    python scripts/daily_reoptimize.py --symbols BTC ETH SOL

    # Dry run (show recommendations, don't apply)
    python scripts/daily_reoptimize.py --dry-run

    # Custom lookback window
    python scripts/daily_reoptimize.py --days 90

Scheduling (cron/Task Scheduler):
    # Linux: run at 3am UTC daily
    0 3 * * * cd /path/to/BotMM && python scripts/daily_reoptimize.py >> logs/reoptimize.log 2>&1

    # Windows Task Scheduler: similar, point to py scripts/daily_reoptimize.py
"""

import sys
import os
import json
import time
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone
from itertools import product
from multiprocessing import Pool, cpu_count
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict

import numpy as np

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from backtest.mm_backtester import MMBacktester, load_candles_csv, Candle
from bot_mm.config import QuoteParams

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

logger = logging.getLogger("daily_reoptimize")

# ---------------------------------------------------------------------------
# Asset profiles — per-asset optimal baseline + constraints
# ---------------------------------------------------------------------------

ASSET_PROFILES = {
    "BTCUSDT": {
        "compound": True,
        "use_bias": True,
        "bias_strength": 0.2,
        "base_params": {
            "base_spread_bps": 2.0,
            "inventory_skew_factor": 0.3,
            "order_size_usd": 150,
            "num_levels": 2,
        },
    },
    "ETHUSDT": {
        "compound": True,
        "use_bias": True,
        "bias_strength": 0.2,
        "base_params": {
            "base_spread_bps": 1.5,
            "inventory_skew_factor": 0.3,
            "order_size_usd": 150,
            "num_levels": 2,
        },
    },
    "SOLUSDT": {
        "compound": False,
        "use_bias": False,
        "bias_strength": 0.0,
        "base_params": {
            "base_spread_bps": 1.5,
            "inventory_skew_factor": 0.5,
            "order_size_usd": 150,
            "num_levels": 2,
        },
    },
    "XRPUSDT": {
        "compound": False,
        "use_bias": True,
        "bias_strength": 0.2,
        "base_params": {
            "base_spread_bps": 1.5,
            "inventory_skew_factor": 0.5,
            "order_size_usd": 150,
            "num_levels": 2,
        },
    },
    # "HYPEUSDT" removed: poor performance (Sharpe too low, capital better allocated elsewhere)
}

# Optimizer search grid (focused — nearby current optimals)
REOPT_GRID = {
    "base_spread_bps": [1.0, 1.5, 2.0, 3.0],
    "vol_multiplier": [1.0, 1.5, 2.0],
    "inventory_skew_factor": [0.3, 0.5, 0.8],
    "order_size_usd": [100, 150],
    "num_levels": [1, 2],
}

# Safety constraints
MAX_DAILY_DRIFT_PCT = 30.0   # Max param change per day (%)
MIN_IMPROVEMENT_PCT = 5.0    # Only apply if score improves by > 5%
HISTORY_DIR = project_root / "data" / "reopt_history"
LIVE_PARAMS_FILE = project_root / "data" / "live_params.json"


@dataclass
class ReoptResult:
    """Result of a single asset reoptimization."""
    symbol: str
    timestamp: str
    old_params: Dict[str, Any]
    new_params: Dict[str, Any]
    old_score: float
    new_score: float
    improvement_pct: float
    applied: bool
    reason: str
    details: Dict[str, Any]


def _run_single_backtest(args):
    """Worker function for parallel optimizer."""
    params_dict, candles_data, symbol, capital, max_pos, use_bias, bias_strength = args

    candles = [Candle(*c) for c in candles_data]

    qp = QuoteParams(
        base_spread_bps=params_dict["base_spread_bps"],
        vol_multiplier=params_dict.get("vol_multiplier", 1.5),
        inventory_skew_factor=params_dict["inventory_skew_factor"],
        order_size_usd=params_dict["order_size_usd"],
        num_levels=params_dict.get("num_levels", 2),
    )

    bt = MMBacktester(
        quote_params=qp,
        max_position_usd=max_pos,
        max_daily_loss=capital * 0.05,
        capital=capital,
        use_bias=use_bias,
        bias_strength=bias_strength,
        use_toxicity=True,
        use_auto_tune=True,
    )

    result = bt.run(candles, symbol)

    # Score: PnL × Sharpe bonus × fill bonus × drawdown penalty
    sharpe_bonus = max(0.5, min(2.0, result.sharpe_ratio / 10.0))
    fill_bonus = min(1.5, result.total_fills / max(1, result.days) / 20.0)
    dd_penalty = max(0.3, 1.0 - result.max_drawdown / capital)

    score = result.net_pnl * sharpe_bonus * max(0.5, fill_bonus) * dd_penalty

    return params_dict, {
        "net_pnl": round(result.net_pnl, 2),
        "sharpe": round(result.sharpe_ratio, 2),
        "fills": result.total_fills,
        "max_dd": round(result.max_drawdown, 2),
    }, score


def optimize_asset(
    symbol: str,
    candles: List[Candle],
    capital: float = 1000.0,
    max_pos: float = 500.0,
    use_bias: bool = False,
    bias_strength: float = 0.0,
    workers: int = 8,
) -> Tuple[Dict, Dict, float]:
    """Run grid search optimizer for a single asset. Returns (best_params, details, score)."""
    # Build param combos
    keys = list(REOPT_GRID.keys())
    values = list(REOPT_GRID.values())
    combos = [dict(zip(keys, v)) for v in product(*values)]

    # Serialize candles for multiprocessing
    candles_data = [(c.timestamp, c.open, c.high, c.low, c.close, c.volume) for c in candles]

    tasks = [
        (combo, candles_data, symbol, capital, max_pos, use_bias, bias_strength)
        for combo in combos
    ]

    logger.info(f"  {symbol}: testing {len(combos)} combinations with {workers} workers...")

    with Pool(workers) as pool:
        if HAS_TQDM:
            results = list(tqdm(
                pool.imap_unordered(_run_single_backtest, tasks),
                total=len(tasks),
                desc=f"  {symbol}",
                ncols=80,
            ))
        else:
            results = pool.map(_run_single_backtest, tasks)

    # Sort by score descending
    results.sort(key=lambda x: x[2], reverse=True)

    if not results or results[0][2] <= 0:
        return {}, {}, 0.0

    best_params, best_details, best_score = results[0]
    return best_params, best_details, best_score


def load_live_params() -> Dict[str, Dict]:
    """Load current live params from JSON file."""
    if LIVE_PARAMS_FILE.exists():
        with open(LIVE_PARAMS_FILE) as f:
            return json.load(f)
    return {}


def save_live_params(params: Dict[str, Dict]):
    """Save updated live params."""
    LIVE_PARAMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LIVE_PARAMS_FILE, "w") as f:
        json.dump(params, f, indent=2)


def check_drift(old_params: Dict, new_params: Dict) -> Tuple[bool, float, str]:
    """Check if param change exceeds max daily drift. Returns (safe, max_drift%, reason)."""
    max_drift = 0.0
    drift_details = []

    for key in new_params:
        if key not in old_params:
            continue
        old_val = old_params[key]
        new_val = new_params[key]

        if isinstance(old_val, bool) or isinstance(new_val, bool):
            if old_val != new_val:
                drift_details.append(f"{key}: {old_val}→{new_val} (bool flip)")
                max_drift = max(max_drift, 100.0)
            continue

        if old_val == 0:
            if new_val != 0:
                max_drift = max(max_drift, 100.0)
                drift_details.append(f"{key}: 0→{new_val}")
            continue

        drift_pct = abs(new_val - old_val) / abs(old_val) * 100
        max_drift = max(max_drift, drift_pct)
        if drift_pct > 10:
            drift_details.append(f"{key}: {old_val}→{new_val} ({drift_pct:.0f}%)")

    safe = max_drift <= MAX_DAILY_DRIFT_PCT
    reason = "; ".join(drift_details) if drift_details else "within bounds"
    return safe, max_drift, reason


def save_history(result: ReoptResult):
    """Append result to daily history log."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    history_file = HISTORY_DIR / f"reopt_{date_str}.json"
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    history = []
    if history_file.exists():
        with open(history_file) as f:
            history = json.load(f)

    history.append(asdict(result))

    with open(history_file, "w") as f:
        json.dump(history, f, indent=2, default=str)


def find_data_file(symbol: str, data_dir: Optional[str] = None) -> Optional[str]:
    """Find candle data CSV for a symbol."""
    if data_dir:
        csv = os.path.join(data_dir, f"{symbol}_1h.csv")
        if os.path.exists(csv):
            return csv

    # Try standard locations
    for d in [
        project_root.parent / "BotHL" / "data" / "cache",
        project_root / "data" / "cache",
    ]:
        csv = d / f"{symbol}_1h.csv"
        if csv.exists():
            return str(csv)

    return None


def run_reoptimization(
    symbols: List[str],
    days: int = 90,
    capital: float = 1000.0,
    max_pos: float = 500.0,
    workers: int = 8,
    dry_run: bool = False,
    data_dir: Optional[str] = None,
) -> List[ReoptResult]:
    """Run daily reoptimization for given symbols."""
    timestamp = datetime.now(timezone.utc).isoformat()
    live_params = load_live_params()
    results = []

    print("=" * 70)
    print(f"  DAILY AUTO-REOPTIMIZER — {timestamp[:19]}Z")
    print("=" * 70)
    print(f"  Assets: {', '.join(symbols)}")
    print(f"  Lookback: {days} days | Capital: ${capital:,.0f} | Max Pos: ${max_pos:,.0f}")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE APPLY'}")
    print()

    for symbol in symbols:
        profile = ASSET_PROFILES.get(symbol, {})
        use_bias = profile.get("use_bias", False)
        bias_strength = profile.get("bias_strength", 0.0)

        # Find data
        csv_file = find_data_file(symbol, data_dir)
        if not csv_file:
            logger.warning(f"  {symbol}: no data found, skipping")
            results.append(ReoptResult(
                symbol=symbol, timestamp=timestamp,
                old_params={}, new_params={}, old_score=0, new_score=0,
                improvement_pct=0, applied=False,
                reason="no data file found", details={},
            ))
            continue

        # Load candles
        candles = load_candles_csv(csv_file, days)
        print(f"  {symbol}: loaded {len(candles)} candles from {csv_file}")

        # Current params (from live_params or profile defaults)
        old_params = live_params.get(symbol, profile.get("base_params", {}))

        # Run optimizer
        t0 = time.time()
        new_params, details, new_score = optimize_asset(
            symbol, candles, capital, max_pos, use_bias, bias_strength, workers,
        )
        elapsed = time.time() - t0

        if not new_params or new_score <= 0:
            print(f"  {symbol}: optimizer returned no profitable params")
            results.append(ReoptResult(
                symbol=symbol, timestamp=timestamp,
                old_params=old_params, new_params={}, old_score=0, new_score=0,
                improvement_pct=0, applied=False,
                reason="no profitable params", details={},
            ))
            continue

        # Calculate old score (backtest with current params)
        old_qp = QuoteParams(**{**QuoteParams().__dict__, **old_params})
        old_bt = MMBacktester(
            quote_params=old_qp,
            max_position_usd=max_pos,
            max_daily_loss=capital * 0.05,
            capital=capital,
            use_bias=use_bias,
            bias_strength=bias_strength,
            use_toxicity=True,
            use_auto_tune=True,
        )
        old_result = old_bt.run(candles, symbol)
        old_sharpe_b = max(0.5, min(2.0, old_result.sharpe_ratio / 10.0))
        old_fill_b = min(1.5, old_result.total_fills / max(1, old_result.days) / 20.0)
        old_dd_p = max(0.3, 1.0 - old_result.max_drawdown / capital)
        old_score = old_result.net_pnl * old_sharpe_b * max(0.5, old_fill_b) * old_dd_p

        # Improvement check
        improvement = ((new_score - old_score) / abs(old_score) * 100) if old_score != 0 else 100.0

        # Drift check
        drift_safe, max_drift, drift_reason = check_drift(old_params, new_params)

        # Decision
        should_apply = (
            improvement >= MIN_IMPROVEMENT_PCT
            and drift_safe
            and not dry_run
        )

        if not drift_safe:
            reason = f"drift too high ({max_drift:.0f}%): {drift_reason}"
        elif improvement < MIN_IMPROVEMENT_PCT:
            reason = f"improvement {improvement:+.1f}% < {MIN_IMPROVEMENT_PCT}% threshold"
        elif dry_run:
            reason = "dry run"
        else:
            reason = f"applied (+{improvement:.1f}%, drift {max_drift:.0f}%)"

        # Apply
        if should_apply:
            live_params[symbol] = new_params
            save_live_params(live_params)

        result = ReoptResult(
            symbol=symbol,
            timestamp=timestamp,
            old_params=old_params,
            new_params=new_params,
            old_score=round(old_score, 2),
            new_score=round(new_score, 2),
            improvement_pct=round(improvement, 1),
            applied=should_apply,
            reason=reason,
            details=details,
        )
        results.append(result)
        save_history(result)

        # Print summary
        status = "[APPLIED]" if should_apply else "[SKIPPED]"
        print(f"\n  {symbol} — {status} ({elapsed:.1f}s)")
        print(f"    Old score: {old_score:,.0f} | New score: {new_score:,.0f} ({improvement:+.1f}%)")
        print(f"    New PnL: ${details.get('net_pnl', 0):,.0f} | Sharpe: {details.get('sharpe', 0):.1f}")
        print(f"    Params: spread={new_params.get('base_spread_bps')}, "
              f"skew={new_params.get('inventory_skew_factor')}, "
              f"size={new_params.get('order_size_usd')}, "
              f"levels={new_params.get('num_levels')}")
        print(f"    Reason: {reason}")

    # Final summary
    applied_count = sum(1 for r in results if r.applied)
    print(f"\n{'=' * 70}")
    print(f"  SUMMARY: {applied_count}/{len(results)} assets updated")
    print(f"  Live params: {LIVE_PARAMS_FILE}")
    print(f"  History: {HISTORY_DIR}")
    print(f"{'=' * 70}")

    return results


def main():
    parser = argparse.ArgumentParser(description="Daily MM Auto-Reoptimizer")
    parser.add_argument("--symbols", nargs="+", default=None,
                        help="Symbols to optimize (default: all in ASSET_PROFILES)")
    parser.add_argument("--days", type=int, default=90,
                        help="Lookback window for optimization (days)")
    parser.add_argument("--capital", type=float, default=1000.0,
                        help="Capital per asset ($)")
    parser.add_argument("--max-pos", type=float, default=500.0,
                        help="Max position per asset ($)")
    parser.add_argument("--workers", type=int, default=max(1, cpu_count() - 2),
                        help="Parallel workers")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show recommendations without applying")
    parser.add_argument("--data-dir", default=None,
                        help="Data directory for candle CSVs")
    parser.add_argument("--min-improvement", type=float, default=5.0,
                        help="Min improvement %% to apply (default: 5)")
    parser.add_argument("--max-drift", type=float, default=30.0,
                        help="Max param drift %% per day (default: 30)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Override safety thresholds
    global MIN_IMPROVEMENT_PCT, MAX_DAILY_DRIFT_PCT
    MIN_IMPROVEMENT_PCT = args.min_improvement
    MAX_DAILY_DRIFT_PCT = args.max_drift

    symbols = args.symbols
    if symbols:
        # Normalize: BTC → BTCUSDT
        symbols = [s if s.endswith("USDT") else f"{s}USDT" for s in symbols]
    else:
        symbols = list(ASSET_PROFILES.keys())

    run_reoptimization(
        symbols=symbols,
        days=args.days,
        capital=args.capital,
        max_pos=args.max_pos,
        workers=args.workers,
        dry_run=args.dry_run,
        data_dir=args.data_dir,
    )


if __name__ == "__main__":
    main()
