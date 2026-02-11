"""
BotMM — Market Making Bot main orchestrator.

Usage:
    py -m bot_mm.main --symbol BTCUSDT --testnet
    py -m bot_mm.main --symbol ETHUSDT --mainnet --capital 2000 --spread 3.0
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

from bot_mm.config import MMBotConfig, AssetMMConfig, QuoteParams, RiskLimits, Exchange
from bot_mm.exchanges.hl_mm import HyperliquidMMExchange
from bot_mm.strategies.basic_mm import BasicMMStrategy

logger = logging.getLogger("bot_mm")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="BotMM — Market Making Bot for Crypto Perpetuals",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--symbol", default="BTCUSDT", help="Trading symbol (default: BTCUSDT)")
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
    """Load config from .env + CLI overrides. Returns (config, is_testnet)."""
    # Load .env from bot_mm directory
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    config = MMBotConfig.load()

    # Determine testnet/mainnet
    is_testnet = not args.mainnet  # Default to testnet unless --mainnet
    if args.testnet:
        is_testnet = True

    # Ensure symbol exists in config
    symbol = args.symbol.upper()
    if symbol not in config.assets:
        config.assets[symbol] = AssetMMConfig(symbol=symbol)

    asset_cfg = config.assets[symbol]

    # CLI overrides
    if args.capital is not None:
        asset_cfg.capital_usd = args.capital
    if args.spread is not None:
        asset_cfg.quote.base_spread_bps = args.spread
    if args.size is not None:
        asset_cfg.quote.order_size_usd = args.size

    return config, asset_cfg, is_testnet


async def run(args: argparse.Namespace):
    """Main async entry point."""
    config, asset_cfg, is_testnet = load_config(args)

    log_level = args.log_level or config.log_level or "INFO"
    setup_logging(log_level)

    logger.info(
        "BotMM starting | symbol=%s | %s | capital=$%.0f | spread=%.1f bps",
        asset_cfg.symbol, "TESTNET" if is_testnet else "⚠️ MAINNET",
        asset_cfg.capital_usd, asset_cfg.quote.base_spread_bps,
    )

    # Validate credentials
    private_key = config.hl_private_key
    if not private_key:
        logger.error("HL_PRIVATE_KEY not set. Configure in bot_mm/.env")
        sys.exit(1)

    # Create exchange adapter
    exchange = HyperliquidMMExchange(
        private_key=private_key,
        wallet_address=config.hl_wallet_address or None,
        testnet=is_testnet,
    )

    # Create strategy
    strategy = BasicMMStrategy(exchange=exchange, config=asset_cfg)

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

        # Set dead man's switch (cancel orders if bot dies, 30s timeout)
        dms_ok = await exchange.set_dead_mans_switch(30_000)
        if dms_ok:
            logger.info("Dead man's switch activated (30s)")
        else:
            logger.warning("Dead man's switch failed — orders may persist if bot crashes")

        # Run strategy + DMS heartbeat concurrently
        strategy_task = asyncio.create_task(strategy.start())
        dms_task = asyncio.create_task(_dms_heartbeat(exchange, shutdown_event))

        # Wait for shutdown signal
        await shutdown_event.wait()

        # Graceful shutdown
        await strategy.stop()

        # Give strategy time to cancel orders
        try:
            await asyncio.wait_for(strategy_task, timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("Strategy shutdown timed out")
            strategy_task.cancel()

        dms_task.cancel()
        try:
            await dms_task
        except asyncio.CancelledError:
            pass

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shutting down")
        await strategy.stop()
    except Exception:
        logger.exception("Fatal error")
        # Emergency cancel
        try:
            await exchange.cancel_all_orders(asset_cfg.symbol)
        except Exception:
            pass
    finally:
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
