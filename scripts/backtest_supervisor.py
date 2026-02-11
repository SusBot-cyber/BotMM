#!/usr/bin/env python3
"""
Meta-Supervisor Backtest — simulate adaptive capital allocation across 5 bots.

Runs backtests for all assets, extracts daily PnL, then simulates:
1. EQUAL: fixed equal allocation (baseline)
2. ADAPTIVE: score-based reallocation every day using 14d rolling window

Scoring: 40% Sharpe + 30% PnL/capital + 20% (1-DD) + 10% consistency
Allocation: score>0.7 → +10%, 0.4-0.7 → hold, 0.2-0.4 → -15%, <0.2 → pause

Usage:
    py scripts/backtest_supervisor.py --capital 50000 --days 365
"""

import sys
import os
import math
import argparse
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from backtest.mm_backtester import MMBacktester, load_candles_csv, Candle
from bot_mm.config import QuoteParams

# Asset profiles (optimal params from optimizer)
ASSETS = {
    "BTCUSDT": {"spread": 2.0, "skew": 0.3, "size": 150, "bias": True, "bias_str": 0.2, "compound": True},
    "ETHUSDT": {"spread": 1.5, "skew": 0.3, "size": 150, "bias": True, "bias_str": 0.2, "compound": True},
    "SOLUSDT": {"spread": 1.5, "skew": 0.5, "size": 150, "bias": False, "bias_str": 0.0, "compound": False},
    "XRPUSDT": {"spread": 1.5, "skew": 0.5, "size": 150, "bias": True, "bias_str": 0.2, "compound": False},
    # "HYPEUSDT": {"spread": 1.5, "skew": 0.3, "size": 100, "bias": False, "bias_str": 0.0, "compound": False},  # removed: poor performance
}


def run_asset_backtest(symbol: str, days: int, capital: float) -> List[float]:
    """Run backtest and return daily PnL list (scaled to $1K base)."""
    p = ASSETS[symbol]

    # Find data
    for d in [
        project_root.parent / "BotHL" / "data" / "cache",
        project_root / "data" / "cache",
    ]:
        csv = d / f"{symbol}_1h.csv"
        if csv.exists():
            break
    else:
        print(f"  {symbol}: no data found, skipping")
        return []

    candles = load_candles_csv(str(csv), days)

    # Scale size proportionally to capital (base is $1K)
    scale = capital / 1000.0
    qp = QuoteParams(
        base_spread_bps=p["spread"],
        vol_multiplier=1.5,
        inventory_skew_factor=p["skew"],
        order_size_usd=p["size"] * scale,
        num_levels=2,
    )

    bt = MMBacktester(
        quote_params=qp,
        max_position_usd=capital * 0.5,
        max_daily_loss=capital * 0.05,
        capital=capital,
        use_bias=p["bias"],
        bias_strength=p["bias_str"],
        use_toxicity=True,
        use_auto_tune=True,
    )

    result = bt.run(candles, symbol)
    return result.daily_pnls


def compute_score(daily_pnls: List[float]) -> Dict[str, float]:
    """Compute scoring metrics from daily PnL window."""
    if len(daily_pnls) < 3:
        return {"sharpe": 0, "return": 0, "dd": 1.0, "consistency": 0, "score": 0}

    arr = np.array(daily_pnls)
    mean = np.mean(arr)
    std = np.std(arr)
    sharpe = (mean / std * math.sqrt(365)) if std > 0 else 0

    total_return = np.sum(arr)
    consistency = np.sum(arr > 0) / len(arr)

    # Max drawdown in window
    cumsum = np.cumsum(arr)
    peak = np.maximum.accumulate(cumsum)
    dd = np.max(peak - cumsum) if len(cumsum) > 0 else 0

    return {
        "sharpe": sharpe,
        "return": total_return,
        "dd": dd,
        "consistency": consistency,
    }


def rank_normalize(values: List[float]) -> List[float]:
    """Rank-based normalization to [0, 1]. Best = 1.0, worst = 0.0."""
    n = len(values)
    if n <= 1:
        return [0.5] * n
    ranked = sorted(range(n), key=lambda i: values[i])
    result = [0.0] * n
    for rank, idx in enumerate(ranked):
        result[idx] = rank / (n - 1)
    return result


def compute_scores_ranked(metrics_list: List[Dict]) -> List[float]:
    """Compute composite scores with absolute thresholds (not rank-based).

    Score based on absolute performance:
    - Sharpe > 5 = good (1.0), < 0 = bad (0.0)
    - Return > 0 = good, < 0 = bad
    - Consistency > 70% = good, < 30% = bad
    - DD < 5% of return = good, > 50% = bad
    """
    scores = []
    for m in metrics_list:
        # Sharpe: clamp to [0, 1] based on [-2, 15] range
        s = max(0, min(1, (m["sharpe"] + 2) / 17))

        # Return: sigmoid-like, positive = good
        r = max(0, min(1, 0.5 + m["return"] / (abs(m["return"]) + 100) * 0.5)) if True else 0.5

        # Drawdown: lower is better, normalize by return magnitude
        ref = max(abs(m["return"]), 10)
        d = max(0, min(1, 1 - m["dd"] / ref))

        # Consistency: direct [0, 1]
        c = m["consistency"]

        score = 0.40 * s + 0.30 * r + 0.20 * d + 0.10 * c
        scores.append(score)

    return scores


def apply_allocation(
    allocations: Dict[str, float],
    scores: Dict[str, float],
    total_capital: float,
    min_capital: float = 500.0,
    max_pct: float = 0.35,
    max_daily_change: float = 0.15,
) -> Dict[str, float]:
    """Apply reward/punishment rules. Returns new allocations."""
    new_alloc = dict(allocations)
    pool = 0.0  # capital freed from punished bots

    symbols = list(allocations.keys())

    # Phase 1: Punish low scorers, free up capital
    for sym in symbols:
        score = scores.get(sym, 0.5)
        current = new_alloc[sym]

        if score < 0.2:  # PAUSE
            change = min(current * 0.30, current - min_capital)
            change = min(change, current * max_daily_change * 2)
            if change > 0:
                new_alloc[sym] = max(min_capital, current - change)
                pool += change
        elif score < 0.4:  # PUNISH
            change = current * 0.10
            change = min(change, current * max_daily_change)
            if current - change >= min_capital:
                new_alloc[sym] = current - change
                pool += change

    # Phase 2: Reward high scorers with freed capital
    rewarded = [s for s in symbols if scores.get(s, 0.5) > 0.7]
    if rewarded and pool > 0:
        share = pool / len(rewarded)
        for sym in rewarded:
            current = new_alloc[sym]
            max_allowed = total_capital * max_pct
            add = min(share, max_allowed - current, current * max_daily_change)
            if add > 0:
                new_alloc[sym] += add
                pool -= add

    # Phase 3: Distribute remaining pool equally among HOLD bots
    if pool > 1.0:
        hold_bots = [s for s in symbols if 0.4 <= scores.get(s, 0.5) <= 0.7]
        if hold_bots:
            share = pool / len(hold_bots)
            for sym in hold_bots:
                current = new_alloc[sym]
                max_allowed = total_capital * max_pct
                add = min(share, max_allowed - current)
                if add > 0:
                    new_alloc[sym] += add

    return new_alloc


def compute_risk_adjustments(
    scores: Dict[str, float],
    current_risk: Dict[str, Dict[str, float]] = None,
    max_risk_change: float = 0.10,
) -> Dict[str, Dict[str, float]]:
    """Compute risk parameter multipliers based on bot scores.

    Returns per-bot dict with:
      - size_mult: order size multiplier (0.3-1.2)
      - spread_mult: spread multiplier (0.8-1.5, higher = wider = safer)
      - max_pos_mult: max position multiplier (0.3-1.2)
      - max_loss_mult: max daily loss multiplier (0.3-1.0)

    Risk adjusts FASTER than capital — immediate response to bad performance.
    """
    targets = {
        "reward":  {"size_mult": 1.10, "spread_mult": 0.90, "max_pos_mult": 1.10, "max_loss_mult": 1.00},
        "hold":    {"size_mult": 1.00, "spread_mult": 1.00, "max_pos_mult": 1.00, "max_loss_mult": 1.00},
        "punish":  {"size_mult": 0.70, "spread_mult": 1.30, "max_pos_mult": 0.70, "max_loss_mult": 0.70},
        "pause":   {"size_mult": 0.40, "spread_mult": 1.50, "max_pos_mult": 0.40, "max_loss_mult": 0.40},
    }

    BOUNDS = {
        "size_mult":    (0.30, 1.20),
        "spread_mult":  (0.80, 1.50),
        "max_pos_mult": (0.30, 1.20),
        "max_loss_mult":(0.30, 1.00),
    }

    result = {}
    for sym, score in scores.items():
        if score >= 0.7:
            target = targets["reward"]
        elif score >= 0.4:
            target = targets["hold"]
        elif score >= 0.2:
            target = targets["punish"]
        else:
            target = targets["pause"]

        adj = {}
        for param, tgt in target.items():
            lo, hi = BOUNDS[param]
            # Smooth transition: blend current → target with rate limit
            if current_risk and sym in current_risk and param in current_risk[sym]:
                cur = current_risk[sym][param]
                delta = tgt - cur
                delta = max(-max_risk_change, min(max_risk_change, delta))
                val = cur + delta
            else:
                val = tgt
            adj[param] = max(lo, min(hi, val))

        result[sym] = adj

    return result


def main():
    parser = argparse.ArgumentParser(description="Meta-Supervisor Backtest Simulation")
    parser.add_argument("--capital", type=float, default=50000, help="Total capital ($)")
    parser.add_argument("--days", type=int, default=365, help="Backtest period")
    parser.add_argument("--window", type=int, default=14, help="Scoring window (days)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    args = parser.parse_args()

    np.random.seed(args.seed)
    symbols = list(ASSETS.keys())
    n_assets = len(symbols)
    base_alloc = args.capital / n_assets

    print("=" * 70)
    print(f"  META-SUPERVISOR BACKTEST SIMULATION")
    print("=" * 70)
    print(f"  Total capital: ${args.capital:,.0f}")
    print(f"  Assets: {', '.join(symbols)}")
    print(f"  Base allocation: ${base_alloc:,.0f}/asset")
    print(f"  Scoring window: {args.window}d rolling")
    print(f"  Period: {args.days} days")
    print()

    # Step 1: Run backtests at $1K base to get daily PnL ratios
    print("  Phase 1: Running backtests (base capital)...")
    daily_pnls = {}
    for sym in symbols:
        print(f"    {sym}...", end=" ", flush=True)
        dpnl = run_asset_backtest(sym, args.days, 1000.0)
        daily_pnls[sym] = dpnl
        print(f"{len(dpnl)} days, total ${sum(dpnl):,.0f}")

    # Trim all series to shortest (so all assets cover same period)
    min_days = min(len(v) for v in daily_pnls.values())
    max_days_raw = max(len(v) for v in daily_pnls.values())
    print(f"\n  Day range: {min_days}-{max_days_raw} days")
    print(f"  Trimming all to {min_days} days (last {min_days}d of each asset)")

    for sym in symbols:
        if len(daily_pnls[sym]) > min_days:
            daily_pnls[sym] = daily_pnls[sym][-min_days:]
    max_days = min_days

    # Step 2: Simulate EQUAL allocation (with compound for BTC/ETH)
    print("\n  Phase 2: Simulating EQUAL allocation...")
    equal_daily = []
    # Track per-asset compound equity (starts at base_alloc)
    eq_equity = {sym: base_alloc for sym in symbols}
    for day in range(max_days):
        day_total = 0.0
        for sym in symbols:
            # PnL scales by current equity (compound) or fixed alloc
            effective = eq_equity[sym]
            day_pnl = daily_pnls[sym][day] * (effective / 1000.0)
            day_total += day_pnl
            # Compound: reinvest PnL into equity for BTC/ETH
            if ASSETS[sym]["compound"]:
                eq_equity[sym] += day_pnl
        equal_daily.append(day_total)

    equal_total = sum(equal_daily)
    equal_equity_curve = [args.capital + sum(equal_daily[:i+1]) for i in range(len(equal_daily))]
    equal_peak = np.maximum.accumulate(equal_equity_curve)
    equal_dd = max(equal_peak - np.array(equal_equity_curve))
    equal_sharpe = np.mean(equal_daily) / np.std(equal_daily) * math.sqrt(365) if np.std(equal_daily) > 0 else 0

    # Step 3: Simulate ADAPTIVE supervisor
    # Supervisor controls BASE allocation only. Compound assets reinvest PnL on top.
    print("  Phase 3: Simulating ADAPTIVE supervisor...")
    allocations = {sym: base_alloc for sym in symbols}  # supervisor-controlled base
    # Compound equity tracks cumulative PnL per asset (on top of base)
    compound_pnl = {sym: 0.0 for sym in symbols}
    risk_adj = {sym: {"size_mult": 1.0, "spread_mult": 1.0, "max_pos_mult": 1.0, "max_loss_mult": 1.0} for sym in symbols}
    adaptive_daily = []
    alloc_history = {sym: [base_alloc] for sym in symbols}
    action_log = {sym: [] for sym in symbols}

    for day in range(max_days):
        day_total = 0.0
        for sym in symbols:
            # Effective capital = supervisor base + compound PnL (if compound asset)
            if ASSETS[sym]["compound"]:
                effective = allocations[sym] + compound_pnl[sym]
            else:
                effective = allocations[sym]
            scale = effective / 1000.0
            risk_effect = risk_adj[sym]["size_mult"] * (2.0 - risk_adj[sym]["spread_mult"])
            day_pnl = daily_pnls[sym][day] * scale * risk_effect
            day_total += day_pnl
            # Compound: accumulate PnL
            if ASSETS[sym]["compound"]:
                compound_pnl[sym] += day_pnl
        adaptive_daily.append(day_total)

        # After scoring window, rebalance base allocations daily
        if day >= args.window:
            metrics = {}
            scores_dict = {}
            for sym in symbols:
                window_pnl = []
                for d in range(day - args.window + 1, day + 1):
                    if ASSETS[sym]["compound"]:
                        eff = allocations[sym] + compound_pnl[sym]
                    else:
                        eff = allocations[sym]
                    scale = eff / 1000.0
                    r_eff = risk_adj[sym]["size_mult"] * (2.0 - risk_adj[sym]["spread_mult"])
                    window_pnl.append(daily_pnls[sym][d] * scale * r_eff)
                metrics[sym] = compute_score(window_pnl)

            metrics_list = [metrics[sym] for sym in symbols]
            scores_list = compute_scores_ranked(metrics_list)
            for i, sym in enumerate(symbols):
                scores_dict[sym] = scores_list[i]

            # Supervisor adjusts BASE allocation only (not compound equity)
            new_alloc = apply_allocation(
                allocations, scores_dict, args.capital
            )
            allocations = new_alloc

            risk_adj = compute_risk_adjustments(scores_dict, risk_adj)

        for sym in symbols:
            alloc_history[sym].append(allocations[sym])

    adaptive_total = sum(adaptive_daily)
    adaptive_equity = [args.capital + sum(adaptive_daily[:i+1]) for i in range(len(adaptive_daily))]
    adaptive_peak = np.maximum.accumulate(adaptive_equity)
    adaptive_dd = max(adaptive_peak - np.array(adaptive_equity))
    adaptive_sharpe = np.mean(adaptive_daily) / np.std(adaptive_daily) * math.sqrt(365) if np.std(adaptive_daily) > 0 else 0

    # Step 4: Print results
    improvement = (adaptive_total - equal_total) / abs(equal_total) * 100 if equal_total != 0 else 0

    print(f"\n{'=' * 70}")
    print(f"  RESULTS — {max_days} days")
    print(f"{'=' * 70}")
    print()
    print(f"  {'Metric':<30} {'EQUAL':>15} {'ADAPTIVE':>15} {'Delta':>10}")
    print(f"  {'-'*70}")
    print(f"  {'Net PnL':<30} {'$'+format(equal_total,',.0f'):>15} {'$'+format(adaptive_total,',.0f'):>15} {improvement:>+9.1f}%")
    print(f"  {'Return':<30} {equal_total/args.capital*100:>14.1f}% {adaptive_total/args.capital*100:>14.1f}%")
    print(f"  {'Sharpe':<30} {equal_sharpe:>15.1f} {adaptive_sharpe:>15.1f}")
    print(f"  {'Max Drawdown':<30} {'$'+format(equal_dd,',.0f'):>15} {'$'+format(adaptive_dd,',.0f'):>15}")
    print(f"  {'Final Equity':<30} {'$'+format(args.capital+equal_total,',.0f'):>15} {'$'+format(args.capital+adaptive_total,',.0f'):>15}")

    # Profitable days
    eq_pos = sum(1 for d in equal_daily if d > 0)
    ad_pos = sum(1 for d in adaptive_daily if d > 0)
    print(f"  {'Profitable Days':<30} {eq_pos}/{max_days} ({eq_pos/max_days*100:.0f}%){' ':>3} {ad_pos}/{max_days} ({ad_pos/max_days*100:.0f}%)")

    # Monthly/Annual
    eq_monthly = equal_total / max_days * 30
    ad_monthly = adaptive_total / max_days * 30
    print(f"  {'Monthly (avg)':<30} {'$'+format(eq_monthly,',.0f'):>15} {'$'+format(ad_monthly,',.0f'):>15}")

    # Per-asset final state
    print(f"\n  FINAL STATE (base=supervisor, compound=accumulated PnL)")
    print(f"  {'-'*80}")
    print(f"  {'Asset':<12} {'Base':>10} {'Compound':>10} {'Effective':>10}  {'Size':>5} {'Spread':>6} {'Mode':>8}")
    print(f"  {'-'*80}")
    for sym in symbols:
        final_alloc = alloc_history[sym][-1]
        cpnl = compound_pnl.get(sym, 0)
        effective = final_alloc + cpnl if ASSETS[sym]["compound"] else final_alloc
        ra = risk_adj[sym]
        mode = "COMPOUND" if ASSETS[sym]["compound"] else "FIXED"
        print(f"  {sym:<12} ${final_alloc:>8,.0f} ${cpnl:>8,.0f} ${effective:>8,.0f}  {ra['size_mult']:>5.2f} {ra['spread_mult']:>6.2f} {mode:>8}")

    # Per-asset PnL comparison (equal uses compound too for BTC/ETH)
    print(f"\n  PER-ASSET PnL COMPARISON (equal includes compound for BTC/ETH)")
    print(f"  {'-'*70}")
    print(f"  {'Asset':<12} {'EQUAL':>12} {'ADAPTIVE':>12} {'Delta':>10} {'Mode':>10}")
    print(f"  {'-'*70}")
    total_eq_check = sum(equal_daily)
    total_ad_check = sum(adaptive_daily)
    # Reconstruct per-asset PnL from equity tracking
    # For EQUAL: use eq_equity final - base
    for sym in symbols:
        eq_pnl = eq_equity[sym] - base_alloc
        if ASSETS[sym]["compound"]:
            ad_pnl = compound_pnl[sym]
        else:
            ad_pnl_sum = 0
            for d in range(max_days):
                eff = allocations[sym]
                scale = eff / 1000.0
                r_eff = risk_adj[sym]["size_mult"] * (2.0 - risk_adj[sym]["spread_mult"])
                ad_pnl_sum += daily_pnls[sym][d] * scale * r_eff
            ad_pnl = ad_pnl_sum
        delta = (ad_pnl - eq_pnl) / abs(eq_pnl) * 100 if eq_pnl != 0 else 0
        mode = "COMPOUND" if ASSETS[sym]["compound"] else "FIXED"
        print(f"  {sym:<12} ${eq_pnl:>10,.0f} ${ad_pnl:>10,.0f} {delta:>+9.1f}% {mode:>10}")
    print(f"  {'-'*70}")
    delta_total = (total_ad_check - total_eq_check) / abs(total_eq_check) * 100 if total_eq_check != 0 else 0
    print(f"  {'TOTAL':<12} ${total_eq_check:>10,.0f} ${total_ad_check:>10,.0f} {delta_total:>+9.1f}%")

    print(f"\n{'=' * 70}")


if __name__ == "__main__":
    main()
