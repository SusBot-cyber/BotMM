"""
Order Book Data Loader — loads recorded L2 snapshots and trades for replay.

Data format:
  L2: data/orderbook/{SYMBOL}/{date}/l2_{HH}.csv
      Columns: timestamp,level,bid_price,bid_size,ask_price,ask_size
  Trades: data/orderbook/{SYMBOL}/{date}/trades_{HH}.csv
      Columns: timestamp,side,price,size
"""

import csv
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Tuple, Union


@dataclass
class L2Level:
    """Single price level in the order book."""
    price: float
    size: float


@dataclass
class OrderBookSnapshot:
    """Full L2 snapshot at a point in time."""
    timestamp: str  # ISO format
    bids: List[L2Level]  # sorted desc by price
    asks: List[L2Level]  # sorted asc by price

    @property
    def mid_price(self) -> float:
        if not self.bids or not self.asks:
            return 0.0
        return (self.bids[0].price + self.asks[0].price) / 2.0

    @property
    def spread_bps(self) -> float:
        if not self.bids or not self.asks:
            return 0.0
        mid = self.mid_price
        if mid == 0:
            return 0.0
        return (self.asks[0].price - self.bids[0].price) / mid * 10000.0

    @property
    def bid_depth(self) -> float:
        """Total bid size in USD."""
        return sum(lvl.price * lvl.size for lvl in self.bids)

    @property
    def ask_depth(self) -> float:
        """Total ask size in USD."""
        return sum(lvl.price * lvl.size for lvl in self.asks)


@dataclass
class TradeTick:
    """A single market trade."""
    timestamp: str  # ISO format
    side: str       # "buy" or "sell"
    price: float
    size: float


class OrderBookLoader:
    """Load and merge L2 + trade data from CSV files."""

    def load_day(
        self,
        symbol: str,
        date: str,
        data_dir: str = "data/orderbook",
    ) -> Tuple[List[OrderBookSnapshot], List[TradeTick]]:
        """
        Load all data for a symbol+date.

        Args:
            symbol: e.g. "BTC"
            date: e.g. "2026-02-11"
            data_dir: root directory for orderbook data

        Returns:
            (snapshots, trades) — each sorted by timestamp
        """
        day_dir = Path(data_dir) / symbol / date
        snapshots: List[OrderBookSnapshot] = []
        trades: List[TradeTick] = []

        if not day_dir.exists():
            return snapshots, trades

        # Load L2 files
        l2_files = sorted(day_dir.glob("l2_*.csv"))
        for fp in l2_files:
            snapshots.extend(self._parse_l2_file(fp))

        # Load trade files
        trade_files = sorted(day_dir.glob("trades_*.csv"))
        for fp in trade_files:
            trades.extend(self._parse_trade_file(fp))

        snapshots.sort(key=lambda s: s.timestamp)
        trades.sort(key=lambda t: t.timestamp)
        return snapshots, trades

    def load_range(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        data_dir: str = "data/orderbook",
    ) -> Tuple[List[OrderBookSnapshot], List[TradeTick]]:
        """
        Load data for a date range (inclusive).

        Args:
            symbol: e.g. "BTC"
            start_date: e.g. "2026-02-10"
            end_date: e.g. "2026-02-11"
            data_dir: root directory for orderbook data

        Returns:
            (snapshots, trades) — merged and sorted by timestamp
        """
        all_snapshots: List[OrderBookSnapshot] = []
        all_trades: List[TradeTick] = []

        current = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")

        while current <= end:
            date_str = current.strftime("%Y-%m-%d")
            snaps, trd = self.load_day(symbol, date_str, data_dir)
            all_snapshots.extend(snaps)
            all_trades.extend(trd)
            current += timedelta(days=1)

        all_snapshots.sort(key=lambda s: s.timestamp)
        all_trades.sort(key=lambda t: t.timestamp)
        return all_snapshots, all_trades

    def create_timeline(
        self,
        snapshots: List[OrderBookSnapshot],
        trades: List[TradeTick],
    ) -> List[Union[OrderBookSnapshot, TradeTick]]:
        """
        Merge snapshots and trades into chronological timeline.

        Returns:
            List of events (OrderBookSnapshot or TradeTick) in time order.
            When timestamps are equal, snapshots come before trades
            so quotes update before fill checks.
        """
        events: List[Union[OrderBookSnapshot, TradeTick]] = []
        si, ti = 0, 0

        while si < len(snapshots) and ti < len(trades):
            snap_ts = snapshots[si].timestamp
            trade_ts = trades[ti].timestamp
            if snap_ts <= trade_ts:
                events.append(snapshots[si])
                si += 1
            else:
                events.append(trades[ti])
                ti += 1

        # Append remaining
        while si < len(snapshots):
            events.append(snapshots[si])
            si += 1
        while ti < len(trades):
            events.append(trades[ti])
            ti += 1

        return events

    # ── Private helpers ─────────────────────────────────────

    def _parse_l2_file(self, filepath: Path) -> List[OrderBookSnapshot]:
        """Parse an L2 CSV file into snapshots, grouping rows by timestamp."""
        snapshots: List[OrderBookSnapshot] = []
        rows_by_ts: dict = {}

        with open(filepath, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts = row["timestamp"]
                if ts not in rows_by_ts:
                    rows_by_ts[ts] = []
                rows_by_ts[ts].append(row)

        for ts in sorted(rows_by_ts.keys()):
            rows = rows_by_ts[ts]
            bids: List[L2Level] = []
            asks: List[L2Level] = []
            for row in rows:
                bp = float(row["bid_price"]) if row.get("bid_price") else 0.0
                bs = float(row["bid_size"]) if row.get("bid_size") else 0.0
                ap = float(row["ask_price"]) if row.get("ask_price") else 0.0
                as_ = float(row["ask_size"]) if row.get("ask_size") else 0.0
                if bp > 0 and bs > 0:
                    bids.append(L2Level(price=bp, size=bs))
                if ap > 0 and as_ > 0:
                    asks.append(L2Level(price=ap, size=as_))

            # Sort: bids desc, asks asc
            bids.sort(key=lambda l: l.price, reverse=True)
            asks.sort(key=lambda l: l.price)

            snapshots.append(OrderBookSnapshot(timestamp=ts, bids=bids, asks=asks))

        return snapshots

    def _parse_trade_file(self, filepath: Path) -> List[TradeTick]:
        """Parse a trades CSV file."""
        trades: List[TradeTick] = []

        with open(filepath, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trades.append(TradeTick(
                    timestamp=row["timestamp"],
                    side=row["side"].lower(),
                    price=float(row["price"]),
                    size=float(row["size"]),
                ))

        return trades
