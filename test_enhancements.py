"""Test enhancements vs the current best (Donchian + sentiment).

Reference baseline (1y ETH/USDT 1h):
  +93.46% return, Sharpe 2.42, MDD -10.84%, 92% months win
"""
from __future__ import annotations

import pandas as pd

from backtest import BTConfig, run_backtest
from data import fetch_ohlcv
from sentiment import fetch_fear_greed
from strategies import donchian_breakout
from strategies_enhanced import (
    donchian_strict_adx,
    with_htf_trend_filter,
    with_vol_filter,
)
from strategies_sentiment import donchian_skip_fear

PARAMS = dict(entry_n=20, exit_n=10, adx_n=14, adx_min=20.0,
              atr_n=14, sl_mult=2.5, tp_mult=5.0)


def rolling_stats(df, sig, label, bars_per_day=24, win_days=30):
    rows = []
    w = win_days * bars_per_day
    for i in range(0, len(df) - w + 1, w):
        sub = df.iloc[i:i + w]
        sig_sub = sig.iloc[i:i + w]
        res = run_backtest(sub, sig_sub, BTConfig())
        s = res.stats()
        rows.append(s["total_return"])
    rs = pd.Series(rows)
    return {
        "label": label,
        "n_windows": len(rs),
        "win_pct": round((rs > 0).mean(), 3),
        "median_ret": round(rs.median(), 4),
        "mean_ret": round(rs.mean(), 4),
        "min_ret": round(rs.min(), 4),
        "max_ret": round(rs.max(), 4),
    }


def main():
    pd.set_option("display.width", 220)
    df = fetch_ohlcv("ETH-USDT", "1h", days=365)
    fng = fetch_fear_greed()

    print("=== Building variants ===")
    baseline = donchian_breakout(df, **PARAMS)
    sentiment = donchian_skip_fear(df, fng, fear_min=25, **PARAMS)
    htf_only = with_htf_trend_filter(df, baseline, htf_rule="1D", ema_n=50)
    htf_sent = with_htf_trend_filter(df, sentiment, htf_rule="1D", ema_n=50)
    htf4h_sent = with_htf_trend_filter(df, sentiment, htf_rule="4h", ema_n=50)
    vol_sent = with_vol_filter(df, sentiment, min_pct=0.003)
    vol_high = with_vol_filter(df, sentiment, min_pct=0.005)
    strict_adx = donchian_strict_adx(df, fng, adx_min=25, **{k: v for k, v in PARAMS.items() if k != "adx_min"})
    strict_adx30 = donchian_strict_adx(df, fng, adx_min=30, **{k: v for k, v in PARAMS.items() if k != "adx_min"})

    # Combinations
    htf_sent_strict = with_htf_trend_filter(df, strict_adx, htf_rule="1D", ema_n=50)
    htf_sent_vol = with_vol_filter(df, htf_sent, min_pct=0.003)

    variants = [
        ("baseline (no sentiment)",  baseline),
        ("+sentiment (current best)", sentiment),
        ("+HTF daily EMA50",          htf_only),
        ("+sentiment+HTF daily EMA50", htf_sent),
        ("+sentiment+HTF 4h EMA50",   htf4h_sent),
        ("+sentiment+vol min 0.3%",   vol_sent),
        ("+sentiment+vol min 0.5%",   vol_high),
        ("+sentiment, ADX>=25",       strict_adx),
        ("+sentiment, ADX>=30",       strict_adx30),
        ("+sentiment+ADX>=25+HTF",    htf_sent_strict),
        ("+sentiment+HTF+vol",        htf_sent_vol),
    ]

    print("\n=== 1-year totals ===")
    rows = []
    for name, sig in variants:
        res = run_backtest(df, sig, BTConfig())
        rows.append({"variant": name, **res.stats()})
    out = pd.DataFrame(rows)
    print(out.to_string(index=False))

    print("\n=== Rolling 30-day non-overlapping windows ===")
    rolling_rows = []
    for name, sig in variants:
        rolling_rows.append(rolling_stats(df, sig, name))
    print(pd.DataFrame(rolling_rows).to_string(index=False))


if __name__ == "__main__":
    main()
