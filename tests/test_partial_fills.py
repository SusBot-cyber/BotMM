"""Tests for partial fill support across order manager, backtester, and data structures."""

import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from dataclasses import dataclass

from bot_mm.exchanges.base_mm import OrderInfo
from bot_mm.core.order_manager import OrderManager, ManagedOrder
from bot_mm.core.quoter import Quote
from bot_mm.core.inventory import InventoryManager


class TestOrderInfoPartialFields(unittest.TestCase):
    """Test OrderInfo data structure with partial fill fields."""

    def test_default_remaining_equals_size(self):
        oi = OrderInfo(oid="1", symbol="BTCUSDT", side="buy", price=100.0,
                       size=1.0, status="open")
        self.assertEqual(oi.remaining_qty, 1.0)
        self.assertEqual(oi.filled_qty, 0.0)

    def test_partially_filled_status(self):
        oi = OrderInfo(oid="1", symbol="BTCUSDT", side="buy", price=100.0,
                       size=1.0, status="partially_filled",
                       filled_qty=0.3, remaining_qty=0.7)
        self.assertEqual(oi.filled_qty, 0.3)
        self.assertEqual(oi.remaining_qty, 0.7)

    def test_fully_filled(self):
        oi = OrderInfo(oid="1", symbol="BTCUSDT", side="sell", price=50.0,
                       size=2.0, status="filled",
                       filled_qty=2.0, remaining_qty=0.0)
        self.assertEqual(oi.filled_qty, 2.0)
        self.assertEqual(oi.remaining_qty, 0.0)


class TestManagedOrderPartial(unittest.TestCase):
    """Test ManagedOrder partial fill tracking."""

    def _make_quote(self, side="buy", price=100.0, size=1.0):
        return Quote(side=side, price=price, size=size, level=0)

    def test_initial_remaining(self):
        q = self._make_quote(size=1.5)
        mo = ManagedOrder(oid="1", symbol="BTC", side="buy",
                          price=100.0, size=1.5, quote=q)
        self.assertEqual(mo.remaining_qty, 1.5)
        self.assertFalse(mo.is_fully_filled)

    def test_partial_fill_updates(self):
        q = self._make_quote(size=1.0)
        mo = ManagedOrder(oid="1", symbol="BTC", side="buy",
                          price=100.0, size=1.0, quote=q)
        mo.filled_qty = 0.4
        self.assertAlmostEqual(mo.remaining_qty, 0.6)
        self.assertFalse(mo.is_fully_filled)

    def test_full_fill(self):
        q = self._make_quote(size=1.0)
        mo = ManagedOrder(oid="1", symbol="BTC", side="buy",
                          price=100.0, size=1.0, quote=q)
        mo.filled_qty = 1.0
        self.assertAlmostEqual(mo.remaining_qty, 0.0)
        self.assertTrue(mo.is_fully_filled)


class TestOrderManagerPartialFills(unittest.TestCase):
    """Test OrderManager.on_fill_event with partial fills."""

    def _make_om(self):
        exchange = MagicMock()
        fills = []
        om = OrderManager(exchange, "BTCUSDT",
                          on_fill=lambda oid, side, price, size, fee: fills.append(
                              (oid, side, price, size, fee)))
        return om, fills

    def _add_order(self, om, oid="1", side="buy", price=100.0, size=1.0):
        q = Quote(side=side, price=price, size=size, level=0)
        om.active_orders[oid] = ManagedOrder(
            oid=oid, symbol="BTCUSDT", side=side,
            price=price, size=size, quote=q)

    def test_partial_fill_keeps_order_active(self):
        om, fills = self._make_om()
        self._add_order(om, oid="1", size=1.0)

        om.on_fill_event("1", "buy", 100.0, 0.3, -0.0045)

        self.assertIn("1", om.active_orders)
        self.assertAlmostEqual(om.active_orders["1"].filled_qty, 0.3)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0][3], 0.3)  # size = 0.3

    def test_full_fill_removes_order(self):
        om, fills = self._make_om()
        self._add_order(om, oid="1", size=1.0)

        om.on_fill_event("1", "buy", 100.0, 1.0, -0.015)

        self.assertNotIn("1", om.active_orders)
        self.assertEqual(len(fills), 1)

    def test_multiple_partial_fills_then_full(self):
        om, fills = self._make_om()
        self._add_order(om, oid="1", size=1.0)

        om.on_fill_event("1", "buy", 100.0, 0.3, -0.0045)
        self.assertIn("1", om.active_orders)
        self.assertAlmostEqual(om.active_orders["1"].filled_qty, 0.3)

        om.on_fill_event("1", "buy", 100.0, 0.5, -0.0075)
        self.assertIn("1", om.active_orders)
        self.assertAlmostEqual(om.active_orders["1"].filled_qty, 0.8)

        om.on_fill_event("1", "buy", 100.0, 0.2, -0.003)
        self.assertNotIn("1", om.active_orders)

        self.assertEqual(len(fills), 3)
        self.assertEqual(om.total_fills, 3)

    def test_fill_for_unknown_order(self):
        om, fills = self._make_om()
        om.on_fill_event("unknown", "sell", 100.0, 0.5, 0.0)
        self.assertEqual(len(fills), 1)
        self.assertEqual(om.total_fills, 1)


class TestCheckPartialFills(unittest.TestCase):
    """Test OrderManager.check_partial_fills against exchange state."""

    def _make_om(self):
        exchange = AsyncMock()
        fills = []
        om = OrderManager(exchange, "BTCUSDT",
                          on_fill=lambda oid, side, price, size, fee: fills.append(
                              (oid, side, price, size, fee)))
        return om, fills

    def _add_order(self, om, oid="1", side="buy", price=100.0, size=1.0):
        q = Quote(side=side, price=price, size=size, level=0)
        om.active_orders[oid] = ManagedOrder(
            oid=oid, symbol="BTCUSDT", side=side,
            price=price, size=size, quote=q)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_order_disappeared_full_fill(self):
        om, fills = self._make_om()
        self._add_order(om, oid="1", side="buy", price=50000.0, size=0.01)

        # Exchange returns no open orders → order was fully filled
        om.exchange.get_open_orders.return_value = []

        detected = self._run(om.check_partial_fills(50000.0, maker_fee=-0.00015))

        self.assertEqual(len(detected), 1)
        self.assertEqual(detected[0][0], "buy")
        self.assertAlmostEqual(detected[0][2], 0.01)
        self.assertNotIn("1", om.active_orders)

    def test_order_partial_fill_detected(self):
        om, fills = self._make_om()
        self._add_order(om, oid="1", side="sell", price=50000.0, size=0.1)

        # Exchange shows order with 0.04 filled
        om.exchange.get_open_orders.return_value = [
            OrderInfo(oid="1", symbol="BTCUSDT", side="sell",
                      price=50000.0, size=0.1, status="partially_filled",
                      filled_qty=0.04, remaining_qty=0.06)
        ]

        detected = self._run(om.check_partial_fills(50000.0, maker_fee=-0.00015))

        self.assertEqual(len(detected), 1)
        self.assertAlmostEqual(detected[0][2], 0.04)
        # Order should still be tracked
        self.assertIn("1", om.active_orders)
        self.assertAlmostEqual(om.active_orders["1"].filled_qty, 0.04)

    def test_no_fills_no_change(self):
        om, fills = self._make_om()
        self._add_order(om, oid="1", side="buy", price=50000.0, size=0.01)

        # Exchange shows order unchanged
        om.exchange.get_open_orders.return_value = [
            OrderInfo(oid="1", symbol="BTCUSDT", side="buy",
                      price=50000.0, size=0.01, status="open",
                      filled_qty=0.0, remaining_qty=0.01)
        ]

        detected = self._run(om.check_partial_fills(50000.0, maker_fee=-0.00015))

        self.assertEqual(len(detected), 0)
        self.assertIn("1", om.active_orders)

    def test_multiple_orders_mixed_fills(self):
        om, fills = self._make_om()
        self._add_order(om, oid="1", side="buy", price=50000.0, size=0.02)
        self._add_order(om, oid="2", side="sell", price=51000.0, size=0.02)

        # Order 1 fully filled (gone), order 2 partial
        om.exchange.get_open_orders.return_value = [
            OrderInfo(oid="2", symbol="BTCUSDT", side="sell",
                      price=51000.0, size=0.02, status="partially_filled",
                      filled_qty=0.01, remaining_qty=0.01)
        ]

        detected = self._run(om.check_partial_fills(50500.0, maker_fee=-0.00015))

        self.assertEqual(len(detected), 2)
        # Order 1 should be removed, order 2 still tracked
        self.assertNotIn("1", om.active_orders)
        self.assertIn("2", om.active_orders)

    def test_empty_active_orders(self):
        om, fills = self._make_om()
        detected = self._run(om.check_partial_fills(50000.0))
        self.assertEqual(len(detected), 0)

    def test_exchange_error_handled(self):
        om, fills = self._make_om()
        self._add_order(om, oid="1")
        om.exchange.get_open_orders.side_effect = Exception("Connection error")

        detected = self._run(om.check_partial_fills(50000.0))
        self.assertEqual(len(detected), 0)
        self.assertIn("1", om.active_orders)


class TestInventoryPartialFills(unittest.TestCase):
    """Test that InventoryManager handles partial fill sizes correctly."""

    def test_partial_fill_opens_partial_position(self):
        inv = InventoryManager("BTC", max_position_usd=5000.0)
        inv.on_fill("buy", 50000.0, 0.003, fee=-0.00225)  # partial: 0.003 BTC
        self.assertAlmostEqual(inv.state.position_size, 0.003)
        self.assertAlmostEqual(inv.state.avg_entry_price, 50000.0)

    def test_multiple_partial_fills_accumulate(self):
        inv = InventoryManager("BTC", max_position_usd=5000.0)
        inv.on_fill("buy", 50000.0, 0.003, fee=0.0)
        inv.on_fill("buy", 50100.0, 0.002, fee=0.0)
        self.assertAlmostEqual(inv.state.position_size, 0.005)
        expected_avg = (50000.0 * 0.003 + 50100.0 * 0.002) / 0.005
        self.assertAlmostEqual(inv.state.avg_entry_price, expected_avg, places=2)

    def test_partial_close_realizes_proportional_pnl(self):
        inv = InventoryManager("BTC", max_position_usd=5000.0)
        inv.on_fill("buy", 50000.0, 0.01, fee=0.0)
        rpnl = inv.on_fill("sell", 50100.0, 0.003, fee=0.0)  # close 30%
        self.assertAlmostEqual(rpnl, 100.0 * 0.003)  # $0.30
        self.assertAlmostEqual(inv.state.position_size, 0.007)


class TestBacktesterPartialFills(unittest.TestCase):
    """Test backtester _simulate_fill with partial fill logic."""

    def test_partial_fill_size_less_than_quote(self):
        """Simulate fill should return size <= quote.size."""
        import numpy as np
        from backtest.mm_backtester import MMBacktester, Candle
        from bot_mm.core.quoter import QuoteParams

        bt = MMBacktester(
            quote_params=QuoteParams(
                base_spread_bps=2.0, vol_multiplier=1.5,
                inventory_skew_factor=0.3, order_size_usd=150,
                num_levels=2,
            ),
            max_position_usd=500,
        )

        np.random.seed(42)

        quote = Quote(side="buy", price=99.0, size=1.5, level=0)
        candle = Candle(
            timestamp="2024-01-01", open=100.0, high=101.0,
            low=98.0, close=99.5, volume=1000.0
        )

        # Run many times and check sizes
        sizes = []
        for _ in range(1000):
            result = bt._simulate_fill(quote, candle, 100.0, 0.01)
            if result is not None:
                _, fill_size, _ = result
                sizes.append(fill_size)
                self.assertLessEqual(fill_size, quote.size * 1.2 + 0.01)
                self.assertGreater(fill_size, 0)

        # Should have fills
        self.assertGreater(len(sizes), 0)
        avg_ratio = np.mean([s / quote.size for s in sizes])
        # With deep penetration (price 99 vs low 98 in range 98-101),
        # penetration = 33% → mostly full fills, avg ratio should be high
        self.assertGreater(avg_ratio, 0.5)
        self.assertLessEqual(avg_ratio, 1.0)


if __name__ == "__main__":
    unittest.main()
