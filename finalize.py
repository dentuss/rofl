"""Final validation of the chosen strategy.

Winner: triple_confirm_long with (ema_fast=9, ema_slow=26, ema_trend=50,
rsi_min=55, adx_min=22). Long-only, 1.5% risk/trade, 5x max leverage.
On ETH-USDT 15m, all parameter variants in our sweep were profitable over
60 days, which is strong evidence of a genuine edge, not curve-fitting.

This script:
  - Confirms 30-day and 60-day windows
  - Cross-asset sanity check (BTC, SOL on 15m)
  - Stress-tests with worse fees / higher slippage
  - Reports per-trade detail for the chosen config
"""
from __future__ import annotations

import pandas as pd

from backtest import BTConfig, run_backtest
from data import fetch_ohlcv
from strategies import triple_confirm_long

CHOSEN = dict(ema_fast=9, ema_slow=26, ema_trend=50, rsi_min=55.0, adx_min=22.0,
              atr_n=14, sl_mult=1.8, tp_mult=3.0)


def run_one(pair, tf, days, cfg=BTConfig()):
    df = fetch_ohlcv(pair, tf, days=days)
    sig = triple_confirm_long(df, **CHOSEN)
    res = run_backtest(df, sig, cfg, long_only=True)
    return res, df


if __name__ == "__main__":
    pd.set_option("display.width", 200)

    print("=" * 80)
    print("CHOSEN STRATEGY:  triple_confirm_long  (long-only)")
    print(f"PARAMS: {CHOSEN}")
    print("=" * 80)

    print("\n--- ETH-USDT 15m, multiple horizons ---")
    for days in (15, 30, 60):
        res, _ = run_one("ETH-USDT", "15m", days)
        print(f"  {days:2d}d:", res.stats())

    print("\n--- Cross-asset sanity (15m, 30d) ---")
    for pair in ("ETH-USDT", "BTC-USDT", "SOL-USDT"):
        res, _ = run_one(pair, "15m", 30)
        print(f"  {pair}:", res.stats())

    print("\n--- Stress test: fee 0.10% + slip 5bp ---")
    stress = BTConfig(fee_rate=0.001, slip_bps=5.0)
    res, _ = run_one("ETH-USDT", "15m", 30, stress)
    print(f"  ETH 15m 30d:", res.stats())
    res, _ = run_one("ETH-USDT", "15m", 60, stress)
    print(f"  ETH 15m 60d:", res.stats())

    print("\n--- Last 5 trades on ETH-USDT 15m, 30d ---")
    res, _ = run_one("ETH-USDT", "15m", 30)
    rows = []
    for t in res.trades[-5:]:
        rows.append({
            "entry": t.entry_time, "exit": t.exit_time,
            "side": t.side, "entry_px": round(t.entry_px, 2),
            "exit_px": round(t.exit_px, 2),
            "notional": round(t.notional, 2),
            "pnl": round(t.pnl, 3),
            "reason": t.reason, "bars": t.bars_held,
        })
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n--- Equity curve summary (30d) ---")
    eq = res.equity_curve
    print(f"  start:  {eq.iloc[0]:.2f}")
    print(f"  peak:   {eq.cummax().iloc[-1]:.2f}")
    print(f"  end:    {eq.iloc[-1]:.2f}")
    print(f"  return: {(eq.iloc[-1]/eq.iloc[0]-1)*100:.2f}%")
    print(f"  mdd:    {((eq/eq.cummax())-1).min()*100:.2f}%")
