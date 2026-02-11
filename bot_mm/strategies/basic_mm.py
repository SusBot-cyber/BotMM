"""
Basic Market Making Strategy — simple spread capture.

Loop (every ~1s):
1. Get mid price from exchange
2. Calculate volatility (simple rolling std)
3. Generate quotes via QuoteEngine
4. Check risk limits
5. Update orders on exchange
6. Track fills and inventory
"""

import asyncio
import logging
import time
from collections import deque
from typing import Optional

from bot_mm.config import AssetMMConfig
from bot_mm.core.inventory import InventoryManager
from bot_mm.core.order_manager import OrderManager
from bot_mm.core.quoter import QuoteEngine
from bot_mm.core.risk import RiskManager, RiskStatus
from bot_mm.exchanges.base_mm import BaseMMExchange

logger = logging.getLogger(__name__)

# Rolling window for volatility estimation
VOL_WINDOW = 20


class BasicMMStrategy:
    """
    Simple market making: place bid+ask around mid price.

    Uses QuoteEngine for spread/skew calculation, InventoryManager
    for position tracking, and RiskManager for circuit breakers.
    """

    def __init__(
        self,
        exchange: BaseMMExchange,
        config: AssetMMConfig,
    ):
        self.exchange = exchange
        self.config = config
        self.symbol = config.symbol

        # Core components
        self.quoter = QuoteEngine(config.quote)
        self.inventory = InventoryManager(
            symbol=self.symbol,
            max_position_usd=config.risk.max_position_usd,
        )
        self.risk = RiskManager(
            max_daily_loss_usd=config.risk.max_daily_loss_usd,
            max_drawdown_pct=config.risk.max_drawdown_pct,
            volatility_pause_mult=config.risk.volatility_pause_mult,
            capital_usd=config.capital_usd,
        )
        self.order_mgr = OrderManager(
            exchange=exchange,
            symbol=self.symbol,
            on_fill=self._handle_fill,
        )

        # Volatility estimation (rolling high-low range as ATR proxy)
        self._price_highs: deque = deque(maxlen=VOL_WINDOW)
        self._price_lows: deque = deque(maxlen=VOL_WINDOW)
        self._last_mid: Optional[float] = None
        self._volatility_pct: float = 0.001  # Default 0.1%

        # State
        self._running = False
        self._iteration = 0
        self._start_time: float = 0.0

        # Directional bias (Kalman+QQE)
        self._bias = None
        self._current_bias: float = 0.0
        self._last_hour: Optional[int] = None

        # Toxicity detector
        self._toxicity = None
        if getattr(config, 'use_toxicity', False):
            from bot_mm.ml.toxicity import ToxicityDetector
            self._toxicity = ToxicityDetector()

        if config.bias.enabled:
            from bot_mm.core.signals import DirectionalBias
            self._bias = DirectionalBias(
                kalman_process_noise=config.bias.kalman_process_noise,
                kalman_measurement_noise=config.bias.kalman_measurement_noise,
                qqe_rsi_period=config.bias.qqe_rsi_period,
                qqe_smoothing=config.bias.qqe_smoothing,
                qqe_factor=config.bias.qqe_factor,
                slope_window=config.bias.slope_window,
                bias_strength=config.bias.bias_strength,
            )

    async def start(self):
        """Main loop — runs until stop() is called."""
        self._running = True
        self._start_time = time.time()
        interval_s = self.config.quote.quote_refresh_ms / 1000.0

        logger.info(
            "Starting BasicMM for %s | spread=%.1f bps | size=$%.0f | interval=%.1fs",
            self.symbol, self.config.quote.base_spread_bps,
            self.config.quote.order_size_usd, interval_s,
        )

        try:
            while self._running:
                cycle_start = time.monotonic()
                try:
                    await self.run_iteration()
                except Exception:
                    logger.exception("Iteration %d failed", self._iteration)
                    self.risk.on_api_error()

                elapsed = time.monotonic() - cycle_start
                sleep_time = max(0, interval_s - elapsed)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
        finally:
            await self._shutdown()

    async def stop(self):
        """Signal the strategy to stop."""
        logger.info("Stopping BasicMM for %s...", self.symbol)
        self._running = False

    async def _shutdown(self):
        """Cancel all orders and log final stats."""
        logger.info("Shutting down — cancelling all orders for %s", self.symbol)
        await self.order_mgr.cancel_all()
        self._log_summary()

    async def run_iteration(self):
        """Execute one quote cycle."""
        self._iteration += 1

        # 1. Get mid price
        mid_price = await self.exchange.get_mid_price(self.symbol)
        if mid_price <= 0:
            logger.warning("Invalid mid price: %.2f", mid_price)
            return

        # 2. Detect large moves
        if self._last_mid is not None:
            move_pct = (mid_price - self._last_mid) / self._last_mid * 100
            if abs(move_pct) > 0.5:
                self.risk.on_large_move(move_pct)
                logger.warning("Large move detected: %+.2f%%", move_pct)

        # 3. Update volatility
        self._update_volatility(mid_price)

        # 4. Update unrealized PnL
        self.inventory.update_unrealized(mid_price)

        # 5. Check risk limits
        equity = self.config.capital_usd + self.inventory.total_pnl
        position_usd = abs(self.inventory.state.position_size * mid_price)
        risk_status = self.risk.check_all(
            daily_pnl=self.inventory.total_pnl,
            equity=equity,
            current_vol=self._volatility_pct,
            position_usd=position_usd,
            max_position_usd=self.config.risk.max_position_usd,
        )

        if risk_status == RiskStatus.HALT:
            if self.order_mgr.num_active > 0:
                logger.warning("RISK HALT: %s — cancelling orders", self.risk.state.reason)
                await self.order_mgr.cancel_all()
            return

        # 6. Update baseline volatility
        self.risk.update_normal_vol(self._volatility_pct)

        # 6b. Update directional bias on hourly boundary
        if self._bias is not None:
            import datetime
            current_hour = datetime.datetime.utcnow().hour
            if self._last_hour is not None and current_hour != self._last_hour:
                result = self._bias.update(mid_price)
                if result is not None:
                    self._current_bias = result.bias
                # Update toxicity on hourly bar
                if self._toxicity is not None:
                    self._toxicity.on_bar(mid_price)
            self._last_hour = current_hour
        elif self._toxicity is not None:
            import datetime
            current_hour = datetime.datetime.utcnow().hour
            if self._last_hour is not None and current_hour != self._last_hour:
                self._toxicity.on_bar(mid_price)
            self._last_hour = current_hour

        # 7. Generate quotes
        inventory_usd = self.inventory.state.position_size * mid_price
        quotes = self.quoter.calculate_quotes(
            mid_price=mid_price,
            volatility_pct=self._volatility_pct,
            inventory_usd=inventory_usd,
            max_position_usd=self.config.risk.max_position_usd,
            directional_bias=self._current_bias,
        )

        # 8. Filter out sides that should be paused
        filtered = []
        for q in quotes:
            if self.inventory.should_pause_side(q.side, mid_price):
                continue
            filtered.append(q)

        # 8b. Toxicity-based spread adjustment
        if self._toxicity is not None and self._toxicity.fills_measured > 10:
            buy_mult, sell_mult = self._toxicity.get_side_multipliers()
            for q in filtered:
                if q.side == "buy":
                    q.price = mid_price - (mid_price - q.price) * buy_mult
                else:
                    q.price = mid_price + (q.price - mid_price) * sell_mult

        # 9. Widen spread if risk is elevated
        if risk_status == RiskStatus.CRITICAL:
            for q in filtered:
                if q.side == "buy":
                    q.price *= (1 - self.config.risk.emergency_spread_mult * 0.0001)
                else:
                    q.price *= (1 + self.config.risk.emergency_spread_mult * 0.0001)

        # 10. Update orders on exchange
        await self.order_mgr.update_quotes(filtered)

        # 11. Detect fills via position change
        await self._detect_fills(mid_price)

        # Periodic logging
        if self._iteration % 60 == 0:
            self._log_status(mid_price)

        self._last_mid = mid_price

    def _update_volatility(self, mid_price: float):
        """
        Estimate volatility using rolling high-low range (ATR proxy).

        Uses the spread between recent price extremes as a fraction
        of mid price. With only mid prices available, we approximate
        high/low using price movement between ticks.
        """
        if self._last_mid is not None:
            tick_high = max(mid_price, self._last_mid)
            tick_low = min(mid_price, self._last_mid)
            self._price_highs.append(tick_high)
            self._price_lows.append(tick_low)

        if len(self._price_highs) >= 3:
            # Average true range proxy: mean(high - low) / mid
            ranges = [h - l for h, l in zip(self._price_highs, self._price_lows)]
            avg_range = sum(ranges) / len(ranges)
            self._volatility_pct = max(avg_range / mid_price, 0.0001)

    async def _detect_fills(self, current_price: float):
        """
        Detect fills by comparing exchange position with local inventory.

        If position changed, infer fill direction and update inventory.
        """
        try:
            pos_data = await self.exchange.get_position(self.symbol)
        except Exception:
            return

        exchange_size = pos_data.get("size", 0.0)
        exchange_side = pos_data.get("side", "none")
        exchange_signed = exchange_size if exchange_side == "long" else -exchange_size if exchange_side == "short" else 0.0

        local_signed = self.inventory.state.position_size
        diff = exchange_signed - local_signed

        if abs(diff) < 1e-10:
            return

        # Infer fill
        side = "buy" if diff > 0 else "sell"
        size = abs(diff)
        fee = size * current_price * self.config.maker_fee  # Assume maker

        realized = self.inventory.on_fill(side, current_price, size, fee)

        # Record fill for toxicity tracking
        if self._toxicity is not None:
            self._toxicity.on_fill(side, current_price, current_price, size)

        logger.info(
            "FILL %s | %s %.6f @ %.2f | fee=%.4f | realized=$%.2f | pos=%.6f | net_pnl=$%.2f",
            self.symbol, side.upper(), size, current_price, fee,
            realized, self.inventory.state.position_size, self.inventory.net_pnl,
        )

    def _handle_fill(self, oid: str, side: str, price: float, size: float, fee: float):
        """Callback from OrderManager on fill events."""
        logger.debug("OrderManager fill: oid=%s %s %.6f @ %.2f", oid, side, size, price)

    def _log_status(self, mid_price: float):
        """Log periodic status update."""
        uptime = time.time() - self._start_time
        bias_str = ""
        if self._bias is not None:
            r = self._bias.last_result
            regime = r.regime.name if r else "WARMUP"
            bias_str = f" | bias={self._current_bias:+.3f} ({regime})"
        tox_str = ""
        if self._toxicity is not None and self._toxicity.fills_measured > 0:
            s = self._toxicity.summary()
            tox_str = f" | tox={s['avg_toxicity']:.3f} ({s['fills_measured']} fills)"
        logger.info(
            "STATUS %s | mid=%.2f | vol=%.4f%% | pos=%.6f ($%.2f) | "
            "pnl=$%.2f (realized=$%.2f) | %s%s%s | uptime=%.0fs",
            self.symbol, mid_price, self._volatility_pct * 100,
            self.inventory.state.position_size,
            abs(self.inventory.state.position_size * mid_price),
            self.inventory.total_pnl, self.inventory.net_pnl,
            self.order_mgr.stats_str, bias_str, tox_str, uptime,
        )

    def _log_summary(self):
        """Log final session summary."""
        uptime = time.time() - self._start_time
        s = self.inventory.state
        logger.info(
            "\n╔══════════════════════════════════════════╗\n"
            "║  SESSION SUMMARY — %s\n"
            "╠══════════════════════════════════════════╣\n"
            "║  Duration:    %.0f seconds\n"
            "║  Iterations:  %d\n"
            "║  Fills:       %d buys, %d sells\n"
            "║  Round trips: %d\n"
            "║  Volume:      $%.2f\n"
            "║  Realized:    $%.4f\n"
            "║  Fees:        $%.4f\n"
            "║  Net PnL:     $%.4f\n"
            "║  Final pos:   %.6f\n"
            "║  %s\n"
            "╚══════════════════════════════════════════╝",
            self.symbol, uptime, self._iteration,
            s.num_buys, s.num_sells, s.round_trips,
            s.volume_traded_usd, s.realized_pnl, s.total_fees,
            self.inventory.net_pnl, s.position_size,
            self.order_mgr.stats_str,
        )
