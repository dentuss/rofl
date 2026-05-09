"""Train and walk-forward test the ML entry filter on 5 years of data.

Pipeline:
  1. triple_long generates raw signals
  2. Label each historical signal: 1 if it eventually hit TP, 0 if SL
  3. Build features per bar (no look-ahead)
  4. Walk-forward train classifier on rolling 1y, predict next 90d
  5. Apply at multiple proba thresholds, measure monthly stats

Goal: lift the monthly median from ~1.5% (baseline ETH) toward ~5%.
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


def rolling_30d(eq, bars_per_day=24):
    w = 30 * bars_per_day
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


def main():
    pd.set_option("display.width", 220)

    pair = "ETH-USDT"
    tf = "1h"
    days = 5 * 365
    print(f"=== ML filter on {pair} {tf} {days}d ===")

    df = fetch_ohlcv(pair, tf, days=days)
    fng = fetch_fear_greed()
    print(f"Bars: {len(df)} | F&G: {len(fng)} days")

    print("Building signals...")
    sig = triple_confirm_long(df, **BASE)
    n_signals = (sig["signal"] != 0).sum()
    print(f"  triple_long signals: {n_signals}")

    print("Building labels (this scans forward, ~30s)...")
    t0 = time.time()
    labels = label_signals(df, sig, max_horizon_bars=96)
    print(f"  done in {time.time() - t0:.1f}s | "
          f"win-rate of labeled set: {labels.dropna().mean():.3f}")

    print("Building features...")
    features = build_features(df, fng=fng)
    print(f"  features: {list(features.columns)}")

    print("Walk-forward predicting (train 365d, step 90d)...")
    t0 = time.time()
    proba = walk_forward_predict(features, labels,
                                 train_bars=365 * 24,
                                 step_bars=90 * 24)
    n_pred = proba.notna().sum()
    print(f"  done in {time.time() - t0:.1f}s | "
          f"predictions: {n_pred} bars covered")

    # Baseline (no ML): only on the OOS region (where we have predictions)
    oos_start = proba.first_valid_index()
    print(f"\nOOS region: {oos_start} -> {df.index[-1]}")

    df_oos = df.loc[oos_start:].copy()
    sig_oos = sig.loc[oos_start:].copy()
    proba_oos = proba.loc[oos_start:].fillna(0)

    print("\n=== Backtests (OOS only, ETH/USDT 1h) ===")
    print(f"{'config':30s} {'trades':>7s} {'ret':>9s} {'mdd':>7s} {'sharpe':>7s}  rolling-30d-stats")
    res = run_backtest(df_oos, sig_oos, BTConfig(), long_only=True)
    s = res.stats()
    roll = rolling_30d(res.equity_curve)
    print(f"{'baseline (no ML)':30s} {s['trades']:>7d} {s['total_return']:>+9.4f} "
          f"{s['max_drawdown']:>+7.4f} {s['sharpe']:>+7.2f}  {roll}")

    for min_proba in (0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70):
        filt = filter_signals_by_proba(sig_oos, proba_oos, min_proba=min_proba)
        res = run_backtest(df_oos, filt, BTConfig(), long_only=True)
        s = res.stats()
        roll = rolling_30d(res.equity_curve)
        print(f"{'+ML p>='+str(min_proba):30s} {s['trades']:>7d} {s['total_return']:>+9.4f} "
              f"{s['max_drawdown']:>+7.4f} {s['sharpe']:>+7.2f}  {roll}")


if __name__ == "__main__":
    main()
