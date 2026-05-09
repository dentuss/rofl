"""Rolling out-of-sample analysis.

For a given (strategy, pair, timeframe), splits the long history into N-day
windows (overlapping or non-overlapping) and reports per-window stats. This
is the most honest test we have short of forward-trading.

Usage:
    python3 rolling_oos.py
    python3 rolling_oos.py 60 30      # 60-day windows, 30-day step (overlapping)
"""
from __future__ import annotations

import sys

import pandas as pd

from backtest import BTConfig, run_backtest
from data import fetch_ohlcv
from strategies import donchian_breakout

CHOSEN = dict(entry_n=20, exit_n=10, adx_n=14, adx_min=20.0,
              atr_n=14, sl_mult=2.5, tp_mult=5.0)


def rolling_oos(pair: str, tf: str, history_days: int,
                window_days: int, step_days: int):
    df = fetch_ohlcv(pair, tf, days=history_days)
    bars_per_day = {"15m": 96, "30m": 48, "1h": 24, "4h": 6}[tf]
    w = window_days * bars_per_day
    s = step_days * bars_per_day

    rows = []
    for i in range(0, len(df) - w + 1, s):
        sub = df.iloc[i:i + w]
        sig = donchian_breakout(sub, **CHOSEN)
        res = run_backtest(sub, sig, BTConfig())
        st = res.stats()
        rows.append({
            "start": sub.index[0].strftime("%Y-%m-%d"),
            "end": sub.index[-1].strftime("%Y-%m-%d"),
            **st,
        })
    return pd.DataFrame(rows)


if __name__ == "__main__":
    pd.set_option("display.width", 200)

    window_days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    step_days = int(sys.argv[2]) if len(sys.argv) > 2 else window_days

    print(f"Rolling OOS: ETH-USDT 1h, {window_days}-day windows, {step_days}-day step")
    print("=" * 90)
    r = rolling_oos("ETH-USDT", "1h", history_days=365,
                    window_days=window_days, step_days=step_days)
    cols = ["start", "end", "trades", "win_rate", "total_return",
            "max_drawdown", "profit_factor", "sharpe"]
    print(r[cols].to_string(index=False))
    print()
    n = len(r)
    print(f"  Profitable windows: {(r['total_return'] > 0).sum()}/{n} "
          f"({(r['total_return'] > 0).mean() * 100:.0f}%)")
    print(f"  Median return: {r['total_return'].median() * 100:.2f}%")
    print(f"  Mean return:   {r['total_return'].mean() * 100:.2f}%")
    print(f"  Best:          {r['total_return'].max() * 100:.2f}%")
    print(f"  Worst:         {r['total_return'].min() * 100:.2f}%")
    print(f"  Median MDD:    {r['max_drawdown'].median() * 100:.2f}%")
    print(f"  Worst MDD:     {r['max_drawdown'].min() * 100:.2f}%")
