"""
Market Making Bot Configuration.

Supports per-asset configuration for multi-exchange market making.
"""

import os
from dataclasses import dataclass, field
from typing import Dict, Optional
from enum import Enum
from pathlib import Path
from dotenv import load_dotenv

_env_path = Path(__file__).parent / '.env'
if _env_path.exists():
    load_dotenv(_env_path)


class Exchange(str, Enum):
    HYPERLIQUID = "hyperliquid"
    BINANCE = "binance"
    BYBIT = "bybit"


@dataclass
class QuoteParams:
    """Parameters for the quote engine."""
    base_spread_bps: float = 2.0        # Minimum spread in basis points
    vol_multiplier: float = 1.5         # Spread widens with volatility
    inventory_skew_factor: float = 0.5  # How much inventory skews quotes
    max_spread_bps: float = 20.0        # Cap spread width
    min_spread_bps: float = 0.5         # Floor spread width
    order_size_usd: float = 100.0       # Size per side in USD
    num_levels: int = 1                 # Quote levels per side
    level_spacing_bps: float = 1.0      # Spacing between levels
    quote_refresh_ms: int = 1000        # How often to refresh quotes


@dataclass
class RiskLimits:
    """Risk management limits."""
    max_position_usd: float = 500.0         # Max inventory per asset
    max_total_position_usd: float = 2000.0  # Max across all assets
    max_daily_loss_usd: float = 50.0        # Stop trading for the day
    max_drawdown_pct: float = 5.0           # % of capital
    volatility_pause_mult: float = 3.0      # Pause if vol > X× normal
    max_orders_per_minute: int = 60
    emergency_spread_mult: float = 3.0      # Widen spread in crisis


@dataclass
class DirectionalBiasParams:
    """Kalman+QQE directional bias for quote skewing."""
    enabled: bool = False
    kalman_process_noise: float = 0.005
    kalman_measurement_noise: float = 0.1
    qqe_rsi_period: int = 14
    qqe_smoothing: int = 5
    qqe_factor: float = 3.5
    slope_window: int = 5
    bias_strength: float = 0.5  # 0-1, how much bias affects quotes


@dataclass
class AssetMMConfig:
    """Configuration for a single MM asset."""
    symbol: str
    exchange: Exchange = Exchange.HYPERLIQUID
    enabled: bool = True

    # Capital
    capital_usd: float = 1000.0

    # Fees (HL defaults)
    maker_fee: float = -0.00015  # Negative = rebate
    taker_fee: float = 0.00045

    # Quote params
    quote: QuoteParams = field(default_factory=QuoteParams)

    # Risk
    risk: RiskLimits = field(default_factory=RiskLimits)

    # Directional bias
    bias: DirectionalBiasParams = field(default_factory=DirectionalBiasParams)


@dataclass
class MMBotConfig:
    """Main bot configuration."""
    assets: Dict[str, AssetMMConfig] = field(default_factory=dict)

    # Exchange credentials
    hl_private_key: str = ""
    hl_wallet_address: str = ""
    hl_mode: str = "testnet"

    # Notifications
    discord_webhook_url: str = ""

    # Logging
    log_level: str = "INFO"

    @classmethod
    def load(cls) -> "MMBotConfig":
        """Load config from environment."""
        config = cls(
            hl_private_key=os.getenv("HL_PRIVATE_KEY", ""),
            hl_wallet_address=os.getenv("HL_WALLET_ADDRESS", ""),
            hl_mode=os.getenv("HL_MODE", "testnet"),
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", ""),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )

        symbols = os.getenv("MM_SYMBOLS", "BTCUSDT").split(",")
        for sym in symbols:
            sym = sym.strip()
            prefix = sym.upper()

            def gf(key, default):
                return float(os.getenv(f"{prefix}_{key}", str(default)))

            quote = QuoteParams(
                base_spread_bps=gf("SPREAD_BPS", 2.0),
                vol_multiplier=gf("VOL_MULT", 1.5),
                inventory_skew_factor=gf("SKEW_FACTOR", 0.5),
                max_spread_bps=gf("MAX_SPREAD_BPS", 20.0),
                min_spread_bps=gf("MIN_SPREAD_BPS", 0.5),
                order_size_usd=gf("ORDER_SIZE_USD", 100.0),
                num_levels=int(gf("NUM_LEVELS", 1)),
                level_spacing_bps=gf("LEVEL_SPACING_BPS", 1.0),
                quote_refresh_ms=int(gf("REFRESH_MS", 1000)),
            )

            risk = RiskLimits(
                max_position_usd=gf("MAX_POS_USD", 500.0),
                max_daily_loss_usd=gf("MAX_DAILY_LOSS", 50.0),
                max_drawdown_pct=gf("MAX_DD_PCT", 5.0),
            )

            exchange_name = os.getenv(f"{prefix}_EXCHANGE", "hyperliquid")
            maker = gf("MAKER_FEE", -0.00015)
            taker = gf("TAKER_FEE", 0.00045)

            config.assets[sym] = AssetMMConfig(
                symbol=sym,
                exchange=Exchange(exchange_name),
                enabled=os.getenv(f"{prefix}_ENABLED", "true").lower() == "true",
                capital_usd=gf("CAPITAL_USD", 1000.0),
                maker_fee=maker,
                taker_fee=taker,
                quote=quote,
                risk=risk,
                bias=DirectionalBiasParams(
                    enabled=os.getenv(f"{prefix}_BIAS_ENABLED", "false").lower() == "true",
                    kalman_process_noise=gf("BIAS_KALMAN_PROCESS_NOISE", 0.005),
                    kalman_measurement_noise=gf("BIAS_KALMAN_MEASUREMENT_NOISE", 0.1),
                    qqe_rsi_period=int(gf("BIAS_QQE_RSI_PERIOD", 14)),
                    qqe_smoothing=int(gf("BIAS_QQE_SMOOTHING", 5)),
                    qqe_factor=gf("BIAS_QQE_FACTOR", 3.5),
                    slope_window=int(gf("BIAS_SLOPE_WINDOW", 5)),
                    bias_strength=gf("BIAS_STRENGTH", 0.5),
                ),
            )

        return config
