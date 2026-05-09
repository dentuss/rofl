"""Compare triple_long on multiple timeframes over 5 years.

Quick win check: do more trades on 15m/30m beat the fee drag and outperform 1h?
"""
from __future__ import annotations

import sys as _sys, os as _os
_THIS_DIR_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _THIS_DIR_PARENT not in _sys.path: _sys.path.insert(0, _THIS_DIR_PARENT)

import pandas as pd

from core.backtest import BTConfig, run_backtest
from core.data import fetch_ohlcv
from core.strategies import triple_confirm_long

BASE = dict(ema_fast=9, ema_slow=26, ema_trend=50,
            rsi_min=55.0, adx_min=22.0,
            atr_n=14, sl_mult=1.8, tp_mult=3.0)


def cagr(start_eq, end_eq, days):
    return (end_eq / start_eq) ** (365 / days) - 1


def yearly(eq):
    rows = []
    for y in sorted({d.year for d in eq.index}):
        s = eq[eq.index.year == y]
        if len(s) < 100:
            continue
        ret = s.iloc[-1] / s.iloc[0] - 1
        peak = s.cummax()
        dd = (s / peak - 1).min()
        rows.append({"year": y, "ret": round(ret, 4), "mdd": round(float(dd), 4)})
    return pd.DataFrame(rows)


def rolling_30d(eq, bpd):
    w = 30 * bpd
    rs = []
    for i in range(0, len(eq) - w + 1, w):
        sub = eq.iloc[i:i + w]
        rs.append(sub.iloc[-1] / sub.iloc[0] - 1)
    rs = pd.Series(rs)
    return {
        "windows": len(rs),
        "win_pct": round(float((rs > 0).mean()), 3),
        "median_pct": round(float(rs.median()) * 100, 2),
        "mean_pct": round(float(rs.mean()) * 100, 2),
        "min_pct": round(float(rs.min()) * 100, 2),
        "max_pct": round(float(rs.max()) * 100, 2),
    }


def main():
    pd.set_option("display.width", 220)
    BPD = {"15m": 96, "30m": 48, "1h": 24}

    print("=" * 80)
    print("triple_long on multiple timeframes — 5 year backtest")
    print("=" * 80)
    rows = []
    for pair in ("ETH-USDT", "BTC-USDT", "SOL-USDT"):
        for tf in ("1h", "30m", "15m"):
            df = fetch_ohlcv(pair, tf, days=5 * 365)
            sig = triple_confirm_long(df, **BASE)
            res = run_backtest(df, sig, BTConfig(), long_only=True)
            stats = res.stats()
            days = (df.index[-1] - df.index[0]).total_seconds() / 86400
            cagr_pct = cagr(100, stats["final_equity"], days) * 100
            roll = rolling_30d(res.equity_curve, BPD[tf])
            rows.append({
                "pair": pair, "tf": tf,
                "trades": stats["trades"],
                "wr": stats["win_rate"],
                "ret": stats["total_return"],
                "mdd": stats["max_drawdown"],
                "sharpe": stats["sharpe"],
                "cagr_pct": round(cagr_pct, 2),
                "monthly_med_pct": roll["median_pct"],
                "monthly_win_pct": roll["win_pct"],
            })
    out = pd.DataFrame(rows)
    print(out.to_string(index=False))


if __name__ == "__main__":
    main()
