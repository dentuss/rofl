"""Live↔backtest parity check for the Donchian-breakout strategy.

Walks the live signal generator forward bar-by-bar over historical data and
compares its signal to the backtest's signal. They must match.
"""
from __future__ import annotations

import pandas as pd

from backtest import BTConfig, run_backtest
from data import fetch_ohlcv
from strategies import donchian_breakout

CHOSEN = dict(entry_n=20, exit_n=10, adx_n=14, adx_min=20.0,
              atr_n=14, sl_mult=2.5, tp_mult=5.0)


def main():
    pair, tf, days = "ETH-USDT", "1h", 90
    df = fetch_ohlcv(pair, tf, days=days)
    full = donchian_breakout(df, **CHOSEN)

    parity_diffs = 0
    sl_diffs = 0
    warmup = 60
    for i in range(warmup, len(df)):
        window = df.iloc[: i + 1]
        live_sig = donchian_breakout(window, **CHOSEN).iloc[-1]
        ref = full.iloc[i]
        if int(live_sig["signal"]) != int(ref["signal"]):
            parity_diffs += 1
        if pd.notna(live_sig["sl"]) and pd.notna(ref["sl"]):
            if abs(live_sig["sl"] - ref["sl"]) / ref["sl"] > 0.001:
                sl_diffs += 1

    bt = run_backtest(df, full, BTConfig())
    print(f"Parity check on {pair} {tf} {days}d:")
    print(f"  bars compared: {len(df) - warmup}")
    print(f"  signal mismatches: {parity_diffs}")
    print(f"  sl mismatches:     {sl_diffs}")
    print(f"  backtest stats: {bt.stats()}")
    assert parity_diffs == 0, "live signal differs from backtest!"
    assert sl_diffs == 0, "live sl differs from backtest!"
    print("PARITY OK")


if __name__ == "__main__":
    main()
