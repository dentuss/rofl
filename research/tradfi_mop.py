"""TRADFI TSMOM — the literature-canonical cell. Our crypto-tuned fast-trend
stack FAILED on commodities (tradfi_sleeve.py: full +0.06 over 24y). Before
closing the asset class: test the spec whose parameters were pre-registered
by the LITERATURE, not by us — Moskowitz, Ooi & Pedersen (2012):

  ONE CELL, NO GRID: sign of the trailing 12-month return (skip nothing),
  rebalanced monthly, position scaled to 40%/ann-vol per name (ex-ante
  60d vol), EW across GC/SI/CL/BZ, cost 8bp per unit turnover.

Multiple-testing note: this is the SECOND test on this data. It is
admissible only because the spec is fixed externally (MOP 2012, Table 1
universe includes these markets); any further variant would be mining and
is not allowed regardless of outcome.

GATE (same as tradfi_sleeve): full Sh(mo) >= 0.5 AND pre-2023-08 >= 0.3.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/tradfi_mop.py
"""
from __future__ import annotations

import sys as _sys, os as _os
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

import numpy as np
import pandas as pd

from research.tradfi_sleeve import NAMES, SPLIT, fetch_yahoo_daily, sh

COST_TURN = 0.0008
VOL_TARGET_A = 0.40


def main():
    closes = {}
    for sym, tag in NAMES.items():
        closes[tag] = fetch_yahoo_daily(sym)["close"]
    C = pd.DataFrame(closes).sort_index()

    r1 = C.pct_change()
    vol = r1.rolling(60, min_periods=40).std() * np.sqrt(252)
    sig = np.sign(C / C.shift(252) - 1)
    w = (VOL_TARGET_A / vol).clip(upper=4.0)
    pos_daily = (sig * w).shift(1)
    is_reb = pd.Series(C.index.day <= 3, index=C.index) & \
        (pd.Series(C.index.month, index=C.index).diff() != 0)
    pos = pos_daily.where(is_reb.to_numpy()[:, None] *
                          np.ones((1, C.shape[1])) > 0).ffill()
    n = C.notna().sum(axis=1).clip(lower=1)
    gross = (pos * r1).sum(axis=1) / n
    turn = pos.diff().abs().sum(axis=1) / n
    r = gross - turn * COST_TURN

    eq = (1 + r.fillna(0)).cumprod()
    mo = eq.resample("ME").last().pct_change().dropna()
    pre, post = mo[mo.index < SPLIT], mo[mo.index >= SPLIT]
    full_s, pre_s, post_s = sh(mo), sh(pre), sh(post)
    mdd = float((eq / eq.cummax() - 1).min())

    print("TRADFI TSMOM (MOP-2012 canonical: 12m sign, monthly, vol-scaled)")
    print(f"  {r.index[0].date()}..{r.index[-1].date()}  "
          f"full {full_s:+.2f}  pre-2023-08 {pre_s:+.2f}  post {post_s:+.2f}"
          f"  MDD {mdd*100:.1f}%")
    for dec, g in mo.groupby((mo.index.year // 5) * 5):
        print(f"    {dec}-{dec+4}: Sh {sh(g):+5.2f}  ({len(g)} months)")
    ok = full_s >= 0.5 and pre_s >= 0.3
    print(f"\nGATE VERDICT: {'PASS — assembly next' if ok else 'FAIL — '
          'commodities CLOSED (two admissible tests spent)'}")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
