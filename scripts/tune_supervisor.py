"""Test supervisor tuning variants — compare allocation strategies."""
import sys, math, copy
from pathlib import Path
from typing import Dict, List
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.mm_backtester import MMBacktester, load_candles_csv
from bot_mm.config import QuoteParams
from scripts.backtest_supervisor import ASSETS, compute_score

symbols = list(ASSETS.keys())
CAPITAL = 50000
BASE_ALLOC = CAPITAL / len(symbols)
DAYS = 365


def load_daily_pnls():
    """Run backtests once, reuse for all variants."""
    data_dir = Path(__file__).parent.parent / "data" / "cache"
    daily_pnls = {}
    for sym in symbols:
        p = ASSETS[sym]
        scale = 1000.0 / 1000.0
        candles = load_candles_csv(str(data_dir / f"{sym}_1h.csv"), DAYS)
        qp = QuoteParams(
            base_spread_bps=p["spread"], vol_multiplier=1.5,
            inventory_skew_factor=p["skew"], order_size_usd=p["size"] * scale, num_levels=2
        )
        bt = MMBacktester(
            quote_params=qp, maker_fee=0.00015, taker_fee=0.00045,
            max_position_usd=1000 * 0.5, max_daily_loss=1000 * 0.05,
            capital=1000, use_bias=p["bias"], bias_strength=p["bias_str"],
            use_toxicity=True, use_auto_tune=True
        )
        r = bt.run(candles, sym)
        daily_pnls[sym] = r.daily_pnls
        print(f"  {sym}: {len(r.daily_pnls)}d, ${sum(r.daily_pnls):,.0f} base PnL", flush=True)
    min_days = min(len(v) for v in daily_pnls.values())
    for sym in symbols:
        daily_pnls[sym] = daily_pnls[sym][-min_days:]
    return daily_pnls, min_days


def simulate(daily_pnls, max_days, cfg):
    """Simulate supervisor with given config. Returns (total_pnl, sharpe, max_dd, profit_days_pct)."""
    window = cfg["window"]
    min_cap = cfg["min_capital"]
    max_pct = cfg["max_pct"]
    max_change = cfg["max_daily_change"]
    pause_thresh = cfg["pause_thresh"]
    punish_thresh = cfg["punish_thresh"]
    pause_cut = cfg["pause_cut"]
    punish_cut = cfg["punish_cut"]
    mean_revert = cfg.get("mean_revert", 0.0)

    allocs = {s: BASE_ALLOC for s in symbols}
    cpnl = {s: 0.0 for s in symbols}
    radj = {s: {"size_mult": 1.0, "spread_mult": 1.0} for s in symbols}
    daily_totals = []

    for day in range(max_days):
        day_total = 0.0
        for sym in symbols:
            eff = allocs[sym] + cpnl[sym] if ASSETS[sym]["compound"] else allocs[sym]
            scale = eff / 1000.0
            re = radj[sym]["size_mult"] * (2.0 - radj[sym]["spread_mult"])
            dp = daily_pnls[sym][day] * scale * re
            day_total += dp
            if ASSETS[sym]["compound"]:
                cpnl[sym] += dp
        daily_totals.append(day_total)

        if day >= window:
            scores = {}
            for sym in symbols:
                wp = []
                for d in range(day - window + 1, day + 1):
                    e2 = allocs[sym] + cpnl[sym] if ASSETS[sym]["compound"] else allocs[sym]
                    s2 = e2 / 1000.0
                    r2 = radj[sym]["size_mult"] * (2.0 - radj[sym]["spread_mult"])
                    wp.append(daily_pnls[sym][d] * s2 * r2)
                m = compute_score(wp)
                s_val = max(0, min(1, (m["sharpe"] + 2) / 17))
                r_val = max(0, min(1, 0.5 + m["return"] / (abs(m["return"]) + 100) * 0.5))
                ref = max(abs(m["return"]), 10)
                d_val = max(0, min(1, 1 - m["dd"] / ref))
                c_val = m["consistency"]
                scores[sym] = 0.40 * s_val + 0.30 * r_val + 0.20 * d_val + 0.10 * c_val

            # Allocation adjustments
            pool = 0.0
            for sym in symbols:
                score = scores[sym]
                current = allocs[sym]
                if score < pause_thresh:
                    change = min(current * pause_cut, current - min_cap)
                    change = min(change, current * max_change * 2)
                    if change > 0:
                        allocs[sym] = max(min_cap, current - change)
                        pool += change
                elif score < punish_thresh:
                    change = current * punish_cut
                    change = min(change, current * max_change)
                    if current - change >= min_cap:
                        allocs[sym] = current - change
                        pool += change

            # Mean revert toward base_alloc
            if mean_revert > 0:
                for sym in symbols:
                    diff = BASE_ALLOC - allocs[sym]
                    allocs[sym] += diff * mean_revert

            rewarded = [s for s in symbols if scores[s] > 0.7]
            if rewarded and pool > 0:
                share = pool / len(rewarded)
                for sym in rewarded:
                    current = allocs[sym]
                    max_allowed = CAPITAL * max_pct
                    add = min(share, max_allowed - current, current * max_change)
                    if add > 0:
                        allocs[sym] += add
                        pool -= add

            if pool > 1.0:
                hold_bots = [s for s in symbols if punish_thresh <= scores[s] <= 0.7]
                if hold_bots:
                    share = pool / len(hold_bots)
                    for sym in hold_bots:
                        add = min(share, CAPITAL * max_pct - allocs[sym])
                        if add > 0:
                            allocs[sym] += add

            # Risk adjustments
            for sym in symbols:
                score = scores[sym]
                if score >= 0.7:
                    tgt = cfg.get("reward_risk", {"size_mult": 1.10, "spread_mult": 0.90})
                elif score >= punish_thresh:
                    tgt = {"size_mult": 1.00, "spread_mult": 1.00}
                elif score >= pause_thresh:
                    tgt = cfg.get("punish_risk", {"size_mult": 0.70, "spread_mult": 1.30})
                else:
                    tgt = cfg.get("pause_risk", {"size_mult": 0.40, "spread_mult": 1.50})
                for k in tgt:
                    cur = radj[sym][k]
                    delta = max(-0.10, min(0.10, tgt[k] - cur))
                    radj[sym][k] = max(0.3, min(1.5, cur + delta))

    total = sum(daily_totals)
    arr = np.array(daily_totals)
    sharpe = np.mean(arr) / np.std(arr) * math.sqrt(365) if np.std(arr) > 0 else 0
    eq = CAPITAL + np.cumsum(arr)
    peak = np.maximum.accumulate(eq)
    dd = float(np.max(peak - eq))
    prof = sum(1 for x in daily_totals if x > 0) / len(daily_totals) * 100

    # Per-asset final
    asset_pnl = {}
    for sym in symbols:
        if ASSETS[sym]["compound"]:
            asset_pnl[sym] = cpnl[sym]
        else:
            # approximate
            total_sym = 0
            for d in range(max_days):
                e2 = allocs[sym]
                s2 = e2 / 1000.0
                r2 = radj[sym]["size_mult"] * (2.0 - radj[sym]["spread_mult"])
                total_sym += daily_pnls[sym][d] * s2 * r2
            asset_pnl[sym] = total_sym

    return {
        "pnl": total, "sharpe": sharpe, "max_dd": dd, "prof_pct": prof,
        "equity": CAPITAL + total, "asset_pnl": asset_pnl,
        "final_allocs": dict(allocs), "monthly": total / max_days * 30
    }


VARIANTS = {
    "V0_CURRENT": {
        "window": 14, "min_capital": 500, "max_pct": 0.35, "max_daily_change": 0.15,
        "pause_thresh": 0.2, "punish_thresh": 0.4, "pause_cut": 0.30, "punish_cut": 0.10,
    },
    "V1_GENTLE": {
        "window": 30, "min_capital": 2500, "max_pct": 0.40, "max_daily_change": 0.08,
        "pause_thresh": 0.15, "punish_thresh": 0.35, "pause_cut": 0.15, "punish_cut": 0.05,
    },
    "V2_SLOW_REVERT": {
        "window": 30, "min_capital": 2500, "max_pct": 0.40, "max_daily_change": 0.08,
        "pause_thresh": 0.15, "punish_thresh": 0.35, "pause_cut": 0.15, "punish_cut": 0.05,
        "mean_revert": 0.02,  # 2% daily mean revert to base_alloc
    },
    "V3_CONSERVATIVE": {
        "window": 45, "min_capital": 5000, "max_pct": 0.35, "max_daily_change": 0.05,
        "pause_thresh": 0.10, "punish_thresh": 0.30, "pause_cut": 0.10, "punish_cut": 0.03,
        "mean_revert": 0.01,
    },
    "V4_RISK_ONLY": {
        "window": 21, "min_capital": 12500, "max_pct": 0.25, "max_daily_change": 0.00,
        "pause_thresh": 0.2, "punish_thresh": 0.4, "pause_cut": 0.0, "punish_cut": 0.0,
        "punish_risk": {"size_mult": 0.60, "spread_mult": 1.40},
        "pause_risk": {"size_mult": 0.30, "spread_mult": 1.50},
    },
    "V5_EQUAL_WEIGHT": {
        "window": 14, "min_capital": 12500, "max_pct": 0.25, "max_daily_change": 0.0,
        "pause_thresh": 0.0, "punish_thresh": 0.0, "pause_cut": 0.0, "punish_cut": 0.0,
    },
}


def main():
    print("Loading backtests (one-time)...", flush=True)
    daily_pnls, max_days = load_daily_pnls()
    print(f"  {max_days} days loaded\n")

    results = {}
    for name, cfg in VARIANTS.items():
        print(f"  Simulating {name}...", end=" ", flush=True)
        r = simulate(daily_pnls, max_days, cfg)
        results[name] = r
        print(f"PnL=${r['pnl']:>8,.0f}  Sharpe={r['sharpe']:.1f}  DD=${r['max_dd']:>6,.0f}  Prof={r['prof_pct']:.0f}%")

    # Summary table
    print()
    print("=" * 110)
    print("  SUPERVISOR TUNING COMPARISON — 365d, 4 assets, $50K, real fee (+0.015%)")
    print("=" * 110)
    print()
    print(f"  {'Variant':<20} {'Net PnL':>9} {'Return':>8} {'Sharpe':>8} {'MaxDD':>8} {'Prof%':>7} {'Monthly':>9} {'Equity':>10}")
    print("  " + "-" * 95)

    best_pnl = max(r["pnl"] for r in results.values())
    for name, r in results.items():
        marker = " <-- BEST" if r["pnl"] == best_pnl else ""
        print(f"  {name:<20} ${r['pnl']:>8,.0f} {r['pnl']/CAPITAL*100:>7.1f}% {r['sharpe']:>8.1f} ${r['max_dd']:>7,.0f} {r['prof_pct']:>6.0f}% ${r['monthly']:>8,.0f} ${r['equity']:>9,.0f}{marker}")

    # Per-asset breakdown for top 3
    sorted_variants = sorted(results.items(), key=lambda x: x[1]["pnl"], reverse=True)

    print()
    print("  PER-ASSET PNL — TOP 3 VARIANTS")
    print("  " + "-" * 80)
    hdr = f"  {'Variant':<20}"
    for sym in symbols:
        hdr += f" | {sym.replace('USDT',''):>8}"
    hdr += f" | {'TOTAL':>8}"
    print(hdr)
    print("  " + "-" * 80)

    for name, r in sorted_variants[:3]:
        row = f"  {name:<20}"
        for sym in symbols:
            row += f" | ${r['asset_pnl'].get(sym, 0):>7,.0f}"
        row += f" | ${r['pnl']:>7,.0f}"
        print(row)

    # Config details for top 3
    print()
    print("  CONFIG DETAILS — TOP 3")
    print("  " + "-" * 90)
    for name, r in sorted_variants[:3]:
        cfg = VARIANTS[name]
        print(f"  {name}:")
        print(f"    window={cfg['window']}d  min_cap=${cfg['min_capital']:,}  max_pct={cfg['max_pct']}  max_change={cfg['max_daily_change']}")
        print(f"    pause<{cfg['pause_thresh']} (cut {cfg['pause_cut']*100:.0f}%)  punish<{cfg['punish_thresh']} (cut {cfg['punish_cut']*100:.0f}%)  mean_revert={cfg.get('mean_revert', 0)}")
        print()

    print("=" * 110)


if __name__ == "__main__":
    main()
