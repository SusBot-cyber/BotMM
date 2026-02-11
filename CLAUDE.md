# BotMM — Market Making Bot

## Overview

Market making bot for crypto perpetuals on Hyperliquid (primary), with future support for Binance and Bybit.

**Strategy:** Capture bid-ask spread by quoting both sides of the order book.  
**Edge:** HL maker rebate (-0.015%), wider spreads than CEX, directional bias from Kalman+QQE.  
**Status:** 🔧 PHASE 1 — Basic spread capture + candle-based backtester

## Architecture

```
bot_mm/
├── main.py                 # Main orchestrator (async event loop)
├── config.py               # Configuration
├── core/
│   ├── quoter.py           # Quote engine (Avellaneda-Stoikov)
│   ├── inventory.py        # Inventory tracking & skew
│   ├── risk.py             # Risk limits, circuit breakers
│   └── order_manager.py    # Order lifecycle
├── exchanges/
│   ├── base_mm.py          # Abstract exchange interface
│   └── hl_mm.py            # Hyperliquid implementation
├── strategies/
│   └── basic_mm.py         # Simple spread capture
└── utils/
    ├── logger.py           # Structured logging
    └── metrics.py          # PnL tracking, fill rates
```

## Backtester

Candle-based MM simulation in `backtest/mm_backtester.py`:
- Simulates spread capture using OHLCV data
- Models fill probability based on price movement through quote levels
- Tracks inventory, PnL, fees (maker rebate on HL)
- ~60% realism (no real order book depth, simplified adverse selection)

## Key Parameters

```
base_spread_bps=2.0        # Min spread in basis points
vol_multiplier=1.5         # Spread widens with volatility
inventory_skew_factor=0.5  # How much inventory affects quotes
max_position_usd=500       # Max inventory per asset
order_size_usd=100         # Size per quote side
maker_fee=-0.00015         # HL maker rebate (negative = earn)
taker_fee=0.00045          # HL taker fee (for hedging)
```

## Revenue Model (Conservative)

| Capital | Daily Rev | Monthly | Annual |
|---------|-----------|---------|--------|
| $1,000  | $5-15     | $150-450| $1.8k-5.4k |
| $5,000  | $15-40    | $450-1.2k| $5.4k-14.4k |

## Quick Start

```bash
pip install -r requirements.txt

# Run backtest simulation
py backtest/mm_backtester.py --symbol BTCUSDT --days 90

# Run live (future)
py -m bot_mm.main --testnet
```
