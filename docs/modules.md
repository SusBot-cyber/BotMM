# BotMM Module Reference

## bot_mm/ — Core Bot

### main.py
Entry point and async orchestrator.

**Key functions:**
- `run(args)` — starts exchange, launches per-symbol strategy tasks + DMS heartbeat + metadata monitor
- `_run_symbol(exchange, config)` — single-symbol strategy lifecycle
- `_metadata_monitor(exchange)` — hourly HL metadata refresh, Discord alert on changes
- `parse_args()` — CLI argument parsing (--symbol, --all, --testnet, --mainnet, --capital)

**Async tasks (concurrent):**
1. Per-symbol strategy loops (1s interval)
2. DMS heartbeat (15s interval)
3. Metadata monitor (1h interval)

---

### config.py
Dataclass-based configuration loaded from environment.

**Classes:**
- `QuoteParams` — spread_bps, vol_multiplier, inventory_skew_factor, order_size_usd, num_levels, bias_strength
- `RiskLimits` — max_position_usd, max_daily_loss_pct, max_open_orders, cooldown_seconds
- `AssetConfig` — combines QuoteParams + RiskLimits for one symbol
- `Exchange` (Enum) — HYPERLIQUID, BINANCE, BYBIT

**Functions:**
- `load_config(symbol) → AssetConfig` — loads from .env with per-asset overrides
- `get_all_symbols() → list[str]` — parses MM_SYMBOLS env var

---

## bot_mm/core/ — Quote Engine & Risk

### quoter.py
Avellaneda-Stoikov quote engine.

**Class: `QuoteEngine`**
- `calculate(mid, vol, position, imbalance, signal, toxicity) → Quote` — main calculation
- `Quote` — dataclass with bid_price, ask_price, bid_size, ask_size

**Pricing formula:**
```
spread = max(min_spread, vol * vol_multiplier)
skew = position * skew_factor * atr
shift = signal * bias_strength * spread
bid = mid - spread/2 + skew + shift
ask = mid + spread/2 + skew + shift
```

---

### risk.py
Circuit breaker and position safety.

**Class: `RiskManager`**
- `check(inventory) → RiskState` — returns SAFE, POSITION_LIMIT, or CIRCUIT_BREAK
- `record_error()` — increments API error counter
- `reset()` — clears error state after cooldown

**RiskState enum:** SAFE, POSITION_LIMIT, CIRCUIT_BREAK

---

### inventory.py
Position and PnL tracking.

**Class: `InventoryTracker`**
- `record_fill(side, price, size, fee)` — updates position, calculates realized PnL
- `unrealized_pnl(current_price) → float` — mark-to-market
- `net_position → float` — current inventory (positive = long)
- `total_fees → float` — cumulative fees paid/earned

---

### order_manager.py
Order lifecycle with smart deduplication.

**Class: `OrderManager`**
- `update(quotes, exchange) → list[OrderResult]` — places/modifies orders
- `cancel_all(exchange)` — emergency cleanup
- Skips modifications when price delta < threshold (saves API calls)

---

### signals.py
Directional bias from Kalman Filter + QQE.

**Class: `DirectionalSignal`**
- `update(candles) → int` — returns -1 (bearish), 0 (neutral), +1 (bullish)
- Uses Kalman filter for price trend, QQE for RSI smoothing
- Output used to shift quotes in trend direction

---

### book_imbalance.py
Order book pressure measurement.

**Class: `BookImbalanceTracker`**
- `update(bids, asks) → float` — returns -1.0 to +1.0 (sell to buy pressure)
- EMA-smoothed over configurable window
- Used by quoter to adjust spread/skew

---

## bot_mm/exchanges/ — Exchange Adapters

### base_mm.py
Abstract interface for exchange adapters.

**Class: `BaseMMExchange` (ABC)**
- `connect()` / `disconnect()`
- `get_mid_price(symbol) → float`
- `get_orderbook(symbol) → dict`
- `place_order(symbol, side, price, size) → OrderResult`
- `modify_orders(orders) → list[OrderResult]`
- `cancel_all_orders(symbol)`
- `get_open_orders(symbol) → list[Order]`
- `get_position(symbol) → Position`
- `set_dead_mans_switch(timeout_ms)`
- `refresh_metadata()`

---

### hl_mm.py
Hyperliquid REST API adapter. All orders flow through this module.

**Class: `HyperliquidMM(BaseMMExchange)`**

Key methods:
- `connect()` — inits SDK, loads szDecimals + known_assets from meta()
- `place_order()` — ALO orders with precise rounding
- `modify_orders()` — batch modify (up to 20)
- `cancel_all_orders()` — cancel by symbol
- `set_dead_mans_switch(timeout_ms)` — HL native DMS
- `refresh_metadata()` — reloads szDecimals, detects changes

Rounding (critical):
- `_round_price(price, symbol)` — max 5 sig figs, max (6-szDecimals) decimals
- `_round_size(size, symbol)` — exact szDecimals from metadata
- `_to_hl_symbol(symbol)` — strips USDT/USD/USDC/PERP, validates vs HL universe

Module-level state:
- `_sz_decimals: dict[str, int]` — asset → szDecimals mapping
- `_known_assets: set[str]` — all HL perp asset names
- `SYMBOL_MAP: dict` — static + dynamically cached symbol mappings

---

## bot_mm/strategies/ — Trading Strategies

### basic_mm.py
Core market-making strategy.

**Class: `BasicMMStrategy`**
- `start(exchange, config)` — main loop entry
- `run_iteration()` — single tick: fetch data → quote → risk check → update orders → detect fills
- `_check_fills()` — compares open orders vs previous snapshot
- `_hot_reload()` — checks `data/live_params.json` every 3600 iterations

---

### adaptive_mm.py
Extended strategy with regime detection.

**Class: `AdaptiveMMStrategy(BasicMMStrategy)`**
- `_detect_vol_regime()` — classifies LOW/MEDIUM/HIGH volatility
- `_adapt_parameters()` — adjusts spread/size per regime
- `_inventory_decay()` — gradually reduces stale positions over time
- `_track_fill_rate()` — targets configurable fill rate, widens/tightens spread

---

## bot_mm/ml/ — Machine Learning

### fill_predictor.py
Fill probability and adverse selection prediction.

**Class: `FillPredictor`**
- `predict(features) → FillPrediction` — fill_prob, adverse_selection_risk
- `train(X, y)` — trains GradientBoosting model
- `save(path)` / `load(path)` — model persistence
- Features: spread, volatility, imbalance, position, time_of_day
- Performance: AUC 0.77

---

### toxicity.py
Post-fill adverse selection tracking.

**Class: `ToxicityTracker`**
- `record_fill(price, side)` — starts tracking
- `update(current_price)` — measures price movement since fill
- `toxicity_score → float` — 0.0 (clean) to 1.0 (toxic)
- EMA-smoothed over configurable window
- Used by quoter to widen spread on toxic flow

---

### auto_tuner.py
Runtime self-optimization.

**Class: `AutoTuner`**
- `maybe_tune(metrics) → dict | None` — returns new params if improvement found
- Evaluates rolling windows (~4h) of PnL, Sharpe, fill_rate
- Adjusts: spread, skew, order_size within safety bounds
- Result: +9% Sharpe improvement, 93% profitable days

---

### dynamic_sizer.py
Adaptive order sizing.

**Class: `DynamicSizer`**
- `get_size(base_size, vol, fill_rate, toxicity, drawdown, inventory) → float`
- Scales down on: high vol, low fill rate, high toxicity, drawdown, large inventory
- Kelly-criterion inspired with hard caps

---

### data_generator.py
Training data generation for FillPredictor.

**Functions:**
- `generate_training_data(candles, params) → DataFrame` — simulates MM and labels fills
- Output columns: features + fill_occurred + adverse_move

---

## bot_mm/data/ — Data Recording

### l2_recorder.py
WebSocket L2 orderbook + trade recorder.

**Class: `L2Recorder`**
- `start(symbols, levels)` — connects WebSocket, starts recording
- `stop()` — graceful shutdown
- Records to `data/orderbook/{SYMBOL}/{YYYY-MM-DD_HH}.csv`
- Hourly file rotation
- Data format: timestamp, type (snapshot/trade), price levels or trade data

---

## bot_mm/utils/ — Utilities

### logger.py
Structured logging with color output.

**Functions:**
- `get_logger(name) → Logger` — returns configured logger
- Console: colored by level (DEBUG gray, INFO green, WARNING yellow, ERROR red)
- File: `logs/botmm.log` with rotation

---

### metrics.py
Performance tracking and statistics.

**Class: `MetricsTracker`**
- `record_fill(pnl, fee, volume)` — updates counters
- `daily_stats() → dict` — PnL, Sharpe, drawdown, fill count, volume
- `cumulative_stats() → dict` — lifetime aggregates
- Sharpe calculation uses daily returns

---

### notifier.py
Discord webhook notifications.

**Class: `DiscordNotifier`**
- `send_fill(symbol, side, price, size, pnl)` — fill alert
- `send_daily_report(stats)` — end-of-day summary
- `send_error(message)` — error alert
- `send_recorder_status(status)` — recorder health
- `send_metadata_change(changes)` — HL metadata alerts
- 18 notification methods total
- Rate-limited to avoid Discord throttling

---

## backtest/ — Backtesting Engines

### mm_backtester.py
Candle-based MM backtester (~60% realism).

- Simulates fills when candle high/low touches quote level
- Fast enough for parameter optimization (grid search)
- Fetches candles from ByBit API
- CLI: `--symbol, --days, --spread, --size, --levels, --skew, --bias, --compound, --auto-tune, --toxicity`

### ob_backtester.py
Order book replay backtester (~90% realism).

- Uses recorded L2 snapshots + trade prints
- Models queue position, partial fills, latency
- Much slower but highly realistic
- CLI via `scripts/run_ob_backtest.py`

### ob_loader.py
Loads recorded orderbook data from CSV files.

- `load(symbol, date) → list[Event]` — parses hourly CSVs
- Returns unified stream of snapshots and trades

---

## scripts/ — CLI Tools

| Script | Purpose |
|--------|---------|
| `run_mm_optimizer.py` | Grid search over params, parallel workers, ranks by composite score |
| `daily_reoptimize.py` | Nightly: fetch data → optimize → apply if >threshold improvement |
| `backtest_supervisor.py` | Simulates meta-supervisor capital allocation across assets |
| `train_fill_model.py` | Trains FillPredictor GradientBoosting model |
| `run_ob_backtest.py` | CLI wrapper for order book replay backtest |
| `record_orderbook.py` | Standalone L2 recorder (same as l2_recorder.py but CLI) |
| `md_to_html.py` | Converts MM_BOT_PLAN.md to styled HTML |

---

## tests/ — Test Suite

343 tests across 16 modules. Run with:
```bash
python -m pytest tests/ -v
```

| Module | Tests | Coverage |
|--------|-------|----------|
| test_quoter | Quote generation, edge cases | QuoteEngine |
| test_risk | Circuit breaker, limits | RiskManager |
| test_inventory | Position, PnL, fills | InventoryTracker |
| test_signals | Kalman, QQE, bias | DirectionalSignal |
| test_book_imbalance | Imbalance calculation | BookImbalanceTracker |
| test_fill_predictor | ML model train/predict | FillPredictor |
| test_dynamic_sizer | Size scaling logic | DynamicSizer |
| test_auto_tuner | Param tuning logic | AutoTuner |
| test_toxicity | Adverse selection | ToxicityTracker |
| test_l2_recorder | Recording lifecycle | L2Recorder |
| test_ob_loader | Data loading/parsing | OBLoader |
| test_ob_backtester | Replay simulation | OBBacktester |
| test_notifier | Discord webhook | DiscordNotifier |
| test_supervisor | Capital allocation | Supervisor |
| test_partial_fills | Fill edge cases | OrderManager |
| test_adaptive | Regime detection | AdaptiveMMStrategy |

Known pre-existing failures:
- `test_fill_predictor` — requires scikit-learn (optional dependency)
- `test_l2_recorder` — requires websockets runtime
