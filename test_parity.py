"""Live↔backtest parity check for triple_long + HTF filter (new default).

Walks the live signal generator forward bar-by-bar over historical data and
compares its signal to the backtest's signal. They must match.
"""
from __future__ import annotations

import pandas as pd

from backtest import BTConfig, run_backtest
from data import fetch_ohlcv
from strategies import triple_confirm_long
from strategies_enhanced import with_htf_trend_filter

BASE = dict(ema_fast=9, ema_slow=26, ema_trend=50,
            rsi_min=55.0, adx_min=22.0,
            atr_n=14, sl_mult=1.8, tp_mult=3.0)


def winner_signal(df):
    s = triple_confirm_long(df, **BASE)
    return with_htf_trend_filter(df, s, htf_rule="1D", ema_n=50)


def main():
    pair, tf, days = "ETH-USDT", "1h", 90
    df = fetch_ohlcv(pair, tf, days=days)
    full = winner_signal(df)

    parity_diffs = 0
    sl_diffs = 0
    warmup = 60
    for i in range(warmup, len(df)):
        window = df.iloc[: i + 1]
        live_sig = winner_signal(window).iloc[-1]
        ref = full.iloc[i]
        if int(live_sig["signal"]) != int(ref["signal"]):
            parity_diffs += 1
        if pd.notna(live_sig["sl"]) and pd.notna(ref["sl"]):
            if abs(live_sig["sl"] - ref["sl"]) / ref["sl"] > 0.001:
                sl_diffs += 1

    bt = run_backtest(df, full, BTConfig(), long_only=True)
    print(f"Parity check (triple_long + HTF EMA50) on {pair} {tf} {days}d:")
    print(f"  bars compared: {len(df) - warmup}")
    print(f"  signal mismatches: {parity_diffs}")
    print(f"  sl mismatches:     {sl_diffs}")
    print(f"  backtest stats: {bt.stats()}")
    assert parity_diffs == 0, "live signal differs from backtest!"
    assert sl_diffs == 0, "live sl differs from backtest!"
    print("PARITY OK")


if __name__ == "__main__":
    main()
