"""Monthly breakdown with supervisor + compound — standalone script."""
import sys, math
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.backtest_supervisor import (
    run_asset_backtest, ASSETS, compute_score,
    compute_scores_ranked, apply_allocation, compute_risk_adjustments
)

symbols = list(ASSETS.keys())
capital = 50000
base_alloc = capital / len(symbols)
days = 225
window = 14

print("Loading backtests...", flush=True)
daily_pnls = {}
for sym in symbols:
    daily_pnls[sym] = run_asset_backtest(sym, days, 1000.0)
    print(f"  {sym}: {len(daily_pnls[sym])}d", flush=True)

min_days = min(len(v) for v in daily_pnls.values())
for sym in symbols:
    daily_pnls[sym] = daily_pnls[sym][-min_days:]

# Simulate ADAPTIVE
allocs = {s: base_alloc for s in symbols}
cpnl = {s: 0.0 for s in symbols}
radj = {s: {"size_mult": 1.0, "spread_mult": 1.0, "max_pos_mult": 1.0, "max_loss_mult": 1.0} for s in symbols}
asset_daily = {s: [] for s in symbols}
alloc_hist = {s: [] for s in symbols}

for day in range(min_days):
    for sym in symbols:
        eff = allocs[sym] + cpnl[sym] if ASSETS[sym]["compound"] else allocs[sym]
        scale = eff / 1000.0
        re = radj[sym]["size_mult"] * (2.0 - radj[sym]["spread_mult"])
        dp = daily_pnls[sym][day] * scale * re
        asset_daily[sym].append(dp)
        if ASSETS[sym]["compound"]:
            cpnl[sym] += dp
        alloc_hist[sym].append(allocs[sym])

    if day >= window:
        metrics = {}
        for sym in symbols:
            wp = []
            for d in range(day - window + 1, day + 1):
                e2 = allocs[sym] + cpnl[sym] if ASSETS[sym]["compound"] else allocs[sym]
                s2 = e2 / 1000.0
                r2 = radj[sym]["size_mult"] * (2.0 - radj[sym]["spread_mult"])
                wp.append(daily_pnls[sym][d] * s2 * r2)
            metrics[sym] = compute_score(wp)
        ml = [metrics[sym] for sym in symbols]
        sl = compute_scores_ranked(ml)
        sd = {sym: sl[i] for i, sym in enumerate(symbols)}
        allocs = apply_allocation(allocs, sd, capital)
        radj = compute_risk_adjustments(sd, radj)

# Build months
months = []
n_full = min_days // 30
for m in range(n_full):
    months.append((f"M{m+1}", m*30, (m+1)*30))
rem = min_days - n_full * 30
if rem > 0:
    months.append((f"M{n_full+1}*", n_full*30, min_days))

syms_short = [s.replace("USDT", "") for s in symbols]

# === PRINT ===
print()
print("=" * 115)
print("  MONTHLY BREAKDOWN — SUPERVISOR + COMPOUND ($50K, 4 assets, 225d)")
print("=" * 115)

# NET PNL
print()
print("  NET PNL ($)")
print(f"  {'Mo':<5} {'Days':>4}", end="")
for s in syms_short: print(f" | {s:>8}", end="")
print(f" | {'TOTAL':>8} | {'CumPnL':>9} | {'Equity':>9}")
print("  " + "-" * 100)

cum = 0.0
cum_asset = {s: 0.0 for s in symbols}
for label, s, e in months:
    d = e - s
    print(f"  {label:<5} {d:>4}", end="")
    mt = 0.0
    for sym in symbols:
        mp = sum(asset_daily[sym][s:e])
        cum_asset[sym] += mp
        mt += mp
        print(f" | ${mp:>7,.0f}", end="")
    cum += mt
    print(f" | ${mt:>7,.0f} | ${cum:>8,.0f} | ${capital+cum:>8,.0f}")

print("  " + "-" * 100)
print(f"  {'TOT':<5} {min_days:>4}", end="")
for sym in symbols: print(f" | ${cum_asset[sym]:>7,.0f}", end="")
print(f" | ${cum:>7,.0f} |           | ${capital+cum:>8,.0f}")

# EFFECTIVE CAPITAL
print()
print("  EFFECTIVE CAPITAL AT MONTH END ($)")
print(f"  {'Mo':<5} {'Days':>4}", end="")
for s in syms_short: print(f" | {s:>10}", end="")
print(f" | {'TOTAL':>10}")
print("  " + "-" * 75)

for label, s, e in months:
    d = e - s
    day_idx = e - 1
    print(f"  {label:<5} {d:>4}", end="")
    mt = 0.0
    for sym in symbols:
        base = alloc_hist[sym][min(day_idx, len(alloc_hist[sym])-1)]
        comp = sum(asset_daily[sym][:e]) if ASSETS[sym]["compound"] else 0
        eff = base + comp
        mt += eff
        print(f" | ${eff:>9,.0f}", end="")
    print(f" | ${mt:>9,.0f}")

# SHARPE
print()
print("  SHARPE RATIO")
print(f"  {'Mo':<5} {'Days':>4}", end="")
for s in syms_short: print(f" | {s:>8}", end="")
print(f" | {'PORT':>8}")
print("  " + "-" * 65)

for label, s, e in months:
    d = e - s
    print(f"  {label:<5} {d:>4}", end="")
    for sym in symbols:
        ad = asset_daily[sym][s:e]
        sh = np.mean(ad) / np.std(ad) * math.sqrt(365) if np.std(ad) > 0 else 0
        print(f" | {sh:>8.1f}", end="")
    td = [sum(asset_daily[sym][day] for sym in symbols) for day in range(s, e)]
    psh = np.mean(td) / np.std(td) * math.sqrt(365) if np.std(td) > 0 else 0
    print(f" | {psh:>8.1f}")

# PROFITABLE DAYS
print()
print("  PROFITABLE DAYS (%)")
print(f"  {'Mo':<5} {'Days':>4}", end="")
for s in syms_short: print(f" | {s:>8}", end="")
print(f" | {'PORT':>8}")
print("  " + "-" * 65)

for label, s, e in months:
    d = e - s
    print(f"  {label:<5} {d:>4}", end="")
    for sym in symbols:
        ad = asset_daily[sym][s:e]
        pos = sum(1 for x in ad if x > 0)
        print(f" | {pos/len(ad)*100:>7.0f}%", end="")
    td = [sum(asset_daily[sym][day] for sym in symbols) for day in range(s, e)]
    ppos = sum(1 for x in td if x > 0)
    print(f" | {ppos/len(td)*100:>7.0f}%")

# SUMMARY
print()
print("  PER-ASSET SUMMARY (Supervisor + Compound)")
print("  " + "-" * 95)
print(f"  {'Asset':<8} {'Net PnL':>9} {'Return':>8} {'Best mo':>9} {'Worst mo':>9} {'Sharpe':>8} {'Mode':>10} {'Final Cap':>10}")
print("  " + "-" * 95)
for sym in symbols:
    total = cum_asset[sym]
    mode = "COMPOUND" if ASSETS[sym]["compound"] else "FIXED"
    short = sym.replace("USDT", "")
    all_ad = asset_daily[sym]
    sh = np.mean(all_ad) / np.std(all_ad) * math.sqrt(365) if np.std(all_ad) > 0 else 0
    monthly_pnls = [sum(asset_daily[sym][s:e]) for _, s, e in months]
    final_base = alloc_hist[sym][-1]
    final_comp = sum(asset_daily[sym]) if ASSETS[sym]["compound"] else 0
    final_eff = final_base + final_comp
    print(f"  {short:<8} ${total:>8,.0f} {total/base_alloc*100:>7.1f}% ${max(monthly_pnls):>8,.0f} ${min(monthly_pnls):>8,.0f} {sh:>8.1f} {mode:>10} ${final_eff:>9,.0f}")

print()
all_total = [sum(asset_daily[sym][d] for sym in symbols) for d in range(min_days)]
port_sh = np.mean(all_total) / np.std(all_total) * math.sqrt(365) if np.std(all_total) > 0 else 0
print(f"  PORTFOLIO: ${cum:>,.0f} PnL | {cum/capital*100:.1f}% return | Sharpe {port_sh:.1f} | ${capital+cum:>,.0f} equity")
print("  * = partial month (15 days)")
print("=" * 115)
