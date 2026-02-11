"""Tests for Order Book Data Loader — uses temp CSV files."""

import csv
import os
import pytest
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.ob_loader import OrderBookLoader, OrderBookSnapshot, TradeTick, L2Level


# ── Helpers ─────────────────────────────────────────────────


def write_l2_csv(filepath: Path, rows: list):
    """Write L2 CSV file with header + rows."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "level", "bid_price", "bid_size", "ask_price", "ask_size"])
        for row in rows:
            writer.writerow(row)


def write_trades_csv(filepath: Path, rows: list):
    """Write trades CSV file with header + rows."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "side", "price", "size"])
        for row in rows:
            writer.writerow(row)


# ── load_day ────────────────────────────────────────────────


class TestLoadDay:
    """Tests for loading a single day of data."""

    def test_load_l2_and_trades(self, tmp_path):
        """Load both L2 and trade files for a day."""
        day_dir = tmp_path / "BTC" / "2026-02-11"

        write_l2_csv(day_dir / "l2_00.csv", [
            ["2026-02-11T00:00:00", 0, 100.0, 1.0, 100.1, 1.0],
            ["2026-02-11T00:00:00", 1, 99.9, 0.5, 100.2, 0.5],
            ["2026-02-11T00:00:01", 0, 100.0, 1.1, 100.1, 0.9],
        ])

        write_trades_csv(day_dir / "trades_00.csv", [
            ["2026-02-11T00:00:00", "buy", 100.1, 0.5],
            ["2026-02-11T00:00:01", "sell", 100.0, 0.3],
        ])

        loader = OrderBookLoader()
        snapshots, trades = loader.load_day("BTC", "2026-02-11", str(tmp_path))

        assert len(snapshots) == 2  # two unique timestamps
        assert len(trades) == 2

        # First snapshot has 2 levels
        assert len(snapshots[0].bids) == 2
        assert len(snapshots[0].asks) == 2

        # Bids sorted descending
        assert snapshots[0].bids[0].price >= snapshots[0].bids[1].price
        # Asks sorted ascending
        assert snapshots[0].asks[0].price <= snapshots[0].asks[1].price

    def test_load_multiple_hour_files(self, tmp_path):
        """Load data from multiple hourly files."""
        day_dir = tmp_path / "BTC" / "2026-02-11"

        write_l2_csv(day_dir / "l2_00.csv", [
            ["2026-02-11T00:00:00", 0, 100.0, 1.0, 100.1, 1.0],
        ])
        write_l2_csv(day_dir / "l2_01.csv", [
            ["2026-02-11T01:00:00", 0, 101.0, 1.0, 101.1, 1.0],
        ])

        loader = OrderBookLoader()
        snapshots, trades = loader.load_day("BTC", "2026-02-11", str(tmp_path))

        assert len(snapshots) == 2
        assert snapshots[0].timestamp < snapshots[1].timestamp

    def test_snapshot_properties(self, tmp_path):
        """Test mid_price, spread_bps, bid/ask_depth."""
        day_dir = tmp_path / "BTC" / "2026-02-11"

        write_l2_csv(day_dir / "l2_00.csv", [
            ["2026-02-11T00:00:00", 0, 100.0, 2.0, 100.2, 3.0],
            ["2026-02-11T00:00:00", 1, 99.8, 1.0, 100.4, 1.0],
        ])

        loader = OrderBookLoader()
        snapshots, _ = loader.load_day("BTC", "2026-02-11", str(tmp_path))

        snap = snapshots[0]
        assert snap.mid_price == pytest.approx(100.1, abs=0.01)
        # spread = (100.2 - 100.0) / 100.1 * 10000 ≈ 19.98 bps
        assert snap.spread_bps == pytest.approx(19.98, abs=0.5)
        # bid_depth = 100.0*2.0 + 99.8*1.0 = 299.8
        assert snap.bid_depth == pytest.approx(299.8, abs=0.1)
        # ask_depth = 100.2*3.0 + 100.4*1.0 = 401.0
        assert snap.ask_depth == pytest.approx(401.0, abs=0.1)

    def test_trade_fields(self, tmp_path):
        """Trade fields should be parsed correctly."""
        day_dir = tmp_path / "ETH" / "2026-02-11"

        write_trades_csv(day_dir / "trades_00.csv", [
            ["2026-02-11T00:00:05", "BUY", 2500.50, 1.5],
        ])

        loader = OrderBookLoader()
        _, trades = loader.load_day("ETH", "2026-02-11", str(tmp_path))

        assert len(trades) == 1
        assert trades[0].side == "buy"
        assert trades[0].price == pytest.approx(2500.50, abs=0.01)
        assert trades[0].size == pytest.approx(1.5, abs=0.01)


# ── load_range ──────────────────────────────────────────────


class TestLoadRange:
    """Tests for loading a date range."""

    def test_multi_day_range(self, tmp_path):
        """Load across multiple dates."""
        for date in ["2026-02-10", "2026-02-11"]:
            day_dir = tmp_path / "BTC" / date
            write_l2_csv(day_dir / "l2_00.csv", [
                [f"{date}T00:00:00", 0, 100.0, 1.0, 100.1, 1.0],
            ])
            write_trades_csv(day_dir / "trades_00.csv", [
                [f"{date}T00:00:01", "sell", 100.0, 0.5],
            ])

        loader = OrderBookLoader()
        snapshots, trades = loader.load_range(
            "BTC", "2026-02-10", "2026-02-11", str(tmp_path)
        )

        assert len(snapshots) == 2
        assert len(trades) == 2
        # Sorted chronologically
        assert snapshots[0].timestamp < snapshots[1].timestamp

    def test_single_day_range(self, tmp_path):
        """Start == end should load single day."""
        day_dir = tmp_path / "BTC" / "2026-02-11"
        write_l2_csv(day_dir / "l2_00.csv", [
            ["2026-02-11T00:00:00", 0, 100.0, 1.0, 100.1, 1.0],
        ])

        loader = OrderBookLoader()
        snapshots, _ = loader.load_range(
            "BTC", "2026-02-11", "2026-02-11", str(tmp_path)
        )

        assert len(snapshots) == 1


# ── Timeline merge ──────────────────────────────────────────


class TestTimeline:
    """Tests for chronological event merging."""

    def test_chronological_order(self):
        """Events should be sorted by timestamp."""
        loader = OrderBookLoader()

        snapshots = [
            OrderBookSnapshot(timestamp="2026-02-11T00:00:00", bids=[], asks=[]),
            OrderBookSnapshot(timestamp="2026-02-11T00:00:02", bids=[], asks=[]),
        ]
        trades = [
            TradeTick(timestamp="2026-02-11T00:00:01", side="buy", price=100.0, size=1.0),
        ]

        timeline = loader.create_timeline(snapshots, trades)

        assert len(timeline) == 3
        assert isinstance(timeline[0], OrderBookSnapshot)
        assert isinstance(timeline[1], TradeTick)
        assert isinstance(timeline[2], OrderBookSnapshot)

    def test_snapshot_before_trade_on_equal_ts(self):
        """When timestamps equal, snapshots come first."""
        loader = OrderBookLoader()

        snapshots = [
            OrderBookSnapshot(timestamp="2026-02-11T00:00:00", bids=[], asks=[]),
        ]
        trades = [
            TradeTick(timestamp="2026-02-11T00:00:00", side="sell", price=100.0, size=0.5),
        ]

        timeline = loader.create_timeline(snapshots, trades)

        assert len(timeline) == 2
        assert isinstance(timeline[0], OrderBookSnapshot)
        assert isinstance(timeline[1], TradeTick)

    def test_empty_timeline(self):
        """Empty inputs should produce empty timeline."""
        loader = OrderBookLoader()
        timeline = loader.create_timeline([], [])
        assert len(timeline) == 0

    def test_only_snapshots(self):
        """Only snapshots, no trades."""
        loader = OrderBookLoader()
        snapshots = [
            OrderBookSnapshot(timestamp="2026-02-11T00:00:00", bids=[], asks=[]),
        ]
        timeline = loader.create_timeline(snapshots, [])
        assert len(timeline) == 1

    def test_only_trades(self):
        """Only trades, no snapshots."""
        loader = OrderBookLoader()
        trades = [
            TradeTick(timestamp="2026-02-11T00:00:01", side="buy", price=100.0, size=1.0),
        ]
        timeline = loader.create_timeline([], trades)
        assert len(timeline) == 1


# ── Edge cases ──────────────────────────────────────────────


class TestEdgeCases:
    """Tests for error handling and edge cases."""

    def test_missing_directory(self, tmp_path):
        """Missing symbol directory should return empty lists."""
        loader = OrderBookLoader()
        snapshots, trades = loader.load_day("NOEXIST", "2026-02-11", str(tmp_path))
        assert len(snapshots) == 0
        assert len(trades) == 0

    def test_empty_csv_files(self, tmp_path):
        """Empty CSV (header only) should return empty lists."""
        day_dir = tmp_path / "BTC" / "2026-02-11"
        write_l2_csv(day_dir / "l2_00.csv", [])
        write_trades_csv(day_dir / "trades_00.csv", [])

        loader = OrderBookLoader()
        snapshots, trades = loader.load_day("BTC", "2026-02-11", str(tmp_path))
        assert len(snapshots) == 0
        assert len(trades) == 0

    def test_missing_trade_file(self, tmp_path):
        """Only L2 file, no trades file — trades should be empty."""
        day_dir = tmp_path / "BTC" / "2026-02-11"
        write_l2_csv(day_dir / "l2_00.csv", [
            ["2026-02-11T00:00:00", 0, 100.0, 1.0, 100.1, 1.0],
        ])

        loader = OrderBookLoader()
        snapshots, trades = loader.load_day("BTC", "2026-02-11", str(tmp_path))
        assert len(snapshots) == 1
        assert len(trades) == 0

    def test_missing_l2_file(self, tmp_path):
        """Only trades file, no L2 — snapshots should be empty."""
        day_dir = tmp_path / "BTC" / "2026-02-11"
        write_trades_csv(day_dir / "trades_00.csv", [
            ["2026-02-11T00:00:01", "sell", 100.0, 0.5],
        ])

        loader = OrderBookLoader()
        snapshots, trades = loader.load_day("BTC", "2026-02-11", str(tmp_path))
        assert len(snapshots) == 0
        assert len(trades) == 1

    def test_snapshot_empty_book(self):
        """Snapshot with no levels should have zero mid/spread/depth."""
        snap = OrderBookSnapshot(timestamp="2026-02-11T00:00:00", bids=[], asks=[])
        assert snap.mid_price == 0.0
        assert snap.spread_bps == 0.0
        assert snap.bid_depth == 0.0
        assert snap.ask_depth == 0.0
