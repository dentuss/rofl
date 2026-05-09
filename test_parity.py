"""Live↔backtest parity check for triple_long.

Walks the live signal generator forward bar-by-bar over historical data and
compares its signal to the backtest's signal. They must match.

Tests both the 'steady' (ETH 1h) and 'high_return' (SOL 30m) presets.
"""
from __future__ import annotations

import pandas as pd

from backtest import BTConfig, run_backtest
from data import fetch_ohlcv
from strategies import triple_confirm_long

BASE = dict(ema_fast=9, ema_slow=26, ema_trend=50,
            rsi_min=55.0, adx_min=22.0,
            atr_n=14, sl_mult=1.8, tp_mult=3.0)


def parity_check(pair: str, tf: str, days: int):
    df = fetch_ohlcv(pair, tf, days=days)
    full = triple_confirm_long(df, **BASE)
    parity_diffs = 0
    sl_diffs = 0
    warmup = 60
    for i in range(warmup, len(df)):
        window = df.iloc[: i + 1]
        live_sig = triple_confirm_long(window, **BASE).iloc[-1]
        ref = full.iloc[i]
        if int(live_sig["signal"]) != int(ref["signal"]):
            parity_diffs += 1
        if pd.notna(live_sig["sl"]) and pd.notna(ref["sl"]):
            if abs(live_sig["sl"] - ref["sl"]) / ref["sl"] > 0.001:
                sl_diffs += 1
    bt = run_backtest(df, full, BTConfig(), long_only=True)
    return {
        "pair": pair, "tf": tf, "days": days,
        "bars_compared": len(df) - warmup,
        "signal_mismatches": parity_diffs,
        "sl_mismatches": sl_diffs,
        "stats": bt.stats(),
    }


def main():
    for pair, tf, days in [
        ("ETH-USDT", "1h",  90),
        ("SOL-USDT", "30m", 60),
    ]:
        r = parity_check(pair, tf, days)
        print(f"Parity check {pair} {tf} {days}d:")
        print(f"  bars compared: {r['bars_compared']}")
        print(f"  signal mismatches: {r['signal_mismatches']}")
        print(f"  sl mismatches:     {r['sl_mismatches']}")
        print(f"  backtest stats: {r['stats']}")
        assert r["signal_mismatches"] == 0, f"signal mismatch for {pair} {tf}"
        assert r["sl_mismatches"] == 0, f"sl mismatch for {pair} {tf}"
        print("  PARITY OK")
        print()


if __name__ == "__main__":
    main()
