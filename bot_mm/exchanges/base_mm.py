"""
Abstract exchange interface for market making adapters.

Defines the contract that all exchange implementations (Hyperliquid, Binance, Bybit)
must follow: market data, order management, position queries, and safety features.
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
import time


@dataclass
class OrderInfo:
    oid: str
    symbol: str
    side: str  # "buy" or "sell"
    price: float
    size: float
    status: str  # "open", "partially_filled", "filled", "cancelled"
    filled_qty: float = 0.0
    remaining_qty: float = 0.0
    is_post_only: bool = True
    created_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if self.remaining_qty == 0.0 and self.filled_qty == 0.0:
            self.remaining_qty = self.size


class BaseMMExchange(ABC):
    """Abstract base class for market-making exchange adapters."""

    @abstractmethod
    async def connect(self):
        """Initialize connection to exchange."""
        ...

    @abstractmethod
    async def disconnect(self):
        """Clean up connection resources."""
        ...

    @abstractmethod
    async def get_mid_price(self, symbol: str) -> float:
        """Return mid price = (best_bid + best_ask) / 2."""
        ...

    @abstractmethod
    async def get_orderbook(self, symbol: str, depth: int = 5) -> dict:
        """Return orderbook: {"bids": [[price, size], ...], "asks": [[price, size], ...]}."""
        ...

    @abstractmethod
    async def place_limit_order(
        self, symbol: str, side: str, price: float, size: float, post_only: bool = True
    ) -> str:
        """Place a limit order. Returns order ID."""
        ...

    @abstractmethod
    async def cancel_order(self, symbol: str, oid: str) -> bool:
        """Cancel a single order by ID. Returns True on success."""
        ...

    @abstractmethod
    async def cancel_all_orders(self, symbol: str) -> int:
        """Cancel all open orders for symbol. Returns count cancelled."""
        ...

    @abstractmethod
    async def batch_modify_orders(self, orders: List[dict]) -> List[str]:
        """Place/modify multiple orders atomically. Returns list of order IDs."""
        ...

    @abstractmethod
    async def get_position(self, symbol: str) -> dict:
        """Return position info: {"size": float, "side": str, "entry_price": float, "unrealized_pnl": float}."""
        ...

    @abstractmethod
    async def get_open_orders(self, symbol: str) -> List[OrderInfo]:
        """Return list of open orders for symbol with fill status."""
        ...

    @abstractmethod
    async def get_balance(self) -> float:
        """Return available account balance in USDC."""
        ...

    @abstractmethod
    async def set_dead_mans_switch(self, timeout_ms: int) -> bool:
        """Activate dead man's switch — cancel all orders if no heartbeat within timeout_ms."""
        ...
