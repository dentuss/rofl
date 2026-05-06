"""Final validation of the chosen strategy.

Winner: donchian_breakout + Fear & Greed sentiment filter on ETH/USDT 1h.
    Donchian: entry_n=20, adx_min=20, sl_mult=2.5, tp_mult=5.0
    Sentiment: skip SHORT entries when F&G index < 25 (extreme fear)

Backtest evidence (100 USDT start, 0.06% fee, 2bp slippage, 1y ETH/USDT 1h):
    Baseline Donchian: +98.72% Sharpe 2.33 MDD -12.2% rolling-month-win 75%
    + sentiment filter:+93.46% Sharpe 2.42 MDD -10.8% rolling-month-win 92%
The filter trades a small return for a much steadier monthly equity curve
(11/12 months profitable vs 9/12).

Key lesson: a strategy that looks great over 30 days can have a -60%
drawdown across a full year. Always validate over enough history to see
at least one adverse regime.
"""
from __future__ import annotations

import pandas as pd

from backtest import BTConfig, run_backtest
from data import fetch_ohlcv
from sentiment import fetch_fear_greed
from strategies import donchian_breakout
from strategies_sentiment import donchian_skip_fear

CHOSEN = dict(entry_n=20, exit_n=10, adx_n=14, adx_min=20.0,
              atr_n=14, sl_mult=2.5, tp_mult=5.0)
FEAR_THRESHOLD = 25.0


def run_one(pair, tf, days, cfg=BTConfig(), use_sentiment=True):
    df = fetch_ohlcv(pair, tf, days=days)
    if use_sentiment:
        fng = fetch_fear_greed()
        sig = donchian_skip_fear(df, fng, fear_min=FEAR_THRESHOLD, **CHOSEN)
    else:
        sig = donchian_breakout(df, **CHOSEN)
    res = run_backtest(df, sig, cfg)
    return res, df


if __name__ == "__main__":
    pd.set_option("display.width", 200)

    print("=" * 80)
    print("CHOSEN STRATEGY:  Donchian breakout + F&G sentiment filter (long+short)")
    print(f"DONCHIAN: {CHOSEN}")
    print(f"SENTIMENT: skip shorts when F&G < {FEAR_THRESHOLD}")
    print("=" * 80)

    print("\n--- Sentiment-filtered vs baseline (ETH-USDT 1h, 365d) ---")
    for label, use_s in [("baseline (no sentiment)", False),
                         ("with sentiment filter ", True)]:
        res, _ = run_one("ETH-USDT", "1h", 365, use_sentiment=use_s)
        print(f"  {label}:", res.stats())

    print("\n--- ETH-USDT 1h with sentiment, multiple horizons ---")
    for days in (30, 60, 90, 180, 365):
        res, _ = run_one("ETH-USDT", "1h", days)
        print(f"  {days:3d}d:", res.stats())

    print("\n--- Cross-asset sanity (1h, 1y) ---")
    for pair in ("ETH-USDT", "BTC-USDT", "SOL-USDT"):
        res, _ = run_one(pair, "1h", 365)
        print(f"  {pair} 365d:", res.stats())

    print("\n--- Stress test on ETH 1h 365d: fee 0.10% + slip 5bp ---")
    stress = BTConfig(fee_rate=0.001, slip_bps=5.0)
    res, _ = run_one("ETH-USDT", "1h", 365, stress)
    print(f"  stressed:", res.stats())

    print("\n--- Rolling non-overlapping 30-day windows on ETH 1h, 1y ---")
    df = fetch_ohlcv("ETH-USDT", "1h", days=365)
    fng = fetch_fear_greed()
    bars_per_day = 24
    w = 30 * bars_per_day
    rows = []
    for i in range(0, len(df) - w + 1, w):
        sub = df.iloc[i:i + w]
        sig = donchian_skip_fear(sub, fng, fear_min=FEAR_THRESHOLD, **CHOSEN)
        res = run_backtest(sub, sig, BTConfig())
        s = res.stats()
        rows.append({
            "window_start": sub.index[0].strftime("%Y-%m-%d"),
            "trades": s["trades"], "win_rate": s["win_rate"],
            "ret": s["total_return"], "mdd": s["max_drawdown"],
            "sharpe": s["sharpe"],
        })
    r = pd.DataFrame(rows)
    print(r.to_string(index=False))
    print(f"\n  Profitable windows: {(r['ret'] > 0).sum()}/{len(r)}")
    print(f"  Median 30d return: {r['ret'].median() * 100:.2f}%")
    print(f"  Worst 30d: {r['ret'].min() * 100:.2f}%")
    print(f"  Best 30d:  {r['ret'].max() * 100:.2f}%")

    print("\n--- Last 5 trades on ETH-USDT 1h, 1y ---")
    res, _ = run_one("ETH-USDT", "1h", 365)
    rows = []
    for t in res.trades[-5:]:
        rows.append({
            "entry": t.entry_time, "exit": t.exit_time,
            "side": t.side, "entry_px": round(t.entry_px, 2),
            "exit_px": round(t.exit_px, 2),
            "notional": round(t.notional, 2),
            "pnl": round(t.pnl, 3),
            "reason": t.reason, "bars": t.bars_held,
        })
    print(pd.DataFrame(rows).to_string(index=False))
