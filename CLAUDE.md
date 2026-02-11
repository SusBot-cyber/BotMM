# BotMM — Market Making Bot

## Overview

Market making bot for crypto perpetuals on Hyperliquid (primary), with future support for Binance and Bybit.

**Strategy:** Capture bid-ask spread by quoting both sides of the order book.
**Edge:** Spread capture + MM microstructure edge, directional bias from Kalman+QQE.
**Fee:** HL maker fee = +0.015% (COST at base tier, rebate only at >$500M 14d vol).
**Repository:** https://github.com/SusBot-cyber/BotMM
**Status:** ✅ Phase 1-5 DONE — L2 Recorder deployed on AWS, ready for live MM testing

---

## Documentation Rules

**These rules MUST be followed in every session.** Update docs as you work, not after.

### When to Update CLAUDE.md (this file)
- **Every session:** Add/update Live Infrastructure, commit history, bug fixes, new features
- **Config changes:** Update Key Parameters, Dependencies
- **Architecture changes:** Update Architecture tree, Phase Status
- **New modules:** Add to Architecture tree with one-line description
- **Performance changes:** Update backtest results tables

### When to Update docs/
- **New module added:** Add entry to `docs/modules.md` (purpose, key classes, public API)
- **Architecture change:** Update `docs/architecture.md` (data flow, component relationships)
- **New deployment:** Update `deploy/README.md` (step-by-step, troubleshooting)

### When to Update README.md
- **Major feature milestone** (new phase complete, performance improvement)
- **New quick-start command** or usage pattern
- **Dependency changes**

### File Naming Convention
Documentation files (markdown) MUST use UPPER CASE names: `README.md`, `CLAUDE.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, etc.
This includes files inside `docs/` directory (e.g., `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/HOW_IT_EARNS.md`).

### Module Docstring Standard
Every Python file MUST have a module-level docstring:
```python
"""
Module Name — one-line purpose.

Detailed description: what it does, key algorithms, data flow.
"""
```
Every class MUST have a one-line docstring. Every public method with >3 lines MUST have a docstring.

### Commit Messages
Format: `type: short description` where type = feat|fix|refactor|docs|test
Include what changed AND why in body for non-trivial commits.

---

## Architecture

```
bot_mm/
├── main.py                     # Main orchestrator (async event loop, DMS, metadata monitor)
├── config.py                   # Per-asset configuration + env loading
├── core/
│   ├── quoter.py               # Quote engine (Avellaneda-Stoikov)
│   ├── inventory.py            # Inventory tracking & skew
│   ├── risk.py                 # Risk limits, circuit breakers
│   ├── order_manager.py        # Order lifecycle, partial fills, dedup
│   ├── signals.py              # Directional bias (Kalman+QQE)
│   └── book_imbalance.py       # L2 order book imbalance tracker
├── exchanges/
│   ├── base_mm.py              # Abstract exchange interface
│   └── hl_mm.py                # Hyperliquid (REST, ALO, batch, dynamic rounding)
├── strategies/
│   ├── basic_mm.py             # Spread capture + bias + toxicity + hot reload
│   └── adaptive_mm.py          # Vol regime, fill rate, inventory decay
├── ml/
│   ├── fill_predictor.py       # GBM fill + adverse selection predictor
│   ├── data_generator.py       # Training data from candles
│   ├── toxicity.py             # Real-time adverse selection detector
│   ├── dynamic_sizer.py        # Adaptive order sizing (vol, fill rate, drawdown)
│   └── auto_tuner.py           # Runtime parameter self-adjustment
├── data/
│   └── l2_recorder.py          # HL WebSocket L2 order book recorder
└── utils/
    ├── logger.py               # Structured logging with colors
    ├── metrics.py              # PnL tracking, fill rates, daily buckets
    └── notifier.py             # Discord webhook notifications

backtest/
├── mm_backtester.py            # Candle-based MM simulation (~60% realism)
├── ob_backtester.py            # Tick-level order book replay (~90% realism)
└── ob_loader.py                # L2/trade data loader for replay

scripts/
├── run_mm_optimizer.py         # Grid search optimizer (quick/normal/full modes)
├── train_fill_model.py         # ML model training pipeline
├── record_orderbook.py         # L2 WebSocket data recorder CLI
├── run_ob_backtest.py          # Order book replay backtest CLI
├── daily_reoptimize.py         # Nightly auto-reoptimizer (144 combos/asset)
├── backtest_supervisor.py      # Meta-supervisor simulation + scoring (V3 tuning)
├── tune_supervisor.py          # Supervisor variant comparison (6 configs)
├── monthly_breakdown.py        # Per-month per-asset raw backtests
├── monthly_supervisor.py       # Monthly view with supervisor + compound
├── fee_comparison.py           # Rebate vs cost vs zero fee comparison
├── detailed_backtest.py        # Full per-asset stats (fees, volume, risk, spread)
└── _calc_fees.py               # Supervisor gross/fee/net calculator

deploy/
├── README.md                   # AWS deployment step-by-step guide
├── setup_ec2.sh                # One-time EC2 setup (Python, venv, systemd, cron)
├── botmm-recorder.service      # Systemd unit file
├── monitor.sh                  # Health check (cron, Discord alerts)
├── sync_to_s3.sh               # S3 backup script
└── .env.example                # Configuration template

tests/                          # 343 tests across 16 test files
```

## Phase Status

| Phase         | Description                                        | Status                             |
|---------------|----------------------------------------------------|------------------------------------|
| **Phase 1**   | Core MM engine (quoter, inventory, risk, backtester) | ✅ DONE                          |
| **Phase 2**   | Adaptive MM, directional bias, book imbalance      | ✅ DONE                            |
| **Phase 3**   | Cross-exchange arb (multi-venue quoting)            | ⏭️ SKIPPED (testnet unusable)     |
| **Phase 4.1** | Historical order book data collection               | ✅ DONE (L2 WebSocket recorder)   |
| **Phase 4.2** | MM backtester (order book replay)                   | ✅ DONE (tick-level, queue pos)   |
| **Phase 4.3** | ML-based spread prediction                          | ✅ DONE (GBM, AUC 0.77)          |
| **Phase 4.4** | Toxicity detection (adverse selection)              | ✅ DONE (per-side EMA tracking)   |
| **Phase 4.5** | Auto-parameter tuning                               | ✅ DONE (runtime self-adjustment) |
| **Phase 5.1** | Partial fills                                       | ✅ DONE (depth-based, 30%)        |
| **Phase 5.2** | DynamicSizer (adaptive order sizing)                | ✅ DONE (76 tests)                |
| **Phase 5.3** | Compound mode (PnL reinvestment)                    | ✅ DONE (BTC/ETH only)            |
| **Phase 5.4** | Daily auto-reoptimizer + hot reload                 | ✅ DONE (144 combos, drift safety)|
| **Phase 5.5** | Meta-supervisor (capital + risk allocation)          | ✅ DONE (V3 tuning, +21.4%)      |

## Current Performance

### Per-Asset ($12.5K capital, 365d, real fee +0.015%)

| Asset      | Gross PnL |   Fees  | Net PnL  | Return | Sharpe | Compound |
|------------|-----------|---------|----------|--------|--------|----------|
| ETH        |   $16,045 |  $4,077 |  $11,969 |  95.8% |    9.0 | ON       |
| XRP        |   $16,225 |  $4,238 |  $11,986 |  95.9% |    9.1 | OFF      |
| SOL        |   $15,372 |  $4,547 |  $10,823 |  86.6% |   10.7 | OFF      |
| BTC        |   $12,109 |  $3,030 |   $9,080 |  72.6% |   11.0 | ON       |
| ~~HYPE~~   |         — |       — |        — |      — |      — | *removed* |

### Portfolio ($50K, 4 assets, 365d, real fee +0.015%)

| Strategy                       |     Gross |     Fees |      Net | Return | Sharpe |
|--------------------------------|-----------|----------|----------|--------|--------|
| Equal (baseline)               |   $70,316 |  $18,703 |  $51,613 | 103.2% |   16.4 |
| **Supervisor V3 + compound**   |   **$85,380** | **$22,710** | **$62,670** | **125.3%** | **16.1** |
| Old V0 supervisor              |         — |        — |  $60,944 | 121.9% |   14.4 |

### Trading Activity (365d, $50K)

| Metric          |          Value |
|-----------------|----------------|
| Total Fills     |         72,142 |
| Fills/day       |            198 |
| Round Trips     |         43,924 |
| Total Volume    |        $135.3M |
| Volume/day      |          $371K |
| Fees total      |        $15,891 |
| Fee % of Gross  |          26.6% |
| Net profit/fill |         $0.608 |
| Fee cost/fill   |         $0.220 |

### Optimizer Results ($1K base)

| Asset      | Spread | Skew | Bias | Size |    PnL | Compound |
|------------|--------|------|------|------|--------|----------|
| BTC        |    2.0 |  0.3 |  0.2 |  150 | $1,206 | ON       |
| ETH        |    1.5 |  0.3 |  0.2 |  150 | $1,269 | ON       |
| SOL        |    1.5 |  0.5 |  OFF |  150 | $1,197 | OFF      |
| XRP        |    1.5 |  0.5 |  0.2 |  150 | $1,228 | OFF      |
| ~~HYPE~~   |      — |    — |    — |    — |      — | *removed* |

## Key Parameters

```
base_spread_bps=2.0        # Min spread in basis points
vol_multiplier=1.5         # Spread widens with volatility
inventory_skew_factor=0.3  # Optimal skew (from 216-combo optimizer)
max_position_usd=500       # Max inventory per asset (scales with capital)
order_size_usd=150         # Optimal size per quote side
num_levels=2               # Optimal quote levels per side
maker_fee=0.00015          # HL maker fee COST (positive = pay, base tier)
taker_fee=0.00045          # HL taker fee (for hedging)
bias_strength=0.2          # Directional bias from Kalman+QQE
max_daily_loss=capital*0.05 # Auto-scaled 5% of capital
```

## Exchange Integration

### Asset Rounding (dynamic from HL metadata)
Bot fetches `szDecimals` from HL `meta()` on connect + hourly refresh. No hardcoded decimals.
Formula: `price_decimals = 6 - szDecimals`, prices capped at 5 significant figures.

| Asset | szDecimals | Price Decimals | Size Decimals   |
|-------|------------|----------------|-----------------|
| BTC   |          5 |              1 | 5               |
| ETH   |          4 |              2 | 4               |
| SOL   |          2 |              4 | 2               |
| XRP   |          0 |              6 | 0 (integers)    |
| HYPE  |          2 |              4 | 2               |

Symbol resolution is automatic: `XYZUSDT` → strips suffix → validates against HL universe.
New assets can be added to `.env` without code changes.

### Fee Convention (Critical)
- HL base tier maker fee = +0.00015 (POSITIVE = cost, NOT rebate)
- Rebates only at >$500M 14d volume or >0.5% market share
- Bot at Tier 0-1: ~$5M 14d volume → pays 0.015% per fill
- `total_fees` accumulates raw values (positive = cost)
- `net_pnl = gross_pnl - total_fees`
- Bot still profitable: gross spread capture > fee cost

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

### Dynamic Sizer (`ml/dynamic_sizer.py`)
- Adjusts order_size_usd based on vol regime, fill rate, inventory, toxicity, drawdown
- 76 tests covering all edge cases

### Auto-Parameter Tuner (`ml/auto_tuner.py`)
- Runtime self-adjustment based on rolling performance windows
- Rules: Sharpe<0 → widen spread, fill_rate<15% → tighten, inventory>70% → increase skew
- Boundaries enforced, drift reset at 70%
- Impact: +9% Sharpe, 93% profitable days

## Meta-Supervisor System

### Dual Control: Capital + Risk (V3_CONSERVATIVE tuning)

| Mechanizm               | Szybkość             | Wpływ               | Limity          |
|--------------------------|----------------------|----------------------|-----------------|
| **Kapitał** (alokacja)  | Wolny (max ±5%/dzień) | Ile $ per bot       | min $5K, max 35% |
| **Ryzyko** (mnożniki)   | Szybki (max ±10%/dzień) | Size, spread, max_pos | bounds enforced |

### Risk Multipliers per Score Zone

| Zone   | Score     | Size  | Spread | MaxPos |
|--------|-----------|-------|--------|--------|
| Reward | >0.7      | 1.10x |  0.90x |  1.10x |
| Hold   | 0.30-0.7  | 1.0x  |  1.0x  |  1.0x  |
| Punish | 0.10-0.30 | 0.70x |  1.30x |  0.70x |
| Pause  | <0.10     | 0.40x |  1.50x |  0.40x |

### Scoring (absolute, not rank-based)
```
score = 0.40 * sharpe_norm + 0.30 * return_norm + 0.20 * (1-dd_norm) + 0.10 * consistency
```
- 45d rolling window, absolute thresholds
- V3_CONSERVATIVE: gentle punishment (3-10% cut), 1% daily mean-revert to equal
- Tested 6 variants — V3 beats original V0 by +$4.5K (+9% PnL) on 365d

### Compound + Supervisor Integration
- **BTC/ETH:** compound ON — reinvest PnL, supervisor controls BASE allocation only
- **SOL/XRP:** compound OFF — supervisor controls full capital + risk
- Result: $50K → $112.7K in 365d (+21.4% vs equal allocation)

### HYPE Staking Analysis
At $50K bot capital with 125% annual return:

| Rabat |   HYPE |    Koszt | Oszcz./rok | Te $ w bocie | Opłaca się?        |
|-------|--------|----------|------------|--------------|---------------------|
| 5%    |     10 |     $300 |     $1,136 |         $376 | ✅ TAK (+$760/rok) |
| 10%   |    100 |   $3,000 |     $2,271 |       $3,759 | ❌ NIE (-$1,488)   |
| 15%   |  1,000 |  $30,000 |     $3,407 |      $37,590 | ❌ NIE             |

**Rekomendacja:** Stakuj 10 HYPE ($300) = 5% rabat. Resztę do bota.
100 HYPE stake opłaca się dopiero przy kapitale bota > ~$83K.

## Pending Fixes (TODO)

### maker_fee default still wrong in these files:
- `bot_mm/config.py` line 75: `maker_fee: float = -0.00015`
- `backtest/mm_backtester.py` line 143 and 812: `maker_fee: float = -0.00015`
- `backtest/ob_backtester.py` line 100: `maker_fee: float = -0.00015`
- `scripts/monthly_breakdown.py` line 34: `maker_fee=-0.00015`
- `scripts/run_ob_backtest.py` line 33: `default=-0.00015`
- `bot_mm/core/order_manager.py` line 207: `maker_fee: float = -0.00015`

Only `scripts/backtest_supervisor.py` has been updated to `+0.00015`.

## Live Infrastructure

### L2 Recorder (AWS EC2)
- **Instance:** t2.micro, Amazon Linux 2023, eu-central-1 (Frankfurt)
- **Elastic IP:** 63.178.163.203
- **Instance ID:** i-042b5fa60b4081d5a
- **Recording:** BTC, ETH, SOL — L2 book (20 levels) + trades, 24/7
- **Service:** systemd `botmm-recorder` (auto-restart, auto-start on boot)
- **Monitoring:** cron every 5min → Discord alerts (disk, freshness, API, memory)
- **Data path:** `/home/ec2-user/BotMM/data/orderbook/{SYMBOL}/{date}/`
- **Storage:** ~60 MB/day, 8GB EBS → ~4 months before cleanup
- **Deployed:** 2026-02-11
- **SSH:** `ssh -i deploy/botmm-key.pem ec2-user@63.178.163.203`
- **Key file:** `deploy/botmm-key.pem` (gitignored via `*.pem`)

### Useful Commands (EC2)
```bash
sudo systemctl status botmm-recorder
journalctl -u botmm-recorder -f
du -sh data/orderbook/
scp -i deploy/botmm-key.pem -r ec2-user@63.178.163.203:~/BotMM/data/orderbook/ ./data/orderbook/
```

### Data Requirements for Tick-Level Backtest

| Cel                    | Minimum  | Data                    |
|------------------------|----------|-------------------------|
| Smoke test             | 1 day    | ~12h L2 + trades        |
| Sensowny backtest      | 2-3 days | ≥2 daily PnL points     |
| Wiarygodne wyniki      | 7+ days  | statystyczna istotność  |
| Produkcyjny benchmark  | 14-30 d  | pełny obraz rynku       |

## Production Cron Schedule

```bash
# 1. Reoptimize params (3am UTC)
0 3 * * * cd /BotMM && python scripts/daily_reoptimize.py

# 2. Meta-supervisor capital allocation (4am UTC)
0 4 * * * cd /BotMM && python scripts/run_meta_supervisor.py

# 3. Bot hot-reloads live_params.json + allocations.json (~1h interval)
```

## Quick Start

```bash
pip install -r requirements.txt

# Backtest (candle-based)
py backtest/mm_backtester.py --symbol BTCUSDT --days 365 --spread 2.0 --size 150 --levels 2 --skew 0.3 --bias --bias-strength 0.2

# Backtest with ML + compound
py backtest/mm_backtester.py --symbol BTCUSDT --days 365 --spread 2.0 --size 150 --levels 2 --skew 0.3 --bias --bias-strength 0.2 --auto-tune --toxicity --compound

# Order book replay backtest (needs recorded data)
py scripts/run_ob_backtest.py --symbol BTC --date 2026-02-12

# Optimizer
py scripts/run_mm_optimizer.py --symbol BTCUSDT --days 365 --quick --workers 10

# Record live L2 data
py scripts/record_orderbook.py --symbols BTC ETH SOL --levels 20

# Tests
py -m pytest tests/ -v
```

## Dependencies

```
numpy>=1.21.0
pandas>=1.4.0
requests>=2.28.0
python-dotenv>=1.0.0
aiohttp>=3.8.0
hyperliquid-python-sdk>=0.1.0
eth-account>=0.8.0
tqdm>=4.65.0
websockets>=12.0
scikit-learn>=1.3.0
joblib>=1.3.0
```

## Bug Fixes Log

### 2026-02-11: HL price/size rounding (this repo)
- **Problem:** Hardcoded `PRICE_DECIMALS` / `SIZE_DECIMALS` dicts, no sig figs enforcement, missing XRP
- **Fix:** Dynamic `szDecimals` from `meta()`, `_round_price()` enforces 5 sig figs + `6-szDecimals` max decimals, `_to_hl_symbol()` auto-resolves any suffix against HL universe
- **Impact:** Prevents order rejections from HL API ("Price must be divisible by tick size")

### 2026-02-11: setup_ec2.sh missing cronie
- **Problem:** `crontab: command not found` on Amazon Linux 2023
- **Fix:** Added `cronie` to dnf install in setup script

### 2026-02-11: HL maker fee was wrong (rebate → cost)
- **Problem:** Entire codebase had `maker_fee = -0.00015` (assumed rebate). HL base tier is +0.015% COST.
- **Fix:** Changed to `maker_fee = +0.00015` in backtest_supervisor.py. Other files still pending.
- **Impact:** ~$16K difference on 225d. Bot still profitable: gross spread > fee cost.
- **Source:** https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
- **Files still with old value:** config.py, mm_backtester.py, ob_backtester.py, order_manager.py, monthly_breakdown.py, run_ob_backtest.py

### 2026-02-11: Supervisor V0 too aggressive
- **Problem:** V0 supervisor (14d window, 30%/10% cuts, min $500) over-punished assets during temporary dips. ETH dropped to $575 base allocation then recovered. Lost $4.5K vs gentler configs.
- **Fix:** V3_CONSERVATIVE tuning: 45d window, 10%/3% cuts, min $5K, 1% daily mean-revert to equal.
- **Impact:** +$4,500 (+9% PnL) vs V0 on 365d. +21.4% vs EQUAL baseline.

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
15. `acd7988` — docs: update CLAUDE.md, MM_BOT_PLAN with Phase 5 + supervisor results
16. `f77afab` — fix: dynamic HL rounding + metadata monitor + docs overhaul
17. `b4897f6` — docs: add architecture.md and modules.md reference
18. `1a78260` — docs: add file naming convention rule (UPPER CASE for doc files)
19. `3e4d41f` — refactor: remove HYPE from active assets (poor performance)
20. `c8b3b50` — feat: monthly breakdown scripts + backtest results snapshot (225d)
21. `46f9875` — fix: HL maker fee +0.015% cost (not rebate), supervisor V3 tuning (+9% PnL)
22. `7fd015a` — docs: HOW_IT_EARNS profit flow, backtest results v2, staking analysis
