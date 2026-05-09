"""Backtest the sentiment variants vs the baseline Donchian.

We test each variant on ETH/USDT 1h over the last year. The honest metric
is whether the filter improves risk-adjusted return without hurting
absolute return too much. Win condition:
  - Higher Sharpe than baseline AND
  - Lower max drawdown than baseline AND
  - Total return at least 80% of baseline
"""
from __future__ import annotations

import sys as _sys, os as _os
_THIS_DIR_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _THIS_DIR_PARENT not in _sys.path: _sys.path.insert(0, _THIS_DIR_PARENT)

import pandas as pd

from core.backtest import BTConfig, run_backtest
from core.data import fetch_ohlcv
from core.sentiment import fetch_fear_greed
from core.strategies import donchian_breakout
from core.strategies_sentiment import (
    donchian_both_filters,
    donchian_long_only_fear,
    donchian_reverse_bias,
    donchian_skip_fear,
    donchian_skip_greed,
)

DONCHIAN_PARAMS = dict(entry_n=20, exit_n=10, adx_n=14, adx_min=20.0,
                       atr_n=14, sl_mult=2.5, tp_mult=5.0)


def _bt(df, sig, long_only=False):
    return run_backtest(df, sig, BTConfig(), long_only=long_only)


def main():
    pd.set_option("display.width", 220)
    df = fetch_ohlcv("ETH-USDT", "1h", days=365)
    fng = fetch_fear_greed()
    print(f"Bars: {len(df)} | F&G coverage: "
          f"{df.index[0].date()} -> {df.index[-1].date()}")

    variants = {
        "baseline":          donchian_breakout(df, **DONCHIAN_PARAMS),
        "skip_greed_75":     donchian_skip_greed(df, fng, greed_max=75, **DONCHIAN_PARAMS),
        "skip_greed_70":     donchian_skip_greed(df, fng, greed_max=70, **DONCHIAN_PARAMS),
        "skip_greed_65":     donchian_skip_greed(df, fng, greed_max=65, **DONCHIAN_PARAMS),
        "skip_fear_25":      donchian_skip_fear(df, fng, fear_min=25, **DONCHIAN_PARAMS),
        "skip_fear_30":      donchian_skip_fear(df, fng, fear_min=30, **DONCHIAN_PARAMS),
        "both_75_25":        donchian_both_filters(df, fng, greed_max=75, fear_min=25, **DONCHIAN_PARAMS),
        "both_70_30":        donchian_both_filters(df, fng, greed_max=70, fear_min=30, **DONCHIAN_PARAMS),
        "reverse_bias":      donchian_reverse_bias(df, fng, **DONCHIAN_PARAMS),
        "long_only_fear55":  donchian_long_only_fear(df, fng, fng_max=55, **DONCHIAN_PARAMS),
    }

    rows = []
    for name, sig in variants.items():
        res = _bt(df, sig)
        rows.append({"variant": name, **res.stats()})
    out = pd.DataFrame(rows)
    print("\n=== ETH/USDT 1h, 365 days ===")
    print(out.to_string(index=False))

    # Same on rolling non-overlapping 30d windows for the best variants
    print("\n=== Rolling 30-day windows: which variant has the steadiest edge? ===")
    bars_per_day = 24
    w = 30 * bars_per_day
    summary = []
    for name in variants:
        rets = []
        mdds = []
        for i in range(0, len(df) - w + 1, w):
            sub = df.iloc[i:i + w]
            sig_fn = variants[name].iloc[i:i + w]
            res = _bt(sub, sig_fn)
            s = res.stats()
            rets.append(s["total_return"])
            mdds.append(s["max_drawdown"])
        rs = pd.Series(rets)
        ms = pd.Series(mdds)
        summary.append({
            "variant": name,
            "monthly_median": round(rs.median(), 4),
            "monthly_mean": round(rs.mean(), 4),
            "monthly_min": round(rs.min(), 4),
            "monthly_max": round(rs.max(), 4),
            "win_pct": round((rs > 0).mean(), 3),
            "median_mdd": round(ms.median(), 4),
        })
    s = pd.DataFrame(summary)
    print(s.to_string(index=False))


if __name__ == "__main__":
    main()
