"""Monthly breakdown — runs separate 30d backtests for each month slice."""
import sys, math, csv
from pathlib import Path
from typing import List
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.mm_backtester import MMBacktester, load_candles_csv, Candle
from bot_mm.config import QuoteParams

ASSETS = {
    "BTCUSDT": {"spread": 2.0, "skew": 0.3, "size": 150, "bias": True, "bias_str": 0.2},
    "ETHUSDT": {"spread": 1.5, "skew": 0.3, "size": 150, "bias": True, "bias_str": 0.2},
    "SOLUSDT": {"spread": 1.5, "skew": 0.5, "size": 150, "bias": False, "bias_str": 0.0},
    "XRPUSDT": {"spread": 1.5, "skew": 0.5, "size": 150, "bias": True, "bias_str": 0.2},
}

CAPITAL = 12500  # $50K / 4 assets


def run_month(symbol: str, candles: List[Candle]):
    p = ASSETS[symbol]
    scale = CAPITAL / 1000.0
    params = QuoteParams(
        base_spread_bps=p["spread"],
        vol_multiplier=1.5,
        inventory_skew_factor=p["skew"],
        order_size_usd=p["size"] * scale,
        num_levels=2,
    )
    bt = MMBacktester(
        quote_params=params,
        maker_fee=-0.00015,
        taker_fee=0.00045,
        max_position_usd=CAPITAL * 0.5,
        max_daily_loss=CAPITAL * 0.05,
        capital=CAPITAL,
        use_bias=p["bias"],
        bias_strength=p["bias_str"],
        use_toxicity=True,
        use_auto_tune=True,
    )
    return bt.run(candles, symbol)


def main():
    data_dir = Path(__file__).parent.parent / "data" / "cache"
    total_days = 225
    total_candles = total_days * 24  # 1h

    # Load all candles
    all_candles = {}
    for sym in ASSETS:
        csv_path = data_dir / f"{sym}_1h.csv"
        candles = load_candles_csv(str(csv_path), days=0)  # load all
        candles = candles[-total_candles:]
        all_candles[sym] = candles
        print(f"  {sym}: {len(candles)} candles ({candles[0].timestamp[:10]} to {candles[-1].timestamp[:10]})")

    # Split into months (30d = 720 candles)
    month_size = 30 * 24
    n_months = total_days // 30
    remainder = total_days % 30

    months = []
    for m in range(n_months):
        s = m * month_size
        e = s + month_size
        months.append((f"M{m+1}", s, e))
    if remainder > 0:
        s = n_months * month_size
        months.append((f"M{n_months+1}*", s, len(list(all_candles.values())[0])))

    # Run backtests per month per asset
    results = {}  # {sym: [(label, result), ...]}
    for sym in ASSETS:
        results[sym] = []
        for label, s, e in months:
            chunk = all_candles[sym][s:e]
            print(f"  {sym} {label} ({len(chunk)//24}d)...", end="", flush=True)
            r = run_month(sym, chunk)
            results[sym].append((label, r))
            print(f" ${r.net_pnl:,.0f}", flush=True)

    # Print results
    syms_short = [s.replace("USDT", "") for s in ASSETS]

    print()
    print("=" * 120)
    print("  MONTHLY BREAKDOWN — PER-ASSET BACKTEST ($12,500/asset, 225 days)")
    print("=" * 120)

    # === NET PNL TABLE ===
    print()
    print("  NET PNL ($)")
    hdr = f"  {'Month':<6} {'Days':>4}"
    for s in syms_short:
        hdr += f" | {s:>8}"
    hdr += f" | {'TOTAL':>8} | {'CumPnL':>8} | {'Equity':>9}"
    print(hdr)
    print("  " + "-" * 108)

    cum = 0.0
    for i, (label, s, e) in enumerate(months):
        d = (e - s) // 24
        row = f"  {label:<6} {d:>4}"
        mt = 0.0
        for sym in ASSETS:
            _, r = results[sym][i]
            row += f" | ${r.net_pnl:>7,.0f}"
            mt += r.net_pnl
        cum += mt
        row += f" | ${mt:>7,.0f} | ${cum:>7,.0f} | ${50000+cum:>8,.0f}"
        print(row)

    print("  " + "-" * 108)
    row = f"  {'TOTAL':<6} {total_days:>4}"
    grand = 0.0
    for sym in ASSETS:
        t = sum(r.net_pnl for _, r in results[sym])
        row += f" | ${t:>7,.0f}"
        grand += t
    row += f" | ${grand:>7,.0f} |          | ${50000+grand:>8,.0f}"
    print(row)

    # === FILLS TABLE ===
    print()
    print("  FILLS (count)")
    hdr = f"  {'Month':<6} {'Days':>4}"
    for s in syms_short:
        hdr += f" | {s:>8}"
    hdr += f" | {'TOTAL':>8}"
    print(hdr)
    print("  " + "-" * 80)

    for i, (label, s, e) in enumerate(months):
        d = (e - s) // 24
        row = f"  {label:<6} {d:>4}"
        mt = 0
        for sym in ASSETS:
            _, r = results[sym][i]
            row += f" | {r.total_fills:>8}"
            mt += r.total_fills
        row += f" | {mt:>8}"
        print(row)

    # === SHARPE TABLE ===
    print()
    print("  SHARPE RATIO")
    hdr = f"  {'Month':<6} {'Days':>4}"
    for s in syms_short:
        hdr += f" | {s:>8}"
    print(hdr)
    print("  " + "-" * 60)

    for i, (label, s, e) in enumerate(months):
        d = (e - s) // 24
        row = f"  {label:<6} {d:>4}"
        for sym in ASSETS:
            _, r = results[sym][i]
            row += f" | {r.sharpe_ratio:>8.1f}"
        print(row)

    # === WIN RATE TABLE ===
    print()
    print("  PROFITABLE DAYS (%)")
    hdr = f"  {'Month':<6} {'Days':>4}"
    for s in syms_short:
        hdr += f" | {s:>8}"
    print(hdr)
    print("  " + "-" * 60)

    for i, (label, s, e) in enumerate(months):
        d = (e - s) // 24
        row = f"  {label:<6} {d:>4}"
        for sym in ASSETS:
            _, r = results[sym][i]
            if r.daily_pnls:
                pos = sum(1 for x in r.daily_pnls if x > 0)
                pct = pos / len(r.daily_pnls) * 100
            else:
                pct = 0
            row += f" | {pct:>7.0f}%"
        print(row)

    # === MAX DRAWDOWN TABLE ===
    print()
    print("  MAX DRAWDOWN ($)")
    hdr = f"  {'Month':<6} {'Days':>4}"
    for s in syms_short:
        hdr += f" | {s:>8}"
    print(hdr)
    print("  " + "-" * 60)

    for i, (label, s, e) in enumerate(months):
        d = (e - s) // 24
        row = f"  {label:<6} {d:>4}"
        for sym in ASSETS:
            _, r = results[sym][i]
            row += f" | ${r.max_drawdown:>7,.0f}"
        print(row)

    # === PER-ASSET SUMMARY ===
    print()
    print("  PER-ASSET TOTALS (225 days)")
    print("  " + "-" * 85)
    print(f"  {'Asset':<8} {'Net PnL':>9} {'Return':>8} {'Fills':>7} {'Fills/d':>8} {'Sharpe':>8} {'MaxDD':>8} {'Avg Spread':>11}")
    print("  " + "-" * 85)
    for sym in ASSETS:
        total_pnl = sum(r.net_pnl for _, r in results[sym])
        total_fills = sum(r.total_fills for _, r in results[sym])
        all_daily = []
        for _, r in results[sym]:
            all_daily.extend(r.daily_pnls)
        sharpe = np.mean(all_daily) / np.std(all_daily) * math.sqrt(365) if np.std(all_daily) > 0 else 0
        max_dd = max(r.max_drawdown for _, r in results[sym])
        avg_spread = np.mean([r.avg_spread_captured_bps for _, r in results[sym]])
        short = sym.replace("USDT", "")
        print(f"  {short:<8} ${total_pnl:>8,.0f} {total_pnl/CAPITAL*100:>7.1f}% {total_fills:>7} {total_fills/total_days:>8.1f} {sharpe:>8.1f} ${max_dd:>7,.0f} {avg_spread:>10.2f}bp")

    print()
    print(f"  PORTFOLIO: ${grand:>,.0f} net PnL | {grand/50000*100:.1f}% return | ${50000+grand:>,.0f} final equity")
    print("  * = partial month (15 days)")
    print("=" * 120)


if __name__ == "__main__":
    main()
