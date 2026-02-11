"""Tests for L2 order book recorder (no real WebSocket connections)."""

import asyncio
import csv
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot_mm.data.l2_recorder import (
    L2_HEADER,
    TRADES_HEADER,
    L2Recorder,
    L2Snapshot,
    TradeRecord,
)


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def tmp_output(tmp_path):
    """Temporary output directory for CSV files."""
    return str(tmp_path / "orderbook")


@pytest.fixture
def recorder(tmp_output):
    """L2Recorder with temp output dir."""
    return L2Recorder(symbols=["BTC"], output_dir=tmp_output, n_levels=5)


@pytest.fixture
def multi_recorder(tmp_output):
    """L2Recorder with multiple symbols."""
    return L2Recorder(symbols=["BTC", "ETH", "SOL"], output_dir=tmp_output, n_levels=5)


# ------------------------------------------------------- L2 snapshot parsing


def _make_l2_message(coin="BTC", n_levels=3):
    """Build a realistic HL l2Book WebSocket message."""
    bids = [{"px": str(100000 - i * 10), "sz": str(0.5 + i * 0.1)} for i in range(n_levels)]
    asks = [{"px": str(100010 + i * 10), "sz": str(0.4 + i * 0.1)} for i in range(n_levels)]
    return json.dumps(
        {
            "channel": "l2Book",
            "data": {"coin": coin, "levels": [bids, asks]},
        }
    )


class TestL2SnapshotParsing:
    def test_parse_l2_update(self, recorder, tmp_output):
        """L2 book message is parsed into bids/asks tuples."""
        recorder._start_time = 0
        msg = _make_l2_message("BTC", n_levels=3)
        asyncio.get_event_loop().run_until_complete(recorder._handle_message(msg))

        assert "BTC" in recorder._books
        snap = recorder._books["BTC"]
        assert len(snap.bids) == 3
        assert len(snap.asks) == 3
        assert snap.bids[0] == (100000.0, 0.5)
        assert snap.asks[0] == (100010.0, 0.4)

    def test_l2_snapshot_written_to_csv(self, recorder, tmp_output):
        """Snapshot rows are written to an l2_*.csv file."""
        recorder._start_time = 0
        msg = _make_l2_message("BTC", n_levels=2)
        asyncio.get_event_loop().run_until_complete(recorder._handle_message(msg))
        recorder._close_all_files()

        csvs = list(Path(tmp_output).rglob("l2_*.csv"))
        assert len(csvs) == 1

        with open(csvs[0], newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        # header + 2 level rows
        assert rows[0] == L2_HEADER
        assert len(rows) == 3
        assert rows[1][1] == "0"  # level index

    def test_l2_empty_levels(self, recorder, tmp_output):
        """Handles missing levels gracefully."""
        recorder._start_time = 0
        msg = json.dumps(
            {"channel": "l2Book", "data": {"coin": "BTC", "levels": [[], []]}}
        )
        asyncio.get_event_loop().run_until_complete(recorder._handle_message(msg))
        assert recorder._stats["snapshots_recorded"] == 1


# ----------------------------------------------------------- trade parsing


def _make_trade_message(coin="BTC", side="Buy", px="99999.0", sz="0.01", time_ms=1707600000000):
    return json.dumps(
        {
            "channel": "trades",
            "data": [{"coin": coin, "side": side, "px": px, "sz": sz, "time": time_ms}],
        }
    )


class TestTradeParsing:
    def test_parse_trade(self, recorder, tmp_output):
        """Trade message is parsed and written to CSV."""
        recorder._start_time = 0
        msg = _make_trade_message("BTC", "Buy", "99500.0", "0.05")
        asyncio.get_event_loop().run_until_complete(recorder._handle_message(msg))
        recorder._close_all_files()

        csvs = list(Path(tmp_output).rglob("trades_*.csv"))
        assert len(csvs) == 1

        with open(csvs[0], newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows[0] == TRADES_HEADER
        assert len(rows) == 2
        assert rows[1][1] == "buy"  # side lowercased
        assert rows[1][2] == "99500.0"

    def test_trade_missing_time_field(self, recorder, tmp_output):
        """Trade without 'time' field uses current UTC time."""
        recorder._start_time = 0
        msg = json.dumps(
            {
                "channel": "trades",
                "data": [{"coin": "BTC", "side": "Sell", "px": "100000", "sz": "0.1"}],
            }
        )
        asyncio.get_event_loop().run_until_complete(recorder._handle_message(msg))
        assert recorder._stats["trades_recorded"] == 1

    def test_multiple_trades_in_batch(self, recorder, tmp_output):
        """Batch of trades in a single message."""
        recorder._start_time = 0
        msg = json.dumps(
            {
                "channel": "trades",
                "data": [
                    {"coin": "BTC", "side": "Buy", "px": "100000", "sz": "0.1", "time": 1707600000000},
                    {"coin": "BTC", "side": "Sell", "px": "99999", "sz": "0.2", "time": 1707600001000},
                ],
            }
        )
        asyncio.get_event_loop().run_until_complete(recorder._handle_message(msg))
        assert recorder._stats["trades_recorded"] == 2


# ------------------------------------------------- CSV rotation / creation


class TestCSVRotation:
    def test_csv_file_created_in_correct_path(self, recorder, tmp_output):
        """CSV is created under output_dir/COIN/YYYY-MM-DD/."""
        recorder._start_time = 0
        msg = _make_l2_message("BTC", n_levels=1)
        asyncio.get_event_loop().run_until_complete(recorder._handle_message(msg))
        recorder._close_all_files()

        # Should have BTC subdirectory with a date subdirectory
        btc_dir = Path(tmp_output) / "BTC"
        assert btc_dir.exists()
        date_dirs = list(btc_dir.iterdir())
        assert len(date_dirs) == 1

    def test_hourly_rotation_closes_old_file(self, recorder, tmp_output):
        """When hour changes, old file is closed and new one opened."""
        recorder._start_time = 0

        # Write first snapshot to create an initial file
        msg = _make_l2_message("BTC", n_levels=1)
        asyncio.get_event_loop().run_until_complete(recorder._handle_message(msg))

        old_keys = list(recorder._csv_writers.keys())
        assert len(old_keys) == 1

        # Remove current hour's writer so next write triggers rotation logic
        current_key = old_keys[0]
        recorder._close_file(current_key)

        # Insert a fake old-hour writer (simulates leftover from previous hour)
        fake_old_key = ("BTC", "l2", "2026-01-01_00")
        recorder._file_handles[fake_old_key] = MagicMock()
        recorder._file_handles[fake_old_key].closed = False
        recorder._csv_writers[fake_old_key] = MagicMock()

        # Next write must close the fake old key and open a new current one
        asyncio.get_event_loop().run_until_complete(recorder._handle_message(msg))

        assert fake_old_key not in recorder._file_handles

    def test_header_written_once(self, recorder, tmp_output):
        """CSV header is written only on file creation, not on append."""
        recorder._start_time = 0
        msg = _make_l2_message("BTC", n_levels=1)
        asyncio.get_event_loop().run_until_complete(recorder._handle_message(msg))
        asyncio.get_event_loop().run_until_complete(recorder._handle_message(msg))
        recorder._close_all_files()

        csvs = list(Path(tmp_output).rglob("l2_*.csv"))
        with open(csvs[0], newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        # 1 header + 2 data rows (1 level × 2 snapshots)
        header_count = sum(1 for r in rows if r == L2_HEADER)
        assert header_count == 1


# --------------------------------------------------------- reconnect logic


class TestReconnect:
    @pytest.mark.asyncio
    async def test_reconnect_increments_counter(self, recorder):
        """Failed connection increments reconnect counter."""
        recorder.max_reconnect_attempts = 2
        recorder.reconnect_delay = 0.01

        with patch.object(
            recorder,
            "_connect_and_subscribe",
            side_effect=ConnectionError("refused"),
        ):
            await recorder.start()

        assert recorder._stats["reconnects"] == 2

    @pytest.mark.asyncio
    async def test_stop_breaks_reconnect_loop(self, recorder):
        """Calling stop() exits the reconnect loop."""
        recorder.reconnect_delay = 0.01

        call_count = 0

        async def fake_connect():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                recorder.stop()
            raise ConnectionError("test")

        with patch.object(recorder, "_connect_and_subscribe", side_effect=fake_connect):
            await recorder.start()

        assert call_count >= 2
        assert not recorder._running


# ---------------------------------------------------------- summary stats


class TestSummary:
    def test_summary_returns_all_fields(self, recorder):
        """Summary dict contains all expected keys."""
        recorder._start_time = 0  # set so uptime works
        s = recorder.summary()
        assert "symbols" in s
        assert "uptime_seconds" in s
        assert "snapshots_recorded" in s
        assert "trades_recorded" in s
        assert "messages_received" in s
        assert "reconnects" in s
        assert "open_files" in s

    def test_summary_counts_accurate(self, recorder, tmp_output):
        """Summary counts match actual writes."""
        recorder._start_time = 0
        l2_msg = _make_l2_message("BTC", n_levels=2)
        trade_msg = _make_trade_message("BTC")

        loop = asyncio.get_event_loop()
        loop.run_until_complete(recorder._handle_message(l2_msg))
        loop.run_until_complete(recorder._handle_message(l2_msg))
        loop.run_until_complete(recorder._handle_message(trade_msg))

        s = recorder.summary()
        assert s["snapshots_recorded"] == 2
        assert s["trades_recorded"] == 1
        assert s["messages_received"] == 3


# ------------------------------------------------------- graceful shutdown


class TestShutdown:
    def test_stop_sets_running_false(self, recorder):
        """stop() sets _running to False."""
        recorder._running = True
        recorder.stop()
        assert not recorder._running

    def test_close_all_files(self, recorder, tmp_output):
        """_close_all_files closes and removes all handles."""
        recorder._start_time = 0
        msg = _make_l2_message("BTC", n_levels=1)
        asyncio.get_event_loop().run_until_complete(recorder._handle_message(msg))

        assert len(recorder._file_handles) > 0
        recorder._close_all_files()
        assert len(recorder._file_handles) == 0
        assert len(recorder._csv_writers) == 0


# -------------------------------------------------------- multiple symbols


class TestMultiSymbol:
    def test_multiple_symbols_separate_dirs(self, multi_recorder, tmp_output):
        """Each symbol gets its own subdirectory."""
        multi_recorder._start_time = 0
        for coin in ["BTC", "ETH", "SOL"]:
            msg = _make_l2_message(coin, n_levels=1)
            asyncio.get_event_loop().run_until_complete(
                multi_recorder._handle_message(msg)
            )
        multi_recorder._close_all_files()

        for coin in ["BTC", "ETH", "SOL"]:
            assert (Path(tmp_output) / coin).exists()

    def test_symbol_normalization(self):
        """BTCUSDT is normalized to BTC."""
        r = L2Recorder(symbols=["BTCUSDT", "ETHUSDT", "SOL"])
        assert r.symbols == ["BTC", "ETH", "SOL"]

    def test_multiple_symbols_independent_stats(self, multi_recorder, tmp_output):
        """Stats accumulate across all symbols."""
        multi_recorder._start_time = 0
        for coin in ["BTC", "ETH"]:
            msg = _make_l2_message(coin, n_levels=1)
            asyncio.get_event_loop().run_until_complete(
                multi_recorder._handle_message(msg)
            )

        s = multi_recorder.summary()
        assert s["snapshots_recorded"] == 2
        assert s["messages_received"] == 2


# --------------------------------------------------------- message routing


class TestMessageRouting:
    def test_unknown_channel_ignored(self, recorder):
        """Unknown channel messages are silently ignored."""
        recorder._start_time = 0
        msg = json.dumps({"channel": "unknown", "data": {"foo": "bar"}})
        asyncio.get_event_loop().run_until_complete(recorder._handle_message(msg))
        assert recorder._stats["messages_received"] == 1
        assert recorder._stats["snapshots_recorded"] == 0
        assert recorder._stats["trades_recorded"] == 0

    def test_message_without_channel_ignored(self, recorder):
        """Messages without 'channel' key are silently ignored."""
        recorder._start_time = 0
        msg = json.dumps({"method": "subscribed"})
        asyncio.get_event_loop().run_until_complete(recorder._handle_message(msg))
        assert recorder._stats["messages_received"] == 1
        assert recorder._stats["snapshots_recorded"] == 0
