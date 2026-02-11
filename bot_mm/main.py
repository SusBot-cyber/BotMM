"""
BotMM — Market Making Bot main orchestrator.

Usage:
    py -m bot_mm.main --symbol BTCUSDT --testnet
    py -m bot_mm.main --all --testnet
    py -m bot_mm.main --symbol ETHUSDT --mainnet --capital 2000 --spread 3.0
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import List

from dotenv import load_dotenv

from bot_mm.config import MMBotConfig, AssetMMConfig, QuoteParams, RiskLimits, Exchange
from bot_mm.exchanges.hl_mm import HyperliquidMMExchange
from bot_mm.strategies.basic_mm import BasicMMStrategy
from bot_mm.utils.notifier import MMDiscordNotifier

logger = logging.getLogger("bot_mm")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="BotMM — Market Making Bot for Crypto Perpetuals",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--symbol", default=None, help="Trading symbol (default: from MM_SYMBOLS)")
    parser.add_argument("--all", action="store_true", default=False, help="Run all enabled symbols from MM_SYMBOLS")
    parser.add_argument("--testnet", action="store_true", default=False, help="Use testnet (default)")
    parser.add_argument("--mainnet", action="store_true", default=False, help="Use mainnet")
    parser.add_argument("--capital", type=float, default=None, help="Capital in USD (overrides .env)")
    parser.add_argument("--spread", type=float, default=None, help="Base spread in bps (overrides .env)")
    parser.add_argument("--size", type=float, default=None, help="Order size in USD (overrides .env)")
    parser.add_argument("--log-level", default=None, help="Log level (DEBUG/INFO/WARNING)")
    return parser.parse_args()


def setup_logging(level: str = "INFO"):
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Quiet noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("hyperliquid").setLevel(logging.WARNING)


def load_config(args: argparse.Namespace) -> tuple:
    """Load config from .env + CLI overrides. Returns (config, asset_configs, is_testnet)."""
    # Load .env from bot_mm directory
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    config = MMBotConfig.load()

    # Determine testnet/mainnet
    is_testnet = not args.mainnet  # Default to testnet unless --mainnet
    if args.testnet:
        is_testnet = True

    # Determine which symbols to run
    if args.all:
        # Run all enabled symbols from MM_SYMBOLS
        symbols = [sym for sym, cfg in config.assets.items() if cfg.enabled]
    elif args.symbol:
        symbols = [args.symbol.upper()]
    else:
        # Default: first symbol from MM_SYMBOLS
        symbols = list(config.assets.keys())[:1] if config.assets else ["BTCUSDT"]

    # Ensure all symbols exist in config
    asset_configs: List[AssetMMConfig] = []

    # Load optimized params from daily reoptimizer (if available)
    live_params_file = Path(__file__).parent.parent / "data" / "live_params.json"
    live_params = {}
    if live_params_file.exists():
        try:
            with open(live_params_file) as f:
                live_params = json.load(f)
            logger.info("Loaded optimized params from %s", live_params_file)
        except Exception:
            logger.warning("Failed to load live_params.json, using defaults")

    for sym in symbols:
        if sym not in config.assets:
            config.assets[sym] = AssetMMConfig(symbol=sym)
        asset_cfg = config.assets[sym]

        # CLI overrides (only when running single symbol)
        if len(symbols) == 1:
            if args.capital is not None:
                asset_cfg.capital_usd = args.capital
            if args.spread is not None:
                asset_cfg.quote.base_spread_bps = args.spread
            if args.size is not None:
                asset_cfg.quote.order_size_usd = args.size

        # Apply daily-reoptimized params (lower priority than CLI)
        if sym in live_params and not (args.spread or args.size):
            lp = live_params[sym]
            if "base_spread_bps" in lp:
                asset_cfg.quote.base_spread_bps = lp["base_spread_bps"]
            if "inventory_skew_factor" in lp:
                asset_cfg.quote.inventory_skew_factor = lp["inventory_skew_factor"]
            if "order_size_usd" in lp:
                asset_cfg.quote.order_size_usd = lp["order_size_usd"]
            if "num_levels" in lp:
                asset_cfg.quote.num_levels = lp["num_levels"]
            if "vol_multiplier" in lp:
                asset_cfg.quote.vol_multiplier = lp["vol_multiplier"]
            logger.info("  %s: applied optimized params: %s", sym, lp)

        asset_configs.append(asset_cfg)

    return config, asset_configs, is_testnet


async def _run_symbol(
    exchange: HyperliquidMMExchange,
    asset_cfg: AssetMMConfig,
    shutdown_event: asyncio.Event,
):
    """Run strategy for a single symbol until shutdown."""
    strategy = BasicMMStrategy(exchange=exchange, config=asset_cfg)
    try:
        strategy_task = asyncio.create_task(strategy.start())
        await shutdown_event.wait()
        await strategy.stop()
        try:
            await asyncio.wait_for(strategy_task, timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("Strategy shutdown timed out for %s", asset_cfg.symbol)
            strategy_task.cancel()
    except Exception:
        logger.exception("Fatal error in %s strategy", asset_cfg.symbol)
        try:
            await exchange.cancel_all_orders(asset_cfg.symbol)
        except Exception:
            pass


async def run(args: argparse.Namespace):
    """Main async entry point."""
    config, asset_configs, is_testnet = load_config(args)

    log_level = args.log_level or config.log_level or "INFO"
    setup_logging(log_level)

    symbols_str = ", ".join(c.symbol for c in asset_configs)
    logger.info(
        "BotMM starting | symbols=%s | %s",
        symbols_str, "TESTNET" if is_testnet else "⚠️ MAINNET",
    )
    for ac in asset_configs:
        logger.info(
            "  %s: capital=$%.0f | spread=%.1f bps | size=$%.0f | max_pos=$%.0f",
            ac.symbol, ac.capital_usd, ac.quote.base_spread_bps,
            ac.quote.order_size_usd, ac.risk.max_position_usd,
        )

    # Discord notifier
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
    notifier = MMDiscordNotifier(webhook_url) if webhook_url else None

    # Validate credentials
    private_key = config.hl_private_key
    if not private_key:
        logger.error("HL_PRIVATE_KEY not set. Configure in bot_mm/.env")
        sys.exit(1)

    # Create exchange adapter (shared across symbols)
    exchange = HyperliquidMMExchange(
        private_key=private_key,
        wallet_address=config.hl_wallet_address or None,
        testnet=is_testnet,
    )

    # Signal handling for graceful shutdown
    shutdown_event = asyncio.Event()

    def signal_handler():
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    try:
        # Connect to exchange
        await exchange.connect()

        # Send startup notification
        if notifier and notifier.is_configured:
            await notifier.send_startup(
                symbols=[c.symbol for c in asset_configs],
                exchange="Hyperliquid " + ("testnet" if is_testnet else "mainnet"),
                config={
                    "capital": f"${sum(c.capital_usd for c in asset_configs):,.0f}",
                    "spread": f"{asset_configs[0].quote.base_spread_bps} bps",
                    "size": f"${asset_configs[0].quote.order_size_usd:,.0f}",
                },
            )

        # Set dead man's switch (cancel orders if bot dies, 30s timeout)
        dms_ok = await exchange.set_dead_mans_switch(30_000)
        if dms_ok:
            logger.info("Dead man's switch activated (30s)")
        else:
            logger.warning("Dead man's switch failed — orders may persist if bot crashes")

        # Run all symbols concurrently + DMS heartbeat
        tasks = [
            asyncio.create_task(_run_symbol(exchange, ac, shutdown_event))
            for ac in asset_configs
        ]
        dms_task = asyncio.create_task(_dms_heartbeat(exchange, shutdown_event))

        # Wait for shutdown signal or any task failure
        done, pending = await asyncio.wait(
            tasks + [dms_task],
            return_when=asyncio.FIRST_EXCEPTION,
        )

        # If we got here without shutdown, trigger it
        if not shutdown_event.is_set():
            shutdown_event.set()
            # Wait for remaining tasks
            for t in pending:
                try:
                    await asyncio.wait_for(t, timeout=10.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    t.cancel()

        # Cancel DMS
        dms_task.cancel()
        try:
            await dms_task
        except asyncio.CancelledError:
            pass

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shutting down")
        shutdown_event.set()
    except Exception as exc:
        logger.exception("Fatal error")
        if notifier and notifier.is_configured:
            await notifier.send_alert("Fatal Error", str(exc), level="error")
        # Emergency cancel all symbols
        for ac in asset_configs:
            try:
                await exchange.cancel_all_orders(ac.symbol)
            except Exception:
                pass
    finally:
        # Send shutdown notification
        if notifier and notifier.is_configured:
            await notifier.send_shutdown("Graceful shutdown")
        await exchange.disconnect()
        logger.info("BotMM stopped")


async def _dms_heartbeat(exchange: HyperliquidMMExchange, shutdown_event: asyncio.Event):
    """Periodically refresh dead man's switch."""
    while not shutdown_event.is_set():
        try:
            await asyncio.sleep(15)
            if not shutdown_event.is_set():
                await exchange.set_dead_mans_switch(30_000)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.warning("DMS heartbeat failed", exc_info=True)


def main():
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
