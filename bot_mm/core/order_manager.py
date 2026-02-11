"""
Order Manager — order lifecycle, deduplication, and fill tracking.

Compares new quotes with existing orders and only modifies when
price has changed beyond a threshold (avoids wasting API rate limit).
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from bot_mm.core.quoter import Quote
from bot_mm.exchanges.base_mm import BaseMMExchange, OrderInfo

logger = logging.getLogger(__name__)

# Don't modify orders if price changed less than this (basis points)
MIN_MODIFY_THRESHOLD_BPS = 0.5


@dataclass
class ManagedOrder:
    """An order tracked by the order manager."""
    oid: str
    symbol: str
    side: str
    price: float
    size: float
    quote: Quote
    placed_at: float = 0.0
    modify_count: int = 0
    filled_qty: float = 0.0

    @property
    def remaining_qty(self) -> float:
        return self.size - self.filled_qty

    @property
    def is_fully_filled(self) -> bool:
        return self.filled_qty >= self.size - 1e-12


class OrderManager:
    """
    Manages order lifecycle: placement, modification, cancellation.

    Tracks active orders and avoids unnecessary modifications
    when price changes are below threshold.
    """

    def __init__(
        self,
        exchange: BaseMMExchange,
        symbol: str,
        on_fill: Optional[Callable] = None,
    ):
        self.exchange = exchange
        self.symbol = symbol
        self.on_fill = on_fill

        self.active_orders: Dict[str, ManagedOrder] = {}

        # Stats
        self.total_placed = 0
        self.total_modified = 0
        self.total_cancelled = 0
        self.total_fills = 0

    async def update_quotes(self, new_quotes: List[Quote]):
        """
        Update orders to match new quotes.

        Strategy: cancel-and-replace. For each (side, level) slot:
        - If no change needed (price within threshold): skip
        - If order needs update or is new: cancel old (if any), place new
        - If slot removed from desired: cancel
        """
        now = time.time()

        # Build map: (side, level) -> quote
        desired: Dict[Tuple[str, int], Quote] = {}
        for q in new_quotes:
            key = (q.side, q.level)
            desired[key] = q

        # Build map: (side, level) -> managed order
        existing: Dict[Tuple[str, int], ManagedOrder] = {}
        for oid, mo in self.active_orders.items():
            key = (mo.quote.side, mo.quote.level)
            existing[key] = mo

        to_cancel_oids: List[str] = []
        to_place: List[Quote] = []

        # Match desired quotes to existing orders
        for key, quote in desired.items():
            if key in existing:
                mo = existing[key]
                if self._should_modify(mo, quote):
                    to_cancel_oids.append(mo.oid)
                    to_place.append(quote)
                    self.total_modified += 1
                # else: order is close enough, leave it
            else:
                to_place.append(quote)

        # Cancel orders that have no matching desired quote
        for key, mo in existing.items():
            if key not in desired:
                to_cancel_oids.append(mo.oid)

        # Execute cancellations
        for oid in to_cancel_oids:
            try:
                await self.exchange.cancel_order(self.symbol, oid)
                self.active_orders.pop(oid, None)
                self.total_cancelled += 1
            except Exception as e:
                logger.warning("Cancel failed oid=%s: %s", oid, e)
                self.active_orders.pop(oid, None)

        # Batch place new orders if exchange supports it
        if to_place:
            batch = [
                {"symbol": self.symbol, "side": q.side, "price": q.price,
                 "size": q.size, "post_only": True}
                for q in to_place
            ]
            try:
                oids = await self.exchange.batch_modify_orders(batch)
                for oid, quote in zip(oids, to_place):
                    if oid:
                        self.active_orders[oid] = ManagedOrder(
                            oid=oid, symbol=self.symbol, side=quote.side,
                            price=quote.price, size=quote.size,
                            quote=quote, placed_at=now,
                        )
                        self.total_placed += 1
            except Exception as e:
                logger.warning("Batch place failed, falling back to individual: %s", e)
                for quote in to_place:
                    await self._place_single(quote, now)

    async def _place_single(self, quote: Quote, now: float):
        """Place a single order via place_limit_order."""
        try:
            oid = await self.exchange.place_limit_order(
                self.symbol, quote.side, quote.price, quote.size, post_only=True
            )
            if oid:
                self.active_orders[oid] = ManagedOrder(
                    oid=oid, symbol=self.symbol, side=quote.side,
                    price=quote.price, size=quote.size,
                    quote=quote, placed_at=now,
                )
                self.total_placed += 1
        except Exception as e:
            logger.warning("Place failed %s@%.2f: %s", quote.side, quote.price, e)

    def _should_modify(self, managed: ManagedOrder, new_quote: Quote) -> bool:
        """Check if price change exceeds minimum threshold."""
        old_price = managed.price
        if old_price == 0:
            return True
        diff_bps = abs(new_quote.price - old_price) / old_price * 10000
        size_changed = abs(new_quote.size - managed.size) / max(managed.size, 1e-12) > 0.05
        return diff_bps > MIN_MODIFY_THRESHOLD_BPS or size_changed

    def on_fill_event(self, oid: str, side: str, price: float, size: float, fee: float = 0.0):
        """
        Process a fill event (full or partial).

        For partial fills, updates filled_qty and keeps the order active.
        For full fills, removes the order from tracking.
        """
        if oid in self.active_orders:
            mo = self.active_orders[oid]
            mo.filled_qty += size
            if mo.is_fully_filled:
                self.active_orders.pop(oid, None)

        self.total_fills += 1
        if self.on_fill:
            self.on_fill(oid, side, price, size, fee)

    async def cancel_all(self):
        """Cancel all active orders."""
        if not self.active_orders:
            return 0

        try:
            count = await self.exchange.cancel_all_orders(self.symbol)
            logger.info("Cancelled %d orders for %s", count, self.symbol)
        except Exception as e:
            logger.error("Batch cancel failed: %s — cancelling individually", e)
            for oid in list(self.active_orders.keys()):
                try:
                    await self.exchange.cancel_order(self.symbol, oid)
                except Exception:
                    pass

        n = len(self.active_orders)
        self.active_orders.clear()
        return n

    async def check_partial_fills(self, current_price: float, maker_fee: float = -0.00015):
        """
        Check exchange for partial fills on active orders.

        Queries open orders from exchange and compares with tracked state.
        Orders that disappeared were fully filled; orders with reduced size
        had partial fills.
        """
        if not self.active_orders:
            return []

        fills_detected = []
        try:
            exchange_orders = await self.exchange.get_open_orders(self.symbol)
        except Exception as e:
            logger.warning("check_partial_fills: get_open_orders failed: %s", e)
            return fills_detected

        exchange_oids = {o.oid: o for o in exchange_orders}

        for oid in list(self.active_orders.keys()):
            mo = self.active_orders[oid]

            if oid not in exchange_oids:
                # Order gone from exchange → fully filled (remaining qty)
                fill_size = mo.remaining_qty
                if fill_size > 1e-12:
                    fee = mo.price * fill_size * maker_fee
                    self.on_fill_event(oid, mo.side, mo.price, fill_size, fee)
                    fills_detected.append((mo.side, mo.price, fill_size))
                else:
                    # Already fully tracked, just clean up
                    self.active_orders.pop(oid, None)
            else:
                # Order still open — check if partially filled
                exch_order = exchange_oids[oid]
                if exch_order.filled_qty > mo.filled_qty + 1e-12:
                    partial_size = exch_order.filled_qty - mo.filled_qty
                    fee = mo.price * partial_size * maker_fee
                    self.on_fill_event(oid, mo.side, mo.price, partial_size, fee)
                    fills_detected.append((mo.side, mo.price, partial_size))

        return fills_detected

    @property
    def num_active(self) -> int:
        return len(self.active_orders)

    @property
    def stats_str(self) -> str:
        return (
            f"Orders: {self.num_active} active | "
            f"placed={self.total_placed} mod={self.total_modified} "
            f"canc={self.total_cancelled} fills={self.total_fills}"
        )
