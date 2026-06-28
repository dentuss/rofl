"""Research: does ETH earn its slice, or should it be swapped/dropped?

ETH was the laggard in the live run (0/4 closed, dragging open short, lowest
Sharpe, lot-step-inefficient at a ~$45 slice). Compare the current 5-pair
inj_heavy basket against swapping ETH for a higher-Sharpe pre-screened pair
(RUNE/AAVE/NEAR, per FINDINGS) and against dropping to 4 pairs.

Portfolio = weighted sum of per-pair slices (matches the per-bot architecture:
each bot trades its own slice independently). Raw bidir signal (no regime/F&G
here — relative comparison across baskets is what matters). Research only.

Run:  python research/test_eth_swap.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.backtest import BTConfig, run_backtest  # noqa: E402
from core.data import fetch_ohlcv  # noqa: E402
from core.strategies import triple_confirm_bidir  # noqa: E402

BASE = dict(ema_fast=9, ema_slow=26, ema_trend=50, rsi_min=55.0, adx_min=22.0,
            atr_n=14, sl_mult=1.8, tp_mult=3.0)
DAYS = 400
CFG = BTConfig(starting_equity=100.0, risk_per_trade=0.02, max_leverage=5.0,
               max_bars_in_trade=96, allow_short=True)

# inj_heavy weights
W = dict(INJ=0.40, SOL=0.20, ADA=0.15, ETH=0.15, LINK=0.10)


def pair_returns(pair: str) -> pd.Series:
    df = fetch_ohlcv(f"{pair}-USDT", "1h", days=DAYS)
    sig = triple_confirm_bidir(df, **BASE)
    eq = run_backtest(df, sig, CFG, long_only=False).equity_curve
    return eq.pct_change().fillna(0.0).rename(pair)


def portfolio_stats(weights: dict, rets: dict) -> dict:
    cols = [rets[p] for p in weights]
    R = pd.concat(cols, axis=1, join="inner").fillna(0.0)
    w = np.array([weights[p] for p in weights])
    w = w / w.sum()
    port = (R.values * w).sum(axis=1)
    eq = (1 + pd.Series(port, index=R.index)).cumprod()
    total = eq.iloc[-1] - 1
    mdd = float((eq / eq.cummax() - 1).min())
    # monthly Sharpe (matches FINDINGS methodology)
    m = eq.resample("ME").last().pct_change().dropna()
    sharpe = float(m.mean() / m.std() * np.sqrt(12)) if m.std() > 0 else 0.0
    return dict(total=float(total), sharpe=sharpe, mdd=mdd, worst_mo=float(m.min()))


def main():
    pairs = ["INJ", "SOL", "ADA", "ETH", "LINK", "RUNE", "AAVE", "NEAR"]
    rets = {}
    for p in pairs:
        try:
            rets[p] = pair_returns(p)
        except Exception as e:
            print(f"{p}: fetch/bt failed: {e}")
    print(f"\nPer-pair (standalone, {DAYS}d): " +
          "  ".join(f"{p} {(((1+rets[p]).prod())-1)*100:+.0f}%" for p in pairs if p in rets))

    variants = {
        "keep ETH (current)":  dict(INJ=.40, SOL=.20, ADA=.15, ETH=.15, LINK=.10),
        "swap ETH->RUNE":      dict(INJ=.40, SOL=.20, ADA=.15, RUNE=.15, LINK=.10),
        "swap ETH->AAVE":      dict(INJ=.40, SOL=.20, ADA=.15, AAVE=.15, LINK=.10),
        "swap ETH->NEAR":      dict(INJ=.40, SOL=.20, ADA=.15, NEAR=.15, LINK=.10),
        "drop ETH (4-pair)":   dict(INJ=.40, SOL=.20, ADA=.15, LINK=.10),
    }
    print(f"\nPortfolio comparison ({DAYS}d, inj_heavy weights):\n")
    print(f"  {'variant':22s}{'return':>9s}{'Sharpe(mo)':>12s}{'MDD':>8s}{'worstMo':>9s}")
    for name, wts in variants.items():
        if not all(p in rets for p in wts):
            print(f"  {name:22s}  (missing pair data)"); continue
        s = portfolio_stats(wts, rets)
        print(f"  {name:22s}{s['total']*100:+8.0f}%{s['sharpe']:>12.2f}"
              f"{s['mdd']*100:>7.0f}%{s['worst_mo']*100:>8.0f}%")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
