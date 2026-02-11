"""
Discord Notification System for Market Making Bot.

Sends recording stats, daily reports, and alerts to Discord webhook.
"""

import asyncio
import logging
import os
import time
import requests
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2)

# Rate limit: suppress duplicate error notifications within this window
_ERROR_RATE_LIMIT_SECONDS = 300  # 5 minutes


class MMDiscordNotifier:
    """Discord webhook notifier for Market Making Bot."""

    COLOR_GREEN = 0x00FF00
    COLOR_RED = 0xFF0000
    COLOR_ORANGE = 0xFFAA00
    COLOR_BLUE = 0x0099FF
    COLOR_PURPLE = 0x9B59B6

    def __init__(self, webhook_url: str, bot_name: str = "BotMM"):
        self.webhook_url = webhook_url
        self.bot_name = bot_name
        self._last_error_sent: float = 0.0

    @property
    def is_configured(self) -> bool:
        """Check if webhook URL is valid."""
        return bool(
            self.webhook_url and self.webhook_url.startswith("https://discord")
        )

    def _send_sync(self, payload: Dict[str, Any]) -> bool:
        """Synchronous POST to Discord webhook (runs in thread pool)."""
        try:
            response = requests.post(
                self.webhook_url, json=payload, timeout=10
            )
            if response.status_code in (200, 204):
                return True
            logger.warning(
                "Discord webhook failed: status=%s body=%s",
                response.status_code,
                response.text[:200],
            )
            return False
        except Exception as e:
            logger.error("Failed to send Discord notification: %s", e)
            return False

    async def send_raw(self, payload: Dict[str, Any]) -> bool:
        """Send raw payload to Discord."""
        if not self.is_configured:
            return False
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, self._send_sync, payload)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _embed(
        self,
        title: str,
        color: int,
        fields: list,
        description: str = "",
        footer_extra: str = "",
    ) -> Dict[str, Any]:
        footer_text = (
            f"{self.bot_name} | {footer_extra}" if footer_extra else self.bot_name
        )
        embed: Dict[str, Any] = {
            "title": title,
            "color": color,
            "fields": fields,
            "footer": {"text": footer_text},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if description:
            embed["description"] = description
        return embed

    @staticmethod
    def _fmt_uptime(seconds: float) -> str:
        """Format seconds into human-readable Xh Ym."""
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}m"

    @staticmethod
    def _fmt_number(n) -> str:
        """Format number with comma separators."""
        if isinstance(n, float):
            return f"{n:,.2f}"
        return f"{n:,}"

    @staticmethod
    def _dir_size_mb(path: str) -> str:
        """Calculate directory size in MB."""
        total = 0
        try:
            for dirpath, _dirnames, filenames in os.walk(path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    try:
                        total += os.path.getsize(fp)
                    except OSError:
                        pass
        except OSError:
            return "N/A"
        return f"{total / (1024 * 1024):.1f} MB"

    # ------------------------------------------------------------------
    # L2 Recorder notifications
    # ------------------------------------------------------------------

    async def send_recorder_started(
        self, symbols: list, output_dir: str
    ) -> bool:
        """Recording started notification."""
        embed = self._embed(
            title="📡 Recording Started",
            color=self.COLOR_GREEN,
            fields=[
                {
                    "name": "Symbols",
                    "value": ", ".join(symbols),
                    "inline": False,
                },
                {
                    "name": "Output",
                    "value": f"`{output_dir}`",
                    "inline": False,
                },
            ],
        )
        return await self.send_raw({"embeds": [embed]})

    async def send_recorder_stats(self, stats: dict) -> bool:
        """Periodic recording stats notification."""
        uptime = self._fmt_uptime(stats.get("uptime_seconds", 0))
        snapshots = self._fmt_number(stats.get("snapshots", 0))
        trades = self._fmt_number(stats.get("trades", 0))
        reconnects = self._fmt_number(stats.get("reconnects", 0))
        disk = self._dir_size_mb(stats.get("output_dir", ""))

        embed = self._embed(
            title="📊 Recorder Stats",
            color=self.COLOR_BLUE,
            fields=[
                {"name": "Uptime", "value": uptime, "inline": True},
                {"name": "Snapshots", "value": snapshots, "inline": True},
                {"name": "Trades", "value": trades, "inline": True},
                {"name": "Reconnects", "value": reconnects, "inline": True},
                {"name": "Disk Usage", "value": disk, "inline": True},
            ],
        )
        return await self.send_raw({"embeds": [embed]})

    async def send_recorder_stopped(
        self, stats: dict, reason: str = "Manual"
    ) -> bool:
        """Recording stopped notification with final summary."""
        uptime = self._fmt_uptime(stats.get("uptime_seconds", 0))
        snapshots = self._fmt_number(stats.get("snapshots", 0))
        trades = self._fmt_number(stats.get("trades", 0))

        embed = self._embed(
            title="⏹️ Recording Stopped",
            color=self.COLOR_ORANGE,
            fields=[
                {"name": "Reason", "value": reason, "inline": True},
                {"name": "Uptime", "value": uptime, "inline": True},
                {"name": "Snapshots", "value": snapshots, "inline": True},
                {"name": "Trades", "value": trades, "inline": True},
            ],
        )
        return await self.send_raw({"embeds": [embed]})

    async def send_recorder_error(
        self, error: str, context: str = ""
    ) -> bool:
        """Recording error notification (rate-limited)."""
        now = time.monotonic()
        if now - self._last_error_sent < _ERROR_RATE_LIMIT_SECONDS:
            logger.debug("Error notification suppressed (rate limit)")
            return False
        self._last_error_sent = now

        embed = self._embed(
            title="❌ Recorder Error",
            color=self.COLOR_RED,
            description=f"```{error}```",
            fields=[],
            footer_extra=context,
        )
        if context:
            embed["fields"].append(
                {"name": "Context", "value": context, "inline": False}
            )
        return await self.send_raw({"embeds": [embed]})

    async def send_recorder_reconnect(
        self, attempt: int, max_attempts: int, error: str
    ) -> bool:
        """WebSocket reconnection attempt notification."""
        embed = self._embed(
            title="🔄 WebSocket Reconnecting",
            color=self.COLOR_ORANGE,
            fields=[
                {
                    "name": "Attempt",
                    "value": f"{attempt}/{max_attempts}",
                    "inline": True,
                },
                {"name": "Error", "value": f"```{error}```", "inline": False},
            ],
        )
        return await self.send_raw({"embeds": [embed]})

    # ------------------------------------------------------------------
    # General MM Bot notifications
    # ------------------------------------------------------------------

    async def send_startup(
        self, symbols: list, exchange: str, config: dict
    ) -> bool:
        """Bot startup with config summary."""
        config_lines = "\n".join(f"{k}: {v}" for k, v in config.items())

        embed = self._embed(
            title="🚀 BotMM Started",
            color=self.COLOR_GREEN,
            fields=[
                {"name": "Exchange", "value": exchange, "inline": True},
                {
                    "name": "Symbols",
                    "value": ", ".join(symbols),
                    "inline": True,
                },
                {
                    "name": "Config",
                    "value": f"```{config_lines}```",
                    "inline": False,
                },
            ],
        )
        return await self.send_raw({"embeds": [embed]})

    async def send_shutdown(
        self, reason: str, metrics: dict = None
    ) -> bool:
        """Bot shutdown with optional final metrics."""
        fields = [{"name": "Reason", "value": reason, "inline": False}]
        if metrics:
            for k, v in metrics.items():
                fields.append({"name": k, "value": str(v), "inline": True})

        embed = self._embed(
            title="🛑 BotMM Stopped",
            color=self.COLOR_RED,
            fields=fields,
        )
        return await self.send_raw({"embeds": [embed]})

    async def send_daily_report(self, metrics: dict) -> bool:
        """Daily P&L report."""
        pnl = metrics.get("pnl", 0.0)
        color = self.COLOR_GREEN if pnl >= 0 else self.COLOR_RED
        pnl_sign = "+" if pnl >= 0 else ""

        volume = self._fmt_number(metrics.get("volume", 0))
        fills = self._fmt_number(metrics.get("fills", 0))
        round_trips = self._fmt_number(metrics.get("round_trips", 0))
        avg_spread = metrics.get("avg_spread_bps", 0.0)
        inv_util = metrics.get("inventory_utilization_pct", 0.0)

        embed = self._embed(
            title="📈 Daily Report",
            color=color,
            fields=[
                {
                    "name": "Net PnL",
                    "value": f"{pnl_sign}{pnl:.2f} USDC",
                    "inline": True,
                },
                {"name": "Volume", "value": f"{volume} USDC", "inline": True},
                {"name": "Fills", "value": fills, "inline": True},
                {"name": "Round Trips", "value": round_trips, "inline": True},
                {
                    "name": "Avg Spread",
                    "value": f"{avg_spread:.1f} bps",
                    "inline": True,
                },
                {
                    "name": "Inventory Util",
                    "value": f"{inv_util:.1f}%",
                    "inline": True,
                },
            ],
        )
        return await self.send_raw({"embeds": [embed]})

    async def send_alert(
        self, title: str, message: str, level: str = "warning"
    ) -> bool:
        """Generic alert (warning=orange, error=red, info=blue)."""
        level_colors = {
            "warning": self.COLOR_ORANGE,
            "error": self.COLOR_RED,
            "info": self.COLOR_BLUE,
        }
        color = level_colors.get(level, self.COLOR_ORANGE)

        embed = self._embed(
            title=f"⚠️ {title}",
            color=color,
            description=message,
            fields=[],
        )
        return await self.send_raw({"embeds": [embed]})
