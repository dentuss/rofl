"""Live↔backtest parity check for the sentiment-filtered Donchian strategy.

Walks the live signal generator forward bar-by-bar over historical data and
compares its signal to the backtest's signal. They must match.
"""
from __future__ import annotations

import pandas as pd

from backtest import BTConfig, run_backtest
from data import fetch_ohlcv
from sentiment import fetch_fear_greed
from strategies_sentiment import donchian_skip_fear

CHOSEN = dict(entry_n=20, exit_n=10, adx_n=14, adx_min=20.0,
              atr_n=14, sl_mult=2.5, tp_mult=5.0)
FEAR_THRESHOLD = 25.0


def main():
    pair, tf, days = "ETH-USDT", "1h", 90
    df = fetch_ohlcv(pair, tf, days=days)
    fng = fetch_fear_greed()
    full = donchian_skip_fear(df, fng, fear_min=FEAR_THRESHOLD, **CHOSEN)

    parity_diffs = 0
    sl_diffs = 0
    warmup = 60
    for i in range(warmup, len(df)):
        window = df.iloc[: i + 1]
        live_sig = donchian_skip_fear(window, fng, fear_min=FEAR_THRESHOLD,
                                      **CHOSEN).iloc[-1]
        ref = full.iloc[i]
        if int(live_sig["signal"]) != int(ref["signal"]):
            parity_diffs += 1
        if pd.notna(live_sig["sl"]) and pd.notna(ref["sl"]):
            if abs(live_sig["sl"] - ref["sl"]) / ref["sl"] > 0.001:
                sl_diffs += 1

    bt = run_backtest(df, full, BTConfig())
    print(f"Parity check (Donchian + skip_fear) on {pair} {tf} {days}d:")
    print(f"  bars compared: {len(df) - warmup}")
    print(f"  signal mismatches: {parity_diffs}")
    print(f"  sl mismatches:     {sl_diffs}")
    print(f"  backtest stats: {bt.stats()}")
    assert parity_diffs == 0, "live signal differs from backtest!"
    assert sl_diffs == 0, "live sl differs from backtest!"
    print("PARITY OK")


if __name__ == "__main__":
    main()
