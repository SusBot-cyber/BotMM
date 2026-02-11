"""
Inventory Manager — tracks MM position, PnL, and fill statistics.
"""

from dataclasses import dataclass, field
from typing import Optional
import time


@dataclass
class Fill:
    """A single fill event."""
    timestamp: float
    side: str        # "buy" or "sell"
    price: float
    size: float
    fee: float       # Negative = rebate
    is_maker: bool = True


@dataclass
class InventoryState:
    """Current inventory state for one asset."""
    symbol: str
    position_size: float = 0.0      # Net position (+ = long, - = short)
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_fees: float = 0.0
    num_buys: int = 0
    num_sells: int = 0
    volume_traded_usd: float = 0.0
    round_trips: int = 0
    daily_high_inv: float = 0.0
    daily_low_inv: float = 0.0


class InventoryManager:
    """Tracks inventory across fills and manages position limits."""

    def __init__(self, symbol: str, max_position_usd: float = 500.0):
        self.state = InventoryState(symbol=symbol)
        self.max_position_usd = max_position_usd
        self.fills: list = []

    @property
    def position_usd(self) -> float:
        """Current position in USD terms."""
        return abs(self.state.position_size * self.state.avg_entry_price) if self.state.avg_entry_price else 0

    @property
    def inventory_ratio(self) -> float:
        """Position as fraction of max (-1 to +1)."""
        if self.max_position_usd == 0:
            return 0
        return self.state.position_size * self.state.avg_entry_price / self.max_position_usd if self.state.avg_entry_price else 0

    def on_fill(self, side: str, price: float, size: float, fee: float = 0.0) -> float:
        """
        Process a fill. Returns realized PnL from this fill (0 if opening).

        Args:
            side: "buy" or "sell"
            price: Fill price
            size: Fill size (always positive)
            fee: Fee paid (negative = rebate)

        Returns:
            Realized PnL from this fill
        """
        realized = 0.0
        signed_size = size if side == "buy" else -size

        old_pos = self.state.position_size

        if old_pos == 0 or (old_pos > 0 and side == "buy") or (old_pos < 0 and side == "sell"):
            # Opening or adding to position
            total_cost = self.state.avg_entry_price * abs(old_pos) + price * size
            self.state.position_size += signed_size
            if abs(self.state.position_size) > 0:
                self.state.avg_entry_price = total_cost / abs(self.state.position_size)
        else:
            # Reducing position
            close_size = min(size, abs(old_pos))
            if old_pos > 0:
                realized = (price - self.state.avg_entry_price) * close_size
            else:
                realized = (self.state.avg_entry_price - price) * close_size

            self.state.position_size += signed_size

            # If position flipped, set new avg entry
            remaining = size - close_size
            if remaining > 0:
                self.state.avg_entry_price = price
            elif abs(self.state.position_size) < 1e-10:
                self.state.position_size = 0.0
                self.state.avg_entry_price = 0.0

            if close_size > 0:
                self.state.round_trips += 1

        self.state.realized_pnl += realized
        self.state.total_fees += fee
        self.state.volume_traded_usd += price * size

        if side == "buy":
            self.state.num_buys += 1
        else:
            self.state.num_sells += 1

        # Track daily extremes
        pos_usd = self.state.position_size * price
        self.state.daily_high_inv = max(self.state.daily_high_inv, pos_usd)
        self.state.daily_low_inv = min(self.state.daily_low_inv, pos_usd)

        self.fills.append(Fill(
            timestamp=time.time(),
            side=side, price=price, size=size, fee=fee,
        ))

        return realized

    def update_unrealized(self, current_price: float):
        """Update unrealized PnL based on current price."""
        if self.state.position_size == 0:
            self.state.unrealized_pnl = 0.0
            return

        if self.state.position_size > 0:
            self.state.unrealized_pnl = (current_price - self.state.avg_entry_price) * self.state.position_size
        else:
            self.state.unrealized_pnl = (self.state.avg_entry_price - current_price) * abs(self.state.position_size)

    @property
    def total_pnl(self) -> float:
        """Total PnL including unrealized and fees.
        Fees convention: positive = cost, negative = rebate.
        We subtract fees so costs reduce PnL and rebates increase it.
        """
        return self.state.realized_pnl + self.state.unrealized_pnl - self.state.total_fees

    @property
    def net_pnl(self) -> float:
        """Realized PnL - fees (no unrealized)."""
        return self.state.realized_pnl - self.state.total_fees

    def should_pause_side(self, side: str, current_price: float = 0.0) -> bool:
        """Check if we should pause quoting on a side due to inventory."""
        price = current_price if current_price > 0 else self.state.avg_entry_price
        pos_usd = self.state.position_size * price if price else 0
        threshold = self.max_position_usd * 0.8

        if side == "buy" and pos_usd > threshold:
            return True  # Too long, don't buy more
        if side == "sell" and pos_usd < -threshold:
            return True  # Too short, don't sell more
        return False

    def should_hedge(self, current_price: float = 0.0) -> bool:
        """Check if inventory needs hedging (>90% of max)."""
        price = current_price if current_price > 0 else self.state.avg_entry_price
        pos_usd = abs(self.state.position_size * price) if price else 0
        return pos_usd > self.max_position_usd * 0.9

    def reset_daily(self):
        """Reset daily counters."""
        self.state.daily_high_inv = 0.0
        self.state.daily_low_inv = 0.0
