"""ASSEMBLY v3 — the stacking round's survivors joined to the trend book.

Components (each individually seat-earning, see sleeve_battery.py /
tradfi_mop.py):
  BOOK      BLEND50_CONF (deployed; daily returns rebuilt here and cached
            to research/.book_daily.parquet for reuse)
  XSMOM21   QUAL23 21d residual-vs-BTC momentum, weekly, IV-sized, 8bp
            turnover + real funding (Sh 1.00, pre-2023 +0.85, corr 0.18)
  MOPTF     MOP-2012 12m TSMOM on GC/SI/CL/BZ, monthly, vol-scaled, 8bp
            turnover (25y Sh 0.53, every era positive except 2015-19 flat)

PRE-REGISTERED assembly: inverse-vol weights on daily returns over the
common window (same law as every assembly before). Report: pairwise corr,
combined Sh full/IS/OOS + thirds, dial table @15/25%, plus the two-sleeve
(XSMOM+MOPTF) long view from 2021 for context. NOTHING here changes the
live book — survivors still owe forward paper stages (XSMOM weekly
executor; MOPTF on the real 4-month-old Bybit contracts).

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/assembly_v3.py
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
from research.sleeve_battery import build_book_daily
from research.sleeve_diagnosis import port_returns, base_w
from research.tradfi_mop import COST_TURN, VOL_TARGET_A
from research.tradfi_sleeve import NAMES, fetch_yahoo_daily
from research.tsmom_sleeve import QUAL23

SPLIT = pd.Timestamp("2023-08-17", tz="UTC")
BOOK_CACHE = _os.path.join(_PARENT, "research", ".book_daily.parquet")


def sh(mo: pd.Series) -> float:
    return float(mo.mean() / mo.std() * np.sqrt(12)) \
        if len(mo) > 3 and mo.std() > 0 else float("nan")


def stats_line(r: pd.Series, label: str):
    eq = (1 + r.fillna(0)).cumprod()
    mo = eq.resample("ME").last().pct_change().dropna()
    n = len(mo)
    i, o = mo.iloc[:int(n * 0.6)], mo.iloc[int(n * 0.6):]
    b3 = np.array_split(mo, 3)
    th = "  ".join(f"{sh(x):+.2f}" for x in b3)
    mdd = float((eq / eq.cummax() - 1).min())
    print(f"  {label:22s} Sh {sh(mo):+5.2f}  IS {sh(i):+5.2f} -> OOS "
          f"{sh(o):+5.2f}  dMDD {mdd*100:6.1f}%  thirds {th}")
    return mo


def xsmom21_daily() -> pd.Series:
    closes, fund_d = {}, {}
    for p in QUAL23:
        closes[p] = fetch_ohlcv_bybit(p, "1d", days=2000)["close"]
        f = fetch_funding(p, days=2000, source="auto")
        fund_d[p] = f["funding_rate"].resample("1D").sum() \
            if f is not None and len(f) else pd.Series(dtype=float)
    C = pd.DataFrame(closes)
    F = pd.DataFrame(fund_d)
    mom = C.pct_change(21).sub(C["BTC-USDT"].pct_change(21), axis=0) \
        .shift(1).drop(columns=["BTC-USDT"])
    rank = mom.rank(axis=1, pct=True)
    raw = pd.DataFrame(0.0, index=C.index, columns=mom.columns)
    raw[rank >= 0.8] = 1.0
    raw[rank <= 0.2] = -1.0
    is_reb = pd.Series(C.index.dayofweek == 0, index=C.index)
    sigw = raw.where(is_reb).ffill().fillna(0.0)
    pos = (sigw * base_w(C).drop(columns=["BTC-USDT"])).fillna(0.0)
    pos = pos.reindex(columns=C.columns).fillna(0.0)
    return port_returns(C, F, pos)


def moptf_daily() -> pd.Series:
    closes = {tag: fetch_yahoo_daily(sym)["close"]
              for sym, tag in NAMES.items()}
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
    return (pos * r1).sum(axis=1) / n - pos.diff().abs().sum(axis=1) / n \
        * COST_TURN


def main():
    if _os.path.exists(BOOK_CACHE):
        book = pd.read_parquet(BOOK_CACHE)["r"]
        print(f"book daily: cache ({len(book)}d)", flush=True)
    else:
        print("book daily: rebuilding (~3 min) ...", flush=True)
        book = build_book_daily()
        pd.DataFrame({"r": book}).to_parquet(BOOK_CACHE)
    xs = xsmom21_daily()
    tf = moptf_daily()
    tf.index = tf.index.tz_localize("UTC") if tf.index.tz is None else tf.index

    D = pd.concat([book.rename("book"), xs.rename("xsmom"),
                   tf.rename("moptf")], axis=1)
    D = D[D.index >= SPLIT].dropna(subset=["book"])
    D["moptf"] = D["moptf"].fillna(0.0)     # TradFi holidays/weekends: flat
    D = D.dropna()

    print("\n" + "=" * 86)
    print(f"COMPONENTS on the common window  {D.index[0].date()}.."
          f"{D.index[-1].date()}")
    print("=" * 86)
    mo_b = stats_line(D["book"], "BOOK (deployed)")
    mo_x = stats_line(D["xsmom"], "XSMOM21")
    mo_t = stats_line(D["moptf"], "MOP-TSMOM (tradfi)")
    cm = pd.concat([mo_b, mo_x, mo_t], axis=1)
    cm.columns = ["book", "xsmom", "moptf"]
    print("\n  monthly corr matrix:")
    print(cm.corr().round(2).to_string().replace("\n", "\n  "))

    iv = 1.0 / D.std()
    iv = iv / iv.sum()
    # CAP40: same de-concentration protocol as path_weights_history's CAP40
    # cell — no single sleeve above 40% (the carry-0.76 lesson).
    cap = iv.copy()
    for c in cap.index:
        if cap[c] > 0.40:
            excess = cap[c] - 0.40
            cap[c] = 0.40
            others = cap.drop(c)
            cap.loc[others.index] = others + excess * others / others.sum()
    eqw = pd.Series(1 / 3, index=D.columns)
    print(f"\n  weights  IV: " + "  ".join(f"{c} {iv[c]:.2f}" for c in D)
          + "   CAP40: " + "  ".join(f"{c} {cap[c]:.2f}" for c in D))
    r_all = (D * iv).sum(axis=1)

    print("\n" + "=" * 86)
    print("ASSEMBLED (weight schemes — pre-registered sensitivity, all shown)")
    print("=" * 86)
    stats_line(r_all, "3-way IV")
    stats_line((D * cap).sum(axis=1), "3-way CAP40")
    stats_line((D * eqw).sum(axis=1), "3-way EQ")
    stats_line((D[["book", "xsmom"]]
                * (iv[["book", "xsmom"]] / iv[["book", "xsmom"]].sum()))
               .sum(axis=1), "BOOK+XSMOM IV")
    stats_line(D[["book", "xsmom"]].mul(
        pd.Series({"book": 0.6, "xsmom": 0.4})).sum(axis=1),
        "BOOK60+XSMOM40")
    vol_all = float(r_all.std() * np.sqrt(365))
    for d in (0.15, 0.25):
        lev = d / vol_all
        eq = (1 + r_all * lev).cumprod()
        mo = eq.resample("ME").last().pct_change().dropna()
        yrs = max((r_all.index[-1] - r_all.index[0]).days, 1) / 365
        cagr = (float(eq.iloc[-1])) ** (1 / yrs) - 1
        mdd = float((eq / eq.cummax() - 1).min())
        print(f"  @ {d:.0%} vol (x{lev:.1f}): CAGR {cagr*100:5.1f}%  "
              f"Sh {sh(mo):+.2f}  dMDD {mdd*100:6.1f}%  "
              f"worst mo {float(mo.min())*100:5.1f}%")

    # long view: the two new sleeves from 2021 (book didn't exist then)
    L = pd.concat([xs.rename("xsmom"), tf.rename("moptf")], axis=1)
    L["moptf"] = L["moptf"].fillna(0.0)
    L = L.dropna()
    ivl = 1.0 / L.std()
    ivl = ivl / ivl.sum()
    print("\nLONG VIEW (sleeves only, from first common data):")
    stats_line((L * ivl).sum(axis=1), "XSMOM+MOPTF 2021+")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
