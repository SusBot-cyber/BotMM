"""Quick calc: supervisor gross/fees/net breakdown."""
equal_net = 51613
adaptive_net = 62670
capital = 50000

# From raw backtests: gross=$59,751, fees=$15,891, net=$43,858
raw_gross = 59751
raw_fees = 15891
raw_net = 43858
raw_vol = 135266250

# Fee/gross ratio is constant (0.015% of volume, volume scales with capital)
fee_ratio = raw_fees / raw_gross  # 26.6%
net_ratio = raw_net / raw_gross   # 73.4%

equal_gross = equal_net / net_ratio
equal_fees = equal_gross - equal_net
adaptive_gross = adaptive_net / net_ratio
adaptive_fees = adaptive_gross - adaptive_net

# Volume scales with effective capital
equal_vol = raw_vol * (equal_gross / raw_gross)
adaptive_vol = raw_vol * (adaptive_gross / raw_gross)

W = 80
print("=" * W)
print("  SUPERVISOR V3 + COMPOUND — PEŁNY BREAKDOWN (365d, $50K, fee +0.015%)")
print("=" * W)

print()
hdr = f"  {'':30} {'EQUAL':>12} {'SUPERVISOR V3':>14} {'Delta':>10}"
print(hdr)
print("  " + "-" * 70)
print(f"  {'Gross PnL':30} ${equal_gross:>11,.0f} ${adaptive_gross:>13,.0f}")
print(f"  {'Fees (maker 0.015%)':30} -${equal_fees:>10,.0f} -${adaptive_fees:>12,.0f}")
print(f"  {'NET PnL':30} ${equal_net:>11,.0f} ${adaptive_net:>13,.0f}   +{(adaptive_net-equal_net)/equal_net*100:.1f}%")
print("  " + "-" * 70)
print(f"  {'Return':30} {equal_net/capital*100:>11.1f}% {adaptive_net/capital*100:>13.1f}%")
print(f"  {'Final Equity':30} ${capital+equal_net:>11,.0f} ${capital+adaptive_net:>13,.0f}")
print(f"  {'Fee % of Gross':30} {fee_ratio*100:>11.1f}% {fee_ratio*100:>13.1f}%")
print()
print(f"  {'Monthly Net':30} ${equal_net/12:>11,.0f} ${adaptive_net/12:>13,.0f}")
print(f"  {'Daily Net':30} ${equal_net/365:>11,.0f} ${adaptive_net/365:>13,.0f}")
print()
print("  " + "-" * 70)
print(f"  {'Est. Total Volume':30} ${equal_vol:>11,.0f} ${adaptive_vol:>13,.0f}")
print(f"  {'Est. Daily Volume':30} ${equal_vol/365:>11,.0f} ${adaptive_vol/365:>13,.0f}")
print(f"  {'Est. Total Fills':30} {raw_vol/raw_gross*equal_gross/1875:>11,.0f} {raw_vol/raw_gross*adaptive_gross/1875:>13,.0f}")

print()
print("  " + "=" * 70)
print("  PER-ASSET (z supervisor ADAPTIVE V3):")
print("  " + "-" * 70)

# Per-asset from supervisor run
assets = {
    "BTC": {"net": 17599, "mode": "COMPOUND", "base": 12500, "eff_final": 30099},
    "ETH": {"net": 20165, "mode": "COMPOUND", "base": 12500, "eff_final": 32665},
    "SOL": {"net": 14130, "mode": "FIXED",    "base": 12500, "eff_final": 12500},
    "XRP": {"net": 11877, "mode": "FIXED",    "base": 12500, "eff_final": 12500},
}

print(f"  {'Asset':<7} {'Net PnL':>10} {'Gross(est)':>12} {'Fees(est)':>11} {'Return':>9} {'Final$':>10} {'Mode':>10}")
print("  " + "-" * 70)
total_n = 0
total_g = 0
total_f = 0
for sym, d in assets.items():
    g = d["net"] / net_ratio
    f = g - d["net"]
    ret = d["net"] / d["base"] * 100
    total_n += d["net"]
    total_g += g
    total_f += f
    print(f"  {sym:<7} ${d['net']:>9,} ${g:>11,.0f} ${f:>10,.0f} {ret:>8.1f}% ${d['eff_final']:>9,} {d['mode']:>10}")

print("  " + "-" * 70)
print(f"  {'TOTAL':<7} ${total_n:>9,} ${total_g:>11,.0f} ${total_f:>10,.0f} {total_n/capital*100:>8.1f}% ${capital+total_n:>9,}")
print()
print("=" * W)
