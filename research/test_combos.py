"""Test combinations of the enhancements that survived individual testing.

Survivors: sentiment filter, multi-pair (ETH+SOL), HTF daily trend filter.
Killed: trailing stop, vol filter, ADX>=30.
"""
from __future__ import annotations

import sys as _sys, os as _os
_THIS_DIR_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _THIS_DIR_PARENT not in _sys.path: _sys.path.insert(0, _THIS_DIR_PARENT)

import pandas as pd

from core.backtest import BTConfig, run_backtest
from core.data import fetch_ohlcv
from core.portfolio import run_portfolio
from core.sentiment import fetch_fear_greed
from core.strategies import donchian_breakout
from core.strategies_enhanced import with_htf_trend_filter
from core.strategies_sentiment import donchian_skip_fear

PARAMS = dict(entry_n=20, exit_n=10, adx_n=14, adx_min=20.0,
              atr_n=14, sl_mult=2.5, tp_mult=5.0)


def rolling_stats(eq, win_days=30, bars_per_day=24):
    w = win_days * bars_per_day
    rets = []
    mdds = []
    for i in range(0, len(eq) - w + 1, w):
        sub = eq.iloc[i:i + w]
        rets.append(sub.iloc[-1] / sub.iloc[0] - 1)
        mdds.append((sub / sub.cummax() - 1).min())
    rs = pd.Series(rets)
    ms = pd.Series(mdds)
    return {
        "win_pct": round((rs > 0).mean(), 3),
        "median": round(rs.median(), 4),
        "mean": round(rs.mean(), 4),
        "min": round(rs.min(), 4),
        "max": round(rs.max(), 4),
        "med_mdd": round(ms.median(), 4),
    }


def main():
    pd.set_option("display.width", 220)
    fng = fetch_fear_greed()

    def sig_sentiment(local_df):
        return donchian_skip_fear(local_df, fng, fear_min=25, **PARAMS)

    def sig_sent_htf(local_df):
        s = donchian_skip_fear(local_df, fng, fear_min=25, **PARAMS)
        return with_htf_trend_filter(local_df, s, htf_rule="1D", ema_n=50)

    def sig_baseline(local_df):
        return donchian_breakout(local_df, **PARAMS)

    print("=" * 80)
    print("FINAL COMBO COMPARISON (1y, 100 USDT, 0.06% fee, 2bp slip)")
    print("=" * 80)

    rows = []

    # Single-pair variants
    df_eth = fetch_ohlcv("ETH-USDT", "1h", days=365)
    for label, fn in [
        ("[1] ETH only, baseline",                  sig_baseline),
        ("[2] ETH only, sentiment",                 sig_sentiment),
        ("[3] ETH only, sentiment+HTF",             sig_sent_htf),
    ]:
        sig = fn(df_eth)
        res = run_backtest(df_eth, sig, BTConfig())
        rows.append({"variant": label, **res.stats(),
                     **{f"m_{k}": v for k, v in rolling_stats(res.equity_curve).items()}})

    # Portfolio variants
    for label, pairs, weights, fn in [
        ("[4] ETH+SOL 50/50, sentiment",        ["ETH-USDT", "SOL-USDT"], [0.5, 0.5], sig_sentiment),
        ("[5] ETH+SOL 70/30, sentiment",        ["ETH-USDT", "SOL-USDT"], [0.7, 0.3], sig_sentiment),
        ("[6] ETH+SOL 70/30, sentiment+HTF",    ["ETH-USDT", "SOL-USDT"], [0.7, 0.3], sig_sent_htf),
        ("[7] ETH+SOL 50/50, sentiment+HTF",    ["ETH-USDT", "SOL-USDT"], [0.5, 0.5], sig_sent_htf),
    ]:
        r = run_portfolio(pairs, "1h", 365, fn, weights=weights)
        rows.append({"variant": label,
                     "trades": "—", "win_rate": "—",
                     "avg_win": "—", "avg_loss": "—", "profit_factor": "—",
                     "total_return": r.stats["total_return"],
                     "final_equity": r.stats["final_equity"],
                     "max_drawdown": r.stats["max_drawdown"],
                     "sharpe": r.stats["sharpe"],
                     **{f"m_{k}": v for k, v in rolling_stats(r.equity_curve).items()}})

    df = pd.DataFrame(rows)
    cols_total = ["variant", "total_return", "final_equity", "max_drawdown", "sharpe"]
    cols_monthly = ["variant", "m_win_pct", "m_median", "m_mean", "m_min", "m_max", "m_med_mdd"]
    print("\n--- 1-year totals ---")
    print(df[cols_total].to_string(index=False))
    print("\n--- Rolling 30d non-overlapping windows ---")
    print(df[cols_monthly].to_string(index=False))


if __name__ == "__main__":
    main()
