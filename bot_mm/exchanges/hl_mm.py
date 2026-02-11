import asyncio
import logging
import time
from typing import List, Dict, Optional

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from bot_mm.exchanges.base_mm import BaseMMExchange, OrderInfo

logger = logging.getLogger(__name__)

SYMBOL_MAP = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "SOLUSDT": "SOL",
    "HYPEUSDT": "HYPE",
}

PRICE_DECIMALS = {
    "BTC": 0,
    "ETH": 1,
    "SOL": 3,
    "HYPE": 3,
}

SIZE_DECIMALS = {
    "BTC": 5,
    "ETH": 4,
    "SOL": 2,
    "HYPE": 2,
}

# Significant figures for HL price rounding
PRICE_SIG_FIGS = 5


def _round_price(price: float, asset: str) -> float:
    """Round price to asset-specific decimals."""
    decimals = PRICE_DECIMALS.get(asset, 2)
    return round(price, decimals)


def _round_size(size: float, asset: str) -> float:
    """Round size to asset-specific decimals."""
    decimals = SIZE_DECIMALS.get(asset, 2)
    return round(size, decimals)


def _to_hl_symbol(symbol: str) -> str:
    """Convert BTCUSDT → BTC for Hyperliquid."""
    hl = SYMBOL_MAP.get(symbol)
    if hl is None:
        raise ValueError(f"Unknown symbol: {symbol}. Supported: {list(SYMBOL_MAP.keys())}")
    return hl


class HyperliquidMMExchange(BaseMMExchange):
    """Hyperliquid exchange adapter for market making."""

    def __init__(
        self,
        private_key: str,
        wallet_address: Optional[str] = None,
        testnet: bool = False,
    ):
        self._private_key = private_key
        self._wallet_address = wallet_address
        self._testnet = testnet
        self._exchange: Optional[Exchange] = None
        self._info: Optional[Info] = None
        self._meta: Optional[dict] = None

    async def connect(self):
        """Initialize Hyperliquid SDK connections."""
        try:
            base_url = "https://api.hyperliquid-testnet.xyz" if self._testnet else None
            account = eth_account.Account.from_key(self._private_key)
            address = self._wallet_address or account.address

            self._info = Info(base_url=base_url, skip_ws=True)
            self._exchange = Exchange(
                account, base_url=base_url, account_address=address
            )

            # Cache metadata for asset index lookups
            self._meta = await asyncio.to_thread(self._info.meta)
            logger.info(
                "Connected to Hyperliquid %s | wallet=%s",
                "testnet" if self._testnet else "mainnet",
                address[:10] + "...",
            )
        except Exception:
            logger.exception("Failed to connect to Hyperliquid")
            raise

    async def disconnect(self):
        """No persistent connection to close for REST SDK."""
        logger.info("Hyperliquid MM adapter disconnected")

    def _get_asset_index(self, asset: str) -> int:
        """Get numeric asset index from metadata."""
        if self._meta is None:
            raise RuntimeError("Not connected — call connect() first")
        for i, info in enumerate(self._meta["universe"]):
            if info["name"] == asset:
                return i
        raise ValueError(f"Asset {asset} not found in Hyperliquid metadata")

    # ── Market data ──────────────────────────────────────────────

    async def get_orderbook(self, symbol: str, depth: int = 5) -> dict:
        """Fetch L2 orderbook snapshot."""
        asset = _to_hl_symbol(symbol)
        try:
            snap = await asyncio.to_thread(self._info.l2_snapshot, asset)
            bids = [[float(p), float(s)] for p, s in snap["levels"][0][:depth]]
            asks = [[float(p), float(s)] for p, s in snap["levels"][1][:depth]]
            return {"bids": bids, "asks": asks}
        except Exception:
            logger.exception("get_orderbook failed for %s", symbol)
            raise

    async def get_mid_price(self, symbol: str) -> float:
        """Mid price from best bid/ask."""
        ob = await self.get_orderbook(symbol, depth=1)
        if not ob["bids"] or not ob["asks"]:
            raise ValueError(f"Empty orderbook for {symbol}")
        return (ob["bids"][0][0] + ob["asks"][0][0]) / 2.0

    # ── Order management ─────────────────────────────────────────

    async def place_limit_order(
        self,
        symbol: str,
        side: str,
        price: float,
        size: float,
        post_only: bool = True,
    ) -> str:
        """Place a single limit order. Returns order ID."""
        asset = _to_hl_symbol(symbol)
        is_buy = side.lower() == "buy"
        rounded_price = _round_price(price, asset)
        rounded_size = _round_size(size, asset)

        order_type = {"limit": {"tif": "Alo"}} if post_only else {"limit": {"tif": "Gtc"}}

        try:
            result = await asyncio.to_thread(
                self._exchange.order,
                asset,
                is_buy,
                rounded_size,
                rounded_price,
                order_type,
            )
            status = result.get("status", "")
            if status == "ok":
                oid = result["response"]["data"]["statuses"][0].get("resting", {}).get("oid", "")
                if not oid:
                    # Order filled immediately (crossed the book despite ALO)
                    filled = result["response"]["data"]["statuses"][0].get("filled", {})
                    oid = filled.get("oid", "unknown")
                    logger.warning("Order filled immediately (ALO crossed): %s %s %s @ %s", side, rounded_size, asset, rounded_price)
                else:
                    logger.info("Order placed: %s %s %s @ %s [oid=%s]", side, rounded_size, asset, rounded_price, oid)
                return str(oid)
            else:
                error_msg = result.get("response", {}).get("data", str(result))
                logger.error("Order rejected: %s | %s %s %s @ %s", error_msg, side, rounded_size, asset, rounded_price)
                raise RuntimeError(f"Order rejected: {error_msg}")
        except RuntimeError:
            raise
        except Exception:
            logger.exception("place_limit_order failed: %s %s %s @ %s", side, rounded_size, asset, rounded_price)
            raise

    async def cancel_order(self, symbol: str, oid: str) -> bool:
        """Cancel a single order."""
        asset = _to_hl_symbol(symbol)
        try:
            result = await asyncio.to_thread(
                self._exchange.cancel, asset, int(oid)
            )
            success = result.get("status", "") == "ok"
            if success:
                logger.debug("Cancelled order %s for %s", oid, asset)
            else:
                logger.warning("Cancel failed for oid=%s: %s", oid, result)
            return success
        except Exception:
            logger.exception("cancel_order failed: oid=%s, %s", oid, symbol)
            return False

    async def cancel_all_orders(self, symbol: str) -> int:
        """Cancel all open orders for a symbol."""
        asset = _to_hl_symbol(symbol)
        try:
            open_orders = await asyncio.to_thread(
                self._info.open_orders, self._exchange.account_address
            )
            to_cancel = [o for o in open_orders if o["coin"] == asset]
            if not to_cancel:
                return 0

            cancels = [
                {"coin": asset, "oid": int(o["oid"])} for o in to_cancel
            ]
            result = await asyncio.to_thread(
                self._exchange.bulk_cancel, cancels
            )
            cancelled = len(to_cancel) if result.get("status") == "ok" else 0
            logger.info("Cancelled %d orders for %s", cancelled, asset)
            return cancelled
        except Exception:
            logger.exception("cancel_all_orders failed for %s", symbol)
            return 0

    async def batch_modify_orders(self, orders: List[dict]) -> List[str]:
        """Place multiple orders atomically via bulk_orders.

        Each dict in `orders`:
            {"symbol": str, "side": str, "price": float, "size": float, "post_only": bool}
        Returns list of order IDs (one per order).
        """
        if not orders:
            return []

        hl_orders = []
        for o in orders:
            asset = _to_hl_symbol(o["symbol"])
            is_buy = o["side"].lower() == "buy"
            price = _round_price(o["price"], asset)
            size = _round_size(o["size"], asset)
            post_only = o.get("post_only", True)
            order_type = {"limit": {"tif": "Alo"}} if post_only else {"limit": {"tif": "Gtc"}}

            hl_orders.append({
                "coin": asset,
                "is_buy": is_buy,
                "sz": size,
                "limit_px": price,
                "order_type": order_type,
                "reduce_only": False,
            })

        try:
            result = await asyncio.to_thread(
                self._exchange.bulk_orders, hl_orders
            )
            if result.get("status") != "ok":
                error_msg = result.get("response", str(result))
                logger.error("batch_modify_orders failed: %s", error_msg)
                raise RuntimeError(f"Batch order failed: {error_msg}")

            statuses = result["response"]["data"]["statuses"]
            oids = []
            for s in statuses:
                if "resting" in s:
                    oids.append(str(s["resting"]["oid"]))
                elif "filled" in s:
                    oids.append(str(s["filled"]["oid"]))
                elif "error" in s:
                    logger.warning("Batch order item error: %s", s["error"])
                    oids.append("")
                else:
                    oids.append("")

            logger.info("Batch placed %d/%d orders", sum(1 for o in oids if o), len(orders))
            return oids
        except RuntimeError:
            raise
        except Exception:
            logger.exception("batch_modify_orders failed")
            raise

    # ── Position & balance ───────────────────────────────────────

    async def get_position(self, symbol: str) -> dict:
        """Get current position for symbol."""
        asset = _to_hl_symbol(symbol)
        try:
            state = await asyncio.to_thread(
                self._info.user_state, self._exchange.account_address
            )
            for pos in state.get("assetPositions", []):
                item = pos.get("position", {})
                if item.get("coin") == asset:
                    szi = float(item.get("szi", 0))
                    return {
                        "size": abs(szi),
                        "side": "long" if szi > 0 else "short" if szi < 0 else "none",
                        "entry_price": float(item.get("entryPx", 0)),
                        "unrealized_pnl": float(item.get("unrealizedPnl", 0)),
                        "liquidation_price": float(item.get("liquidationPx", 0) or 0),
                        "leverage": float(item.get("leverage", {}).get("value", 1)),
                    }
            return {"size": 0.0, "side": "none", "entry_price": 0.0, "unrealized_pnl": 0.0}
        except Exception:
            logger.exception("get_position failed for %s", symbol)
            raise

    async def get_open_orders(self, symbol: str) -> List[OrderInfo]:
        """Get open orders with fill status for a symbol."""
        asset = _to_hl_symbol(symbol)
        try:
            open_orders = await asyncio.to_thread(
                self._info.open_orders, self._exchange.account_address
            )
            result = []
            for o in open_orders:
                if o.get("coin") != asset:
                    continue
                orig_sz = float(o.get("origSz", o.get("sz", 0)))
                remaining_sz = float(o.get("sz", 0))
                filled_qty = orig_sz - remaining_sz
                side = "buy" if o.get("side", "").lower() in ("b", "buy") else "sell"

                status = "open"
                if filled_qty > 1e-12:
                    status = "partially_filled"

                result.append(OrderInfo(
                    oid=str(o["oid"]),
                    symbol=symbol,
                    side=side,
                    price=float(o.get("limitPx", 0)),
                    size=orig_sz,
                    status=status,
                    filled_qty=filled_qty,
                    remaining_qty=remaining_sz,
                ))
            return result
        except Exception:
            logger.exception("get_open_orders failed for %s", symbol)
            raise

    async def get_balance(self) -> float:
        """Get available USDC balance (cross-margin withdrawable)."""
        try:
            state = await asyncio.to_thread(
                self._info.user_state, self._exchange.account_address
            )
            return float(state.get("withdrawable", 0))
        except Exception:
            logger.exception("get_balance failed")
            raise

    # ── Safety ───────────────────────────────────────────────────

    async def set_dead_mans_switch(self, timeout_ms: int) -> bool:
        """Activate dead man's switch via schedule_cancel.

        If no heartbeat within timeout_ms, all open orders are cancelled.
        Call periodically to keep alive; set timeout_ms=0 to disable.
        """
        try:
            result = await asyncio.to_thread(
                self._exchange.schedule_cancel, int(time.time() * 1000) + timeout_ms
            )
            success = result.get("status", "") == "ok"
            if success:
                logger.debug("Dead man's switch set: %dms", timeout_ms)
            else:
                logger.warning("Dead man's switch failed: %s", result)
            return success
        except Exception:
            logger.exception("set_dead_mans_switch failed")
            return False
