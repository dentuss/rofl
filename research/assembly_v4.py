"""ASSEMBLY v4 — round-2 seat (XSBAB-60) joined to book + XSMOM-21 (+MOPTF
reference). Same pre-registered protocol as v3: IV / CAP40 (no sleeve above
40%) / EQ schemes all shown, plus the deployable proposals with the book
majority-weighted. MOPTF is shown but stays OUT of the deployable cells
(venue stage pending).

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/assembly_v4.py
"""
from __future__ import annotations

import sys as _sys, os as _os
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

import numpy as np
import pandas as pd

from core.data import fetch_ohlcv_bybit
from core.funding import fetch_funding
from research.assembly_v3 import (BOOK_CACHE, moptf_daily, sh, stats_line,
                                  xsmom21_daily)
from research.sleeve_battery import build_book_daily
from research.sleeve_diagnosis import port_returns, base_w
from research.tsmom_sleeve import QUAL23

SPLIT = pd.Timestamp("2023-08-17", tz="UTC")


def xsbab60_daily() -> pd.Series:
    closes, fund_d = {}, {}
    for p in QUAL23:
        closes[p] = fetch_ohlcv_bybit(p, "1d", days=2000)["close"]
        f = fetch_funding(p, days=2000, source="auto")
        fund_d[p] = f["funding_rate"].resample("1D").sum() \
            if f is not None and len(f) else pd.Series(dtype=float)
    C = pd.DataFrame(closes)
    F = pd.DataFrame(fund_d)
    r1 = C.pct_change()
    rb = r1["BTC-USDT"]
    beta = r1.rolling(60, min_periods=40).cov(rb) \
        .div(rb.rolling(60, min_periods=40).var(), axis=0).shift(1) \
        .drop(columns=["BTC-USDT"])
    rank = beta.rank(axis=1, pct=True)
    raw = pd.DataFrame(0.0, index=C.index, columns=beta.columns)
    raw[rank <= 0.2] = 1.0
    raw[rank >= 0.8] = -1.0
    is_reb = pd.Series(C.index.dayofweek == 0, index=C.index)
    sigw = raw.where(is_reb).ffill().fillna(0.0)
    pos = (sigw * base_w(C).drop(columns=["BTC-USDT"])).fillna(0.0)
    pos = pos.reindex(columns=C.columns).fillna(0.0)
    return port_returns(C, F, pos)


def main():
    if _os.path.exists(BOOK_CACHE):
        book = pd.read_parquet(BOOK_CACHE)["r"]
    else:
        book = build_book_daily()
        pd.DataFrame({"r": book}).to_parquet(BOOK_CACHE)
    xs = xsmom21_daily()
    bab = xsbab60_daily()
    tf = moptf_daily()
    tf.index = tf.index.tz_localize("UTC") if tf.index.tz is None else tf.index

    D = pd.concat([book.rename("book"), xs.rename("xsmom"),
                   bab.rename("xsbab"), tf.rename("moptf")],
                  axis=1, sort=True)
    D = D[D.index >= SPLIT].dropna(subset=["book"])
    D["moptf"] = D["moptf"].fillna(0.0)
    D = D.dropna()

    print("=" * 88)
    print(f"COMPONENTS  {D.index[0].date()}..{D.index[-1].date()}")
    print("=" * 88)
    mos = {}
    for c, label in (("book", "BOOK (deployed)"), ("xsmom", "XSMOM21"),
                     ("xsbab", "XSBAB60"), ("moptf", "MOP-TSMOM")):
        mos[c] = stats_line(D[c], label)
    cm = pd.concat(mos.values(), axis=1)
    cm.columns = list(mos)
    print("\n  monthly corr matrix:")
    print(cm.corr().round(2).to_string().replace("\n", "\n  "))

    crypto = D[["book", "xsmom", "xsbab"]]
    iv = 1.0 / crypto.std()
    iv = iv / iv.sum()
    cap = iv.copy()
    for c in cap.index:
        if cap[c] > 0.40:
            excess = cap[c] - 0.40
            cap[c] = 0.40
            others = cap.drop(c)
            cap.loc[others.index] = others + excess * others / others.sum()
    print(f"\n  weights  IV: " + "  ".join(f"{c} {iv[c]:.2f}" for c in crypto)
          + "   CAP40: " + "  ".join(f"{c} {cap[c]:.2f}" for c in crypto))

    print("\n" + "=" * 88)
    print("ASSEMBLED (deployable candidates exclude MOPTF pending its venue "
          "stage)")
    print("=" * 88)
    stats_line((crypto * iv).sum(axis=1), "3-way IV")
    stats_line((crypto * cap).sum(axis=1), "3-way CAP40")
    stats_line(crypto.mul(pd.Series(
        {"book": 0.5, "xsmom": 0.25, "xsbab": 0.25})).sum(axis=1),
        "BOOK50/XS25/BAB25")
    stats_line(crypto.mul(pd.Series(
        {"book": 0.6, "xsmom": 0.2, "xsbab": 0.2})).sum(axis=1),
        "BOOK60/XS20/BAB20")
    stats_line(D[["book", "xsmom"]].mul(pd.Series(
        {"book": 0.6, "xsmom": 0.4})).sum(axis=1), "BOOK60/XSMOM40 (v3)")

    r_dep = crypto.mul(pd.Series(
        {"book": 0.5, "xsmom": 0.25, "xsbab": 0.25})).sum(axis=1)
    vol = float(r_dep.std() * np.sqrt(365))
    for d in (0.15, 0.25):
        lev = d / vol
        eq = (1 + r_dep * lev).cumprod()
        mo = eq.resample("ME").last().pct_change().dropna()
        yrs = max((r_dep.index[-1] - r_dep.index[0]).days, 1) / 365
        cagr = float(eq.iloc[-1]) ** (1 / yrs) - 1
        mdd = float((eq / eq.cummax() - 1).min())
        print(f"  BOOK50/XS25/BAB25 @ {d:.0%} vol (x{lev:.1f}): "
              f"CAGR {cagr*100:5.1f}%  Sh {sh(mo):+.2f}  dMDD {mdd*100:6.1f}%"
              f"  worst mo {float(mo.min())*100:5.1f}%")

    # long view: all three sleeves, 2021+
    L = pd.concat([xs.rename("xsmom"), bab.rename("xsbab"),
                   tf.rename("moptf")], axis=1, sort=True)
    L["moptf"] = L["moptf"].fillna(0.0)
    L = L.dropna()
    ivl = 1.0 / L.std()
    ivl = ivl / ivl.sum()
    print("\nLONG VIEW (three sleeves, no book, from first common data):")
    stats_line((L * ivl).sum(axis=1), "XS+BAB+MOPTF 2021+")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
