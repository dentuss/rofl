"""Scan every (strategy, pair, timeframe) combination and rank them.

This is the strategy-selection step. We score each config by a composite
that rewards profit, profit factor, and trade count, and penalises drawdown.
"""
from __future__ import annotations

from itertools import product

import pandas as pd

from backtest import BTConfig, run_backtest
from data import fetch_ohlcv
from strategies import REGISTRY

PAIRS = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
TIMEFRAMES = ["15m", "1h"]
DEFAULT_DAYS = 365  # use full year - 30d is too short to detect regime risk


def score(s: dict) -> float:
    """Composite ranking score. Higher is better."""
    if s.get("trades", 0) < 5:
        return -1e9  # not enough samples
    pf = s["profit_factor"]
    if pf == float("inf"):
        pf = 5.0
    pf = min(pf, 5.0)
    ret = s["total_return"]
    dd = abs(s["max_drawdown"])
    # Reward return and PF, punish drawdown, soft floor on trade count
    return ret * 100 - dd * 60 + pf * 5 + min(s["trades"], 40) * 0.1


def scan(days: int = DEFAULT_DAYS) -> pd.DataFrame:
    rows = []
    for pair, tf in product(PAIRS, TIMEFRAMES):
        df = fetch_ohlcv(pair, tf, days=days)
        for name, sc in REGISTRY.items():
            sig = sc.fn(df)
            res = run_backtest(df, sig, BTConfig(), long_only=sc.long_only)
            st = res.stats()
            rows.append({
                "strategy": name, "pair": pair, "tf": tf,
                **st, "score": round(score(st), 2),
            })
    out = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
    return out


if __name__ == "__main__":
    import sys
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)
    days = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DAYS
    print(f"Scanning {days} days of history...")
    df = scan(days=days)
    print(df.to_string(index=False))
    print()
    print("Top 5:")
    print(df.head(5).to_string(index=False))
