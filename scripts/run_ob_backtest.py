"""
CLI for Order Book Replay Backtester.

Usage:
    py scripts/run_ob_backtest.py --symbol BTC --date 2026-02-11
    py scripts/run_ob_backtest.py --symbol BTC --start 2026-02-10 --end 2026-02-11
    py scripts/run_ob_backtest.py --symbol BTC --date 2026-02-11 --data-dir data/orderbook
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bot_mm.config import QuoteParams
from backtest.ob_loader import OrderBookLoader
from backtest.ob_backtester import OBBacktester, print_results


def main():
    parser = argparse.ArgumentParser(description="Order Book Replay Backtester")
    parser.add_argument("--symbol", default="BTC", help="Symbol (e.g. BTC, ETH, SOL)")
    parser.add_argument("--date", default=None, help="Single date (YYYY-MM-DD)")
    parser.add_argument("--start", default=None, help="Start date for range (YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="End date for range (YYYY-MM-DD)")
    parser.add_argument("--data-dir", default="data/orderbook", help="Data directory")
    parser.add_argument("--spread", type=float, default=2.0, help="Base spread (bps)")
    parser.add_argument("--size", type=float, default=100.0, help="Order size ($)")
    parser.add_argument("--levels", type=int, default=1, help="Quote levels per side")
    parser.add_argument("--max-pos", type=float, default=500.0, help="Max position ($)")
    parser.add_argument("--capital", type=float, default=1000.0, help="Starting capital ($)")
    parser.add_argument("--maker-fee", type=float, default=-0.00015, help="Maker fee (neg=rebate)")
    parser.add_argument("--taker-fee", type=float, default=0.00045, help="Taker fee")
    parser.add_argument("--refresh", type=int, default=1, help="Refresh quotes every N snapshots")
    parser.add_argument("--no-queue", action="store_true", help="Disable queue position simulation")
    parser.add_argument("--vol-mult", type=float, default=1.5, help="Volatility multiplier")
    parser.add_argument("--skew", type=float, default=0.5, help="Inventory skew factor")
    parser.add_argument("--max-daily-loss", type=float, default=50.0, help="Max daily loss ($)")
    args = parser.parse_args()

    if not args.date and not (args.start and args.end):
        parser.error("Provide --date or both --start and --end")

    params = QuoteParams(
        base_spread_bps=args.spread,
        order_size_usd=args.size,
        num_levels=args.levels,
        vol_multiplier=args.vol_mult,
        inventory_skew_factor=args.skew,
    )

    loader = OrderBookLoader()

    print(f"Loading {args.symbol} data from {args.data_dir}...")

    if args.date:
        snapshots, trades = loader.load_day(args.symbol, args.date, args.data_dir)
    else:
        snapshots, trades = loader.load_range(args.symbol, args.start, args.end, args.data_dir)

    if not snapshots:
        print(f"ERROR: No L2 data found for {args.symbol}")
        print(f"  Expected: {args.data_dir}/{args.symbol}/<date>/l2_*.csv")
        sys.exit(1)

    print(f"Loaded {len(snapshots):,} snapshots, {len(trades):,} trades")

    backtester = OBBacktester(
        quote_params=params,
        maker_fee=args.maker_fee,
        taker_fee=args.taker_fee,
        max_position_usd=args.max_pos,
        max_daily_loss=args.max_daily_loss,
        capital=args.capital,
        quote_refresh_snapshots=args.refresh,
        use_queue_position=not args.no_queue,
    )

    result = backtester.run(snapshots, trades, symbol=args.symbol)
    print_results(result, params)


if __name__ == "__main__":
    main()
