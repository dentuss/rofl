"""Portfolio assembly v2 — the Phase-4 promoted trend book (BLEND50_CONF)
replaces MAJORS8/RSCD3+VT as the trend component.

A. TREND-ONLY vol dials (the DEPLOYABLE book — sleeves are still blocked by
   the long-history gate): daily-granularity path at 15/25/35/50% ann vol.
B. 3-SLEEVE assembly (INFORMATION ONLY — leverage on this book stays blocked
   until the sleeves earn it forward): trend v2 + TSMOM-90 + carry,
   inverse-vol weights recomputed on the new trend stream, same dials.
C. Month grid for the trend-only book @ 25% vol.

Everything daily-granularity (the honest path lesson: month-end smoothing
understated MDD by half). Includes tp_as_limit=True (adopted 2026-07-06,
research/tp_limit.py — 0 fills lost, fees -18% on the triple book).

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/assemble_v2.py
"""
from __future__ import annotations

import sys as _sys, os as _os, time
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

import numpy as np
import pandas as pd

import core.regime as _regime
from core.backtest_enhanced import EnhancedBTConfig, run_backtest_enhanced
from core.data import fetch_ohlcv_bybit
from core.funding import fetch_funding
from core.risk import DEFAULT_DECAY_TIERS
from core.sentiment import align_to_bars, fetch_fear_greed
from core.strategies import triple_confirm_bidir
from research.cost_engine import (apply_funding_real, regime_mask, fng_persist,
                                  build, FEE_TAKER, FEE_MAKER)
from research.entry_families import pullback_in_trend
from research.regime_upgrades import wf_labels_conf
from research.tsmom_sleeve import sleeve_returns, QUAL23
from research.carry_sleeve import carry_returns
from research.vol_target import vt_mult

MAJORS8 = [f"{b}-USDT" for b in
           ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX"]]
TOTAL = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))
COMMON_START = pd.Timestamp("2023-08-17", tz="UTC")
WARMUP_D = 365
BPD = 6
VTGTS = (0.15, 0.25, 0.35, 0.50)


def dstats(r: pd.Series, label: str, lev: float = 1.0):
    r = r * lev
    eq = TOTAL * (1 + r).cumprod()
    yrs = max((r.index[-1] - r.index[0]).days, 1) / 365
    cagr = (float(eq.iloc[-1]) / TOTAL) ** (1 / yrs) - 1
    mdd = float((eq / eq.cummax() - 1).min())
    mo = eq.resample("ME").last().pct_change().dropna()
    sh_m = float(mo.mean() / mo.std() * np.sqrt(12)) if mo.std() > 0 else 0.0
    wk = eq.resample("W").last().pct_change().dropna()
    n = len(mo)
    i, o = mo.iloc[:int(n * 0.6)], mo.iloc[int(n * 0.6):]
    shi = float(i.mean() / i.std() * np.sqrt(12)) if i.std() > 0 else 0.0
    sho = float(o.mean() / o.std() * np.sqrt(12)) if o.std() > 0 else 0.0
    print(f"{label:24s}{float(eq.iloc[-1]):>9.0f}{cagr*100:>8.1f}{sh_m:>7.2f}"
          f"{mdd*100:>8.1f}{float(r.min())*100:>8.1f}{float(wk.min())*100:>8.1f}"
          f"{float(mo.min())*100:>8.1f}{shi:>7.2f}{sho:>8.2f}")
    return eq


HDR = (f"{'':24s}{'final$':>9s}{'CAGR%':>8s}{'Sh(mo)':>7s}{'dMDD%':>8s}"
       f"{'worstD%':>8s}{'worstW%':>8s}{'worstM%':>8s}{'IS Sh':>7s}{'OOS Sh':>8s}")


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print("ASSEMBLY v2 — building BLEND50_CONF trend book ...", flush=True)
    fng = fetch_fear_greed()

    eq_t, eq_p = {}, {}
    for p in MAJORS8:
        t0 = time.time()
        df = fetch_ohlcv_bybit(p, "4h", days=DAYS)
        regs, conf = wf_labels_conf(df, BPD)
        fa = align_to_bars(fng, df.index)
        fund = fetch_funding(p, days=DAYS, source="auto")
        a = regs.reindex(df.index, method="ffill").fillna("CHOP")
        mult = np.where(a == "CHOP", 0.5, 1.0) * \
            vt_mult(df).reindex(df.index).fillna(1.0).to_numpy() * \
            (0.5 + 0.5 * conf.reindex(df.index).fillna(1.0).to_numpy())
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]
        for tag, raw in [("t", triple_confirm_bidir(df, tp_mult=6.0)),
                         ("p", pullback_in_trend(df))]:
            sig = fng_persist(regime_mask(raw, regs), fa)
            sig["risk_mult"] = mult
            cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                                   max_leverage=5.0,
                                   eq_decay_tiers=DEFAULT_DECAY_TIERS,
                                   cooldown_bars=3, fee_rate=FEE_TAKER,
                                   fee_maker=FEE_MAKER,
                                   entry_style="maker_close", tp_as_limit=True)
            eq, tr = run_backtest_enhanced(dfe, sig[sig.index >= cut], cfg)
            (eq_t if tag == "t" else eq_p)[p] = apply_funding_real(eq, tr, fund)
        print(f"  {p:10s} {time.time()-t0:5.0f}s", flush=True)

    idx = None
    for p in MAJORS8:
        e = eq_t[p][eq_t[p].index >= COMMON_START]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    w = {p: 1 / 8 for p in MAJORS8}
    pt = build(eq_t, w, idx)
    pp = build(eq_p, w, idx)
    blend = 0.5 * pt / pt.iloc[0] + 0.5 * pp / pp.iloc[0]
    trend_d = blend.resample("1D").last().pct_change().dropna()

    # sleeves (info only)
    closes, fund_d = {}, {}
    for p in QUAL23:
        closes[p] = fetch_ohlcv_bybit(p, "1d", days=DAYS)["close"]
        f = fetch_funding(p, days=DAYS, source="auto")
        fund_d[p] = f["funding_rate"].resample("1D").sum() \
            if f is not None and len(f) else pd.Series(dtype=float)
    closes = pd.DataFrame(closes)
    fund_daily = pd.DataFrame(fund_d)
    tsmom_d = sleeve_returns(closes, fund_daily, 90)
    carry_d = carry_returns(closes, fund_daily)

    D = pd.concat([trend_d.rename("trend"), tsmom_d.rename("tsmom"),
                   carry_d.rename("carry")], axis=1, join="inner").dropna()
    D = D[D.index >= COMMON_START]

    print("\n" + "=" * 96)
    print(f"A. TREND-ONLY (BLEND50_CONF) VOL DIALS — the deployable book  "
          f"({D.index[0].date()}..{D.index[-1].date()})")
    print("=" * 96)
    print(HDR)
    r_t = D["trend"]
    v_t = float(r_t.std() * np.sqrt(365))
    print(f"  (realized daily vol {v_t:.1%} ann at unit weights)")
    for vt in VTGTS:
        dstats(r_t, f"TREND v2 @ {vt:.0%} (x{vt/v_t:.1f})", lev=vt / v_t)

    print("\n" + "=" * 96)
    print("B. 3-SLEEVE ASSEMBLY v2 (INFO ONLY — sleeve leverage blocked by "
          "long-history gate)")
    print("=" * 96)
    iv = 1.0 / D.std()
    iv = iv / iv.sum()
    print("  IV weights: " + "  ".join(f"{c} {iv[c]:.2f}" for c in D.columns))
    print(HDR)
    r_iv = (D * iv).sum(axis=1)
    v_iv = float(r_iv.std() * np.sqrt(365))
    for vt in VTGTS:
        dstats(r_iv, f"ASSEMBLED @ {vt:.0%} (x{vt/v_iv:.1f})", lev=vt / v_iv)

    print("\n" + "=" * 96)
    print(f"C. TREND-ONLY @ 25% VOL — month grid (start ${TOTAL:.0f})")
    print("=" * 96)
    eq = TOTAL * (1 + r_t * (0.25 / v_t)).cumprod()
    mo = eq.resample("ME").last().pct_change().dropna() * 100
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    print(f"{'year':6s}" + "".join(f"{m:>7s}" for m in months) + f"{'YEAR':>9s}")
    for y, grp in mo.groupby(mo.index.year):
        cells = {ts.month: v for ts, v in grp.items()}
        line = f"{y:<6d}"
        for m in range(1, 13):
            line += f"{cells[m]:>7.1f}" if m in cells else f"{'':>7s}"
        yr = float((1 + grp / 100).prod() - 1) * 100
        line += f"{yr:>9.1f}"
        print(line)


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
