"""Drill-down on SOL 30m: year-by-year, regime breakdown, robustness.

Then test portfolios that include SOL 30m (no ML) alongside ETH/BTC at 1h.
"""
from __future__ import annotations

import sys as _sys, os as _os
_THIS_DIR_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _THIS_DIR_PARENT not in _sys.path: _sys.path.insert(0, _THIS_DIR_PARENT)

import time

import numpy as np
import pandas as pd

from core.backtest import BTConfig, run_backtest
from core.data import fetch_ohlcv
from core.ml_filter import (
    build_features,
    filter_signals_by_proba,
    label_signals,
    walk_forward_predict,
)
from core.sentiment import fetch_fear_greed
from core.strategies import triple_confirm_long

BASE = dict(ema_fast=9, ema_slow=26, ema_trend=50,
            rsi_min=55.0, adx_min=22.0,
            atr_n=14, sl_mult=1.8, tp_mult=3.0)
BPD = {"15m": 96, "30m": 48, "1h": 24}


def yearly(eq):
    rows = []
    for y in sorted({d.year for d in eq.index}):
        s = eq[eq.index.year == y]
        if len(s) < 100:
            continue
        ret = s.iloc[-1] / s.iloc[0] - 1
        peak = s.cummax()
        dd = (s / peak - 1).min()
        rows.append({"year": y, "ret": round(ret, 4),
                     "mdd": round(float(dd), 4),
                     "end": round(float(s.iloc[-1]), 2)})
    return pd.DataFrame(rows)


def rolling_30d(eq, bpd=24):
    w = 30 * bpd
    rs = []
    for i in range(0, len(eq) - w + 1, w):
        sub = eq.iloc[i:i + w]
        rs.append(sub.iloc[-1] / sub.iloc[0] - 1)
    rs = pd.Series(rs)
    if rs.empty:
        return None
    return {
        "n": len(rs),
        "win_pct": round(float((rs > 0).mean()), 3),
        "median_pct": round(float(rs.median()) * 100, 2),
        "mean_pct": round(float(rs.mean()) * 100, 2),
        "min_pct": round(float(rs.min()) * 100, 2),
        "max_pct": round(float(rs.max()) * 100, 2),
    }


def cagr(eq):
    days = (eq.index[-1] - eq.index[0]).total_seconds() / 86400
    return (eq.iloc[-1] / eq.iloc[0]) ** (365 / days) - 1


def main():
    pd.set_option("display.width", 220)

    print("=" * 80)
    print("SOL-USDT 30m year-by-year (5y, no ML, no filters)")
    print("=" * 80)
    df = fetch_ohlcv("SOL-USDT", "30m", days=5 * 365)
    sig = triple_confirm_long(df, **BASE)
    res = run_backtest(df, sig, BTConfig(), long_only=True)
    print(f"5y total: {res.stats()}")
    print(f"Annualized: CAGR {cagr(res.equity_curve)*100:.1f}%")
    print(f"Rolling-30d: {rolling_30d(res.equity_curve, BPD['30m'])}")
    print("\nYear by year:")
    print(yearly(res.equity_curve).to_string(index=False))

    print("\n" + "=" * 80)
    print("Portfolios mixing SOL 30m (no ML) with ETH/BTC 1h (no ML)")
    print("=" * 80)
    df_eth = fetch_ohlcv("ETH-USDT", "1h", days=5 * 365)
    df_btc = fetch_ohlcv("BTC-USDT", "1h", days=5 * 365)
    df_sol1h = fetch_ohlcv("SOL-USDT", "1h", days=5 * 365)
    df_sol30m = fetch_ohlcv("SOL-USDT", "30m", days=5 * 365)

    sigs = {
        ("ETH-USDT","1h"):   triple_confirm_long(df_eth, **BASE),
        ("BTC-USDT","1h"):   triple_confirm_long(df_btc, **BASE),
        ("SOL-USDT","1h"):   triple_confirm_long(df_sol1h, **BASE),
        ("SOL-USDT","30m"):  triple_confirm_long(df_sol30m, **BASE),
    }
    dfs = {
        ("ETH-USDT","1h"):   df_eth,
        ("BTC-USDT","1h"):   df_btc,
        ("SOL-USDT","1h"):   df_sol1h,
        ("SOL-USDT","30m"):  df_sol30m,
    }

    def portfolio(allocations, risk: float = 0.015):
        eqs = []
        for key, w in allocations.items():
            cfg = BTConfig(risk_per_trade=risk, starting_equity=100 * w)
            r = run_backtest(dfs[key], sigs[key], cfg, long_only=True)
            eqs.append(r.equity_curve.resample("1h").last().ffill())
        merged = None
        for eq in eqs:
            merged = eq if merged is None else merged.add(eq, fill_value=0)
        return merged

    portfolios = [
        ("ETH+BTC 1h 50/50",
         {("ETH-USDT","1h"):0.5, ("BTC-USDT","1h"):0.5}),
        ("ETH+BTC+SOL 33/33/33 (all 1h)",
         {("ETH-USDT","1h"):0.34, ("BTC-USDT","1h"):0.33, ("SOL-USDT","1h"):0.33}),
        ("ETH+BTC+SOL30m 33/33/33",
         {("ETH-USDT","1h"):0.34, ("BTC-USDT","1h"):0.33, ("SOL-USDT","30m"):0.33}),
        ("ETH+BTC+SOL30m 25/25/50",
         {("ETH-USDT","1h"):0.25, ("BTC-USDT","1h"):0.25, ("SOL-USDT","30m"):0.50}),
        ("ETH+BTC+SOL30m 20/20/60",
         {("ETH-USDT","1h"):0.20, ("BTC-USDT","1h"):0.20, ("SOL-USDT","30m"):0.60}),
        ("SOL30m 100%",
         {("SOL-USDT","30m"):1.0}),
        ("ETH+BTC+SOL+SOL30m 25/25/25/25",
         {("ETH-USDT","1h"):0.25, ("BTC-USDT","1h"):0.25,
          ("SOL-USDT","1h"):0.25, ("SOL-USDT","30m"):0.25}),
    ]

    print(f"{'name':45s}{'cagr':>8s}{'mdd':>8s}{'final':>9s}  monthly")
    for name, alloc in portfolios:
        for risk in (0.015, 0.020):
            eq = portfolio(alloc, risk=risk)
            c = cagr(eq) * 100
            peak = eq.cummax()
            dd = (eq / peak - 1).min() * 100
            roll = rolling_30d(eq, 24)
            print(f"{name + f' r={risk*100:.1f}%':45s}{c:>+7.1f}%{dd:>+7.1f}% {eq.iloc[-1]:>8.0f}  "
                  f"win={roll['win_pct']:.2f} med={roll['median_pct']:+.2f}% "
                  f"min={roll['min_pct']:+.2f}% max={roll['max_pct']:+.2f}%")


if __name__ == "__main__":
    main()
