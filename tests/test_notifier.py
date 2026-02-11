"""Tests for MMDiscordNotifier."""

import asyncio
import time
from unittest.mock import patch, MagicMock

import pytest

from bot_mm.utils.notifier import MMDiscordNotifier, _ERROR_RATE_LIMIT_SECONDS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run async coroutine synchronously."""
    return asyncio.run(coro)


def _mock_response(status=204):
    resp = MagicMock()
    resp.status_code = status
    resp.text = ""
    return resp


VALID_URL = "https://discord.com/api/webhooks/123/abc"


# ---------------------------------------------------------------------------
# is_configured
# ---------------------------------------------------------------------------

class TestIsConfigured:
    def test_valid_url(self):
        n = MMDiscordNotifier(VALID_URL)
        assert n.is_configured is True

    def test_invalid_url(self):
        n = MMDiscordNotifier("https://example.com/hook")
        assert n.is_configured is False

    def test_empty_url(self):
        n = MMDiscordNotifier("")
        assert n.is_configured is False

    def test_none_url(self):
        n = MMDiscordNotifier(None)
        assert n.is_configured is False


# ---------------------------------------------------------------------------
# send_raw
# ---------------------------------------------------------------------------

class TestSendRaw:
    @patch("bot_mm.utils.notifier.requests.post", return_value=_mock_response(204))
    def test_success(self, mock_post):
        n = MMDiscordNotifier(VALID_URL)
        payload = {"content": "hello"}
        result = _run(n.send_raw(payload))
        assert result is True
        mock_post.assert_called_once_with(VALID_URL, json=payload, timeout=10)

    def test_not_configured(self):
        n = MMDiscordNotifier("")
        result = _run(n.send_raw({"content": "hello"}))
        assert result is False


# ---------------------------------------------------------------------------
# recorder_started embed
# ---------------------------------------------------------------------------

class TestRecorderStarted:
    @patch("bot_mm.utils.notifier.requests.post", return_value=_mock_response())
    def test_embed_fields(self, mock_post):
        n = MMDiscordNotifier(VALID_URL)
        _run(n.send_recorder_started(["BTCUSDT", "ETHUSDT"], "data/output"))

        payload = mock_post.call_args[1]["json"]
        embed = payload["embeds"][0]
        assert embed["color"] == MMDiscordNotifier.COLOR_GREEN
        assert "BTCUSDT" in embed["fields"][0]["value"]
        assert "ETHUSDT" in embed["fields"][0]["value"]
        assert "data/output" in embed["fields"][1]["value"]


# ---------------------------------------------------------------------------
# recorder_stats embed
# ---------------------------------------------------------------------------

class TestRecorderStats:
    @patch("bot_mm.utils.notifier.requests.post", return_value=_mock_response())
    def test_stats_formatting(self, mock_post):
        n = MMDiscordNotifier(VALID_URL)
        stats = {
            "uptime_seconds": 7260,  # 2h 1m
            "snapshots": 12345,
            "trades": 6789,
            "reconnects": 3,
            "output_dir": "",  # avoid real disk scan
        }
        _run(n.send_recorder_stats(stats))

        payload = mock_post.call_args[1]["json"]
        fields = {f["name"]: f["value"] for f in payload["embeds"][0]["fields"]}

        assert fields["Uptime"] == "2h 1m"
        assert fields["Snapshots"] == "12,345"
        assert fields["Trades"] == "6,789"
        assert fields["Reconnects"] == "3"


# ---------------------------------------------------------------------------
# daily_report
# ---------------------------------------------------------------------------

class TestDailyReport:
    @patch("bot_mm.utils.notifier.requests.post", return_value=_mock_response())
    def test_profit_green(self, mock_post):
        n = MMDiscordNotifier(VALID_URL)
        metrics = {
            "pnl": 42.5,
            "volume": 100000,
            "fills": 320,
            "round_trips": 160,
            "avg_spread_bps": 1.2,
            "inventory_utilization_pct": 65.3,
        }
        _run(n.send_daily_report(metrics))

        embed = mock_post.call_args[1]["json"]["embeds"][0]
        assert embed["color"] == MMDiscordNotifier.COLOR_GREEN
        pnl_field = next(f for f in embed["fields"] if f["name"] == "Net PnL")
        assert "+42.50" in pnl_field["value"]

    @patch("bot_mm.utils.notifier.requests.post", return_value=_mock_response())
    def test_loss_red(self, mock_post):
        n = MMDiscordNotifier(VALID_URL)
        metrics = {"pnl": -15.0, "volume": 0, "fills": 0, "round_trips": 0,
                    "avg_spread_bps": 0, "inventory_utilization_pct": 0}
        _run(n.send_daily_report(metrics))

        embed = mock_post.call_args[1]["json"]["embeds"][0]
        assert embed["color"] == MMDiscordNotifier.COLOR_RED
        pnl_field = next(f for f in embed["fields"] if f["name"] == "Net PnL")
        assert "-15.00" in pnl_field["value"]


# ---------------------------------------------------------------------------
# error rate limiting
# ---------------------------------------------------------------------------

class TestErrorRateLimiting:
    @patch("bot_mm.utils.notifier.requests.post", return_value=_mock_response())
    def test_second_error_suppressed(self, mock_post):
        n = MMDiscordNotifier(VALID_URL)

        result1 = _run(n.send_recorder_error("first error"))
        assert result1 is True
        assert mock_post.call_count == 1

        # Second call within rate limit window — suppressed
        result2 = _run(n.send_recorder_error("second error"))
        assert result2 is False
        assert mock_post.call_count == 1  # no additional call

    @patch("bot_mm.utils.notifier.requests.post", return_value=_mock_response())
    def test_error_after_window(self, mock_post):
        n = MMDiscordNotifier(VALID_URL)

        _run(n.send_recorder_error("first"))
        # Simulate time passing beyond rate limit
        n._last_error_sent -= _ERROR_RATE_LIMIT_SECONDS + 1

        result = _run(n.send_recorder_error("second"))
        assert result is True
        assert mock_post.call_count == 2


# ---------------------------------------------------------------------------
# alert levels
# ---------------------------------------------------------------------------

class TestAlertLevels:
    @patch("bot_mm.utils.notifier.requests.post", return_value=_mock_response())
    def test_warning_orange(self, mock_post):
        n = MMDiscordNotifier(VALID_URL)
        _run(n.send_alert("Test", "msg", level="warning"))
        embed = mock_post.call_args[1]["json"]["embeds"][0]
        assert embed["color"] == MMDiscordNotifier.COLOR_ORANGE

    @patch("bot_mm.utils.notifier.requests.post", return_value=_mock_response())
    def test_error_red(self, mock_post):
        n = MMDiscordNotifier(VALID_URL)
        _run(n.send_alert("Test", "msg", level="error"))
        embed = mock_post.call_args[1]["json"]["embeds"][0]
        assert embed["color"] == MMDiscordNotifier.COLOR_RED

    @patch("bot_mm.utils.notifier.requests.post", return_value=_mock_response())
    def test_info_blue(self, mock_post):
        n = MMDiscordNotifier(VALID_URL)
        _run(n.send_alert("Test", "msg", level="info"))
        embed = mock_post.call_args[1]["json"]["embeds"][0]
        assert embed["color"] == MMDiscordNotifier.COLOR_BLUE
