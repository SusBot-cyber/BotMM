# BotMM — Market Making Bot

## Overview

Market making bot for crypto perpetuals on Hyperliquid (primary), with future support for Binance and Bybit.

**Strategy:** Capture bid-ask spread by quoting both sides of the order book.
**Edge:** HL maker rebate (-0.015%), wider spreads than CEX, directional bias from Kalman+QQE.
**Repository:** https://github.com/SusBot-cyber/BotMM
**Status:** ✅ Phase 1-4 DONE, Phase 5 (Supervisor + Compound) DONE — Ready for live testing

## Current Performance

### Per-Asset ($10K capital, optimized, 225-365d)

| Asset | Net PnL | Sharpe | Profitable | Period |
|-------|---------|--------|------------|--------|
| ETH | $11,554 | 10.9 | 81% | 365d |
| XRP | $11,033 | 12.5 | 83% | 365d |
| BTC | $10,536 | 17.5 | 90% | 365d |
| SOL | $9,253 | 10.8 | 78% | 365d |
| HYPE | $5,774 | 9.1 | 73% | 225d |

### Portfolio ($50K, 5 assets, 225d, supervisor + compound)

| Strategy | Net PnL | Return | Sharpe |
|----------|---------|--------|--------|
| Equal (no compound) | $33,913 | 67.8% | 24.2 |
| Equal + compound BTC/ETH | $41,084 | 82.2% | 23.3 |
| **Supervisor + compound** | **$49,979** | **100.0%** | 22.4 |

**Best portfolio config:** Supervisor (21d window) + compound BTC/ETH + fixed SOL/XRP/HYPE → **+21.6%** vs equal

### Optimizer Results ($1K base)

| Asset | Spread | Skew | Bias | Size | PnL | Compound |
|-------|--------|------|------|------|-----|----------|
| BTC | 2.0 | 0.3 | 0.2 | 150 | $1,206 | ON |
| ETH | 1.5 | 0.3 | 0.2 | 150 | $1,269 | ON |
| SOL | 1.5 | 0.5 | OFF | 150 | $1,197 | OFF |
| XRP | 1.5 | 0.5 | 0.2 | 150 | $1,228 | OFF |
| HYPE | 1.5 | 0.3 | OFF | 100 | $552 | OFF |

## Architecture

```
bot_mm/
├── main.py                     # Main orchestrator (async event loop)
├── config.py                   # Per-asset configuration + env loading
├── core/
│   ├── quoter.py               # Quote engine (Avellaneda-Stoikov)
│   ├── inventory.py            # Inventory tracking & skew
│   ├── risk.py                 # Risk limits, circuit breakers
│   ├── order_manager.py        # Order lifecycle
│   ├── signals.py              # Directional bias (Kalman+QQE)
│   └── book_imbalance.py       # L2 order book imbalance tracker
├── exchanges/
│   ├── base_mm.py              # Abstract exchange interface
│   └── hl_mm.py                # Hyperliquid (REST, ALO, batch modify)
├── strategies/
│   ├── basic_mm.py             # Simple spread capture + bias + toxicity + hot reload
│   └── adaptive_mm.py          # Vol regime, fill rate tracking, inventory decay
├── ml/
│   ├── fill_predictor.py       # GBM fill + adverse selection predictor
│   ├── data_generator.py       # Training data from candles
│   ├── toxicity.py             # Real-time adverse selection detector
│   └── auto_tuner.py           # Runtime parameter self-adjustment
├── data/
│   └── l2_recorder.py          # HL WebSocket L2 order book recorder
└── utils/
    ├── logger.py               # Structured logging
    └── metrics.py              # PnL tracking, fill rates

backtest/
├── mm_backtester.py            # Candle-based MM simulation (~60% realism)
├── ob_backtester.py            # Tick-level order book replay (~90% realism)
└── ob_loader.py                # L2/trade data loader for replay

scripts/
├── run_mm_optimizer.py         # Grid search optimizer (quick/normal/full modes)
├── train_fill_model.py         # ML model training pipeline
├── record_orderbook.py         # L2 WebSocket data recorder (HL)
├── run_ob_backtest.py          # Order book replay backtest CLI
├── daily_reoptimize.py         # Nightly auto-reoptimizer (144 combos/asset)
└── backtest_supervisor.py      # Meta-supervisor simulation + scoring

tests/                          # 343 tests total
├── test_quoter.py              # 14 tests
├── test_inventory.py           # 20 tests
├── test_risk.py                # 16 tests
├── test_signals.py             # 16 tests
├── test_adaptive.py            # 24 tests
├── test_book_imbalance.py      # 10 tests
├── test_fill_predictor.py      # 16 tests
├── test_toxicity.py            # 14 tests
├── test_auto_tuner.py          # 31 tests
├── test_l2_recorder.py         # 20 tests
├── test_ob_backtester.py       # 23 tests
├── test_ob_loader.py           # 16 tests
├── test_partial_fills.py       # 1 test
├── test_dynamic_sizer.py       # 76 tests
└── test_supervisor.py          # 66 tests
```

## Phase Status

| Phase | Description | Status |
|-------|-------------|--------|
| **Phase 1** | Core MM engine (quoter, inventory, risk, backtester) | ✅ DONE |
| **Phase 2** | Adaptive MM, directional bias, book imbalance, multi-asset | ✅ DONE |
| **Phase 3** | Cross-exchange arb (multi-venue quoting) | ⏭️ SKIPPED (testnet unusable) |
| **Phase 4.1** | Historical order book data collection | ✅ DONE (L2 WebSocket recorder) |
| **Phase 4.2** | MM backtester (order book replay) | ✅ DONE (tick-level, queue position) |
| **Phase 4.3** | ML-based spread prediction | ✅ DONE (GBM, AUC 0.77) |
| **Phase 4.4** | Toxicity detection (adverse selection) | ✅ DONE (per-side EMA tracking) |
| **Phase 4.5** | Auto-parameter tuning | ✅ DONE (runtime self-adjustment) |
| **Phase 5.1** | Partial fills | ✅ DONE (depth-based, 30% threshold) |
| **Phase 5.2** | DynamicSizer (adaptive order sizing) | ✅ DONE (76 tests) |
| **Phase 5.3** | Compound mode (PnL reinvestment) | ✅ DONE (BTC/ETH only) |
| **Phase 5.4** | Daily auto-reoptimizer + hot reload | ✅ DONE (144 combos, drift safety) |
| **Phase 5.5** | Meta-supervisor (capital + risk allocation) | ✅ DONE (dual control, +21.6%) |

## Key Parameters

```
base_spread_bps=2.0        # Min spread in basis points
vol_multiplier=1.5         # Spread widens with volatility
inventory_skew_factor=0.3  # Optimal skew (from 216-combo optimizer)
max_position_usd=500       # Max inventory per asset (scales with capital)
order_size_usd=150         # Optimal size per quote side
num_levels=2               # Optimal quote levels per side
maker_fee=-0.00015         # HL maker rebate (negative = earn)
taker_fee=0.00045          # HL taker fee (for hedging)
bias_strength=0.2          # Directional bias from Kalman+QQE
max_daily_loss=capital*0.05 # Auto-scaled 5% of capital
```

## ML Modules

### Fill Predictor (`ml/fill_predictor.py`)
- GBM (Gradient Boosting) model predicting fill probability + adverse selection
- 15 features: candle range, distance to mid, momentum, vol regime, etc.
- Fill AUC=1.0 (deterministic), Adverse AUC=0.77
- Impact: +1.2% PnL, +4.5% Sharpe

### Toxicity Detector (`ml/toxicity.py`)
- Tracks price movement after fills (+1, +5, +N bars)
- Toxicity score = adverse_move / ATR, clamped [0,1]
- EMA smoothing per side (buy/sell separately)
- Spread multiplier: >0.6 toxicity → 1.5x, >0.4 → 1.25x, <0.2 → 0.9x

### Auto-Parameter Tuner (`ml/auto_tuner.py`)
- Runtime self-adjustment based on rolling performance windows
- Rules: Sharpe<0 → widen spread, fill_rate<15% → tighten, inventory>70% → increase skew
- Boundaries enforced, drift reset at 70%
- Impact: +9% Sharpe, 93% profitable days

### L2 Order Book Recorder (`data/l2_recorder.py`)
- Hyperliquid WebSocket (`wss://api.hyperliquid.xyz/ws`)
- Subscribes to L2 book + trade feed per symbol
- Hourly CSV rotation: `data/orderbook/{SYMBOL}/{date}/l2_{HH}.csv`
- Auto-reconnect with exponential backoff

## Meta-Supervisor System

### Dual Control: Capital + Risk
Supervisor zarządza dwoma wymiarami jednocześnie:

| Mechanizm | Szybkość | Wpływ | Limity |
|-----------|----------|-------|--------|
| **Kapitał** (alokacja bazowa) | Wolny (max ±15%/dzień) | Ile $ per bot | min $500, max 35% |
| **Ryzyko** (mnożniki) | Szybki (max ±10%/dzień) | Size, spread, max_pos | bounds enforced |

### Risk Multipliers per Score Zone

| Zone | Score | Size | Spread | MaxPos | PnL Effect |
|------|-------|------|--------|--------|------------|
| Reward | >0.7 | 1.10x | 0.90x | 1.10x | +21% |
| Hold | 0.4-0.7 | 1.0x | 1.0x | 1.0x | neutral |
| Punish | 0.2-0.4 | 0.70x | 1.30x | 0.70x | -51% |
| Pause | <0.2 | 0.40x | 1.50x | 0.40x | -80% |

### Compound + Supervisor Integration
- **BTC/ETH:** compound ON — reinvest PnL, supervisor controls BASE allocation only
- **SOL/XRP/HYPE:** compound OFF — supervisor controls full capital + risk
- No conflict: compound grows equity on top of supervisor base
- Result: $50K → $100K in 225d (+21.6% vs equal allocation)

### Scoring (absolute, not rank-based)
```
score = 0.40 * sharpe_norm + 0.30 * return_norm + 0.20 * (1-dd_norm) + 0.10 * consistency
```
- 21d rolling window
- Absolute thresholds (all good bots → no one punished)

## Quick Start

```bash
pip install -r requirements.txt

# Run candle-based backtest
py backtest/mm_backtester.py --symbol BTCUSDT --days 365 --spread 2.0 --size 150 --levels 2 --skew 0.3 --bias --bias-strength 0.2

# Run with all ML features + compound
py backtest/mm_backtester.py --symbol BTCUSDT --days 365 --spread 2.0 --size 150 --levels 2 --skew 0.3 --bias --bias-strength 0.2 --auto-tune --toxicity --compound

# Run optimizer (find best params)
py scripts/run_mm_optimizer.py --symbol BTCUSDT --days 365 --quick --workers 10

# Run daily reoptimizer
py scripts/daily_reoptimize.py --dry-run

# Run meta-supervisor backtest simulation
py scripts/backtest_supervisor.py --capital 50000 --days 365 --window 21

# Train ML model
py scripts/train_fill_model.py --symbol BTCUSDT --days 365

# Record live L2 data
py scripts/record_orderbook.py --symbols BTC ETH SOL --levels 20

# Run order book replay backtest (needs recorded data)
py scripts/run_ob_backtest.py --symbol BTC --date 2026-02-12

# Run tests
py -m pytest tests/ -v
```

## Production Cron Schedule

```bash
# 1. Reoptimize params (3am UTC)
0 3 * * * cd /BotMM && python scripts/daily_reoptimize.py

# 2. Meta-supervisor capital allocation (4am UTC)
0 4 * * * cd /BotMM && python scripts/run_meta_supervisor.py

# 3. Bot hot-reloads live_params.json + allocations.json (~1h interval)
```

## Fee Convention (Critical)

- HL maker fee = -0.00015 (NEGATIVE = rebate, we GET money)
- `total_fees` accumulates raw values (negative for rebates)
- `net_pnl = realized_pnl - total_fees` (subtracting negative = adding rebate)

## Dependencies

```
numpy>=1.21.0
requests>=2.28.0
python-dotenv>=1.0.0
tqdm>=4.65.0
hyperliquid-python-sdk>=0.1.0
eth-account>=0.8.0
aiohttp>=3.8.0
scikit-learn>=1.3.0
joblib>=1.3.0
websockets>=12.0
```

## Commit History

1. `e867e6f` — Initial BotMM: Avellaneda-Stoikov market maker with backtester
2. `c66befc` — Phase 2: adaptive MM, directional bias, book imbalance, multi-asset (102 tests)
3. `cc6d6ca` — Phase 4.1: MM parameter optimizer (best: $1,206, +103%)
4. `ea58b63` — Phase 4.2: ML fill prediction (GBM, AUC 0.77, +1.2% PnL)
5. `0de9bab` — Phase 4.3-4.4: Toxicity detection + integration (132 tests)
6. `c6223a9` — Phase 4.5: Auto-parameter tuner (Sharpe +9%, 93% profitable days)
7. `5b92710` — Phase 4.1-4.2: L2 order book recorder + tick-level replay backtester (222 tests)
8. `eb053a0` — Phase 5.2: DynamicSizer — adaptive order sizing (257 tests)
9. `4d0f3c3` — fix: scale max_daily_loss with capital (5%)
10. `7c639f7` — feat: --compound flag for daily PnL reinvestment
11. `881a63d` — feat: daily auto-reoptimizer + live_params.json integration
12. `6b16526` — feat: hot param reload — zero downtime param updates
13. `9ee9b63` — feat: supervisor risk adjustments + 66 tests (dual control)
14. `445366d` — feat: compound + supervisor separation (base capital only)
