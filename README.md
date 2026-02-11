# BotMM — Crypto Market Making Bot

Automated market making bot for crypto perpetuals on **Hyperliquid**, with ML-enhanced spread management and meta-supervisor capital allocation.

## What It Does

Places bid and ask orders around the mid price to capture the bid-ask spread. Earns on every round-trip trade plus Hyperliquid's maker rebate (-0.015%).

**Core strategy:** Avellaneda-Stoikov model with directional bias (Kalman+QQE), toxicity detection, and adaptive parameter tuning.

## Backtest Results

### Per-Asset ($10K capital, optimized)

| Asset | Net PnL | Sharpe | Win Rate | Period |
|-------|---------|--------|----------|--------|
| ETH | $11,554 | 10.9 | 81% | 365d |
| XRP | $11,033 | 12.5 | 83% | 365d |
| BTC | $10,536 | 17.5 | 90% | 365d |
| SOL | $9,253 | 10.8 | 78% | 365d |

### Portfolio ($50K, 4 assets, 225 days)

| Strategy | Net PnL | Return | Sharpe |
|----------|---------|--------|--------|
| Equal weight | $42,301 | 84.6% | 22.5 |
| **+ Supervisor + Compound** | **$51,423** | **102.8%** | **20.8** |

## Features

- **Avellaneda-Stoikov quote engine** — volatility-scaled spreads, inventory skew
- **Directional bias** — Kalman Filter + QQE shifts quotes with trend
- **ML fill prediction** — GBM model (AUC 0.77) predicts fill probability and adverse selection
- **Toxicity detection** — tracks post-fill price movement, widens spread on toxic flow
- **Auto-parameter tuning** — runtime self-adjustment (Sharpe +9%, 93% profitable days)
- **Dynamic order sizing** — adapts to vol regime, fill rate, drawdown
- **Meta-supervisor** — dual capital + risk allocation across assets (+21.6% vs equal)
- **Compound mode** — reinvest PnL for BTC/ETH
- **Order book replay backtest** — tick-level simulation with queue position (~90% realism)
- **Daily auto-reoptimizer** — nightly parameter search + hot reload (zero downtime)
- **L2 data recorder** — 24/7 WebSocket recording (deployed on AWS EC2)
- **Dead man's switch** — auto-cancel orders if bot crashes
- **Discord notifications** — alerts, daily reports, recorder monitoring

## Architecture

```
bot_mm/
├── main.py              # Async orchestrator (DMS heartbeat, metadata monitor)
├── config.py            # Per-asset config from .env
├── core/                # Quote engine, inventory, risk, signals
├── exchanges/           # HL adapter (dynamic rounding from metadata)
├── strategies/          # BasicMM, AdaptiveMM
├── ml/                  # Fill predictor, toxicity, auto-tuner, dynamic sizer
├── data/                # L2 WebSocket recorder
└── utils/               # Logger, metrics, Discord notifier

backtest/                # Candle-based + tick-level order book replay
scripts/                 # Optimizer, reoptimizer, supervisor, ML training
tests/                   # 343 tests
deploy/                  # AWS EC2 setup, systemd, monitoring
```

## Quick Start

```bash
# Install
pip install -r requirements.txt

# Run backtest
python backtest/mm_backtester.py --symbol BTCUSDT --days 365 \
  --spread 2.0 --size 150 --levels 2 --skew 0.3 \
  --bias --bias-strength 0.2

# With ML features
python backtest/mm_backtester.py --symbol BTCUSDT --days 365 \
  --spread 2.0 --size 150 --levels 2 --skew 0.3 \
  --bias --bias-strength 0.2 --auto-tune --toxicity --compound

# Find optimal parameters
python scripts/run_mm_optimizer.py --symbol BTCUSDT --days 365 --quick --workers 10

# Record live L2 data
python scripts/record_orderbook.py --symbols BTC ETH SOL --levels 20

# Order book replay backtest (needs recorded data)
python scripts/run_ob_backtest.py --symbol BTC --date 2026-02-12

# Run tests
python -m pytest tests/ -v
```

## Live Trading

```bash
# Configure
cp bot_mm/.env.example bot_mm/.env
# Edit: HL_PRIVATE_KEY, HL_WALLET_ADDRESS, MM_SYMBOLS

# Run (testnet, default)
python -m bot_mm.main --symbol BTCUSDT --testnet

# Run all symbols (mainnet)
python -m bot_mm.main --all --mainnet --capital 10000
```

## Exchange Support

| Exchange | Status | Role |
|----------|--------|------|
| **Hyperliquid** | ✅ Implemented | Primary (maker rebates, wider spreads) |
| Binance Futures | 🔲 Planned | Altcoin MM + hedging |
| Bybit | 🔲 Planned | Redundancy + cross-exchange arb |

Asset precision (szDecimals) is loaded dynamically from HL metadata on connect and refreshed hourly. New assets can be added to `.env` without code changes.

## Deployment

L2 data recorder runs 24/7 on AWS EC2 (free tier). See [deploy/README.md](deploy/README.md) for step-by-step setup.

## Key Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `base_spread_bps` | 2.0 | Minimum spread in basis points |
| `vol_multiplier` | 1.5 | Spread widens with volatility |
| `inventory_skew_factor` | 0.3 | How much inventory skews quotes |
| `order_size_usd` | 150 | Size per quote side |
| `num_levels` | 2 | Quote levels per side |
| `bias_strength` | 0.2 | Directional bias intensity |
| `max_daily_loss` | 5% of capital | Circuit breaker threshold |

## Dependencies

- Python 3.11+
- numpy, pandas, aiohttp, websockets
- hyperliquid-python-sdk, eth-account
- scikit-learn (for ML features)
- See [requirements.txt](requirements.txt)

## Tests

```bash
python -m pytest tests/ -v
# 343 tests across 16 modules
```

## License

Private repository. All rights reserved.
