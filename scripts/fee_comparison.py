"""Compare backtest results with rebate vs real cost fees."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from backtest.mm_backtester import MMBacktester, load_candles_csv
from bot_mm.config import QuoteParams

data_dir = Path(__file__).parent.parent / "data" / "cache"
assets = {
    "BTCUSDT": {"spread": 2.0, "skew": 0.3, "size": 150, "bias": True, "bias_str": 0.2},
    "ETHUSDT": {"spread": 1.5, "skew": 0.3, "size": 150, "bias": True, "bias_str": 0.2},
    "SOLUSDT": {"spread": 1.5, "skew": 0.5, "size": 150, "bias": False, "bias_str": 0.0},
    "XRPUSDT": {"spread": 1.5, "skew": 0.5, "size": 150, "bias": True, "bias_str": 0.2},
}

capital = 12500
scale = capital / 1000.0

for fee_label, maker_fee in [("REBATE -0.015%", -0.00015), ("COST +0.015%", 0.00015), ("ZERO 0%", 0.0)]:
    print(f"=== {fee_label} ===")
    total_pnl = 0
    total_fees = 0
    total_gross = 0
    for sym, p in assets.items():
        candles = load_candles_csv(str(data_dir / f"{sym}_1h.csv"), 225)
        qp = QuoteParams(
            base_spread_bps=p["spread"], vol_multiplier=1.5,
            inventory_skew_factor=p["skew"], order_size_usd=p["size"] * scale, num_levels=2
        )
        bt = MMBacktester(
            quote_params=qp, maker_fee=maker_fee, taker_fee=0.00045,
            max_position_usd=capital * 0.5, max_daily_loss=capital * 0.05,
            capital=capital, use_bias=p["bias"], bias_strength=p["bias_str"],
            use_toxicity=True, use_auto_tune=True
        )
        r = bt.run(candles, sym)
        short = sym.replace("USDT", "")
        print(f"  {short}: Net=${r.net_pnl:>8,.0f}  Gross=${r.gross_pnl:>8,.0f}  Fees=${r.total_fees:>8,.0f}  Fills={r.total_fills}")
        total_pnl += r.net_pnl
        total_fees += r.total_fees
        total_gross += r.gross_pnl
    print(f"  ---")
    print(f"  TOTAL: Net=${total_pnl:>8,.0f}  Gross=${total_gross:>8,.0f}  Fees=${total_fees:>8,.0f}")
    print(f"  Return on $50K: {total_pnl/50000*100:.1f}%")
    print()
