"""PnL and performance metrics tracker for BotMM."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict

import numpy as np


@dataclass
class _DailyBucket:
    """Single day's metrics."""
    pnl: float = 0.0
    fees: float = 0.0
    volume: float = 0.0
    fills: int = 0
    date: str = ""


@dataclass
class MetricsTracker:
    """Tracks MM bot PnL, fills, spread capture, and inventory usage."""

    # Cumulative
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_fees: float = 0.0
    total_volume: float = 0.0
    buy_fills: int = 0
    sell_fills: int = 0
    round_trips: int = 0

    # Spread capture (running average)
    _spread_sum: float = field(default=0.0, repr=False)
    _spread_count: int = field(default=0, repr=False)

    # Inventory utilization samples
    _inv_samples: list = field(default_factory=list, repr=False)
    max_position_usd: float = 500.0

    # Daily tracking
    _daily: _DailyBucket = field(default_factory=_DailyBucket, repr=False)
    _daily_history: Dict[str, _DailyBucket] = field(default_factory=dict, repr=False)

    _start_ts: float = field(default_factory=time.time, repr=False)

    # --- Recording methods ---

    def record_fill(self, side: str, price: float, size_usd: float,
                    fee: float, spread_bps: float | None = None) -> None:
        """Record a single fill event.

        Args:
            side: 'buy' or 'sell'.
            price: Fill price.
            size_usd: Notional USD value.
            fee: Fee amount (negative = rebate).
            spread_bps: Spread captured in bps (if known).
        """
        if side == "buy":
            self.buy_fills += 1
        else:
            self.sell_fills += 1

        self.total_fees += fee
        self.total_volume += size_usd

        self._daily.fees += fee
        self._daily.volume += size_usd
        self._daily.fills += 1

        if spread_bps is not None:
            self._spread_sum += spread_bps
            self._spread_count += 1

    def record_round_trip(self, pnl: float) -> None:
        """Record a completed buy+sell round trip."""
        self.round_trips += 1
        self.realized_pnl += pnl
        self._daily.pnl += pnl

    def update_unrealized(self, unrealized: float) -> None:
        """Update current unrealized PnL from open inventory."""
        self.unrealized_pnl = unrealized

    def sample_inventory(self, position_usd: float) -> None:
        """Record an inventory utilization sample."""
        if self.max_position_usd > 0:
            util = min(abs(position_usd) / self.max_position_usd, 1.0)
            self._inv_samples.append(util)

    # --- Aggregated properties ---

    @property
    def total_fills(self) -> int:
        return self.buy_fills + self.sell_fills

    @property
    def net_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def avg_spread_bps(self) -> float:
        return self._spread_sum / self._spread_count if self._spread_count else 0.0

    @property
    def inventory_utilization(self) -> float:
        """Average inventory utilization as fraction 0-1."""
        if not self._inv_samples:
            return 0.0
        return float(np.mean(self._inv_samples))

    @property
    def uptime_hours(self) -> float:
        return (time.time() - self._start_ts) / 3600

    # --- Summary / export ---

    def get_summary(self) -> dict:
        """Return all metrics as a flat dict."""
        return {
            "realized_pnl": round(self.realized_pnl, 4),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "net_pnl": round(self.net_pnl, 4),
            "total_fees": round(self.total_fees, 4),
            "total_volume": round(self.total_volume, 2),
            "buy_fills": self.buy_fills,
            "sell_fills": self.sell_fills,
            "total_fills": self.total_fills,
            "round_trips": self.round_trips,
            "avg_spread_bps": round(self.avg_spread_bps, 2),
            "inventory_utilization": round(self.inventory_utilization, 4),
            "uptime_hours": round(self.uptime_hours, 2),
            "daily_pnl": round(self._daily.pnl, 4),
            "daily_volume": round(self._daily.volume, 2),
            "daily_fills": self._daily.fills,
        }

    def log_metrics(self, logger: logging.Logger) -> None:
        """Print formatted summary to logger."""
        s = self.get_summary()
        logger.info("=" * 50)
        logger.info("METRICS SUMMARY")
        logger.info("=" * 50)
        logger.info(f"  Net PnL:       ${s['net_pnl']:+.4f}  "
                     f"(realized: ${s['realized_pnl']:+.4f}, "
                     f"unrealized: ${s['unrealized_pnl']:+.4f})")
        logger.info(f"  Fees:          ${s['total_fees']:+.4f}")
        logger.info(f"  Volume:        ${s['total_volume']:,.2f}")
        logger.info(f"  Fills:         {s['total_fills']}  "
                     f"(buy: {s['buy_fills']}, sell: {s['sell_fills']})")
        logger.info(f"  Round trips:   {s['round_trips']}")
        logger.info(f"  Avg spread:    {s['avg_spread_bps']:.2f} bps")
        logger.info(f"  Inv. util:     {s['inventory_utilization']:.1%}")
        logger.info(f"  Uptime:        {s['uptime_hours']:.1f}h")
        logger.info(f"  Today:         ${s['daily_pnl']:+.4f} PnL, "
                     f"{s['daily_fills']} fills, "
                     f"${s['daily_volume']:,.2f} vol")
        logger.info("=" * 50)

    # --- Daily reset ---

    def reset_daily(self) -> None:
        """Archive today's counters and start fresh."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._daily.date = today
        self._daily_history[today] = self._daily
        self._daily = _DailyBucket()

    def get_daily_history(self) -> Dict[str, dict]:
        """Return archived daily buckets as dicts."""
        return {
            date: {
                "pnl": round(b.pnl, 4),
                "fees": round(b.fees, 4),
                "volume": round(b.volume, 2),
                "fills": b.fills,
            }
            for date, b in self._daily_history.items()
        }
