#!/usr/bin/env python3
"""
CLI script to record L2 order book data from Hyperliquid WebSocket.

Usage:
    py scripts/record_orderbook.py --symbols BTC ETH SOL --levels 20
    py scripts/record_orderbook.py --symbols BTC --duration 3600   # 1 hour
    py scripts/record_orderbook.py --symbols BTC --duration 0      # indefinite
"""

import argparse
import asyncio
import logging
import signal
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot_mm.data.l2_recorder import L2Recorder


def parse_args():
    p = argparse.ArgumentParser(description="Record L2 order book from Hyperliquid")
    p.add_argument(
        "--symbols",
        nargs="+",
        default=["BTC"],
        help="Symbols to record (default: BTC)",
    )
    p.add_argument("--levels", type=int, default=20, help="Order book depth (default: 20)")
    p.add_argument("--sig-figs", type=int, default=5, help="Price sig figs (default: 5)")
    p.add_argument(
        "--output",
        default="data/orderbook",
        help="Output directory (default: data/orderbook)",
    )
    p.add_argument(
        "--duration",
        type=int,
        default=0,
        help="Recording duration in seconds, 0=indefinite (default: 0)",
    )
    p.add_argument(
        "--stats-interval",
        type=int,
        default=60,
        help="Print stats every N seconds (default: 60)",
    )
    return p.parse_args()


async def run(args):
    recorder = L2Recorder(
        symbols=args.symbols,
        output_dir=args.output,
        n_levels=args.levels,
        n_sig_figs=args.sig_figs,
    )

    # Graceful shutdown on Ctrl+C
    loop = asyncio.get_event_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler():
        print("\n⏹  Stopping recorder...")
        recorder.stop()
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows: signal handlers not supported in event loop
            signal.signal(sig, lambda s, f: _signal_handler())

    # Stats printer task
    async def print_stats():
        while not shutdown_event.is_set():
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=args.stats_interval)
                break
            except asyncio.TimeoutError:
                s = recorder.summary()
                print(
                    f"📊 [{s['uptime_seconds']:.0f}s] "
                    f"snapshots={s['snapshots_recorded']} "
                    f"trades={s['trades_recorded']} "
                    f"msgs={s['messages_received']} "
                    f"reconnects={s['reconnects']}"
                )

    # Duration limiter task
    async def duration_limiter():
        if args.duration > 0:
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=args.duration)
            except asyncio.TimeoutError:
                print(f"\n⏰ Duration reached ({args.duration}s). Stopping...")
                recorder.stop()
                shutdown_event.set()

    tasks = [
        asyncio.ensure_future(recorder.start()),
        asyncio.ensure_future(print_stats()),
    ]
    if args.duration > 0:
        tasks.append(asyncio.ensure_future(duration_limiter()))

    await asyncio.gather(*tasks, return_exceptions=True)

    # Final summary
    s = recorder.summary()
    print("\n" + "=" * 50)
    print("RECORDING SUMMARY")
    print("=" * 50)
    print(f"  Symbols:    {', '.join(s['symbols'])}")
    print(f"  Uptime:     {s['uptime_seconds']:.1f}s")
    print(f"  Snapshots:  {s['snapshots_recorded']}")
    print(f"  Trades:     {s['trades_recorded']}")
    print(f"  Messages:   {s['messages_received']}")
    print(f"  Reconnects: {s['reconnects']}")
    print("=" * 50)


def main():
    args = parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print(f"🔴 L2 Recorder — symbols: {args.symbols}, levels: {args.levels}")
    print(f"   Output: {args.output}")
    if args.duration > 0:
        print(f"   Duration: {args.duration}s")
    else:
        print("   Duration: indefinite (Ctrl+C to stop)")
    print()

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
