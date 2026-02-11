"""
Order Book Replay Backtester — tick-by-tick MM simulation.

Unlike the candle-based backtester which estimates fills,
this engine replays actual order book states and trade flow
to determine realistic fill behavior.

Realism: ~90% — uses real L2 data and trade flow.
Missing: queue position simulation is approximate, no partial fills from depth.
"""

import math
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.ob_loader import OrderBookSnapshot, TradeTick, L2Level
from bot_mm.core.quoter import QuoteEngine, QuoteParams, Quote
from bot_mm.core.inventory import InventoryManager
from bot_mm.core.risk import RiskManager, RiskStatus


@dataclass
class OBBacktestResult:
    """Results from order book replay backtest."""
    symbol: str
    duration_hours: float
    total_snapshots: int
    total_market_trades: int

    # PnL
    gross_pnl: float = 0.0
    total_fees: float = 0.0
    net_pnl: float = 0.0

    # Fills
    total_fills: int = 0
    buy_fills: int = 0
    sell_fills: int = 0
    fills_per_hour: float = 0.0

    # Queue / execution quality
    avg_queue_position: float = 0.0
    avg_fill_time_ms: float = 0.0

    # Inventory
    max_inventory_usd: float = 0.0
    avg_inventory_usd: float = 0.0

    # Risk
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0

    # Spread
    avg_spread_quoted_bps: float = 0.0
    avg_spread_captured_bps: float = 0.0
    avg_market_spread_bps: float = 0.0

    # Adverse selection
    adverse_fills: int = 0
    adverse_pct: float = 0.0

    # Daily breakdown
    daily_pnls: list = field(default_factory=list)


@dataclass
class PendingOrder:
    """A resting limit order in the simulated book."""
    side: str          # "buy" or "sell"
    price: float
    size: float
    remaining: float   # unfilled size
    placed_at: str     # timestamp
    level: int         # which quote level (0 = best)
    queue_position: float  # estimated queue depth ahead (in USD)


class OBBacktester:
    """
    Order book replay MM backtester.

    Simulation model:
    1. On each L2 snapshot: update market state, generate new quotes
    2. On each trade tick: check if any pending order would fill
    3. Fill logic:
       - Our bid fills if market trade is a sell at <= our bid price
       - Our ask fills if market trade is a buy at >= our ask price
       - Queue position: estimate based on book depth at our price level
       - Partial fills possible based on trade size vs our size
    """

    def __init__(
        self,
        quote_params: QuoteParams = None,
        maker_fee: float = -0.00015,   # HL rebate
        taker_fee: float = 0.00045,
        max_position_usd: float = 500.0,
        max_daily_loss: float = 50.0,
        capital: float = 1000.0,
        quote_refresh_snapshots: int = 1,
        use_queue_position: bool = True,
    ):
        self.quote_params = quote_params or QuoteParams()
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.max_position_usd = max_position_usd
        self.max_daily_loss = max_daily_loss
        self.capital = capital
        self.quote_refresh_snapshots = quote_refresh_snapshots
        self.use_queue_position = use_queue_position

        self.engine = QuoteEngine(self.quote_params)
        self.inventory: Optional[InventoryManager] = None
        self.risk: Optional[RiskManager] = None

        # State
        self._pending_bids: List[PendingOrder] = []
        self._pending_asks: List[PendingOrder] = []
        self._current_snapshot: Optional[OrderBookSnapshot] = None
        self._snapshot_count = 0
        self._volatility_pct = 0.005  # initial estimate
        self._mid_prices: List[float] = []

        # Tracking
        self._fill_times: List[float] = []
        self._queue_positions: List[float] = []
        self._spreads_quoted: List[float] = []
        self._spreads_captured: List[float] = []
        self._market_spreads: List[float] = []
        self._inventory_samples: List[float] = []
        self._adverse_count = 0
        self._equity_curve: List[float] = []
        self._daily_pnl_tracker: dict = {}

    def run(
        self,
        snapshots: List[OrderBookSnapshot],
        trades: List[TradeTick],
        symbol: str = "BTC",
    ) -> OBBacktestResult:
        """Run tick-level replay backtest."""
        if not snapshots:
            return OBBacktestResult(
                symbol=symbol, duration_hours=0, total_snapshots=0,
                total_market_trades=len(trades),
            )

        self.inventory = InventoryManager(symbol=symbol, max_position_usd=self.max_position_usd)
        self.risk = RiskManager(
            max_daily_loss_usd=self.max_daily_loss,
            capital_usd=self.capital,
        )

        # Reset state
        self._pending_bids = []
        self._pending_asks = []
        self._current_snapshot = None
        self._snapshot_count = 0
        self._mid_prices = []
        self._fill_times = []
        self._queue_positions = []
        self._spreads_quoted = []
        self._spreads_captured = []
        self._market_spreads = []
        self._inventory_samples = []
        self._adverse_count = 0
        self._equity_curve = [self.capital]
        self._daily_pnl_tracker = {}

        # Build merged timeline
        from backtest.ob_loader import OrderBookLoader
        loader = OrderBookLoader()
        timeline = loader.create_timeline(snapshots, trades)

        for event in timeline:
            if isinstance(event, OrderBookSnapshot):
                self._on_snapshot(event)
            elif isinstance(event, TradeTick):
                self._on_trade(event)

        # Compile results
        return self._compile_results(symbol, snapshots, trades)

    def _on_snapshot(self, snapshot: OrderBookSnapshot):
        """Process new L2 snapshot — update market state and refresh quotes."""
        self._current_snapshot = snapshot
        self._snapshot_count += 1

        mid = snapshot.mid_price
        if mid > 0:
            self._mid_prices.append(mid)
            self._market_spreads.append(snapshot.spread_bps)

        # Update volatility estimate from mid price returns
        if len(self._mid_prices) >= 20:
            recent = self._mid_prices[-20:]
            returns = [(recent[i] - recent[i - 1]) / recent[i - 1]
                       for i in range(1, len(recent))]
            if returns:
                self._volatility_pct = max(
                    sum(abs(r) for r in returns) / len(returns),
                    0.0001,
                )

        # Sample inventory
        if self.inventory:
            self._inventory_samples.append(abs(self.inventory.position_usd))

        # Refresh quotes every N snapshots
        if self._snapshot_count % self.quote_refresh_snapshots == 0 and mid > 0:
            self._refresh_quotes(snapshot)

    def _refresh_quotes(self, snapshot: OrderBookSnapshot):
        """Generate new quotes from QuoteEngine and replace pending orders."""
        mid = snapshot.mid_price
        inv_usd = self.inventory.state.position_size * mid if self.inventory else 0.0

        # Check risk
        equity = self.capital + (self.inventory.state.realized_pnl if self.inventory else 0.0)
        daily_pnl = self.inventory.state.realized_pnl if self.inventory else 0.0

        status = self.risk.check_all(
            daily_pnl=daily_pnl,
            equity=equity,
            current_vol=self._volatility_pct,
            position_usd=abs(inv_usd),
            max_position_usd=self.max_position_usd,
        )

        if status == RiskStatus.HALT:
            self._pending_bids = []
            self._pending_asks = []
            return

        # Book imbalance from L2
        bid_depth = snapshot.bid_depth
        ask_depth = snapshot.ask_depth
        total_depth = bid_depth + ask_depth
        book_imbalance = (bid_depth - ask_depth) / total_depth if total_depth > 0 else 0.0

        quotes = self.engine.calculate_quotes(
            mid_price=mid,
            volatility_pct=self._volatility_pct,
            inventory_usd=inv_usd,
            max_position_usd=self.max_position_usd,
            book_imbalance=book_imbalance,
        )

        # Replace all pending orders with new quotes
        self._pending_bids = []
        self._pending_asks = []

        for q in quotes:
            queue_pos = self._estimate_queue_position_from_quote(q, snapshot)
            order = PendingOrder(
                side=q.side,
                price=q.price,
                size=q.size,
                remaining=q.size,
                placed_at=snapshot.timestamp,
                level=q.level,
                queue_position=queue_pos,
            )
            if q.side == "buy":
                self._pending_bids.append(order)
            else:
                self._pending_asks.append(order)

        # Track quoted spread
        if self._pending_bids and self._pending_asks:
            best_bid = max(o.price for o in self._pending_bids)
            best_ask = min(o.price for o in self._pending_asks)
            spread_bps = (best_ask - best_bid) / mid * 10000.0
            self._spreads_quoted.append(spread_bps)

    def _on_trade(self, trade: TradeTick):
        """Process market trade — check pending orders for fills."""
        if not self._current_snapshot:
            return

        # Market sell → can fill our bids
        if trade.side == "sell":
            remaining_trade_size = trade.size
            for order in list(self._pending_bids):
                if remaining_trade_size <= 0:
                    break
                fill_size = self._check_fill(order, trade, remaining_trade_size)
                if fill_size and fill_size > 0:
                    self._execute_fill(order, trade, fill_size)
                    remaining_trade_size -= fill_size

        # Market buy → can fill our asks
        elif trade.side == "buy":
            remaining_trade_size = trade.size
            for order in list(self._pending_asks):
                if remaining_trade_size <= 0:
                    break
                fill_size = self._check_fill(order, trade, remaining_trade_size)
                if fill_size and fill_size > 0:
                    self._execute_fill(order, trade, fill_size)
                    remaining_trade_size -= fill_size

    def _check_fill(
        self,
        order: PendingOrder,
        trade: TradeTick,
        available_size: float,
    ) -> Optional[float]:
        """
        Check if a pending order fills against a market trade.

        Returns fill_size or None.
        """
        if order.remaining <= 0:
            return None

        # Check price match
        if order.side == "buy" and trade.price > order.price:
            return None
        if order.side == "sell" and trade.price < order.price:
            return None

        # Queue position check
        if self.use_queue_position and order.queue_position > 0:
            # Trade must eat through queue before reaching us
            queue_size_units = order.queue_position / trade.price if trade.price > 0 else 0
            if available_size <= queue_size_units:
                # Trade consumed by queue ahead of us
                order.queue_position -= available_size * trade.price
                order.queue_position = max(order.queue_position, 0)
                return None
            # Remaining after eating queue
            available_after_queue = available_size - queue_size_units
            order.queue_position = 0
        else:
            available_after_queue = available_size

        # Fill size = min(available, our remaining)
        fill_size = min(available_after_queue, order.remaining)
        return fill_size if fill_size > 0 else None

    def _execute_fill(self, order: PendingOrder, trade: TradeTick, fill_size: float):
        """Process a fill — update inventory, fees, tracking."""
        fee = self.maker_fee * order.price * fill_size

        realized = self.inventory.on_fill(
            side=order.side,
            price=order.price,
            size=fill_size,
            fee=fee,
        )

        order.remaining -= fill_size
        if order.remaining <= 1e-12:
            if order.side == "buy":
                if order in self._pending_bids:
                    self._pending_bids.remove(order)
            else:
                if order in self._pending_asks:
                    self._pending_asks.remove(order)

        # Track fill time (ms between placed_at and trade timestamp)
        try:
            placed = datetime.fromisoformat(order.placed_at)
            filled = datetime.fromisoformat(trade.timestamp)
            fill_time_ms = (filled - placed).total_seconds() * 1000.0
            self._fill_times.append(fill_time_ms)
        except (ValueError, TypeError):
            pass

        # Track queue position at fill
        self._queue_positions.append(order.queue_position)

        # Track captured spread
        mid = self._current_snapshot.mid_price if self._current_snapshot else order.price
        if mid > 0:
            if order.side == "buy":
                captured_bps = (mid - order.price) / mid * 10000.0
            else:
                captured_bps = (order.price - mid) / mid * 10000.0
            self._spreads_captured.append(captured_bps)

        # Adverse selection: trade price significantly past our level
        if order.side == "buy" and trade.price < order.price * 0.999:
            self._adverse_count += 1
        elif order.side == "sell" and trade.price > order.price * 1.001:
            self._adverse_count += 1

        # Track equity
        equity = self.capital + self.inventory.state.realized_pnl + self.inventory.state.total_fees
        self._equity_curve.append(equity)

        # Daily PnL
        date_key = trade.timestamp[:10] if len(trade.timestamp) >= 10 else "unknown"
        if date_key not in self._daily_pnl_tracker:
            self._daily_pnl_tracker[date_key] = 0.0
        self._daily_pnl_tracker[date_key] += realized + fee

    def _estimate_queue_position(
        self, order: PendingOrder, snapshot: OrderBookSnapshot
    ) -> float:
        """Estimate queue depth ahead of our order at a given price level (USD)."""
        if order.side == "buy":
            # Sum bid depth at prices > our price (better bids ahead)
            depth = sum(
                lvl.price * lvl.size for lvl in snapshot.bids
                if lvl.price > order.price
            )
            # Plus fraction of depth at our price level (we're at the back)
            depth += sum(
                lvl.price * lvl.size * 0.5 for lvl in snapshot.bids
                if abs(lvl.price - order.price) < 1e-9
            )
            return depth
        else:
            # Sum ask depth at prices < our price (better asks ahead)
            depth = sum(
                lvl.price * lvl.size for lvl in snapshot.asks
                if lvl.price < order.price
            )
            depth += sum(
                lvl.price * lvl.size * 0.5 for lvl in snapshot.asks
                if abs(lvl.price - order.price) < 1e-9
            )
            return depth

    def _estimate_queue_position_from_quote(
        self, quote: Quote, snapshot: OrderBookSnapshot
    ) -> float:
        """Estimate queue position for a new quote based on current book."""
        if not self.use_queue_position:
            return 0.0

        dummy = PendingOrder(
            side=quote.side, price=quote.price, size=quote.size,
            remaining=quote.size, placed_at="", level=quote.level,
            queue_position=0,
        )
        return self._estimate_queue_position(dummy, snapshot)

    def _compile_results(
        self,
        symbol: str,
        snapshots: List[OrderBookSnapshot],
        trades: List[TradeTick],
    ) -> OBBacktestResult:
        """Compute final backtest metrics."""
        # Duration
        if len(snapshots) >= 2:
            try:
                t0 = datetime.fromisoformat(snapshots[0].timestamp)
                t1 = datetime.fromisoformat(snapshots[-1].timestamp)
                duration_hours = max((t1 - t0).total_seconds() / 3600.0, 0.001)
            except (ValueError, TypeError):
                duration_hours = 1.0
        else:
            duration_hours = 0.0

        inv = self.inventory
        total_fills = inv.state.num_buys + inv.state.num_sells if inv else 0

        # Gross PnL = realized PnL (excluding fees)
        gross_pnl = inv.state.realized_pnl if inv else 0.0
        total_fees = inv.state.total_fees if inv else 0.0
        net_pnl = gross_pnl + total_fees

        # Drawdown from equity curve
        max_dd = 0.0
        peak = self._equity_curve[0] if self._equity_curve else self.capital
        for eq in self._equity_curve:
            peak = max(peak, eq)
            dd = peak - eq
            max_dd = max(max_dd, dd)

        # Sharpe from daily PnLs
        daily_pnls = list(self._daily_pnl_tracker.values())
        sharpe = 0.0
        if len(daily_pnls) >= 2:
            import statistics
            mean_d = statistics.mean(daily_pnls)
            std_d = statistics.stdev(daily_pnls)
            if std_d > 0:
                sharpe = (mean_d / std_d) * math.sqrt(365)

        result = OBBacktestResult(
            symbol=symbol,
            duration_hours=duration_hours,
            total_snapshots=len(snapshots),
            total_market_trades=len(trades),
            gross_pnl=gross_pnl,
            total_fees=total_fees,
            net_pnl=net_pnl,
            total_fills=total_fills,
            buy_fills=inv.state.num_buys if inv else 0,
            sell_fills=inv.state.num_sells if inv else 0,
            fills_per_hour=total_fills / duration_hours if duration_hours > 0 else 0,
            avg_queue_position=(
                sum(self._queue_positions) / len(self._queue_positions)
                if self._queue_positions else 0.0
            ),
            avg_fill_time_ms=(
                sum(self._fill_times) / len(self._fill_times)
                if self._fill_times else 0.0
            ),
            max_inventory_usd=max(self._inventory_samples) if self._inventory_samples else 0.0,
            avg_inventory_usd=(
                sum(self._inventory_samples) / len(self._inventory_samples)
                if self._inventory_samples else 0.0
            ),
            max_drawdown=max_dd,
            sharpe_ratio=sharpe,
            avg_spread_quoted_bps=(
                sum(self._spreads_quoted) / len(self._spreads_quoted)
                if self._spreads_quoted else 0.0
            ),
            avg_spread_captured_bps=(
                sum(self._spreads_captured) / len(self._spreads_captured)
                if self._spreads_captured else 0.0
            ),
            avg_market_spread_bps=(
                sum(self._market_spreads) / len(self._market_spreads)
                if self._market_spreads else 0.0
            ),
            adverse_fills=self._adverse_count,
            adverse_pct=(
                self._adverse_count / total_fills * 100.0
                if total_fills > 0 else 0.0
            ),
            daily_pnls=daily_pnls,
        )

        return result


def print_results(result: OBBacktestResult, params: QuoteParams):
    """Print formatted OB backtest results (matches mm_backtester style)."""
    print()
    print("=" * 70)
    print(f"  ORDER BOOK REPLAY BACKTEST — {result.symbol}")
    print("=" * 70)

    print(f"\n  Duration: {result.duration_hours:.1f} hours")
    print(f"  Snapshots: {result.total_snapshots:,}  |  Market trades: {result.total_market_trades:,}")
    print(f"  Spread: {params.base_spread_bps} bps base, {params.vol_multiplier}x vol mult")
    print(f"  Size:   ${params.order_size_usd}/side, {params.num_levels} level(s)")

    print(f"\n  {'METRIC':<30} {'VALUE':>15}")
    print(f"  {'-'*45}")
    print(f"  {'Realized PnL':<30} {'$'+format(result.gross_pnl, ',.2f'):>15}")
    print(f"  {'Fees (rebates)':<30} {'$'+format(result.total_fees, ',.2f'):>15}")
    print(f"  {'NET PnL':<30} {'$'+format(result.net_pnl, ',.2f'):>15}")
    print()
    print(f"  {'Total fills':<30} {result.total_fills:>15}")
    print(f"  {'  Buy fills':<30} {result.buy_fills:>15}")
    print(f"  {'  Sell fills':<30} {result.sell_fills:>15}")
    print(f"  {'Fills/hour':<30} {result.fills_per_hour:>15.1f}")
    print()
    print(f"  {'Max inventory ($)':<30} {'$'+format(result.max_inventory_usd, ',.0f'):>15}")
    print(f"  {'Avg inventory ($)':<30} {'$'+format(result.avg_inventory_usd, ',.0f'):>15}")
    print()
    print(f"  {'Max drawdown':<30} {'$'+format(result.max_drawdown, ',.2f'):>15}")
    print(f"  {'Sharpe ratio (ann.)':<30} {result.sharpe_ratio:>15.2f}")
    print()
    print(f"  {'Avg spread quoted (bps)':<30} {result.avg_spread_quoted_bps:>15.2f}")
    print(f"  {'Avg spread captured (bps)':<30} {result.avg_spread_captured_bps:>15.2f}")
    print(f"  {'Avg market spread (bps)':<30} {result.avg_market_spread_bps:>15.2f}")
    print()
    print(f"  {'Queue & Execution':<30}")
    print(f"  {'-'*45}")
    print(f"  {'Avg queue position ($)':<30} {'$'+format(result.avg_queue_position, ',.0f'):>15}")
    print(f"  {'Avg fill time (ms)':<30} {result.avg_fill_time_ms:>15.0f}")
    print()
    print(f"  {'Adverse Selection':<30}")
    print(f"  {'-'*45}")
    print(f"  {'Adverse fills':<30} {result.adverse_fills:>15}")
    print(f"  {'Adverse %':<30} {result.adverse_pct:>14.1f}%")

    if result.duration_hours > 0:
        hourly_avg = result.net_pnl / result.duration_hours
        daily = hourly_avg * 24
        monthly = daily * 30
        print()
        print(f"  {'Avg hourly PnL':<30} {'$'+format(hourly_avg, ',.2f'):>15}")
        print(f"  {'Daily (projected)':<30} {'$'+format(daily, ',.2f'):>15}")
        print(f"  {'Monthly (projected)':<30} {'$'+format(monthly, ',.0f'):>15}")

    if result.daily_pnls:
        pos_days = sum(1 for d in result.daily_pnls if d > 0)
        neg_days = sum(1 for d in result.daily_pnls if d < 0)
        print()
        n_days = len(result.daily_pnls)
        print(f"  {'Profitable days':<30} {pos_days:>7} / {n_days}")
        print(f"  {'Loss days':<30} {neg_days:>7} / {n_days}")
        if result.daily_pnls:
            print(f"  {'Best day':<30} {'$'+format(max(result.daily_pnls), ',.2f'):>15}")
            print(f"  {'Worst day':<30} {'$'+format(min(result.daily_pnls), ',.2f'):>15}")

    print()
    print("=" * 70)
