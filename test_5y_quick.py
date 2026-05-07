"""Faster 5y validation: skip the giant param sweep, focus on the
most informative comparisons.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backtest import BTConfig, run_backtest
from data import fetch_ohlcv
from portfolio import run_portfolio
from sentiment import fetch_fear_greed
from strategies import triple_confirm_long
from strategies_enhanced import with_htf_trend_filter
from strategies_sentiment import _align_fng


def triple_long_skip_greed(df, fng, greed_max=75, **kw):
    base = triple_confirm_long(df, **kw)
    fng_aligned = _align_fng(df, fng)
    mask = (base["signal"] == 1) & (fng_aligned > greed_max)
    base.loc[mask, "signal"] = 0
    base.loc[mask, ["sl", "tp"]] = np.nan
    return base


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


def rolling_30d(eq):
    bpd = 24
    w = 30 * bpd
    rs = []
    for i in range(0, len(eq) - w + 1, w):
        sub = eq.iloc[i:i + w]
        rs.append(sub.iloc[-1] / sub.iloc[0] - 1)
    rs = pd.Series(rs)
    return {
        "windows": len(rs),
        "win_pct": round(float((rs > 0).mean()), 3),
        "median": round(float(rs.median()), 4),
        "mean": round(float(rs.mean()), 4),
        "min": round(float(rs.min()), 4),
        "max": round(float(rs.max()), 4),
    }


BASE = dict(ema_fast=9, ema_slow=26, ema_trend=50,
            rsi_min=55.0, adx_min=22.0,
            atr_n=14, sl_mult=1.8, tp_mult=3.0)


def main():
    pd.set_option("display.width", 220)
    fng = fetch_fear_greed()
    df_eth = fetch_ohlcv("ETH-USDT", "1h", days=5 * 365)
    df_btc = fetch_ohlcv("BTC-USDT", "1h", days=5 * 365)
    df_sol = fetch_ohlcv("SOL-USDT", "1h", days=5 * 365)

    print("=" * 80)
    print("PART 1: triple_long year-by-year on each pair (5y)")
    print("=" * 80)
    for label, df in [("ETH", df_eth), ("BTC", df_btc), ("SOL", df_sol)]:
        sig = triple_confirm_long(df, **BASE)
        res = run_backtest(df, sig, BTConfig(), long_only=True)
        print(f"\n{label} | 5y total: {res.stats()}")
        print(yearly(res.equity_curve).to_string(index=False))

    print("\n" + "=" * 80)
    print("PART 2: triple_long with skip_greed sentiment filter (5y)")
    print("=" * 80)
    for label, df in [("ETH", df_eth), ("BTC", df_btc)]:
        print(f"\n{label}:")
        for greed in (60, 70, 80, 100):
            if greed == 100:
                sig = triple_confirm_long(df, **BASE)
                lab = "no filter"
            else:
                sig = triple_long_skip_greed(df, fng, greed_max=greed, **BASE)
                lab = f"skip greed > {greed}"
            res = run_backtest(df, sig, BTConfig(), long_only=True)
            print(f"  {lab:25s}: {res.stats()}")

    print("\n" + "=" * 80)
    print("PART 3: HTF daily trend filter on triple_long (5y)")
    print("=" * 80)
    for label, df in [("ETH", df_eth), ("BTC", df_btc)]:
        print(f"\n{label}:")
        base_sig = triple_confirm_long(df, **BASE)
        res_no = run_backtest(df, base_sig, BTConfig(), long_only=True)
        print(f"  no HTF filter:           {res_no.stats()}")
        for ema_n in (50, 100, 200):
            filt = with_htf_trend_filter(df, base_sig, htf_rule="1D", ema_n=ema_n)
            res = run_backtest(df, filt, BTConfig(), long_only=True)
            print(f"  HTF daily EMA{ema_n:3d}:        {res.stats()}")

    print("\n" + "=" * 80)
    print("PART 4: Multi-pair portfolio with triple_long (5y)")
    print("=" * 80)
    def sig_fn(local_df):
        return triple_confirm_long(local_df, **BASE)

    portfolios = [
        (["ETH-USDT"],                       [1.0]),
        (["BTC-USDT"],                       [1.0]),
        (["SOL-USDT"],                       [1.0]),
        (["ETH-USDT", "BTC-USDT"],           [0.5, 0.5]),
        (["ETH-USDT", "SOL-USDT"],           [0.7, 0.3]),
        (["ETH-USDT", "BTC-USDT", "SOL-USDT"], [0.4, 0.4, 0.2]),
        (["ETH-USDT", "BTC-USDT", "SOL-USDT"], [0.34, 0.33, 0.33]),
    ]
    for pairs, weights in portfolios:
        r = run_portfolio(pairs, "1h", 5 * 365, sig_fn, weights=weights, long_only=True)
        roll = rolling_30d(r.equity_curve)
        print(f"\n  {str(pairs)} {weights}:")
        print(f"    totals: {r.stats}")
        print(f"    rolling-30d: {roll}")

    print("\n" + "=" * 80)
    print("PART 5: WINNER candidate - triple_long + HTF + portfolio - year-by-year")
    print("=" * 80)
    def winner_sig(local_df):
        s = triple_confirm_long(local_df, **BASE)
        return with_htf_trend_filter(local_df, s, htf_rule="1D", ema_n=50)

    r = run_portfolio(["ETH-USDT", "BTC-USDT", "SOL-USDT"], "1h", 5 * 365,
                      winner_sig, weights=[0.4, 0.4, 0.2], long_only=True)
    print(f"\n  ETH/BTC/SOL 40/40/20 + HTF EMA50: {r.stats}")
    print(f"  rolling-30d: {rolling_30d(r.equity_curve)}")
    print("\n  Year-by-year:")
    print(yearly(r.equity_curve).to_string(index=False))


if __name__ == "__main__":
    main()
