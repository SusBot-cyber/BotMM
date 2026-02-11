# BotMM Architecture

## System Overview

BotMM is an automated market-making bot for Hyperliquid perpetual futures. It places bid/ask quotes around mid price, captures spread on round-trip fills, and earns HL maker rebates.

```
                          ┌──────────────────────────────────────────────────┐
                          │                    main.py                       │
                          │  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
                          │  │ Strategy │  │   DMS    │  │   Metadata    │  │
                          │  │  per sym  │  │ heartbeat│  │   monitor     │  │
                          │  │  (1s loop)│  │  (15s)   │  │   (1h)        │  │
                          │  └────┬─────┘  └──────────┘  └───────────────┘  │
                          └───────┼─────────────────────────────────────────┘
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
        ┌──────────┐       ┌──────────┐        ┌──────────┐
        │  Quoter  │       │   Risk   │        │ Exchange │
        │(A-S model)│      │(breakers)│        │ Adapter  │
        └────┬─────┘       └──────────┘        │  (HL)    │
             │                                  └────┬─────┘
    ┌────────┼────────┐                              │
    ▼        ▼        ▼                              ▼
┌───────┐┌───────┐┌────────┐                  ┌──────────┐
│Signals││ Book  ││Toxicity│                  │Hyperliquid│
│Kalman ││Imbal. ││  ML    │                  │  REST API │
│+ QQE  ││       ││        │                  └──────────┘
└───────┘└───────┘└────────┘
```

## Data Flow

### 1. Price → Quote → Order (every ~1 second)

```
Market Data (HL API)
    │
    ├─→ Volatility estimate (EMA of returns)
    ├─→ Book imbalance (bid vs ask pressure)
    ├─→ Directional signal (Kalman + QQE: -1/0/+1)
    │
    ▼
QuoteEngine.calculate()
    │
    ├─ base_spread = max(min_spread, vol * vol_multiplier)
    ├─ inventory_skew = position * skew_factor * atr
    ├─ directional_shift = signal * bias_strength * spread
    ├─ toxicity_widening (if ML enabled)
    │
    ├─→ bid_price = mid - spread/2 + skew + shift
    └─→ ask_price = mid + spread/2 + skew + shift
         │
         ▼
    OrderManager.update()
         │
         ├─ Skip if delta < threshold (save API calls)
         ├─ Cancel stale orders
         └─ Place new ALO orders (Add Liquidity Only)
              │
              ▼
         HyperliquidMM API
              │
              ├─ _round_price(): 5 sig figs, (6-szDecimals) decimals
              └─ _round_size(): szDecimals from metadata
```

### 2. Fill Detection & PnL Tracking

```
Order filled (detected via open_orders diff)
    │
    ▼
Inventory.record_fill()
    │
    ├─ Update net position
    ├─ Track entry price (FIFO)
    ├─ Calculate realized PnL
    │
    ▼
Metrics.record_fill()
    │
    ├─ Cumulative PnL (net of fees)
    ├─ Daily Sharpe, drawdown
    ├─ Fill rate, volume
    │
    ▼
Notifier (Discord webhook)
```

### 3. Risk Management

```
RiskManager (checked before every quote)
    │
    ├─ Max position: ±max_position_usd
    ├─ Daily loss limit: max_daily_loss (% of capital)
    ├─ API error counter: circuit break at threshold
    ├─ Cooldown after breaker trip
    │
    ▼
    SAFE → quote normally
    POSITION_LIMIT → quote one-sided (reduce only)
    CIRCUIT_BREAK → cancel all, wait cooldown
```

## Component Relationships

### Core Components

| Component | Depends On | Provides |
|-----------|-----------|----------|
| `QuoteEngine` | signals, book_imbalance, toxicity, config | bid/ask prices & sizes |
| `RiskManager` | inventory, metrics | go/no-go decision |
| `Inventory` | fills from exchange | position, PnL |
| `OrderManager` | exchange adapter | order lifecycle |
| `Signals` | candle data | directional bias (-1/0/+1) |
| `BookImbalance` | L2 orderbook | buy/sell pressure |

### ML Components (Optional)

| Component | Input | Output | Update Freq |
|-----------|-------|--------|-------------|
| `FillPredictor` | spread, vol, imbalance, position | fill_prob, adverse_selection | per-quote |
| `ToxicityTracker` | post-fill price movement | toxicity score (0-1) | per-fill |
| `AutoTuner` | rolling PnL, Sharpe, fill_rate | adjusted spread/skew/size | ~4 hours |
| `DynamicSizer` | vol, fill_rate, toxicity, DD | scaled order size | per-quote |

### Strategy Hierarchy

```
BasicMMStrategy
    │
    ├─ run_iteration()     # 1s main loop
    ├─ _check_fills()      # fill detection via order diff
    ├─ _hot_reload()       # params from live_params.json (every 3600 iter)
    │
    ▼
AdaptiveMMStrategy (extends BasicMM)
    │
    ├─ _detect_vol_regime()    # low/medium/high vol buckets
    ├─ _adapt_parameters()     # adjust spread/size per regime
    ├─ _inventory_decay()      # time-based position reduction
    └─ _track_fill_rate()      # adaptive fill rate targeting
```

## Exchange Integration

### Hyperliquid Adapter (hl_mm.py)

**Order Types:**
- `place_order()` — ALO (Add Liquidity Only) to guarantee maker rebate
- `modify_orders()` — batch modify (up to 20 orders per call)
- `cancel_all_orders()` — emergency cleanup

**Price/Size Rounding (Critical):**
- szDecimals loaded from `info.meta()["universe"]` on connect
- Prices: `round(price, 6 - szDecimals)` then enforce max 5 significant figures
- Sizes: `round(size, szDecimals)` — XRP has szDecimals=0 (integers only!)
- `refresh_metadata()` runs hourly via `_metadata_monitor` task

**Dead Man's Switch:**
- 15-second heartbeat cancels all orders if bot stops responding
- Managed by main.py alongside strategy tasks

**Symbol Resolution:**
- `_to_hl_symbol("BTCUSDT")` → `"BTC"` (strips USDT/USD/USDC/PERP suffixes)
- Validates against `_known_assets` set from HL universe metadata
- New assets auto-detected without code changes

## Backtesting

### Two Engines

| Engine | Realism | Speed | Data Source |
|--------|---------|-------|-------------|
| `mm_backtester.py` | ~60% | Fast | 1h candles (ByBit API) |
| `ob_backtester.py` | ~90% | Slow | Recorded L2 + trades |

### Order Book Replay (ob_backtester.py)

```
Recorded Data (CSV)
    │
    ├─ L2 snapshots (20 levels, every update)
    ├─ Trade prints (price, size, side)
    │
    ▼
OBBacktester
    │
    ├─ Queue position simulation
    ├─ Partial fill modeling
    ├─ Realistic latency (50ms default)
    ├─ Maker rebate applied
    │
    ▼
Results: PnL, Sharpe, fills, inventory path
```

## Infrastructure

### Live Deployment

```
┌─────────────────────────────────────────┐
│  AWS EC2 (t2.micro, eu-central-1)       │
│  Amazon Linux 2023                       │
│                                          │
│  systemd: botmm-recorder.service         │
│  ├─ L2 + trades WebSocket recording      │
│  ├─ Symbols: BTC, ETH, SOL              │
│  └─ Data: ~/BotMM/data/orderbook/        │
│                                          │
│  cron: monitor.sh (every 5 min)          │
│  └─ Restart if process dies              │
│                                          │
│  Elastic IP: 63.178.163.203              │
│  SSH key: deploy/botmm-key.pem           │
└─────────────────────────────────────────┘
```

### Monitoring

- **DMS heartbeat** (15s) — auto-cancel orders on crash
- **Metadata monitor** (1h) — detect new assets, szDecimals changes → Discord alert
- **Recorder monitor** (5min cron) — restart if WebSocket drops
- **Discord notifier** — fills, daily stats, errors, recorder status

## Configuration

### Per-Asset Config (config.py)

Each symbol gets independent `AssetConfig(quote_params, risk_limits)`:
- `QuoteParams`: spread, vol_mult, skew, size, levels, bias
- `RiskLimits`: max_position, max_daily_loss, max_orders

### Hot Reload

Bot checks `data/live_params.json` every 3600 iterations (~1 hour). Changes apply without restart:
- spread, skew, size, levels, bias_strength
- Logged on reload

### Environment Variables

```
HL_PRIVATE_KEY=0x...         # Hyperliquid wallet private key
HL_WALLET_ADDRESS=0x...      # Wallet address
MM_SYMBOLS=BTCUSDT,ETHUSDT   # Comma-separated symbols
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```
