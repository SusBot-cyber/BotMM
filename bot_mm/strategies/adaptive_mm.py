"""
Adaptive Market Making Strategy — volatility-aware spread and size management.

Extends BasicMMStrategy with:
1. Volatility regime detection (LOW / NORMAL / HIGH) using 3 rolling windows
2. Spread & size adaptation per regime
3. Fill rate tracking (adverse selection detection)
4. Inventory decay (age-based spread widening to incentivize mean-reversion)
"""

import logging
import time
from collections import deque
from enum import Enum
from typing import List

from bot_mm.config import AssetMMConfig
from bot_mm.core.quoter import Quote
from bot_mm.exchanges.base_mm import BaseMMExchange
from bot_mm.strategies.basic_mm import BasicMMStrategy

logger = logging.getLogger(__name__)


class VolRegime(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


# Regime detection thresholds
LOW_VOL_RATIO = 0.7    # short_vol < 0.7 * long_vol → LOW
HIGH_VOL_RATIO = 1.5   # short_vol > 1.5 * long_vol → HIGH

# Spread/size multipliers per regime
REGIME_SPREAD_MULT = {
    VolRegime.LOW: 0.7,
    VolRegime.NORMAL: 1.0,
    VolRegime.HIGH: 1.5,
}
REGIME_SIZE_MULT = {
    VolRegime.LOW: 1.3,
    VolRegime.NORMAL: 1.0,
    VolRegime.HIGH: 0.6,
}

# Fill rate thresholds
FILL_RATE_TOO_LOW = 0.20   # < 20% → spreads too wide, tighten
FILL_RATE_TOO_HIGH = 0.60  # > 60% → adverse selection, widen
FILL_RATE_SPREAD_ADJ = 0.15  # ±15% spread adjustment for fill rate

# Fill tracking window
FILL_TRACK_WINDOW = 100

# Inventory decay
DEFAULT_INVENTORY_DECAY_CANDLES = 30  # Start widening after this many candles
INVENTORY_DECAY_MAX_MULT = 1.4        # Max widen multiplier on stale side


class AdaptiveMMStrategy(BasicMMStrategy):
    """
    Adaptive market making: adjusts spreads and sizes based on
    volatility regime, fill rate, and inventory staleness.
    """

    def __init__(
        self,
        exchange: BaseMMExchange,
        config: AssetMMConfig,
        vol_window_short: int = 5,
        vol_window_medium: int = 20,
        vol_window_long: int = 50,
        inventory_decay_candles: int = DEFAULT_INVENTORY_DECAY_CANDLES,
    ):
        super().__init__(exchange, config)

        # Volatility regime tracking — rolling windows of price returns
        self.vol_window_short = vol_window_short
        self.vol_window_medium = vol_window_medium
        self.vol_window_long = vol_window_long
        self._returns: deque = deque(maxlen=vol_window_long)
        self._regime = VolRegime.NORMAL

        # Fill rate tracking (rolling window of cycles)
        self._fill_events: deque = deque(maxlen=FILL_TRACK_WINDOW)
        self._quotes_placed_count: deque = deque(maxlen=FILL_TRACK_WINDOW)

        # Inventory decay — track when inventory last changed direction
        self._inventory_decay_candles = inventory_decay_candles
        self._inventory_unchanged_count: int = 0
        self._last_inventory_sign: int = 0  # -1, 0, +1

    # ── Volatility regime ───────────────────────────────────

    def _record_return(self, mid_price: float):
        """Record a price return for regime detection."""
        if self._last_mid is not None and self._last_mid > 0:
            ret = (mid_price - self._last_mid) / self._last_mid
            self._returns.append(ret)

    def _calc_rolling_vol(self, window: int) -> float:
        """Standard deviation of recent returns over the given window."""
        n = min(window, len(self._returns))
        if n < 2:
            return 0.0
        recent = list(self._returns)[-n:]
        mean = sum(recent) / n
        var = sum((r - mean) ** 2 for r in recent) / (n - 1)
        return var ** 0.5

    def detect_regime(self) -> VolRegime:
        """Classify volatility regime using short vs long rolling vol."""
        short_vol = self._calc_rolling_vol(self.vol_window_short)
        long_vol = self._calc_rolling_vol(self.vol_window_long)

        if long_vol <= 0:
            self._regime = VolRegime.NORMAL
        elif short_vol < LOW_VOL_RATIO * long_vol:
            self._regime = VolRegime.LOW
        elif short_vol > HIGH_VOL_RATIO * long_vol:
            self._regime = VolRegime.HIGH
        else:
            self._regime = VolRegime.NORMAL

        return self._regime

    @property
    def regime(self) -> VolRegime:
        return self._regime

    @property
    def short_vol(self) -> float:
        return self._calc_rolling_vol(self.vol_window_short)

    @property
    def medium_vol(self) -> float:
        return self._calc_rolling_vol(self.vol_window_medium)

    @property
    def long_vol(self) -> float:
        return self._calc_rolling_vol(self.vol_window_long)

    # ── Fill rate tracking ──────────────────────────────────

    def record_fills(self, num_fills: int, num_quotes: int):
        """Record fill and quote counts for one cycle."""
        self._fill_events.append(num_fills)
        self._quotes_placed_count.append(max(num_quotes, 1))

    @property
    def fill_rate(self) -> float:
        """Rolling fill rate: total fills / total quotes placed."""
        total_fills = sum(self._fill_events)
        total_quotes = sum(self._quotes_placed_count)
        if total_quotes == 0:
            return 0.0
        return total_fills / total_quotes

    def _fill_rate_spread_adj(self) -> float:
        """
        Spread multiplier based on fill rate.
        < 20% → tighten (mult < 1), > 60% → widen (mult > 1).
        """
        rate = self.fill_rate
        if len(self._fill_events) < 5:
            return 1.0  # Not enough data
        if rate < FILL_RATE_TOO_LOW:
            return 1.0 - FILL_RATE_SPREAD_ADJ  # Tighten
        if rate > FILL_RATE_TOO_HIGH:
            return 1.0 + FILL_RATE_SPREAD_ADJ  # Widen
        return 1.0

    # ── Inventory decay ─────────────────────────────────────

    def _update_inventory_age(self, inventory_usd: float):
        """Track how long inventory has been on the same side without reducing."""
        if abs(inventory_usd) < 1e-6:
            self._inventory_unchanged_count = 0
            self._last_inventory_sign = 0
            return

        current_sign = 1 if inventory_usd > 0 else -1
        if current_sign == self._last_inventory_sign:
            self._inventory_unchanged_count += 1
        else:
            self._inventory_unchanged_count = 0
            self._last_inventory_sign = current_sign

    def _inventory_decay_mult(self, side: str, inventory_usd: float) -> float:
        """
        Widen the side that would reduce inventory when position is stale.

        If long for too long → widen asks less / widen bids more
        (actually: widen the reducing side = sell side gets tighter to attract fills,
        and buy side gets wider to discourage adding).

        Returns a spread multiplier for the given side.
        """
        if self._inventory_unchanged_count < self._inventory_decay_candles:
            return 1.0

        decay_progress = min(
            (self._inventory_unchanged_count - self._inventory_decay_candles)
            / self._inventory_decay_candles,
            1.0,
        )
        widen = 1.0 + decay_progress * (INVENTORY_DECAY_MAX_MULT - 1.0)

        # Widen the side that adds to inventory (discourage), keep reducing side normal
        if inventory_usd > 0 and side == "buy":
            return widen  # Long → widen buys (discourage adding)
        if inventory_usd < 0 and side == "sell":
            return widen  # Short → widen sells (discourage adding)

        return 1.0

    # ── Quote adjustment ────────────────────────────────────

    def adjust_quotes(
        self, quotes: List[Quote], mid_price: float, inventory_usd: float
    ) -> List[Quote]:
        """
        Apply adaptive adjustments to raw quotes from QuoteEngine.

        Adjustments applied in order:
        1. Regime-based spread/size scaling
        2. Fill-rate-based spread correction
        3. Inventory decay per-side widening
        """
        regime = self._regime
        regime_spread = REGIME_SPREAD_MULT[regime]
        regime_size = REGIME_SIZE_MULT[regime]
        fill_adj = self._fill_rate_spread_adj()

        adjusted = []
        for q in quotes:
            # Compute target spread offset from mid
            if q.side == "buy":
                raw_offset = mid_price - q.price
            else:
                raw_offset = q.price - mid_price

            # Scale offset by regime + fill rate
            new_offset = raw_offset * regime_spread * fill_adj

            # Inventory decay per-side
            decay = self._inventory_decay_mult(q.side, inventory_usd)
            new_offset *= decay

            # Rebuild price
            if q.side == "buy":
                new_price = mid_price - new_offset
            else:
                new_price = mid_price + new_offset

            # Scale size by regime
            new_size = q.size * regime_size

            adjusted.append(Quote(
                price=new_price,
                size=new_size,
                side=q.side,
                level=q.level,
            ))

        return adjusted

    # ── Main iteration override ─────────────────────────────

    async def run_iteration(self):
        """Execute one adaptive quote cycle."""
        self._iteration += 1

        # 1. Get mid price
        mid_price = await self.exchange.get_mid_price(self.symbol)
        if mid_price <= 0:
            logger.warning("Invalid mid price: %.2f", mid_price)
            return

        # 2. Record return for regime detection
        self._record_return(mid_price)

        # 3. Detect large moves
        if self._last_mid is not None:
            move_pct = (mid_price - self._last_mid) / self._last_mid * 100
            if abs(move_pct) > 0.5:
                self.risk.on_large_move(move_pct)
                logger.warning("Large move detected: %+.2f%%", move_pct)

        # 4. Update volatility (parent's ATR-based)
        self._update_volatility(mid_price)

        # 5. Detect regime
        self.detect_regime()

        # 6. Update unrealized PnL
        self.inventory.update_unrealized(mid_price)

        # 7. Check risk limits
        equity = self.config.capital_usd + self.inventory.total_pnl
        position_usd = abs(self.inventory.state.position_size * mid_price)
        risk_status = self.risk.check_all(
            daily_pnl=self.inventory.total_pnl,
            equity=equity,
            current_vol=self._volatility_pct,
            position_usd=position_usd,
            max_position_usd=self.config.risk.max_position_usd,
        )

        from bot_mm.core.risk import RiskStatus

        if risk_status == RiskStatus.HALT:
            if self.order_mgr.num_active > 0:
                logger.warning("RISK HALT: %s — cancelling orders", self.risk.state.reason)
                await self.order_mgr.cancel_all()
            return

        # 8. Update baseline volatility
        self.risk.update_normal_vol(self._volatility_pct)

        # 9. Generate raw quotes
        inventory_usd = self.inventory.state.position_size * mid_price
        quotes = self.quoter.calculate_quotes(
            mid_price=mid_price,
            volatility_pct=self._volatility_pct,
            inventory_usd=inventory_usd,
            max_position_usd=self.config.risk.max_position_usd,
        )

        # 10. Apply adaptive adjustments
        quotes = self.adjust_quotes(quotes, mid_price, inventory_usd)

        # 11. Track inventory age
        self._update_inventory_age(inventory_usd)

        # 12. Filter paused sides
        filtered = []
        for q in quotes:
            if self.inventory.should_pause_side(q.side, mid_price):
                continue
            filtered.append(q)

        # 13. Emergency spread widening
        if risk_status == RiskStatus.CRITICAL:
            for q in filtered:
                if q.side == "buy":
                    q.price *= (1 - self.config.risk.emergency_spread_mult * 0.0001)
                else:
                    q.price *= (1 + self.config.risk.emergency_spread_mult * 0.0001)

        # 14. Track fills vs quotes for fill rate
        old_fills = self.order_mgr.total_fills
        await self.order_mgr.update_quotes(filtered)
        await self._detect_fills(mid_price)
        new_fills = self.order_mgr.total_fills - old_fills
        self.record_fills(new_fills, len(filtered))

        # Periodic logging with regime info
        if self._iteration % 60 == 0:
            self._log_adaptive_status(mid_price)

        self._last_mid = mid_price

    def _log_adaptive_status(self, mid_price: float):
        """Log status with adaptive strategy details."""
        uptime = time.time() - self._start_time
        logger.info(
            "ADAPTIVE %s | mid=%.2f | regime=%s | vol_s=%.6f vol_l=%.6f | "
            "fill_rate=%.1f%% | inv_age=%d | pos=%.6f ($%.2f) | "
            "pnl=$%.2f | uptime=%.0fs",
            self.symbol, mid_price, self._regime.value,
            self.short_vol, self.long_vol,
            self.fill_rate * 100, self._inventory_unchanged_count,
            self.inventory.state.position_size,
            abs(self.inventory.state.position_size * mid_price),
            self.inventory.total_pnl, uptime,
        )
