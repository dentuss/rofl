"""Out-of-sample + parameter-robustness validation.

For each promising strategy/pair/tf, we:
  1. Run on a longer (90d) history to make sure 30d wasn't a fluke
  2. Walk-forward: split into halves and check both
  3. Param sweep: jitter the key params and see if results stay positive
"""
from __future__ import annotations

import sys as _sys, os as _os
_THIS_DIR_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _THIS_DIR_PARENT not in _sys.path: _sys.path.insert(0, _THIS_DIR_PARENT)

import itertools

import pandas as pd

from core.backtest import BTConfig, run_backtest
from core.data import fetch_ohlcv
from core.strategies import (
    REGISTRY,
    bb_meanrev,
    triple_confirm_long,
    ema_trend,
)


def long_history(strategy_name: str, pair: str, tf: str, days: int = 90):
    df = fetch_ohlcv(pair, tf, days=days)
    sc = REGISTRY[strategy_name]
    sig = sc.fn(df)
    res = run_backtest(df, sig, BTConfig(), long_only=sc.long_only)
    return res, df


def walk_forward(strategy_name: str, pair: str, tf: str, days: int = 60):
    df = fetch_ohlcv(pair, tf, days=days)
    n = len(df)
    half1 = df.iloc[: n // 2]
    half2 = df.iloc[n // 2 :]
    sc = REGISTRY[strategy_name]
    rows = []
    for label, slice_df in (("first_half", half1), ("second_half", half2)):
        sig = sc.fn(slice_df)
        res = run_backtest(slice_df, sig, BTConfig(), long_only=sc.long_only)
        rows.append({"window": label, **res.stats()})
    return pd.DataFrame(rows)


def param_sweep_bb(pair="SOL-USDT", tf="1h", days=60):
    df = fetch_ohlcv(pair, tf, days=days)
    rows = []
    for n, k, rb, rs in itertools.product(
        [15, 20, 25],
        [1.8, 2.0, 2.2, 2.5],
        [25, 30, 35],
        [65, 70, 75],
    ):
        sig = bb_meanrev(df, n=n, k=k, rsi_buy=rb, rsi_sell=rs)
        res = run_backtest(df, sig)
        st = res.stats()
        rows.append({"n": n, "k": k, "rb": rb, "rs": rs, **st})
    return pd.DataFrame(rows).sort_values("total_return", ascending=False).reset_index(drop=True)


def param_sweep_triple(pair="ETH-USDT", tf="15m", days=60):
    df = fetch_ohlcv(pair, tf, days=days)
    rows = []
    for ef, es, et, rmin, amin in itertools.product(
        [9, 12], [21, 26], [50, 100], [50, 55, 60], [18, 22, 25],
    ):
        sig = triple_confirm_long(df, ema_fast=ef, ema_slow=es, ema_trend=et,
                                  rsi_min=rmin, adx_min=amin)
        res = run_backtest(df, sig, long_only=True)
        st = res.stats()
        rows.append({"ef": ef, "es": es, "et": et, "rmin": rmin, "amin": amin, **st})
    return pd.DataFrame(rows).sort_values("total_return", ascending=False).reset_index(drop=True)


if __name__ == "__main__":
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)

    print("=" * 80)
    print("LONG HISTORY (60 days) — top candidates")
    print("=" * 80)
    for name, pair, tf in [
        ("bb_meanrev", "SOL-USDT", "1h"),
        ("triple_long", "ETH-USDT", "15m"),
        ("bb_meanrev", "ETH-USDT", "1h"),
        ("ema_trend",  "BTC-USDT", "1h"),
    ]:
        res, _ = long_history(name, pair, tf, days=60)
        print(f"\n{name:18s} {pair:9s} {tf:3s} 60d:", res.stats())

    print("\n" + "=" * 80)
    print("WALK-FORWARD (60d, two halves)")
    print("=" * 80)
    for name, pair, tf in [
        ("bb_meanrev", "SOL-USDT", "1h"),
        ("triple_long", "ETH-USDT", "15m"),
    ]:
        print(f"\n{name} {pair} {tf}:")
        print(walk_forward(name, pair, tf, days=60).to_string(index=False))

    print("\n" + "=" * 80)
    print("PARAM SWEEP — bb_meanrev SOL-USDT 1h (top 8 / bottom 5)")
    print("=" * 80)
    s = param_sweep_bb()
    print(s.head(8).to_string(index=False))
    print("...")
    print(s.tail(5).to_string(index=False))
    print(f"Positive configs: {(s['total_return'] > 0).sum()}/{len(s)}")

    print("\n" + "=" * 80)
    print("PARAM SWEEP — triple_long ETH-USDT 15m (top 8 / bottom 5)")
    print("=" * 80)
    s = param_sweep_triple()
    print(s.head(8).to_string(index=False))
    print("...")
    print(s.tail(5).to_string(index=False))
    print(f"Positive configs: {(s['total_return'] > 0).sum()}/{len(s)}")
