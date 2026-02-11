#!/usr/bin/env python3
"""Detailed 365d backtest — full stats per asset with fees, volume, fills."""
import sys, math
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.mm_backtester import MMBacktester, load_candles_csv
from bot_mm.config import QuoteParams
from scripts.backtest_supervisor import ASSETS

CAPITAL = 50000
DAYS = 365
symbols = list(ASSETS.keys())
BASE_ALLOC = CAPITAL / len(symbols)


def run_full(sym, capital):
    p = ASSETS[sym]
    data_dir = Path(__file__).parent.parent / "data" / "cache"
    candles = load_candles_csv(str(data_dir / f"{sym}_1h.csv"), DAYS)
    scale = capital / 1000.0
    qp = QuoteParams(
        base_spread_bps=p["spread"], vol_multiplier=1.5,
        inventory_skew_factor=p["skew"], order_size_usd=p["size"] * scale, num_levels=2
    )
    bt = MMBacktester(
        quote_params=qp, maker_fee=0.00015, taker_fee=0.00045,
        max_position_usd=capital * 0.5, max_daily_loss=capital * 0.05,
        capital=capital, use_bias=p["bias"], bias_strength=p["bias_str"],
        use_toxicity=True, use_auto_tune=True
    )
    return bt.run(candles, sym)


def main():
    results = {}
    print("Running per-asset backtests at $12,500 capital...\n")
    for sym in symbols:
        print(f"  {sym}...", end=" ", flush=True)
        r = run_full(sym, BASE_ALLOC)
        results[sym] = r
        print(f"done ({r.days}d, {r.total_fills} fills)")

    # Volume calc: fills * avg_size
    # Fee calc from result
    print()
    W = 90  # table width

    # ════════════ HEADER ════════════
    print("═" * W)
    print(f"  DETAILED BACKTEST — {DAYS}d, {len(symbols)} assets, ${CAPITAL:,} capital, real fee +0.015%")
    print("═" * W)

    # ════════════ PER-ASSET TABLE ════════════
    print()
    print("  ┌─────────┬───────────┬───────────┬───────────┬──────────┬──────────┬─────────┐")
    print("  │ Asset   │ Gross PnL │   Fees    │  Net PnL  │  Return  │  Sharpe  │ Compound│")
    print("  ├─────────┼───────────┼───────────┼───────────┼──────────┼──────────┼─────────┤")

    totals = {"gross": 0, "fees": 0, "net": 0}
    for sym in symbols:
        r = results[sym]
        s = sym.replace("USDT", "")
        mode = "ON" if ASSETS[sym]["compound"] else "OFF"
        ret = r.net_pnl / BASE_ALLOC * 100
        totals["gross"] += r.gross_pnl
        totals["fees"] += r.total_fees
        totals["net"] += r.net_pnl
        print(f"  │ {s:<7} │ ${r.gross_pnl:>8,.0f} │ ${r.total_fees:>8,.0f} │ ${r.net_pnl:>8,.0f} │ {ret:>6.1f}%  │ {r.sharpe_ratio:>7.1f}  │  {mode:<5}  │")
        if sym != symbols[-1]:
            print("  ├─────────┼───────────┼───────────┼───────────┼──────────┼──────────┼─────────┤")

    print("  ├─────────┼───────────┼───────────┼───────────┼──────────┼──────────┼─────────┤")
    tot_ret = totals["net"] / CAPITAL * 100
    print(f"  │ TOTAL   │ ${totals['gross']:>8,.0f} │ ${totals['fees']:>8,.0f} │ ${totals['net']:>8,.0f} │ {tot_ret:>6.1f}%  │    —     │    —    │")
    print("  └─────────┴───────────┴───────────┴───────────┴──────────┴──────────┴─────────┘")

    # ════════════ TRADING ACTIVITY ════════════
    print()
    print("  ┌─────────┬─────────┬──────────┬──────────┬───────────┬──────────┬──────────┐")
    print("  │ Asset   │  Fills  │ Fills/d  │ RndTrips │ Volume($) │ Vol/day  │ Partials │")
    print("  ├─────────┼─────────┼──────────┼──────────┼───────────┼──────────┼──────────┤")

    t_fills = 0
    t_vol = 0
    t_rt = 0
    t_partials = 0
    for sym in symbols:
        r = results[sym]
        s = sym.replace("USDT", "")
        # Estimate volume: fills * order_size_usd
        p = ASSETS[sym]
        avg_size = p["size"] * (BASE_ALLOC / 1000.0)
        vol = r.total_fills * avg_size
        vol_d = vol / r.days
        t_fills += r.total_fills
        t_vol += vol
        t_rt += r.round_trips
        t_partials += r.partial_fills
        print(f"  │ {s:<7} │ {r.total_fills:>7,} │ {r.fills_per_day:>7.0f}  │ {r.round_trips:>8,} │ ${vol:>9,.0f} │ ${vol_d:>7,.0f} │ {r.partial_fills:>8,} │")
        if sym != symbols[-1]:
            print("  ├─────────┼─────────┼──────────┼──────────┼───────────┼──────────┼──────────┤")

    print("  ├─────────┼─────────┼──────────┼──────────┼───────────┼──────────┼──────────┤")
    t_fpd = t_fills / DAYS
    t_vol_d = t_vol / DAYS
    print(f"  │ TOTAL   │ {t_fills:>7,} │ {t_fpd:>7.0f}  │ {t_rt:>8,} │ ${t_vol:>9,.0f} │ ${t_vol_d:>7,.0f} │ {t_partials:>8,} │")
    print("  └─────────┴─────────┴──────────┴──────────┴───────────┴──────────┴──────────┘")

    # ════════════ RISK METRICS ════════════
    print()
    print("  ┌─────────┬──────────┬──────────┬──────────┬──────────┬──────────┬──────────┐")
    print("  │ Asset   │  MaxDD   │ DD/PnL%  │ Prof.d%  │ AvgInv$  │ MaxInv$  │ RiskHalt │")
    print("  ├─────────┼──────────┼──────────┼──────────┼──────────┼──────────┼──────────┤")

    for sym in symbols:
        r = results[sym]
        s = sym.replace("USDT", "")
        dd_pnl = r.max_drawdown / max(r.net_pnl, 1) * 100
        pdays = sum(1 for x in r.daily_pnls if x > 0)
        prof_pct = pdays / r.days * 100
        print(f"  │ {s:<7} │ ${r.max_drawdown:>7,.0f} │ {dd_pnl:>7.1f}% │ {prof_pct:>7.0f}% │ ${r.avg_inventory_usd:>7,.0f} │ ${r.max_inventory_usd:>7,.0f} │ {r.risk_halts:>8} │")
        if sym != symbols[-1]:
            print("  ├─────────┼──────────┼──────────┼──────────┼──────────┼──────────┼──────────┤")

    print("  └─────────┴──────────┴──────────┴──────────┴──────────┴──────────┴──────────┘")

    # ════════════ SPREAD & ML ════════════
    print()
    print("  ┌─────────┬──────────┬──────────┬──────────┬──────────┬──────────┬──────────┐")
    print("  │ Asset   │ Sprd.Qtd │ Sprd.Cap │ Toxicity │ ToxFill% │ ML Skip  │ AutoTune │")
    print("  ├─────────┼──────────┼──────────┼──────────┼──────────┼──────────┼──────────┤")

    for sym in symbols:
        r = results[sym]
        s = sym.replace("USDT", "")
        print(f"  │ {s:<7} │ {r.avg_spread_quoted_bps:>6.1f}bp │ {r.avg_spread_captured_bps:>6.1f}bp │ {r.toxicity_avg:>8.3f} │ {r.toxic_fills_pct:>7.1f}% │ {r.ml_skipped_quotes:>8,} │ {r.tuner_adjustments:>8,} │")
        if sym != symbols[-1]:
            print("  ├─────────┼──────────┼──────────┼──────────┼──────────┼──────────┼──────────┤")

    print("  └─────────┴──────────┴──────────┴──────────┴──────────┴──────────┴──────────┘")

    # ════════════ DAILY AVERAGES ════════════
    print()
    print("  ┌─────────┬──────────┬──────────┬──────────┬──────────┬──────────┐")
    print("  │ Asset   │  PnL/day │ Fee/day  │ Gross/d  │ PnL/fill │ Fee/fill │")
    print("  ├─────────┼──────────┼──────────┼──────────┼──────────┼──────────┤")

    for sym in symbols:
        r = results[sym]
        s = sym.replace("USDT", "")
        pnl_d = r.net_pnl / r.days
        fee_d = r.total_fees / r.days
        gross_d = r.gross_pnl / r.days
        pnl_f = r.net_pnl / max(r.total_fills, 1)
        fee_f = r.total_fees / max(r.total_fills, 1)
        print(f"  │ {s:<7} │ ${pnl_d:>7.2f} │ ${fee_d:>7.2f} │ ${gross_d:>7.2f} │ ${pnl_f:>7.4f} │ ${fee_f:>7.4f} │")
        if sym != symbols[-1]:
            print("  ├─────────┼──────────┼──────────┼──────────┼──────────┼──────────┤")

    tot_pnl_d = totals["net"] / DAYS
    tot_fee_d = totals["fees"] / DAYS
    tot_gross_d = totals["gross"] / DAYS
    tot_pnl_f = totals["net"] / max(t_fills, 1)
    tot_fee_f = totals["fees"] / max(t_fills, 1)
    print("  ├─────────┼──────────┼──────────┼──────────┼──────────┼──────────┤")
    print(f"  │ TOTAL   │ ${tot_pnl_d:>7.2f} │ ${tot_fee_d:>7.2f} │ ${tot_gross_d:>7.2f} │ ${tot_pnl_f:>7.4f} │ ${tot_fee_f:>7.4f} │")
    print("  └─────────┴──────────┴──────────┴──────────┴──────────┴──────────┘")

    # ════════════ SUMMARY ════════════
    print()
    print("═" * W)
    fee_pct_of_gross = totals["fees"] / max(totals["gross"], 1) * 100
    print(f"  Gross PnL:     ${totals['gross']:>10,.0f}")
    print(f"  Total Fees:    ${totals['fees']:>10,.0f}  ({fee_pct_of_gross:.1f}% of gross)")
    print(f"  Net PnL:       ${totals['net']:>10,.0f}")
    print(f"  Total Volume:  ${t_vol:>10,.0f}")
    print(f"  Total Fills:   {t_fills:>10,}")
    print(f"  Fee/Volume:    {totals['fees']/max(t_vol,1)*100:.4f}%")
    print(f"  Net/Volume:    {totals['net']/max(t_vol,1)*100:.4f}%")
    print()
    print(f"  NOTE: These are RAW per-asset results at $12,500 fixed capital.")
    print(f"  With supervisor V3 + compound: $50K → $112,670 (+125.3%)")
    print("═" * W)


if __name__ == "__main__":
    main()
