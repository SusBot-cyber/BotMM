#!/usr/bin/env python3
"""
MM Parameter Optimizer — Grid search for market making strategy.

Tests parameter combinations in parallel using multiprocessing,
scores results by composite metric (PnL × Sharpe × fill rate),
and reports top strategies with parameter impact analysis.

Usage:
    python scripts/run_mm_optimizer.py --symbol BTCUSDT --days 90 --quick --workers 4
    python scripts/run_mm_optimizer.py --symbol BTCUSDT --days 365 --workers 10
    python scripts/run_mm_optimizer.py --symbol BTCUSDT --days 365 --full --save-json
"""

import sys
import os
import json
import time
import argparse
from pathlib import Path
from datetime import datetime
from itertools import product
from multiprocessing import Pool, cpu_count
from functools import partial
from typing import List, Dict, Tuple, Optional, Any
from dataclasses import asdict

import numpy as np

# Add project root for imports
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from backtest.mm_backtester import MMBacktester, MMBacktestResult, load_candles_csv, Candle
from bot_mm.config import QuoteParams

# Try tqdm
try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

# ---------------------------------------------------------------------------
# Parameter grids
# ---------------------------------------------------------------------------

NORMAL_GRID = {
    'base_spread_bps': [1.0, 1.5, 2.0, 3.0, 5.0],
    'vol_multiplier': [0.5, 1.0, 1.5, 2.0, 3.0],
    'inventory_skew_factor': [0.2, 0.5, 0.8, 1.0],
    'order_size_usd': [50, 100, 150, 200],
    'num_levels': [1, 2, 3],
    'level_spacing_bps': [0.5, 1.0, 2.0],
    'max_position_usd': [300, 500, 750, 1000],
    'use_bias': [False, True],
    'bias_strength': [0.1, 0.2, 0.3, 0.5],
}

QUICK_GRID = {
    'base_spread_bps': [1.5, 2.0, 3.0],
    'vol_multiplier': [1.0, 1.5, 2.0],
    'inventory_skew_factor': [0.3, 0.5, 0.8],
    'order_size_usd': [100, 150],
    'num_levels': [1, 2],
    'level_spacing_bps': [1.0],
    'max_position_usd': [500],
    'use_bias': [False, True],
    'bias_strength': [0.2],
}

FULL_GRID = {
    'base_spread_bps': [0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 7.0],
    'vol_multiplier': [0.3, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0],
    'inventory_skew_factor': [0.1, 0.2, 0.3, 0.5, 0.8, 1.0, 1.5],
    'order_size_usd': [25, 50, 100, 150, 200, 300],
    'num_levels': [1, 2, 3, 5],
    'level_spacing_bps': [0.5, 1.0, 2.0, 3.0],
    'max_position_usd': [200, 300, 500, 750, 1000, 1500],
    'use_bias': [False, True],
    'bias_strength': [0.1, 0.2, 0.3, 0.5, 0.8],
}


def build_combinations(grid: Dict[str, List]) -> List[Dict[str, Any]]:
    """
    Build all parameter combinations from grid.
    When use_bias=False, skip bias_strength variations.
    """
    # Separate bias params from the rest
    bias_strengths = grid.get('bias_strength', [0.2])
    use_bias_vals = grid.get('use_bias', [False])

    non_bias_keys = [k for k in grid if k not in ('use_bias', 'bias_strength')]
    non_bias_vals = [grid[k] for k in non_bias_keys]

    combos = []
    for base_combo in product(*non_bias_vals):
        base = dict(zip(non_bias_keys, base_combo))
        for ub in use_bias_vals:
            if ub:
                for bs in bias_strengths:
                    combo = dict(base)
                    combo['use_bias'] = True
                    combo['bias_strength'] = bs
                    combos.append(combo)
            else:
                combo = dict(base)
                combo['use_bias'] = False
                combo['bias_strength'] = 0.0
                combos.append(combo)
    return combos


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_result(result: MMBacktestResult) -> float:
    """Score a backtest result for ranking."""
    if result.total_fills < 50:
        return -999999.0

    if result.net_pnl < 0:
        return result.net_pnl * 2.0

    sharpe_bonus = min(2.0, max(0.5, result.sharpe_ratio / 5.0))
    fill_bonus = min(1.5, result.fills_per_day / 15.0)
    dd_penalty = max(0.5, 1.0 - result.max_drawdown / 100.0)

    return result.net_pnl * sharpe_bonus * fill_bonus * dd_penalty


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

# Global candle data (shared across workers via fork / spawn init)
_worker_candles: List[Candle] = []
_worker_symbol: str = "BTCUSDT"
_worker_capital: float = 1000.0


def _init_worker(candles: List[Candle], symbol: str, capital: float):
    """Initialize worker process globals."""
    global _worker_candles, _worker_symbol, _worker_capital
    _worker_candles = candles
    _worker_symbol = symbol
    _worker_capital = capital


def _run_single(params: Dict[str, Any]) -> Optional[Tuple[Dict, Dict, float]]:
    """
    Worker function: run one backtest and return (params, result_dict, score).
    Must be top-level for pickling.
    """
    np.random.seed(42)
    try:
        qp = QuoteParams(
            base_spread_bps=params['base_spread_bps'],
            vol_multiplier=params['vol_multiplier'],
            inventory_skew_factor=params['inventory_skew_factor'],
            order_size_usd=params['order_size_usd'],
            num_levels=int(params['num_levels']),
            level_spacing_bps=params.get('level_spacing_bps', 1.0),
        )

        bt = MMBacktester(
            quote_params=qp,
            max_position_usd=params['max_position_usd'],
            capital=_worker_capital,
            use_bias=params.get('use_bias', False),
            bias_strength=params.get('bias_strength', 0.0),
        )

        result = bt.run(_worker_candles, _worker_symbol)

        sc = score_result(result)

        # Convert result to serialisable dict (skip daily_pnls for memory)
        rd = {
            'net_pnl': result.net_pnl,
            'gross_pnl': result.gross_pnl,
            'total_fees': result.total_fees,
            'total_fills': result.total_fills,
            'buy_fills': result.buy_fills,
            'sell_fills': result.sell_fills,
            'round_trips': result.round_trips,
            'fills_per_day': result.fills_per_day,
            'max_inventory_usd': result.max_inventory_usd,
            'avg_inventory_usd': result.avg_inventory_usd,
            'max_drawdown': result.max_drawdown,
            'sharpe_ratio': result.sharpe_ratio,
            'avg_spread_captured_bps': result.avg_spread_captured_bps,
            'avg_spread_quoted_bps': result.avg_spread_quoted_bps,
            'days': result.days,
            'risk_halts': result.risk_halts,
            'inventory_pnl': result.inventory_pnl,
        }

        # Win rate proxy: profitable days / total days
        if result.daily_pnls:
            pos_days = sum(1 for d in result.daily_pnls if d > 0)
            rd['win_pct'] = pos_days / len(result.daily_pnls) * 100.0
        else:
            rd['win_pct'] = 0.0

        return (params, rd, sc)

    except Exception:
        return None


# ---------------------------------------------------------------------------
# Parameter impact analysis
# ---------------------------------------------------------------------------

def compute_param_impact(
    all_results: List[Tuple[Dict, Dict, float]],
    grid: Dict[str, List],
) -> Dict[str, List[Dict]]:
    """Compute average metrics per parameter value."""
    impact: Dict[str, List[Dict]] = {}

    analyse_keys = [k for k in grid if k not in ('use_bias', 'bias_strength')]
    analyse_keys.append('use_bias')

    for key in analyse_keys:
        # Collect unique values
        if key == 'use_bias':
            unique_vals = [False, True]
        else:
            unique_vals = sorted(set(grid.get(key, [])))

        rows = []
        for val in unique_vals:
            matching = [r for p, r, s in all_results if p.get(key) == val]
            if not matching:
                continue

            pnls = [r['net_pnl'] for r in matching]
            sharpes = [r['sharpe_ratio'] for r in matching]
            profitable = sum(1 for p in pnls if p > 0)

            rows.append({
                'value': val if not isinstance(val, bool) else str(val),
                'count': len(matching),
                'avg_pnl': float(np.mean(pnls)),
                'avg_sharpe': float(np.mean(sharpes)),
                'profitable_pct': profitable / len(matching) * 100.0,
            })

        impact[key] = rows

    return impact


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def print_header(symbol: str, days: int, capital: float, mode: str, n_combos: int):
    print()
    print("=" * 60)
    print("  MM PARAMETER OPTIMIZER")
    print("=" * 60)
    print()
    print(f"  [CONFIG]")
    print(f"    Symbol:       {symbol}")
    print(f"    Days:         {days}")
    print(f"    Capital:      ${capital:,.0f}")
    print(f"    Mode:         {mode}")
    print(f"    Combinations: {n_combos:,}")
    print()


def print_top_results(
    all_results: List[Tuple[Dict, Dict, float]],
    top_n: int = 20,
):
    print()
    print(f"  TOP {top_n} PARAMETER COMBINATIONS")
    print("=" * 120)
    header = (
        f"  {'Rank':>4}  {'Net PnL':>9}  {'Sharpe':>7}  {'Fills':>6}  {'Win%':>5}  "
        f"{'DD':>7}  {'Score':>8} | {'Spread':>6}  {'VolM':>5}  {'Skew':>5}  "
        f"{'Size':>5}  {'Lvl':>3}  {'Spc':>4}  {'MaxPos':>6}  {'Bias':>5}"
    )
    print(header)
    print("  " + "-" * 116)

    for rank, (params, rd, sc) in enumerate(all_results[:top_n], 1):
        bias_str = f"{params['bias_strength']:.1f}" if params.get('use_bias') else "OFF"
        print(
            f"  {rank:>4}  ${rd['net_pnl']:>8.2f}  {rd['sharpe_ratio']:>7.1f}  "
            f"{rd['total_fills']:>6}  {rd['win_pct']:>4.0f}%  "
            f"${rd['max_drawdown']:>6.2f}  {sc:>8.0f} | "
            f"{params['base_spread_bps']:>6.1f}  {params['vol_multiplier']:>5.1f}  "
            f"{params['inventory_skew_factor']:>5.1f}  "
            f"{params['order_size_usd']:>5.0f}  {params['num_levels']:>3}  "
            f"{params.get('level_spacing_bps', 1.0):>4.1f}  "
            f"{params['max_position_usd']:>6.0f}  {bias_str:>5}"
        )


def print_param_impact(impact: Dict[str, List[Dict]]):
    print()
    print("  PARAMETER IMPACT ANALYSIS")
    print("=" * 80)

    for key, rows in impact.items():
        if not rows:
            continue
        print(f"\n  {key}:")
        for row in rows:
            val_str = str(row['value'])
            print(
                f"    {val_str:>8}: Avg PnL ${row['avg_pnl']:>8.0f} | "
                f"Sharpe {row['avg_sharpe']:>6.1f} | "
                f"Profitable {row['profitable_pct']:>4.0f}%"
            )


# ---------------------------------------------------------------------------
# JSON save
# ---------------------------------------------------------------------------

def save_results_json(
    filepath: str,
    all_results: List[Tuple[Dict, Dict, float]],
    impact: Dict[str, List[Dict]],
    best_params: Dict,
    metadata: Dict,
):
    """Save full results to JSON."""
    # Convert results to serialisable list
    results_list = []
    for params, rd, sc in all_results:
        results_list.append({
            'params': {k: (str(v) if isinstance(v, bool) else v) for k, v in params.items()},
            'result': rd,
            'score': sc,
        })

    data = {
        'metadata': metadata,
        'best_params': best_params,
        'param_impact': impact,
        'all_results': results_list,
    }

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2, default=str)

    print(f"\n  Results saved to: {filepath}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="MM Parameter Optimizer")
    parser.add_argument("--symbol", default="BTCUSDT", help="Trading pair")
    parser.add_argument("--days", type=int, default=365, help="Days of data")
    parser.add_argument("--quick", action="store_true", help="Reduced grid (fast)")
    parser.add_argument("--full", action="store_true", help="Expanded grid (slow)")
    parser.add_argument("--workers", type=int, default=None, help="Parallel workers")
    parser.add_argument("--capital", type=float, default=1000.0, help="Starting capital ($)")
    parser.add_argument("--data-dir", default=None, help="Data cache directory")
    parser.add_argument("--save-json", action="store_true", help="Save results to JSON")
    parser.add_argument("--top-n", type=int, default=20, help="Show top N results")
    args = parser.parse_args()

    # Select grid
    if args.quick:
        grid = QUICK_GRID
        mode_label = "quick"
    elif args.full:
        grid = FULL_GRID
        mode_label = "full"
    else:
        grid = NORMAL_GRID
        mode_label = "normal"

    # Build combinations (skip bias_strength when use_bias=False)
    combos = build_combinations(grid)
    n_combos = len(combos)

    print_header(args.symbol, args.days, args.capital, mode_label, n_combos)

    # Locate data file
    data_dir = args.data_dir
    if data_dir is None:
        bothl_cache = Path(__file__).parent.parent.parent / "BotHL" / "data" / "cache"
        local_cache = Path(__file__).parent.parent / "data" / "cache"
        if bothl_cache.exists():
            data_dir = str(bothl_cache)
        elif local_cache.exists():
            data_dir = str(local_cache)
        else:
            print("  ERROR: No data directory found. Provide --data-dir")
            sys.exit(1)

    csv_file = os.path.join(data_dir, f"{args.symbol}_1h.csv")
    if not os.path.exists(csv_file):
        print(f"  ERROR: Data file not found: {csv_file}")
        try:
            print(f"  Available: {os.listdir(data_dir)}")
        except OSError:
            pass
        sys.exit(1)

    print(f"  Loading {args.symbol} data from {csv_file}...")
    candles = load_candles_csv(csv_file, args.days)
    print(f"  Loaded {len(candles)} candles ({args.days} days)")

    # Workers
    num_workers = args.workers
    if num_workers is None:
        num_workers = max(1, cpu_count() - 2)

    print(f"\n  Testing {n_combos:,} parameter combinations...")
    print(f"  Workers: {num_workers} parallel processes")

    start_time = time.perf_counter()

    # Run optimisation
    all_results: List[Tuple[Dict, Dict, float]] = []
    errors = 0

    try:
        if num_workers == 1:
            # Single-threaded (debug)
            _init_worker(candles, args.symbol, args.capital)
            iterator = tqdm(combos, desc="Optimizing", unit="combo") if HAS_TQDM else combos
            for combo in iterator:
                r = _run_single(combo)
                if r is not None:
                    all_results.append(r)
                else:
                    errors += 1
        else:
            with Pool(
                processes=num_workers,
                initializer=_init_worker,
                initargs=(candles, args.symbol, args.capital),
            ) as pool:
                if HAS_TQDM:
                    iterator = tqdm(
                        pool.imap_unordered(_run_single, combos),
                        total=n_combos,
                        desc="Optimizing",
                        unit="combo",
                        ncols=100,
                    )
                else:
                    iterator = pool.imap_unordered(_run_single, combos)
                    print("  (Install tqdm for progress bars: pip install tqdm)")

                for i, r in enumerate(iterator):
                    if r is not None:
                        all_results.append(r)
                    else:
                        errors += 1

                    if not HAS_TQDM and (i + 1) % 100 == 0:
                        print(f"  Progress: {i+1}/{n_combos} ({(i+1)/n_combos*100:.1f}%)", end="\r")

    except KeyboardInterrupt:
        print("\n\n  [!] Interrupted. Saving partial results...")

    elapsed = time.perf_counter() - start_time
    speed = len(all_results) / elapsed if elapsed > 0 else 0

    # Sort by score descending
    all_results.sort(key=lambda x: x[2], reverse=True)

    print(f"\n  Completed: {len(all_results):,}/{n_combos:,} in {elapsed:.1f}s ({speed:.1f} combo/s)")
    if errors:
        print(f"  Errors: {errors}")

    profitable = sum(1 for _, r, _ in all_results if r['net_pnl'] > 0)
    print(f"  Profitable: {profitable}/{len(all_results)} ({profitable/max(len(all_results),1)*100:.0f}%)")

    if not all_results:
        print("\n  No results. Check data and parameters.")
        sys.exit(1)

    # Print top results
    print_top_results(all_results, args.top_n)

    # Parameter impact analysis
    impact = compute_param_impact(all_results, grid)
    print_param_impact(impact)

    # Best params summary
    best_params, best_rd, best_sc = all_results[0]
    print()
    print("=" * 60)
    print("  BEST PARAMETERS")
    print("=" * 60)
    for k, v in sorted(best_params.items()):
        print(f"    {k:<25} = {v}")
    print(f"\n    Net PnL:   ${best_rd['net_pnl']:,.2f}")
    print(f"    Sharpe:    {best_rd['sharpe_ratio']:.2f}")
    print(f"    Fills:     {best_rd['total_fills']:,}")
    print(f"    Max DD:    ${best_rd['max_drawdown']:,.2f}")
    print(f"    Score:     {best_sc:,.0f}")
    print()

    # Save JSON
    if args.save_json:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        results_dir = os.path.join(project_root, "backtest", "results")
        filepath = os.path.join(results_dir, f"mm_optimization_{args.symbol}_{ts}.json")

        metadata = {
            'symbol': args.symbol,
            'days': args.days,
            'capital': args.capital,
            'mode': mode_label,
            'combinations': n_combos,
            'completed': len(all_results),
            'elapsed_seconds': round(elapsed, 1),
            'speed_combo_per_sec': round(speed, 1),
            'timestamp': datetime.now().isoformat(),
        }

        save_results_json(filepath, all_results, impact, best_params, metadata)

    print("  Done.")


if __name__ == "__main__":
    main()
