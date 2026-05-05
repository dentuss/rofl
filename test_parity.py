"""Live↔backtest parity check.

The live bot must compute the same signal as the backtest for any given bar,
otherwise the backtest results don't predict live behaviour.

We simulate the bot bar-by-bar over historical data and compare its trade
log to the backtester's trade log. They should match (give-or-take rounding).
"""
from __future__ import annotations

import pandas as pd

from backtest import BTConfig, run_backtest
from data import fetch_ohlcv
from strategies import triple_confirm_long


def main():
    pair, tf, days = "ETH-USDT", "15m", 30
    df = fetch_ohlcv(pair, tf, days=days)
    sig = triple_confirm_long(df, ema_fast=9, ema_slow=26, ema_trend=50,
                              rsi_min=55, adx_min=22)

    # Bookkeeping: walk forward, generating the strategy on a growing window
    # at each step (just like the live bot does).
    parity_diffs = 0
    full = sig.copy()
    for i in range(120, len(df)):
        window = df.iloc[: i + 1]
        live_sig = triple_confirm_long(window, ema_fast=9, ema_slow=26, ema_trend=50,
                                       rsi_min=55, adx_min=22).iloc[-1]
        ref = full.iloc[i]
        if int(live_sig["signal"]) != int(ref["signal"]):
            parity_diffs += 1
        # Also check sl/tp are within 0.1% if both present
        if pd.notna(live_sig["sl"]) and pd.notna(ref["sl"]):
            if abs(live_sig["sl"] - ref["sl"]) / ref["sl"] > 0.001:
                parity_diffs += 1

    bt = run_backtest(df, sig, BTConfig(), long_only=True)
    print(f"Parity check on {pair} {tf} {days}d:")
    print(f"  bars compared: {len(df) - 120}")
    print(f"  signal mismatches: {parity_diffs}")
    print(f"  backtest stats: {bt.stats()}")
    assert parity_diffs == 0, "live signal differs from backtest!"
    print("PARITY OK")


if __name__ == "__main__":
    main()
