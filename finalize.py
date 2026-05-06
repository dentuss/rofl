"""Final validation of the chosen strategy.

Winner: donchian_breakout on ETH/USDT 1h with parameters
    entry_n=20, adx_min=20, sl_mult=2.5, tp_mult=5.0
chosen for the best risk-adjusted performance over the FULL YEAR (not just
30 days, which is too short to detect regime risk).

Backtest evidence (100 USDT start, 0.06% fee, 2bp slippage):
    365d:  +101.67% (244 trades, 43.9% WR, PF 1.37, MDD 12.2%, Sharpe 2.37)
    180d:  +21.25% (117 trades, 41.9% WR, PF 1.24, MDD 12.2%, Sharpe 1.41)
Rolling 30-day windows over 1y: 10/12 profitable, median +5.0%/month.
Param sweep: 303/324 configs profitable on 1y (94%).

Key lesson: a strategy that looks great over 30 days can have a -60% drawdown
across a full year. Always validate over enough history to see at least one
adverse regime. We did, and chose accordingly.
"""
from __future__ import annotations

import pandas as pd

from backtest import BTConfig, run_backtest
from data import fetch_ohlcv
from strategies import donchian_breakout

CHOSEN = dict(entry_n=20, exit_n=10, adx_n=14, adx_min=20.0,
              atr_n=14, sl_mult=2.5, tp_mult=5.0)


def run_one(pair, tf, days, cfg=BTConfig()):
    df = fetch_ohlcv(pair, tf, days=days)
    sig = donchian_breakout(df, **CHOSEN)
    res = run_backtest(df, sig, cfg)
    return res, df


if __name__ == "__main__":
    pd.set_option("display.width", 200)

    print("=" * 80)
    print("CHOSEN STRATEGY:  donchian_breakout (long+short)")
    print(f"PARAMS: {CHOSEN}")
    print("=" * 80)

    print("\n--- ETH-USDT 1h, multiple horizons ---")
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
    bars_per_day = 24
    w = 30 * bars_per_day
    rows = []
    for i in range(0, len(df) - w + 1, w):
        sub = df.iloc[i:i + w]
        sig = donchian_breakout(sub, **CHOSEN)
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
