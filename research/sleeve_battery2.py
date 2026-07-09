"""SLEEVE BATTERY 2 — round two of the stacking hunt (round 1: 2 seats /
9 candidates). Same seat price (sleeve law): full Sh(mo) >= 0.5 AND
pre-2023-08 >= 0.0 AND |corr(monthly, deployed book)| <= 0.5. All cells use
the uniform sleeve cost convention (port_returns: 8bp/unit turnover + REAL
per-pair funding) and base_w inverse-vol sizing. Book monthly from the
cached daily series (assembly_v3).

FRESH DIMENSIONS (nothing here re-tests a dead family; funding-family and
commodities budgets are spent and untouched):
  XSVOL-21    dollar-volume (attention) momentum: 21d mean $vol / 126d mean
              - 1, weekly rank; LONG rising-attention quintile, SHORT
              fading (high-volume premium, Gervais et al.)
  XSBAB-60    betting-against-beta: 60d beta vs BTC, weekly rank; LONG
              low-beta quintile, SHORT high-beta (plain quintiles + base_w;
              no beta-parity leverage — stated simplification)
  BREADTH-LF  participation timing: breadth = share of QUAL23 above own
              MA50; LONG the MAJORS8 basket when breadth > 0.5 AND rising
              (5d change > 0), else flat
  BREADTH-LS  BREADTH-LF plus SHORT the basket when breadth < 0.3 AND
              falling
  FNG-CONTRA  BTC contrarian on ENTRENCHED sentiment: >=3 consecutive days
              F&G <= 20 -> LONG until F&G > 40; >=3 days >= 80 -> SHORT
              until < 60 (the bot uses this signal as a brake; this tests
              it as an engine)
  DOMTREND-90 dominance trend: sign of ETH/BTC 90d change, weekly stamps;
              LONG ETH / SHORT BTC when positive, reverse when negative
              (trend sibling of the DEAD ETHBTC-MR — opposite family)

Multiple testing: 6 cells; expect ~0-1 false positives at this bar.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/sleeve_battery2.py
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
from core.sentiment import fetch_fear_greed
from research.assembly_v3 import BOOK_CACHE
from research.sleeve_battery import verdict, build_book_daily
from research.sleeve_diagnosis import port_returns, base_w
from research.tsmom_sleeve import QUAL23

MAJORS8 = [f"{b}-USDT" for b in
           ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX"]]


def weekly(sig_daily: pd.DataFrame) -> pd.DataFrame:
    is_reb = pd.Series(sig_daily.index.dayofweek == 0, index=sig_daily.index)
    return sig_daily.where(is_reb).ffill().fillna(0.0)


def main():
    print("SLEEVE BATTERY 2 — seat price: full>=0.5, pre>=0.0, |corr|<=0.5",
          flush=True)
    if _os.path.exists(BOOK_CACHE):
        book = pd.read_parquet(BOOK_CACHE)["r"]
    else:
        book = build_book_daily()
        pd.DataFrame({"r": book}).to_parquet(BOOK_CACHE)
    book_mo = (1 + book).cumprod().resample("ME").last().pct_change().dropna()

    data, closes, vols, fund_d = {}, {}, {}, {}
    for p in QUAL23:
        df = fetch_ohlcv_bybit(p, "1d", days=2000)
        closes[p] = df["close"]
        vols[p] = df["close"] * df["volume"]
        f = fetch_funding(p, days=2000, source="auto")
        fund_d[p] = f["funding_rate"].resample("1D").sum() \
            if f is not None and len(f) else pd.Series(dtype=float)
    C = pd.DataFrame(closes)
    DV = pd.DataFrame(vols)
    F = pd.DataFrame(fund_d)
    bw = base_w(C)
    results = {}

    # XSVOL-21 -------------------------------------------------------------
    att = (DV.rolling(21).mean() / DV.rolling(126).mean() - 1).shift(1)
    rank = att.rank(axis=1, pct=True)
    raw = pd.DataFrame(0.0, index=C.index, columns=C.columns)
    raw[rank >= 0.8] = 1.0
    raw[rank <= 0.2] = -1.0
    pos = (weekly(raw) * bw).fillna(0.0)
    results["XSVOL-21"] = verdict(port_returns(C, F, pos), "XSVOL-21", book_mo)

    # XSBAB-60 -------------------------------------------------------------
    r1 = C.pct_change()
    rb = r1["BTC-USDT"]
    cov = r1.rolling(60, min_periods=40).cov(rb)
    beta = cov.div(rb.rolling(60, min_periods=40).var(), axis=0).shift(1)
    beta = beta.drop(columns=["BTC-USDT"])
    rank = beta.rank(axis=1, pct=True)
    raw = pd.DataFrame(0.0, index=C.index, columns=beta.columns)
    raw[rank <= 0.2] = 1.0            # long low beta
    raw[rank >= 0.8] = -1.0           # short high beta
    pos = (weekly(raw) * bw.drop(columns=["BTC-USDT"])).fillna(0.0)
    pos = pos.reindex(columns=C.columns).fillna(0.0)
    results["XSBAB-60"] = verdict(port_returns(C, F, pos), "XSBAB-60", book_mo)

    # BREADTH --------------------------------------------------------------
    above = (C > C.rolling(50).mean())
    breadth = above.sum(axis=1) / C.notna().sum(axis=1).clip(lower=1)
    rising = breadth.diff(5) > 0
    long_on = ((breadth > 0.5) & rising).shift(1).fillna(False)
    short_on = ((breadth < 0.3) & ~rising).shift(1).fillna(False)
    base = pd.DataFrame(0.0, index=C.index, columns=C.columns)
    for name, sig in (("BREADTH-LF", long_on.astype(float)),
                      ("BREADTH-LS",
                       long_on.astype(float) - short_on.astype(float))):
        raw = base.copy()
        for m in MAJORS8:
            raw[m] = sig
        pos = (raw * bw).fillna(0.0)
        results[name] = verdict(port_returns(C, F, pos), name, book_mo)

    # FNG-CONTRA -----------------------------------------------------------
    fng = fetch_fear_greed()["fng"]
    fng.index = pd.to_datetime(fng.index, utc=True) \
        if fng.index.tz is None else fng.index
    fear3 = (fng <= 20).rolling(3).sum() == 3
    greed3 = (fng >= 80).rolling(3).sum() == 3
    state = 0.0
    posv = []
    for i in range(len(fng)):
        if state == 1.0 and fng.iloc[i] > 40:
            state = 0.0
        elif state == -1.0 and fng.iloc[i] < 60:
            state = 0.0
        if state == 0.0:
            if fear3.iloc[i]:
                state = 1.0
            elif greed3.iloc[i]:
                state = -1.0
        posv.append(state)
    fpos = pd.Series(posv, index=fng.index).shift(1) \
        .reindex(C.index, method="ffill").fillna(0.0)
    raw = pd.DataFrame(0.0, index=C.index, columns=C.columns)
    raw["BTC-USDT"] = fpos
    pos = (raw * bw).fillna(0.0)
    results["FNG-CONTRA"] = verdict(port_returns(C, F, pos), "FNG-CONTRA",
                                    book_mo)

    # DOMTREND-90 ----------------------------------------------------------
    ratio = C["ETH-USDT"] / C["BTC-USDT"]
    sig = np.sign(ratio / ratio.shift(90) - 1).shift(1)
    raw = pd.DataFrame(0.0, index=C.index, columns=C.columns)
    raw["ETH-USDT"] = sig
    raw["BTC-USDT"] = -sig
    pos = (weekly(raw) * bw).fillna(0.0)
    results["DOMTREND-90"] = verdict(port_returns(C, F, pos), "DOMTREND-90",
                                     book_mo)

    print("\n" + "=" * 64)
    print("SEATS — round 2")
    print("=" * 64)
    for k, v in results.items():
        print(f"  {'SEAT EARNED' if v else 'dead      '}  {k}")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
