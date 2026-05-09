"""Combined approach: ML filter + multi-pair + risk scaling.

Strategy: triple_long generates raw signals; ML keeps only the winners
(p >= 0.40); multi-pair spreads across ETH+BTC+SOL; SOL adds 30m for
extra trades. Higher risk per trade is justified by ML's lower MDD.

Goal: lift monthly median from ~1-2% toward 4-5%.
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd

from backtest import BTConfig, run_backtest
from data import fetch_ohlcv
from ml_filter import (
    build_features,
    filter_signals_by_proba,
    label_signals,
    walk_forward_predict,
)
from sentiment import fetch_fear_greed
from strategies import triple_confirm_long

BASE = dict(ema_fast=9, ema_slow=26, ema_trend=50,
            rsi_min=55.0, adx_min=22.0,
            atr_n=14, sl_mult=1.8, tp_mult=3.0)
BPD = {"15m": 96, "30m": 48, "1h": 24}


def rolling_30d(eq, bpd):
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


def get_ml_filtered(pair: str, tf: str, days: int, fng,
                    min_proba: float = 0.40):
    """Generate ML-filtered signals on the OOS region."""
    df = fetch_ohlcv(pair, tf, days=days)
    sig = triple_confirm_long(df, **BASE)
    labels = label_signals(df, sig, max_horizon_bars=96)
    features = build_features(df, fng=fng)
    train_bars = 365 * BPD[tf]
    step_bars = 90 * BPD[tf]
    proba = walk_forward_predict(features, labels,
                                 train_bars=train_bars,
                                 step_bars=step_bars)
    oos_start = proba.first_valid_index()
    if oos_start is None:
        return None
    df_oos = df.loc[oos_start:].copy()
    sig_oos = sig.loc[oos_start:].copy()
    proba_oos = proba.loc[oos_start:].fillna(0)
    filt = filter_signals_by_proba(sig_oos, proba_oos, min_proba=min_proba)
    return df_oos, sig_oos, filt


def run_with_risk(df, sig, risk: float):
    cfg = BTConfig(risk_per_trade=risk)
    return run_backtest(df, sig, cfg, long_only=True)


def main():
    pd.set_option("display.width", 220)
    fng = fetch_fear_greed()

    print("Building ML-filtered signals for each pair (this takes a minute)...")
    t0 = time.time()
    eth = get_ml_filtered("ETH-USDT", "1h", 5 * 365, fng, min_proba=0.40)
    btc = get_ml_filtered("BTC-USDT", "1h", 5 * 365, fng, min_proba=0.40)
    sol_1h = get_ml_filtered("SOL-USDT", "1h", 5 * 365, fng, min_proba=0.40)
    sol_30m = get_ml_filtered("SOL-USDT", "30m", 5 * 365, fng, min_proba=0.40)
    print(f"  done in {time.time() - t0:.1f}s")

    print("\n=== Per-asset OOS comparison (5y minus 1y warmup ~= 4y) ===")
    print(f"{'config':40s} {'trades':>7s} {'ret':>9s} {'cagr':>7s} {'mdd':>7s} {'sharpe':>7s}  monthly")
    for label, data, tf in [
        ("ETH 1h baseline (no ML)", eth, "1h"),
        ("ETH 1h +ML p>=0.40", eth, "1h"),
        ("BTC 1h baseline (no ML)", btc, "1h"),
        ("BTC 1h +ML p>=0.40", btc, "1h"),
        ("SOL 1h baseline (no ML)", sol_1h, "1h"),
        ("SOL 1h +ML p>=0.40", sol_1h, "1h"),
        ("SOL 30m baseline (no ML)", sol_30m, "30m"),
        ("SOL 30m +ML p>=0.40", sol_30m, "30m"),
    ]:
        df_oos, sig_oos, filt = data
        if "+ML" in label:
            res = run_with_risk(df_oos, filt, 0.015)
        else:
            res = run_with_risk(df_oos, sig_oos, 0.015)
        s = res.stats()
        roll = rolling_30d(res.equity_curve, BPD[tf])
        c = cagr(res.equity_curve) * 100
        print(f"{label:40s} {s['trades']:>7d} {s['total_return']:>+9.4f} "
              f"{c:>+6.1f}% {s['max_drawdown']:>+7.4f} {s['sharpe']:>+7.2f}  {roll}")

    print("\n=== ML-filtered + risk scaling ===")
    for label, data, tf in [
        ("ETH 1h +ML, risk 1.5%", eth, "1h"),
        ("ETH 1h +ML, risk 2.5%", eth, "1h"),
        ("ETH 1h +ML, risk 3.5%", eth, "1h"),
        ("SOL 30m +ML, risk 1.5%", sol_30m, "30m"),
        ("SOL 30m +ML, risk 2.5%", sol_30m, "30m"),
    ]:
        df_oos, _, filt = data
        risk = float(label.split("risk ")[1].rstrip("%")) / 100
        res = run_with_risk(df_oos, filt, risk)
        s = res.stats()
        roll = rolling_30d(res.equity_curve, BPD[tf])
        c = cagr(res.equity_curve) * 100
        print(f"{label:40s} {s['trades']:>7d} {s['total_return']:>+9.4f} "
              f"{c:>+6.1f}% {s['max_drawdown']:>+7.4f} {s['sharpe']:>+7.2f}  {roll}")

    # Manual portfolio: average equity curves of ML-filtered runs
    print("\n=== Multi-pair ML-filtered portfolio ===")
    def portfolio(allocations: dict, risk: float):
        """allocations: {(pair, tf): weight}; weight sums to 1.0."""
        eqs = []
        for (pair, tf), w in allocations.items():
            data = {("ETH-USDT", "1h"): eth, ("BTC-USDT", "1h"): btc,
                    ("SOL-USDT", "1h"): sol_1h,
                    ("SOL-USDT", "30m"): sol_30m}[(pair, tf)]
            df_oos, _, filt = data
            cfg = BTConfig(risk_per_trade=risk, starting_equity=100 * w)
            res = run_backtest(df_oos, filt, cfg, long_only=True)
            eqs.append((res.equity_curve, BPD[tf]))
        # Resample everyone to 1h for combining
        unified = None
        for eq, _ in eqs:
            r = eq.resample("1h").last().ffill()
            unified = r if unified is None else unified.add(r, fill_value=0)
        return unified

    for name, alloc, risk in [
        ("ETH+BTC+SOL 33/33/33 ML",
         {("ETH-USDT","1h"):0.34, ("BTC-USDT","1h"):0.33, ("SOL-USDT","1h"):0.33}, 0.015),
        ("ETH+BTC+SOL 33/33/33 ML, risk 2.5%",
         {("ETH-USDT","1h"):0.34, ("BTC-USDT","1h"):0.33, ("SOL-USDT","1h"):0.33}, 0.025),
        ("ETH+BTC+SOL30m 30/30/40 ML",
         {("ETH-USDT","1h"):0.30, ("BTC-USDT","1h"):0.30, ("SOL-USDT","30m"):0.40}, 0.015),
        ("ETH+BTC+SOL30m 30/30/40 ML, risk 2.5%",
         {("ETH-USDT","1h"):0.30, ("BTC-USDT","1h"):0.30, ("SOL-USDT","30m"):0.40}, 0.025),
        ("ETH+BTC+SOL+SOL30m 25/25/25/25 ML",
         {("ETH-USDT","1h"):0.25, ("BTC-USDT","1h"):0.25,
          ("SOL-USDT","1h"):0.25, ("SOL-USDT","30m"):0.25}, 0.015),
    ]:
        eq = portfolio(alloc, risk)
        c = cagr(eq) * 100
        peak = eq.cummax()
        dd = (eq / peak - 1).min()
        roll = rolling_30d(eq, 24)
        print(f"  {name:55s} CAGR {c:+5.1f}% MDD {dd*100:+5.1f}% final={eq.iloc[-1]:.0f} {roll}")


if __name__ == "__main__":
    main()
