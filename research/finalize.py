"""Final validation of the chosen strategy on 5-year history.

Winner: triple_confirm_long (long-only) on ETH/USDT 1h.
    EMA stack (9>26>50) + RSI(14)>55 + ADX(14)>22
    SL = 1.8x ATR, TP = 3.0x ATR
    No HTF filter on ETH (HTF over-filters and HURTS performance on ETH;
    it does help on BTC, where it improves Sharpe and reduces MDD).
    No sentiment filter (long-only, so skip-shorts-in-fear is moot;
    skip-greed showed minimal/marginal benefit on long-only).

Backtest evidence (100 USDT, 0.06% fee, 2bp slippage, ETH/USDT 1h):
    365d:  +30% (one good year)
    5 years: +201% (every year profitable, including 2022 bear)

Multi-pair (recommended for higher return):
    ETH+SOL 70/30:        +410% / 5y, MDD -22%, Sharpe 1.10
    ETH+BTC+SOL 33/33/33: +434% / 5y, MDD -22%, Sharpe 1.13

Crisis-period evidence (vs buy & hold ETH):
    China mining ban May-Jul 2021:    +1.9% vs -39%
    2022 full bear:                  +25.5% vs -68%
    Terra/Luna May 2022:                <0% vs -56%
    FTX Nov 2022:                     +1.3% vs -24%

Key lesson learned: my initial 1-year backtest favored Donchian breakout,
but on 5 years that strategy LOSES MONEY on ETH and BTC. Always validate
across multiple regimes (bull/bear/chop) before believing a strategy.
"""
from __future__ import annotations

import sys as _sys, os as _os
_THIS_DIR_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _THIS_DIR_PARENT not in _sys.path: _sys.path.insert(0, _THIS_DIR_PARENT)

import pandas as pd

from core.backtest import BTConfig, run_backtest
from core.data import fetch_ohlcv
from core.portfolio import run_portfolio
from core.strategies import triple_confirm_long
from core.strategies_enhanced import with_htf_trend_filter

CHOSEN = dict(ema_fast=9, ema_slow=26, ema_trend=50,
              rsi_min=55.0, adx_min=22.0,
              atr_n=14, sl_mult=1.8, tp_mult=3.0)


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
        "median": round(float(rs.median()) * 100, 2),
        "min": round(float(rs.min()) * 100, 2),
        "max": round(float(rs.max()) * 100, 2),
    }


def run_one(pair, tf, days, cfg=BTConfig(), use_htf=False):
    df = fetch_ohlcv(pair, tf, days=days)
    sig = triple_confirm_long(df, **CHOSEN)
    if use_htf:
        sig = with_htf_trend_filter(df, sig, htf_rule="1D", ema_n=50)
    res = run_backtest(df, sig, cfg, long_only=True)
    return res, df


if __name__ == "__main__":
    pd.set_option("display.width", 200)

    print("=" * 80)
    print("CHOSEN STRATEGY:  triple_confirm_long  (long-only)")
    print(f"PARAMS: {CHOSEN}")
    print("=" * 80)

    print("\n--- ETH-USDT 1h, multiple horizons (no HTF filter) ---")
    for days in (30, 90, 180, 365, 5 * 365):
        res, _ = run_one("ETH-USDT", "1h", days)
        print(f"  {days:5d}d: {res.stats()}")

    print("\n--- 5-year year-by-year on each pair ---")
    for pair in ("ETH-USDT", "BTC-USDT", "SOL-USDT"):
        res, _ = run_one(pair, "1h", 5 * 365)
        print(f"\n{pair}: {res.stats()}")
        print(yearly(res.equity_curve).to_string(index=False))

    print("\n--- Multi-pair portfolios (5y) ---")
    def sig_fn(local_df):
        return triple_confirm_long(local_df, **CHOSEN)
    for pairs, weights in [
        (["ETH-USDT", "SOL-USDT"], [0.7, 0.3]),
        (["ETH-USDT", "BTC-USDT", "SOL-USDT"], [0.34, 0.33, 0.33]),
    ]:
        r = run_portfolio(pairs, "1h", 5 * 365, sig_fn, weights=weights, long_only=True)
        print(f"\n  {pairs} {weights}:")
        print(f"    totals: {r.stats}")
        print(f"    rolling-30d: {rolling_30d(r.equity_curve)}")
        print(f"    year-by-year:")
        print("    " + yearly(r.equity_curve).to_string(index=False).replace("\n", "\n    "))

    print("\n--- Stress test on ETH 5y: fee 0.10% + slip 5bp ---")
    stress = BTConfig(fee_rate=0.001, slip_bps=5.0)
    res, _ = run_one("ETH-USDT", "1h", 5 * 365, stress)
    print(f"  stressed: {res.stats()}")
