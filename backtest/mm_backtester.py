"""
Market Making Backtester — Candle-based simulation.

Simulates MM spread capture using OHLCV data:
- Models fill probability based on price touching quote levels
- Tracks inventory accumulation and PnL
- Accounts for maker rebates / taker fees
- Simulates adverse selection via high-low range

Realism: ~60% — lacks real order book depth and tick-by-tick data.
Good for parameter sensitivity analysis and strategy comparison.
"""

import argparse
import csv
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

# Add parent directory for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from bot_mm.core.quoter import QuoteEngine, QuoteParams, Quote
from bot_mm.core.inventory import InventoryManager
from bot_mm.core.risk import RiskManager, RiskStatus


@dataclass
class Candle:
    """OHLCV candle."""
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class MMTradeLog:
    """Log entry for a simulated fill."""
    timestamp: str
    side: str
    price: float
    size: float
    fee: float
    inventory_after: float
    realized_pnl: float
    reason: str


@dataclass
class MMBacktestResult:
    """Results of MM backtest simulation."""
    symbol: str
    days: int
    candles: int

    # PnL
    gross_pnl: float = 0.0
    total_fees: float = 0.0
    net_pnl: float = 0.0

    # Trades
    total_fills: int = 0
    buy_fills: int = 0
    sell_fills: int = 0
    round_trips: int = 0
    fills_per_day: float = 0.0

    # Inventory
    max_inventory_usd: float = 0.0
    avg_inventory_usd: float = 0.0
    final_inventory_usd: float = 0.0
    inventory_pnl: float = 0.0

    # Risk
    max_drawdown: float = 0.0
    daily_pnl_std: float = 0.0
    sharpe_ratio: float = 0.0
    risk_halts: int = 0

    # Spread
    avg_spread_captured_bps: float = 0.0
    avg_spread_quoted_bps: float = 0.0

    # Directional bias stats
    avg_bias: float = 0.0
    bullish_pct: float = 0.0
    bearish_pct: float = 0.0
    neutral_pct: float = 0.0

    # ML fill prediction stats
    ml_skipped_quotes: int = 0
    ml_widened_quotes: int = 0

    # Daily breakdown
    daily_pnls: list = field(default_factory=list)


class MMBacktester:
    """
    Candle-based market making simulator.

    Fill model:
    - A bid fills if candle low <= bid price
    - An ask fills if candle high >= ask price
    - Fill probability weighted by how deep price penetrated
    - Adverse selection: if both sides fill, apply penalty
    """

    def __init__(
        self,
        quote_params: QuoteParams = None,
        maker_fee: float = -0.00015,  # HL rebate
        taker_fee: float = 0.00045,
        max_position_usd: float = 500.0,
        max_daily_loss: float = 50.0,
        capital: float = 1000.0,
        atr_period: int = 14,
        use_bias: bool = False,
        bias_strength: float = 0.5,
        ml_model_path: Optional[str] = None,
        ml_skip_threshold: float = 0.3,
        ml_adverse_threshold: float = 0.6,
    ):
        self.quote_params = quote_params or QuoteParams()
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.max_position_usd = max_position_usd
        self.max_daily_loss = max_daily_loss
        self.capital = capital
        self.atr_period = atr_period
        self.use_bias = use_bias
        self.bias_strength = bias_strength
        self.ml_skip_threshold = ml_skip_threshold
        self.ml_adverse_threshold = ml_adverse_threshold

        # Load ML fill predictor if model path provided
        if ml_model_path:
            from bot_mm.ml.fill_predictor import FillPredictor
            self.fill_predictor = FillPredictor()
            self.fill_predictor.load(ml_model_path)
        else:
            self.fill_predictor = None

    def run(self, candles: List[Candle], symbol: str = "BTCUSDT") -> MMBacktestResult:
        """Run MM backtest simulation on candle data."""
        quoter = QuoteEngine(self.quote_params)
        inventory = InventoryManager(symbol, self.max_position_usd)
        risk = RiskManager(
            max_daily_loss_usd=self.max_daily_loss,
            capital_usd=self.capital,
        )

        result = MMBacktestResult(symbol=symbol, days=0, candles=len(candles))
        trade_log: List[MMTradeLog] = []

        # Directional bias
        bias_obj = None
        current_bias = 0.0
        bias_samples: List[float] = []
        regime_counts = {0: 0, 1: 0, -1: 0}  # NEUTRAL, BULLISH, BEARISH
        if self.use_bias:
            from bot_mm.core.signals import DirectionalBias
            bias_obj = DirectionalBias(bias_strength=self.bias_strength)

        # Compute ATR
        atrs = self._compute_atr(candles)

        equity = self.capital
        peak_equity = equity
        max_dd = 0.0
        daily_pnls = []
        current_day = ""
        day_pnl = 0.0
        spreads_captured = []
        spreads_quoted = []
        inventory_samples = []

        for i in range(self.atr_period, len(candles)):
            candle = candles[i]
            atr = atrs[i]
            mid_price = (candle.high + candle.low) / 2.0
            volatility_pct = atr / mid_price if mid_price > 0 else 0.001

            # Update directional bias with candle close
            if bias_obj is not None:
                bias_result = bias_obj.update(candle.close)
                if bias_result is not None:
                    current_bias = bias_result.bias
                    bias_samples.append(current_bias)
                    regime_counts[int(bias_result.regime)] += 1

            # Track daily boundaries
            day = candle.timestamp[:10]
            if day != current_day:
                if current_day:
                    daily_pnls.append(day_pnl)
                current_day = day
                day_pnl = 0.0

            # Risk check
            inventory.update_unrealized(mid_price)
            pos_usd = inventory.state.position_size * mid_price
            status = risk.check_all(
                daily_pnl=day_pnl,
                equity=equity,
                current_vol=volatility_pct,
                position_usd=pos_usd,
                max_position_usd=self.max_position_usd,
            )

            if status == RiskStatus.HALT:
                result.risk_halts += 1
                continue

            # Generate quotes
            quotes = quoter.calculate_quotes(
                mid_price=mid_price,
                volatility_pct=volatility_pct,
                inventory_usd=pos_usd,
                max_position_usd=self.max_position_usd,
                directional_bias=current_bias,
            )

            # Track quoted spread
            bids = [q for q in quotes if q.side == "buy"]
            asks = [q for q in quotes if q.side == "sell"]
            if bids and asks:
                best_bid = max(q.price for q in bids)
                best_ask = min(q.price for q in asks)
                quoted_spread_bps = (best_ask - best_bid) / mid_price * 10000
                spreads_quoted.append(quoted_spread_bps)

            # Simulate fills based on candle range
            for quote in quotes:
                # ML fill prediction: skip or widen quotes
                if self.fill_predictor and self.fill_predictor.is_trained:
                    features = self.fill_predictor.extract_features(
                        candle=candle,
                        prev_candle=candles[i-1] if i > 0 else candle,
                        mid_price=mid_price,
                        quote_price=quote.price,
                        quote_side=quote.side,
                        volatility_pct=volatility_pct,
                        inventory_ratio=abs(pos_usd) / self.max_position_usd,
                        vol_regime=1.0,
                        candle_idx=i,
                    )
                    fill_prob, adverse_prob = self.fill_predictor.predict(features)

                    # Skip low-probability fills
                    if fill_prob < self.ml_skip_threshold:
                        result.ml_skipped_quotes += 1
                        continue

                    # Widen spread for high adverse selection risk
                    if adverse_prob > self.ml_adverse_threshold:
                        result.ml_widened_quotes += 1
                        if quote.side == "buy":
                            quote.price *= (1 - 0.0001)  # move bid down
                        else:
                            quote.price *= (1 + 0.0001)  # move ask up

                fill_result = self._simulate_fill(quote, candle, mid_price, volatility_pct)

                if fill_result is not None:
                    fill_price, fill_size, adverse = fill_result

                    # Check if we should pause this side
                    if inventory.should_pause_side(quote.side, mid_price):
                        continue

                    # Calculate fee (maker for normal fills, taker for adverse)
                    # Convention: positive = cost, negative = rebate
                    fee_rate = self.taker_fee if adverse else self.maker_fee
                    fee = fill_price * fill_size * fee_rate

                    # Process fill
                    rpnl = inventory.on_fill(quote.side, fill_price, fill_size, fee)

                    # PnL impact: realized + fee effect (subtract cost, add rebate)
                    fee_impact = -fee  # negative fee (rebate) becomes positive impact
                    day_pnl += rpnl + fee_impact
                    equity += rpnl + fee_impact

                    result.total_fills += 1
                    if quote.side == "buy":
                        result.buy_fills += 1
                    else:
                        result.sell_fills += 1

                    if rpnl != 0:
                        spreads_captured.append(abs(rpnl) / fill_size / mid_price * 10000)

                    trade_log.append(MMTradeLog(
                        timestamp=candle.timestamp,
                        side=quote.side,
                        price=fill_price,
                        size=fill_size,
                        fee=fee,
                        inventory_after=inventory.state.position_size,
                        realized_pnl=rpnl,
                        reason="adverse" if adverse else "maker",
                    ))

            # Drawdown
            peak_equity = max(peak_equity, equity)
            dd = peak_equity - equity
            max_dd = max(max_dd, dd)

            # Inventory sample
            inventory_samples.append(abs(pos_usd))

            # Update vol baseline
            risk.update_normal_vol(volatility_pct)

        # Final day
        if day_pnl != 0:
            daily_pnls.append(day_pnl)

        # Close remaining inventory at last price (mark-to-market)
        if inventory.state.position_size != 0 and len(candles) > 0:
            last_price = candles[-1].close
            inventory.update_unrealized(last_price)
            result.inventory_pnl = inventory.state.unrealized_pnl

        # Compile results — fees: positive=cost, negative=rebate
        result.gross_pnl = inventory.state.realized_pnl
        result.total_fees = inventory.state.total_fees
        result.net_pnl = inventory.state.realized_pnl - inventory.state.total_fees + result.inventory_pnl
        result.round_trips = inventory.state.round_trips
        result.max_inventory_usd = max(inventory_samples) if inventory_samples else 0
        result.avg_inventory_usd = np.mean(inventory_samples) if inventory_samples else 0
        result.final_inventory_usd = inventory.state.position_size * candles[-1].close if candles else 0
        result.max_drawdown = max_dd
        result.daily_pnls = daily_pnls
        result.days = len(daily_pnls)

        if daily_pnls:
            result.daily_pnl_std = float(np.std(daily_pnls))
            avg_daily = float(np.mean(daily_pnls))
            if result.daily_pnl_std > 0:
                result.sharpe_ratio = avg_daily / result.daily_pnl_std * math.sqrt(365)

        result.fills_per_day = result.total_fills / max(result.days, 1)
        result.avg_spread_captured_bps = float(np.mean(spreads_captured)) if spreads_captured else 0
        result.avg_spread_quoted_bps = float(np.mean(spreads_quoted)) if spreads_quoted else 0

        # Bias stats
        if bias_samples:
            result.avg_bias = float(np.mean(bias_samples))
            total_regimes = sum(regime_counts.values())
            if total_regimes > 0:
                result.bullish_pct = regime_counts[1] / total_regimes * 100
                result.bearish_pct = regime_counts[-1] / total_regimes * 100
                result.neutral_pct = regime_counts[0] / total_regimes * 100

        return result

    def _simulate_fill(
        self, quote: Quote, candle: Candle, mid_price: float, vol_pct: float
    ) -> Optional[Tuple[float, float, bool]]:
        """
        Simulate whether a quote would fill during a candle.

        Returns: (fill_price, fill_size, is_adverse) or None
        """
        if quote.side == "buy":
            # Bid fills if candle low touches or goes below bid
            if candle.low <= quote.price:
                # Fill probability based on penetration depth
                penetration = (quote.price - candle.low) / (candle.high - candle.low) if candle.high != candle.low else 0.5
                fill_prob = min(1.0, 0.3 + penetration * 0.7)

                # Adverse selection: if close < open (bearish candle) and price bounced up
                adverse = candle.close < candle.open and candle.close < quote.price

                if np.random.random() < fill_prob:
                    # Fill at quote price (maker)
                    return (quote.price, quote.size, adverse)

        else:  # sell
            # Ask fills if candle high touches or goes above ask
            if candle.high >= quote.price:
                penetration = (candle.high - quote.price) / (candle.high - candle.low) if candle.high != candle.low else 0.5
                fill_prob = min(1.0, 0.3 + penetration * 0.7)

                adverse = candle.close > candle.open and candle.close > quote.price

                if np.random.random() < fill_prob:
                    return (quote.price, quote.size, adverse)

        return None

    def _compute_atr(self, candles: List[Candle]) -> List[float]:
        """Compute ATR for each candle."""
        atrs = [0.0] * len(candles)
        if len(candles) < 2:
            return atrs

        trs = []
        for i in range(1, len(candles)):
            tr = max(
                candles[i].high - candles[i].low,
                abs(candles[i].high - candles[i - 1].close),
                abs(candles[i].low - candles[i - 1].close),
            )
            trs.append(tr)

        # Simple SMA for first ATR
        if len(trs) >= self.atr_period:
            atr = sum(trs[:self.atr_period]) / self.atr_period
            atrs[self.atr_period] = atr

            for i in range(self.atr_period + 1, len(candles)):
                atr = (atr * (self.atr_period - 1) + trs[i - 1]) / self.atr_period
                atrs[i] = atr

        return atrs


def load_candles_csv(filepath: str, days: int = 90) -> List[Candle]:
    """Load candles from CSV (BotHL cache format — Unix millis timestamps)."""
    from datetime import datetime, timezone
    candles = []
    with open(filepath, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts_raw = row.get('timestamp', row.get('open_time', '0'))
            # Convert Unix millis to ISO date string
            try:
                ts_ms = int(ts_raw)
                dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                ts_str = dt.strftime('%Y-%m-%d %H:%M:%S')
            except (ValueError, OSError):
                ts_str = str(ts_raw)

            candles.append(Candle(
                timestamp=ts_str,
                open=float(row['open']),
                high=float(row['high']),
                low=float(row['low']),
                close=float(row['close']),
                volume=float(row.get('volume', 0)),
            ))

    if days > 0:
        max_candles = days * 24  # 1h candles
        candles = candles[-max_candles:]

    return candles


def print_results(result: MMBacktestResult, params: QuoteParams):
    """Print formatted backtest results."""
    print()
    print("=" * 70)
    print(f"  MARKET MAKING BACKTEST — {result.symbol}")
    print("=" * 70)

    print(f"\n  Period: {result.days} days ({result.candles} candles)")
    print(f"  Spread: {params.base_spread_bps} bps base, {params.vol_multiplier}x vol mult")
    print(f"  Size:   ${params.order_size_usd}/side, {params.num_levels} level(s)")

    print(f"\n  {'METRIC':<30} {'VALUE':>15}")
    print(f"  {'-'*45}")
    print(f"  {'Realized PnL':<30} {'$'+format(result.gross_pnl, ',.2f'):>15}")
    print(f"  {'Fees (rebates)':<30} {'$'+format(result.total_fees, ',.2f'):>15}")
    print(f"  {'Inventory PnL':<30} {'$'+format(result.inventory_pnl, ',.2f'):>15}")
    print(f"  {'NET PnL':<30} {'$'+format(result.net_pnl, ',.2f'):>15}")
    print()
    print(f"  {'Total fills':<30} {result.total_fills:>15}")
    print(f"  {'  Buy fills':<30} {result.buy_fills:>15}")
    print(f"  {'  Sell fills':<30} {result.sell_fills:>15}")
    print(f"  {'Round trips':<30} {result.round_trips:>15}")
    print(f"  {'Fills/day':<30} {result.fills_per_day:>15.1f}")
    print()
    print(f"  {'Max inventory ($)':<30} {'$'+format(result.max_inventory_usd, ',.0f'):>15}")
    print(f"  {'Avg inventory ($)':<30} {'$'+format(result.avg_inventory_usd, ',.0f'):>15}")
    print(f"  {'Final inventory ($)':<30} {'$'+format(result.final_inventory_usd, ',.0f'):>15}")
    print()
    print(f"  {'Max drawdown':<30} {'$'+format(result.max_drawdown, ',.2f'):>15}")
    print(f"  {'Daily PnL std':<30} {'$'+format(result.daily_pnl_std, ',.2f'):>15}")
    print(f"  {'Sharpe ratio (ann.)':<30} {result.sharpe_ratio:>15.2f}")
    print(f"  {'Risk halts':<30} {result.risk_halts:>15}")
    print()
    print(f"  {'Avg spread quoted (bps)':<30} {result.avg_spread_quoted_bps:>15.2f}")
    print(f"  {'Avg spread captured (bps)':<30} {result.avg_spread_captured_bps:>15.2f}")

    if result.avg_bias != 0.0 or result.bullish_pct != 0.0:
        print()
        print(f"  {'Directional Bias':<30}")
        print(f"  {'-'*45}")
        print(f"  {'Avg bias':<30} {result.avg_bias:>+15.4f}")
        print(f"  {'Bullish %':<30} {result.bullish_pct:>14.1f}%")
        print(f"  {'Bearish %':<30} {result.bearish_pct:>14.1f}%")
        print(f"  {'Neutral %':<30} {result.neutral_pct:>14.1f}%")

    if result.days > 0:
        daily_avg = result.net_pnl / result.days
        monthly = daily_avg * 30
        annual = daily_avg * 365
        print()
        print(f"  {'Avg daily PnL':<30} {'$'+format(daily_avg, ',.2f'):>15}")
        print(f"  {'Monthly (projected)':<30} {'$'+format(monthly, ',.0f'):>15}")
        print(f"  {'Annual (projected)':<30} {'$'+format(annual, ',.0f'):>15}")

    # ML fill prediction stats
    if result.ml_skipped_quotes > 0 or result.ml_widened_quotes > 0:
        print()
        print(f"  {'ML Stats':<30}")
        print(f"  {'-'*45}")
        print(f"  {'Quotes skipped (low fill %)':<30} {result.ml_skipped_quotes:>15}")
        print(f"  {'Quotes widened (high adverse)':<30} {result.ml_widened_quotes:>15}")

    # Daily PnL distribution
    if result.daily_pnls:
        pos_days = sum(1 for d in result.daily_pnls if d > 0)
        neg_days = sum(1 for d in result.daily_pnls if d < 0)
        zero_days = sum(1 for d in result.daily_pnls if d == 0)
        print()
        print(f"  {'Profitable days':<30} {pos_days:>7} / {result.days} ({pos_days/result.days*100:.0f}%)")
        print(f"  {'Loss days':<30} {neg_days:>7} / {result.days}")
        print(f"  {'Halted days':<30} {zero_days:>7} / {result.days}")
        if result.daily_pnls:
            print(f"  {'Best day':<30} {'$'+format(max(result.daily_pnls), ',.2f'):>15}")
            print(f"  {'Worst day':<30} {'$'+format(min(result.daily_pnls), ',.2f'):>15}")

    print()
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Market Making Backtester")
    parser.add_argument("--symbol", default="BTCUSDT", help="Trading pair")
    parser.add_argument("--days", type=int, default=90, help="Days of data")
    parser.add_argument("--spread", type=float, default=2.0, help="Base spread (bps)")
    parser.add_argument("--size", type=float, default=100.0, help="Order size ($)")
    parser.add_argument("--max-pos", type=float, default=500.0, help="Max position ($)")
    parser.add_argument("--capital", type=float, default=1000.0, help="Starting capital ($)")
    parser.add_argument("--levels", type=int, default=1, help="Quote levels per side")
    parser.add_argument("--vol-mult", type=float, default=1.5, help="Volatility multiplier")
    parser.add_argument("--skew", type=float, default=0.5, help="Inventory skew factor")
    parser.add_argument("--maker-fee", type=float, default=-0.00015, help="Maker fee (neg=rebate)")
    parser.add_argument("--taker-fee", type=float, default=0.00045, help="Taker fee")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--data-dir", default=None, help="Data cache directory")
    parser.add_argument("--bias", action="store_true", help="Enable directional bias (Kalman+QQE)")
    parser.add_argument("--bias-strength", type=float, default=0.5, help="Bias strength 0-1")
    parser.add_argument("--ml-model", default=None, help="Path to trained fill model (.joblib)")
    parser.add_argument("--ml-skip", type=float, default=0.3, help="Skip quotes below this fill probability")
    parser.add_argument("--ml-adverse", type=float, default=0.6, help="Widen spread above this adverse probability")
    args = parser.parse_args()

    np.random.seed(args.seed)

    # Find data file
    data_dir = args.data_dir
    if data_dir is None:
        # Try BotHL cache first
        bothl_cache = Path(__file__).parent.parent.parent / "BotHL" / "data" / "cache"
        local_cache = Path(__file__).parent.parent / "data" / "cache"
        if bothl_cache.exists():
            data_dir = str(bothl_cache)
        elif local_cache.exists():
            data_dir = str(local_cache)
        else:
            print(f"ERROR: No data directory found. Provide --data-dir or place CSV in data/cache/")
            sys.exit(1)

    csv_file = os.path.join(data_dir, f"{args.symbol}_1h.csv")
    if not os.path.exists(csv_file):
        print(f"ERROR: Data file not found: {csv_file}")
        print(f"Available files: {os.listdir(data_dir)}")
        sys.exit(1)

    print(f"Loading {args.symbol} data from {csv_file}...")
    candles = load_candles_csv(csv_file, args.days)
    print(f"Loaded {len(candles)} candles ({args.days} days)")

    params = QuoteParams(
        base_spread_bps=args.spread,
        vol_multiplier=args.vol_mult,
        inventory_skew_factor=args.skew,
        order_size_usd=args.size,
        num_levels=args.levels,
    )

    bt = MMBacktester(
        quote_params=params,
        maker_fee=args.maker_fee,
        taker_fee=args.taker_fee,
        max_position_usd=args.max_pos,
        capital=args.capital,
        use_bias=args.bias,
        bias_strength=args.bias_strength,
        ml_model_path=args.ml_model,
        ml_skip_threshold=args.ml_skip,
        ml_adverse_threshold=args.ml_adverse,
    )

    result = bt.run(candles, args.symbol)
    print_results(result, params)


if __name__ == "__main__":
    main()
