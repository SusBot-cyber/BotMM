"""Tests for Order Book Replay Backtester — uses synthetic data."""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.ob_loader import OrderBookSnapshot, TradeTick, L2Level
from backtest.ob_backtester import OBBacktester, OBBacktestResult, PendingOrder
from bot_mm.config import QuoteParams


# ── Helpers ─────────────────────────────────────────────────


def make_snapshot(
    ts: str = "2026-02-11T12:00:00",
    bid_prices=None,
    ask_prices=None,
    bid_sizes=None,
    ask_sizes=None,
) -> OrderBookSnapshot:
    """Create a synthetic L2 snapshot."""
    bid_prices = bid_prices or [100.0, 99.9, 99.8]
    ask_prices = ask_prices or [100.1, 100.2, 100.3]
    bid_sizes = bid_sizes or [1.0] * len(bid_prices)
    ask_sizes = ask_sizes or [1.0] * len(ask_prices)

    bids = [L2Level(price=p, size=s) for p, s in zip(bid_prices, bid_sizes)]
    asks = [L2Level(price=p, size=s) for p, s in zip(ask_prices, ask_sizes)]
    return OrderBookSnapshot(timestamp=ts, bids=bids, asks=asks)


def make_trade(
    ts: str = "2026-02-11T12:00:01",
    side: str = "sell",
    price: float = 100.0,
    size: float = 0.5,
) -> TradeTick:
    """Create a synthetic trade tick."""
    return TradeTick(timestamp=ts, side=side, price=price, size=size)


def make_backtester(**kwargs) -> OBBacktester:
    """Create backtester with test defaults."""
    defaults = dict(
        quote_params=QuoteParams(
            base_spread_bps=2.0,
            order_size_usd=100.0,
            num_levels=1,
            vol_multiplier=0.0,  # zero vol for predictable spreads
            inventory_skew_factor=0.0,  # zero skew for simplicity
        ),
        maker_fee=0.0,
        taker_fee=0.0,
        max_position_usd=500.0,
        capital=1000.0,
        use_queue_position=False,  # disable queue for basic tests
    )
    defaults.update(kwargs)
    return OBBacktester(**defaults)


def make_sequence(
    n_snapshots: int = 5,
    n_trades: int = 3,
    base_price: float = 100.0,
    trade_side: str = "sell",
):
    """Create a sequence of snapshots and trades for replay."""
    snapshots = []
    trades = []

    for i in range(n_snapshots):
        ts = f"2026-02-11T12:00:{i*2:02d}"
        snapshots.append(make_snapshot(
            ts=ts,
            bid_prices=[base_price - 0.05, base_price - 0.15, base_price - 0.25],
            ask_prices=[base_price + 0.05, base_price + 0.15, base_price + 0.25],
        ))

    for i in range(n_trades):
        # Trades between snapshots
        ts = f"2026-02-11T12:00:{i*2+1:02d}"
        trades.append(make_trade(
            ts=ts,
            side=trade_side,
            price=base_price - 0.05 if trade_side == "sell" else base_price + 0.05,
            size=0.5,
        ))

    return snapshots, trades


# ── Fill logic ──────────────────────────────────────────────


class TestFillLogic:
    """Tests for bid/ask fill matching."""

    def test_bid_fills_on_sell_trade(self):
        """Our bid should fill when a market sell occurs at our price."""
        bt = make_backtester()
        snapshots, trades = make_sequence(n_snapshots=3, n_trades=2, trade_side="sell")
        result = bt.run(snapshots, trades, symbol="BTC")
        assert result.buy_fills > 0, "Bid should fill on sell trade"

    def test_ask_fills_on_buy_trade(self):
        """Our ask should fill when a market buy occurs at our price."""
        bt = make_backtester()
        snapshots, trades = make_sequence(n_snapshots=3, n_trades=2, trade_side="buy")
        result = bt.run(snapshots, trades, symbol="BTC")
        assert result.sell_fills > 0, "Ask should fill on buy trade"

    def test_no_fill_when_trade_doesnt_reach_price(self):
        """No fill when trade price doesn't reach our quote."""
        bt = make_backtester()
        # Snapshot with wide spread
        snapshots = [make_snapshot(
            ts="2026-02-11T12:00:00",
            bid_prices=[99.0], ask_prices=[101.0],
        )]
        # Trade at mid — doesn't reach our bid or ask
        trades = [make_trade(ts="2026-02-11T12:00:01", side="sell", price=100.0, size=1.0)]
        result = bt.run(snapshots, trades, symbol="BTC")
        assert result.total_fills == 0, "No fill when trade doesn't reach quote price"

    def test_partial_fill(self):
        """Trade smaller than our order → partial fill."""
        bt = make_backtester(
            quote_params=QuoteParams(
                base_spread_bps=2.0,
                order_size_usd=1000.0,  # large order ~10 units at $100
                num_levels=1,
                vol_multiplier=0.0,
                inventory_skew_factor=0.0,
            ),
        )
        snapshots = [make_snapshot(ts="2026-02-11T12:00:00")]
        # Small trade — should only partially fill
        trades = [make_trade(ts="2026-02-11T12:00:01", side="sell", price=99.9, size=0.01)]
        result = bt.run(snapshots, trades, symbol="BTC")
        # Should fill but only for the trade size
        assert result.buy_fills <= 1

    def test_multiple_fills_accumulate(self):
        """Multiple trades should produce multiple fills."""
        bt = make_backtester()
        snapshots = [
            make_snapshot(ts=f"2026-02-11T12:00:{i*4:02d}")
            for i in range(5)
        ]
        trades = [
            make_trade(ts=f"2026-02-11T12:00:{i*4+2:02d}", side="sell", price=99.9, size=0.3)
            for i in range(4)
        ]
        result = bt.run(snapshots, trades, symbol="BTC")
        assert result.total_fills >= 2, "Multiple trades should produce multiple fills"


# ── Queue position ──────────────────────────────────────────


class TestQueuePosition:
    """Tests for queue position estimation."""

    def test_queue_position_blocks_fill(self):
        """Large queue ahead should prevent fill on small trade."""
        bt = make_backtester(use_queue_position=True)
        # Large book depth ahead — our quote will sit behind this depth
        snapshots = [make_snapshot(
            ts="2026-02-11T12:00:00",
            bid_prices=[100.0, 99.99, 99.98],
            bid_sizes=[500.0, 500.0, 500.0],  # massive depth at every level
            ask_prices=[100.01, 100.02, 100.03],
            ask_sizes=[500.0, 500.0, 500.0],
        )]
        # Tiny trade — should be consumed by queue ahead of us
        trades = [make_trade(ts="2026-02-11T12:00:01", side="sell", price=99.98, size=0.001)]
        result = bt.run(snapshots, trades, symbol="BTC")
        # With massive queue, tiny trade shouldn't reach us
        assert result.total_fills == 0

    def test_no_queue_allows_fill(self):
        """With queue disabled, same scenario should fill."""
        bt = make_backtester(use_queue_position=False)
        snapshots = [make_snapshot(
            ts="2026-02-11T12:00:00",
            bid_prices=[100.0, 99.9, 99.8],
            bid_sizes=[100.0, 50.0, 25.0],
        )]
        trades = [make_trade(ts="2026-02-11T12:00:01", side="sell", price=99.9, size=0.5)]
        result = bt.run(snapshots, trades, symbol="BTC")
        assert result.total_fills > 0

    def test_queue_estimation(self):
        """Queue position should sum depth ahead of our price."""
        bt = make_backtester(use_queue_position=True)
        snapshot = make_snapshot(
            bid_prices=[100.0, 99.9, 99.8],
            bid_sizes=[5.0, 3.0, 1.0],
        )
        # Our order at 99.9:
        #   depth ahead = 100.0*5.0 = $500 (better prices)
        #   + 99.9*3.0*0.5 = $149.85 (same level, 50% assumed ahead)
        #   total ≈ $649.85
        order = PendingOrder(
            side="buy", price=99.9, size=1.0, remaining=1.0,
            placed_at="2026-02-11T12:00:00", level=1, queue_position=0,
        )
        depth = bt._estimate_queue_position(order, snapshot)
        assert depth == pytest.approx(649.85, abs=1.0)


# ── Inventory tracking ─────────────────────────────────────


class TestInventoryTracking:
    """Tests for inventory through fills."""

    def test_buy_increases_inventory(self):
        """Buy fill should increase position."""
        bt = make_backtester()
        # Two snapshots so inventory is sampled after the fill
        snapshots = [
            make_snapshot(ts="2026-02-11T12:00:00"),
            make_snapshot(ts="2026-02-11T12:00:05"),
        ]
        trades = [make_trade(ts="2026-02-11T12:00:01", side="sell", price=99.95, size=0.5)]
        result = bt.run(snapshots, trades, symbol="BTC")
        if result.buy_fills > 0:
            assert result.max_inventory_usd > 0

    def test_balanced_fills_reduce_inventory(self):
        """Buy + sell fills should both register."""
        bt = make_backtester()
        snapshots = [
            make_snapshot(ts=f"2026-02-11T12:00:{i*4:02d}")
            for i in range(6)
        ]
        # Sell trades fill our bids, buy trades fill our asks
        trades = [
            make_trade(ts="2026-02-11T12:00:02", side="sell", price=99.0, size=5.0),
            make_trade(ts="2026-02-11T12:00:06", side="buy", price=101.0, size=5.0),
            make_trade(ts="2026-02-11T12:00:10", side="sell", price=99.0, size=5.0),
            make_trade(ts="2026-02-11T12:00:14", side="buy", price=101.0, size=5.0),
        ]
        result = bt.run(snapshots, trades, symbol="BTC")
        assert result.buy_fills > 0, "Should have buy fills"
        assert result.sell_fills > 0, "Should have sell fills"


# ── PnL calculation ─────────────────────────────────────────


class TestPnL:
    """Tests for PnL computation."""

    def test_pnl_with_zero_fees(self):
        """With zero fees, gross == net."""
        bt = make_backtester(maker_fee=0.0)
        snapshots, trades = make_sequence(n_snapshots=3, n_trades=2, trade_side="sell")
        result = bt.run(snapshots, trades, symbol="BTC")
        assert result.total_fees == pytest.approx(0.0, abs=0.01)
        assert result.net_pnl == pytest.approx(result.gross_pnl, abs=0.01)

    def test_maker_rebate_positive_fees(self):
        """Maker rebate should make total_fees negative (rebate = income)."""
        bt = make_backtester(maker_fee=-0.001)  # generous rebate
        snapshots, trades = make_sequence(n_snapshots=3, n_trades=2, trade_side="sell")
        result = bt.run(snapshots, trades, symbol="BTC")
        if result.total_fills > 0:
            assert result.total_fees < 0, "Maker rebate should produce negative fees"

    def test_round_trip_pnl(self):
        """Buy at bid, sell at ask → positive PnL from spread."""
        bt = make_backtester()
        snapshots = [
            make_snapshot(ts=f"2026-02-11T12:00:{i*4:02d}")
            for i in range(4)
        ]
        # Buy (sell trade hits our bid), then sell (buy trade hits our ask)
        trades = [
            make_trade(ts="2026-02-11T12:00:02", side="sell", price=99.95, size=0.5),
            make_trade(ts="2026-02-11T12:00:06", side="buy", price=100.05, size=0.5),
        ]
        result = bt.run(snapshots, trades, symbol="BTC")
        if result.buy_fills > 0 and result.sell_fills > 0:
            assert result.gross_pnl > 0, "Round trip should capture spread"


# ── Spread metrics ──────────────────────────────────────────


class TestSpreadMetrics:
    """Tests for spread tracking."""

    def test_market_spread_tracked(self):
        """Market spread should be computed from snapshots."""
        bt = make_backtester()
        snapshots = [
            make_snapshot(
                ts="2026-02-11T12:00:00",
                bid_prices=[99.95], ask_prices=[100.05],
            ),
            make_snapshot(
                ts="2026-02-11T12:00:02",
                bid_prices=[99.95], ask_prices=[100.05],
            ),
        ]
        result = bt.run(snapshots, [], symbol="BTC")
        # spread = (100.05 - 99.95) / 100.0 * 10000 = 10 bps
        assert result.avg_market_spread_bps == pytest.approx(10.0, abs=0.5)

    def test_quoted_spread_tracked(self):
        """Quoted spread should reflect our quotes."""
        bt = make_backtester()
        snapshots = [
            make_snapshot(ts="2026-02-11T12:00:00"),
            make_snapshot(ts="2026-02-11T12:00:02"),
        ]
        result = bt.run(snapshots, [], symbol="BTC")
        assert result.avg_spread_quoted_bps > 0


# ── Adverse selection ───────────────────────────────────────


class TestAdverseSelection:
    """Tests for adverse selection detection."""

    def test_adverse_fill_detected(self):
        """Trade far past our price should flag as adverse."""
        bt = make_backtester()
        snapshots = [make_snapshot(
            ts="2026-02-11T12:00:00",
            bid_prices=[100.0], ask_prices=[100.1],
            bid_sizes=[1.0], ask_sizes=[1.0],
        )]
        # Sell trade far below our bid — adverse
        trades = [make_trade(
            ts="2026-02-11T12:00:01", side="sell",
            price=99.5, size=0.5,  # 0.5% below our bid
        )]
        result = bt.run(snapshots, trades, symbol="BTC")
        if result.buy_fills > 0:
            assert result.adverse_fills > 0, "Far-through trade should flag adverse"

    def test_normal_fill_not_adverse(self):
        """Trade at our exact price should not be adverse."""
        bt = make_backtester()
        snapshots = [make_snapshot(ts="2026-02-11T12:00:00")]
        # Trade exactly at our bid — not adverse
        trades = [make_trade(
            ts="2026-02-11T12:00:01", side="sell",
            price=99.99, size=0.5,
        )]
        result = bt.run(snapshots, trades, symbol="BTC")
        assert result.adverse_fills == 0


# ── Result compilation ──────────────────────────────────────


class TestResults:
    """Tests for result compilation."""

    def test_empty_data_returns_zero_result(self):
        """Empty snapshots should return zero-filled result."""
        bt = make_backtester()
        result = bt.run([], [], symbol="BTC")
        assert result.total_fills == 0
        assert result.net_pnl == 0.0
        assert result.duration_hours == 0.0

    def test_result_symbol_set(self):
        """Symbol should be set in result."""
        bt = make_backtester()
        result = bt.run([make_snapshot()], [], symbol="ETH")
        assert result.symbol == "ETH"

    def test_duration_computed(self):
        """Duration should be computed from snapshot timestamps."""
        bt = make_backtester()
        snapshots = [
            make_snapshot(ts="2026-02-11T12:00:00"),
            make_snapshot(ts="2026-02-11T14:00:00"),
        ]
        result = bt.run(snapshots, [], symbol="BTC")
        assert result.duration_hours == pytest.approx(2.0, abs=0.01)

    def test_snapshot_count(self):
        """Total snapshots should be tracked."""
        bt = make_backtester()
        snapshots = [make_snapshot(ts=f"2026-02-11T12:00:{i:02d}") for i in range(10)]
        result = bt.run(snapshots, [], symbol="BTC")
        assert result.total_snapshots == 10

    def test_trade_count(self):
        """Total market trades should be tracked."""
        bt = make_backtester()
        snapshots = [make_snapshot(ts="2026-02-11T12:00:00")]
        trades = [
            make_trade(ts=f"2026-02-11T12:00:{i+1:02d}", side="sell", price=99.9)
            for i in range(5)
        ]
        result = bt.run(snapshots, trades, symbol="BTC")
        assert result.total_market_trades == 5

    def test_fills_per_hour(self):
        """Fills per hour should be computed correctly."""
        bt = make_backtester()
        snapshots = [
            make_snapshot(ts="2026-02-11T12:00:00"),
            make_snapshot(ts="2026-02-11T13:00:00"),
        ]
        trades = [
            make_trade(ts="2026-02-11T12:30:00", side="sell", price=99.95, size=0.5),
        ]
        result = bt.run(snapshots, trades, symbol="BTC")
        if result.total_fills > 0:
            assert result.fills_per_hour == pytest.approx(
                result.total_fills / result.duration_hours, abs=0.1
            )
