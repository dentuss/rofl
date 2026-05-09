"""Validate triple_long on 5y data with parameter sweep, sentiment filter,
and multi-pair portfolio.
"""
from __future__ import annotations

import sys as _sys, os as _os
_THIS_DIR_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _THIS_DIR_PARENT not in _sys.path: _sys.path.insert(0, _THIS_DIR_PARENT)

import itertools

import numpy as np
import pandas as pd

from core.backtest import BTConfig, run_backtest
from core.data import fetch_ohlcv
from core.sentiment import fetch_fear_greed
from core.strategies import triple_confirm_long
from core.strategies_enhanced import with_htf_trend_filter
from core.strategies_sentiment import _align_fng


def triple_long_skip_greed(df, fng, greed_max=75, **kw):
    """Long-only triple_long with optional 'skip in extreme greed' filter."""
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
        rows.append({"year": y, "ret": round(ret, 4), "mdd": round(float(dd), 4),
                     "end": round(float(s.iloc[-1]), 2)})
    return pd.DataFrame(rows)


def main():
    pd.set_option("display.width", 220)
    fng = fetch_fear_greed()
    df_eth = fetch_ohlcv("ETH-USDT", "1h", days=5 * 365)
    df_btc = fetch_ohlcv("BTC-USDT", "1h", days=5 * 365)
    df_sol = fetch_ohlcv("SOL-USDT", "1h", days=5 * 365)

    BASE = dict(ema_fast=9, ema_slow=26, ema_trend=50,
                rsi_min=55.0, adx_min=22.0,
                atr_n=14, sl_mult=1.8, tp_mult=3.0)

    print("=" * 80)
    print("PART 1: triple_long params on 5y data (default vs alternatives)")
    print("=" * 80)
    rows = []
    for ef, es, et, rmin, amin, sl, tp in itertools.product(
        [9, 12], [21, 26], [50, 100], [55, 60],
        [22, 25], [1.5, 1.8, 2.0], [2.5, 3.0, 4.0],
    ):
        params = dict(ema_fast=ef, ema_slow=es, ema_trend=et,
                      rsi_min=rmin, adx_min=amin,
                      sl_mult=sl, tp_mult=tp, atr_n=14)
        sig = triple_confirm_long(df_eth, **params)
        res = run_backtest(df_eth, sig, BTConfig(), long_only=True)
        s = res.stats()
        rows.append({"ef": ef, "es": es, "et": et, "rmin": rmin, "amin": amin,
                     "sl": sl, "tp": tp, **s})
    sweep = pd.DataFrame(rows).sort_values("sharpe", ascending=False)
    print(f"Total configs: {len(sweep)}")
    print(f"Profitable on 5y: {(sweep['total_return'] > 0).sum()}/{len(sweep)}")
    print(f"Sharpe > 0.5 on 5y: {(sweep['sharpe'] > 0.5).sum()}/{len(sweep)}")
    print("\nTop 10 by Sharpe (ETH 5y):")
    print(sweep.head(10).to_string(index=False))

    # Pick a robust config: top Sharpe AND profit_factor > 1.10
    top = sweep[(sweep["profit_factor"] > 1.1)].head(10)
    if not top.empty:
        chosen = top.iloc[0].to_dict()
        print(f"\nChosen ETH params (by Sharpe, PF > 1.1):")
        print(f"  ef={chosen['ef']} es={chosen['es']} et={chosen['et']} "
              f"rmin={chosen['rmin']} amin={chosen['amin']} "
              f"sl={chosen['sl']} tp={chosen['tp']}")

    print("\n" + "=" * 80)
    print("PART 2: Default triple_long year-by-year on each pair")
    print("=" * 80)
    for label, df in [("ETH", df_eth), ("BTC", df_btc), ("SOL", df_sol)]:
        sig = triple_confirm_long(df, **BASE)
        res = run_backtest(df, sig, BTConfig(), long_only=True)
        print(f"\n{label} | 5y: {res.stats()}")
        print(yearly(res.equity_curve).to_string(index=False))

    print("\n" + "=" * 80)
    print("PART 3: triple_long + skip_greed sentiment filter (5y ETH)")
    print("=" * 80)
    for greed in (60, 65, 70, 75, 80, 85, 100):
        if greed == 100:
            sig = triple_confirm_long(df_eth, **BASE)
            label = "no filter"
        else:
            sig = triple_long_skip_greed(df_eth, fng, greed_max=greed, **BASE)
            label = f"skip greed > {greed}"
        res = run_backtest(df_eth, sig, BTConfig(), long_only=True)
        print(f"  {label:25s}: {res.stats()}")

    print("\n" + "=" * 80)
    print("PART 4: HTF daily trend filter on triple_long (5y ETH)")
    print("=" * 80)
    base_sig = triple_confirm_long(df_eth, **BASE)
    res_no = run_backtest(df_eth, base_sig, BTConfig(), long_only=True)
    print(f"  no HTF filter:                {res_no.stats()}")
    for ema_n in (50, 100, 200):
        filt = with_htf_trend_filter(df_eth, base_sig, htf_rule="1D", ema_n=ema_n)
        res = run_backtest(df_eth, filt, BTConfig(), long_only=True)
        print(f"  HTF daily EMA{ema_n:3d}:             {res.stats()}")

    print("\n" + "=" * 80)
    print("PART 5: Multi-pair portfolio summary on 5y")
    print("=" * 80)
    from portfolio import run_portfolio

    def sig_fn(df_local):
        return triple_confirm_long(df_local, **BASE)

    for pairs, weights in [
        (["ETH-USDT"], [1.0]),
        (["BTC-USDT"], [1.0]),
        (["SOL-USDT"], [1.0]),
        (["ETH-USDT", "BTC-USDT"], [0.5, 0.5]),
        (["ETH-USDT", "SOL-USDT"], [0.7, 0.3]),
        (["ETH-USDT", "BTC-USDT", "SOL-USDT"], [0.4, 0.4, 0.2]),
        (["ETH-USDT", "BTC-USDT", "SOL-USDT"], [0.34, 0.33, 0.33]),
    ]:
        r = run_portfolio(pairs, "1h", 5 * 365, sig_fn, weights=weights, long_only=True)
        eq = r.equity_curve
        # compute proper rolling 30d win rate
        bars_per_day = 24
        w = 30 * bars_per_day
        rets = []
        for i in range(0, len(eq) - w + 1, w):
            sub = eq.iloc[i:i + w]
            rets.append(sub.iloc[-1] / sub.iloc[0] - 1)
        rs = pd.Series(rets)
        win_pct = round((rs > 0).mean(), 3)
        median = round(rs.median() * 100, 2)
        worst = round(rs.min() * 100, 2)
        print(f"  {str(pairs):60s} {weights}:")
        print(f"    totals: {r.stats}")
        print(f"    rolling-30d: win_pct={win_pct} median={median}% worst={worst}%")


if __name__ == "__main__":
    main()
