"""
L2 Order Book Recorder — streams and persists HL WebSocket data.

Records:
1. L2 order book snapshots (bids/asks with price+size at N levels)
2. Trade ticks (price, size, side, timestamp)

Storage format: CSV files with rotation (1 file per hour)
Directory: data/orderbook/{SYMBOL}/{date}/
  - l2_{HH}.csv:     timestamp, level, bid_price, bid_size, ask_price, ask_size
  - trades_{HH}.csv: timestamp, side, price, size
"""

import asyncio
import csv
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import TextIOWrapper
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

WS_URL = "wss://api.hyperliquid.xyz/ws"

L2_HEADER = ["timestamp", "level", "bid_price", "bid_size", "ask_price", "ask_size"]
TRADES_HEADER = ["timestamp", "side", "price", "size"]


@dataclass
class L2Snapshot:
    """Single L2 order book snapshot."""

    timestamp: str  # ISO format
    bids: List[Tuple[float, float]] = field(default_factory=list)
    asks: List[Tuple[float, float]] = field(default_factory=list)


@dataclass
class TradeRecord:
    """Single trade tick."""

    timestamp: str
    side: str  # "buy" or "sell"
    price: float
    size: float


class L2Recorder:
    """
    Records L2 order book snapshots and trades from Hyperliquid WebSocket.

    Usage::

        recorder = L2Recorder(symbols=["BTC", "ETH"], output_dir="data/orderbook")
        await recorder.start()  # runs until stopped
        recorder.stop()
    """

    def __init__(
        self,
        symbols: List[str],
        output_dir: str = "data/orderbook",
        n_levels: int = 20,
        n_sig_figs: int = 5,
        snapshot_interval_ms: int = 1000,
        reconnect_delay: float = 5.0,
        max_reconnect_attempts: int = 50,
    ):
        self.symbols = [s.upper().replace("USDT", "") for s in symbols]
        self.output_dir = Path(output_dir)
        self.n_levels = n_levels
        self.n_sig_figs = n_sig_figs
        self.snapshot_interval_ms = snapshot_interval_ms
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_attempts = max_reconnect_attempts

        self._running = False
        self._ws = None
        self._start_time: Optional[float] = None

        # Per-symbol latest book state
        self._books: Dict[str, L2Snapshot] = {}

        # Open file handles: key = (coin, data_type, hour_key)
        self._file_handles: Dict[Tuple[str, str, str], TextIOWrapper] = {}
        self._csv_writers: Dict[Tuple[str, str, str], csv.writer] = {}

        # Periodic flush counter
        self._writes_since_flush: int = 0

        # Stats
        self._stats: Dict[str, int] = {
            "snapshots_recorded": 0,
            "trades_recorded": 0,
            "reconnects": 0,
            "messages_received": 0,
        }

    # ------------------------------------------------------------------ public

    async def start(self):
        """Connect to WebSocket and start recording. Blocks until stopped."""
        self._running = True
        self._start_time = time.monotonic()
        attempt = 0

        while self._running and attempt < self.max_reconnect_attempts:
            try:
                await self._connect_and_subscribe()
                attempt = 0  # reset on successful connection
            except Exception as e:
                if not self._running:
                    break
                attempt += 1
                delay = min(self.reconnect_delay * (2 ** min(attempt - 1, 5)), 60.0)
                self._stats["reconnects"] += 1
                logger.warning(
                    "WebSocket disconnected (%s), reconnect %d/%d in %.1fs",
                    e,
                    attempt,
                    self.max_reconnect_attempts,
                    delay,
                )
                await asyncio.sleep(delay)

        if attempt >= self.max_reconnect_attempts:
            logger.error(
                "Max reconnect attempts (%d) reached", self.max_reconnect_attempts
            )

        self._close_all_files()

    def stop(self):
        """Signal to stop recording."""
        self._running = False
        if self._ws is not None:
            try:
                asyncio.get_event_loop().create_task(self._ws.close())
            except RuntimeError:
                pass

    # --------------------------------------------------------------- internal

    async def _connect_and_subscribe(self):
        """Connect to HL WebSocket and subscribe to L2 + trades."""
        import websockets

        logger.info("Connecting to %s ...", WS_URL)
        async with websockets.connect(
            WS_URL, ping_interval=20, ping_timeout=10
        ) as ws:
            self._ws = ws
            logger.info("Connected. Subscribing to %d symbols.", len(self.symbols))

            for coin in self.symbols:
                await ws.send(
                    json.dumps(
                        {
                            "method": "subscribe",
                            "subscription": {
                                "type": "l2Book",
                                "coin": coin,
                                "nSigFigs": self.n_sig_figs,
                                "nLevels": self.n_levels,
                            },
                        }
                    )
                )
                await ws.send(
                    json.dumps(
                        {
                            "method": "subscribe",
                            "subscription": {"type": "trades", "coin": coin},
                        }
                    )
                )

            logger.info("Subscribed. Recording started.")

            async for message in ws:
                if not self._running:
                    break
                try:
                    await self._handle_message(message)
                except Exception:
                    logger.exception("Error handling message")

    async def _handle_message(self, message: str):
        """Parse and route incoming WebSocket messages."""
        data = json.loads(message)
        self._stats["messages_received"] += 1

        channel = data.get("channel")
        msg_data = data.get("data")
        if not channel or msg_data is None:
            return

        if channel == "l2Book":
            coin = msg_data.get("coin", "")
            self._handle_l2_update(coin, msg_data)
        elif channel == "trades":
            trades_list = msg_data if isinstance(msg_data, list) else [msg_data]
            for t in trades_list:
                coin = t.get("coin", "")
                self._handle_trade(coin, t)

    def _handle_l2_update(self, coin: str, data: dict):
        """Process L2 book update and write snapshot."""
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        levels = data.get("levels", [[], []])
        bids_raw = levels[0] if len(levels) > 0 else []
        asks_raw = levels[1] if len(levels) > 1 else []

        bids = [(float(b.get("px", 0)), float(b.get("sz", 0))) for b in bids_raw]
        asks = [(float(a.get("px", 0)), float(a.get("sz", 0))) for a in asks_raw]

        snapshot = L2Snapshot(timestamp=now, bids=bids, asks=asks)
        self._books[coin] = snapshot
        self._write_l2_snapshot(coin, snapshot)

    def _handle_trade(self, coin: str, data: dict):
        """Process trade tick and write to CSV."""
        ts = data.get("time", "")
        if isinstance(ts, (int, float)):
            ts = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            )
        elif not ts:
            ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

        trade = TradeRecord(
            timestamp=ts,
            side=data.get("side", "").lower(),
            price=float(data.get("px", 0)),
            size=float(data.get("sz", 0)),
        )
        self._write_trade(coin, trade)

    def _write_l2_snapshot(self, coin: str, snapshot: L2Snapshot):
        """Write L2 snapshot to CSV (hourly rotation)."""
        writer = self._get_csv_writer(coin, "l2")
        n = max(len(snapshot.bids), len(snapshot.asks))
        for i in range(n):
            bp, bs = snapshot.bids[i] if i < len(snapshot.bids) else ("", "")
            ap, az = snapshot.asks[i] if i < len(snapshot.asks) else ("", "")
            writer.writerow([snapshot.timestamp, i, bp, bs, ap, az])
        self._stats["snapshots_recorded"] += 1
        self._maybe_flush()

    def _write_trade(self, coin: str, trade: TradeRecord):
        """Write trade to CSV (hourly rotation)."""
        writer = self._get_csv_writer(coin, "trades")
        writer.writerow([trade.timestamp, trade.side, trade.price, trade.size])
        self._stats["trades_recorded"] += 1
        self._maybe_flush()

    def _maybe_flush(self):
        """Flush open file handles every 100 writes."""
        self._writes_since_flush += 1
        if self._writes_since_flush >= 100:
            for fh in self._file_handles.values():
                if fh and not fh.closed:
                    fh.flush()
            self._writes_since_flush = 0

    def _get_csv_writer(self, coin: str, data_type: str) -> csv.writer:
        """Get or create CSV writer with hourly file rotation."""
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        hour_str = now.strftime("%H")
        key = (coin, data_type, f"{date_str}_{hour_str}")

        if key in self._csv_writers:
            return self._csv_writers[key]

        # Close previous file for same coin+data_type if hour changed
        for old_key in list(self._file_handles):
            if (
                old_key[0] == coin
                and old_key[1] == data_type
                and old_key[2] != key[2]
            ):
                self._close_file(old_key)

        dir_path = self.output_dir / coin / date_str
        dir_path.mkdir(parents=True, exist_ok=True)
        file_path = dir_path / f"{data_type}_{hour_str}.csv"

        write_header = not file_path.exists()
        fh = open(file_path, "a", newline="", encoding="utf-8")
        writer = csv.writer(fh)

        if write_header:
            header = L2_HEADER if data_type == "l2" else TRADES_HEADER
            writer.writerow(header)

        self._file_handles[key] = fh
        self._csv_writers[key] = writer
        logger.debug("Opened CSV: %s", file_path)
        return writer

    def _close_file(self, key: Tuple[str, str, str]):
        """Close a single file handle."""
        fh = self._file_handles.pop(key, None)
        if fh and not fh.closed:
            fh.flush()
            fh.close()
        self._csv_writers.pop(key, None)

    def _close_all_files(self):
        """Close all open file handles."""
        for key in list(self._file_handles):
            self._close_file(key)

    def summary(self) -> dict:
        """Return recording stats."""
        uptime = time.monotonic() - self._start_time if self._start_time else 0.0
        return {
            "symbols": self.symbols,
            "uptime_seconds": round(uptime, 1),
            "snapshots_recorded": self._stats["snapshots_recorded"],
            "trades_recorded": self._stats["trades_recorded"],
            "messages_received": self._stats["messages_received"],
            "reconnects": self._stats["reconnects"],
            "open_files": len(self._file_handles),
        }
