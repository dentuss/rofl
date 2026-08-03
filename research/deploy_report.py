"""Deploy report — the exact config the live compose runs, at the exact
deposit. This is a REPORT of the promoted stack (no new cells): BLEND50_CONF
= MAJORS8 x {triple_bidir tp6, pullback_in_trend} 50/50, walk-forward regime
mask + F&G persistence + decay tiers + CHOP half-size + VT + CONF sizing,
maker entries, TP-as-limit, real funding. Daily granularity.

Prints: the UNIT-WEIGHTS row (what L1/L2 actually runs — no vol dial), the
dial ladder for L3 reference, and the unit-weights month grid at the real
deposit (TOTAL_EQUITY env, default 1800.00). All percentages are
deposit-invariant; only the final-$ column scales with the deposit.

Run:  PYTHONIOENCODING=utf-8 ./.venv/bin/python research/deploy_report.py
      (Windows: ./.venv/Scripts/python.exe)
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
from core.strategies import pullback_in_trend, triple_confirm_bidir
from research.cost_engine import (apply_funding_real, regime_mask, fng_persist,
                                  build, FEE_TAKER, FEE_MAKER)
from research.regime_upgrades import wf_labels_conf
from research.vol_target import vt_mult

MAJORS8 = [f"{b}-USDT" for b in
           ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX"]]
TOTAL = float(_os.environ.get("TOTAL_EQUITY", 1800.00))
DAYS = int(_os.environ.get("DAYS", 2000))
COMMON_START = pd.Timestamp("2023-08-17", tz="UTC")
WARMUP_D = 365
BPD = 6
DIALS = (0.15, 0.25, 0.35, 0.50)


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
    win = float((mo > 0).mean() * 100) if len(mo) else 0.0
    print(f"{label:26s}{float(eq.iloc[-1]):>9.0f}{cagr*100:>8.1f}{sh_m:>7.2f}"
          f"{mdd*100:>8.1f}{float(r.min())*100:>8.1f}{float(wk.min())*100:>8.1f}"
          f"{float(mo.min())*100:>8.1f}{win:>6.0f}{shi:>7.2f}{sho:>8.2f}")


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print(f"DEPLOY REPORT — BLEND50_CONF as wired, deposit ${TOTAL:.2f}",
          flush=True)
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
    r = blend.resample("1D").last().pct_change().dropna()
    vol = float(r.std() * np.sqrt(365))

    hdr = (f"{'':26s}{'final$':>9s}{'CAGR%':>8s}{'Sh(mo)':>7s}{'dMDD%':>8s}"
           f"{'worstD%':>8s}{'worstW%':>8s}{'worstM%':>8s}{'win%':>6s}"
           f"{'IS Sh':>7s}{'OOS Sh':>8s}")

    print("\n" + "=" * 105)
    print(f"THE DEPLOYED BOOK  {r.index[0].date()}..{r.index[-1].date()}  "
          f"start ${TOTAL:.2f}  (realized vol {vol:.1%} ann at unit weights)")
    print("=" * 105)
    print(hdr)
    dstats(r, "UNIT WEIGHTS (L1/L2 -- live)", lev=1.0)
    for d in DIALS:
        dstats(r, f"@ {d:.0%} vol dial (x{d/vol:.1f})  [L3]", lev=d / vol)

    print("\n" + "=" * 105)
    print(f"MONTH GRID — UNIT WEIGHTS (the L1/L2 experience, start ${TOTAL:.2f})")
    print("=" * 105)
    eq = TOTAL * (1 + r).cumprod()
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
    print(f"\n  legs: -t triple_bidir tp6, -p pullback 40/60 recross; "
          f"16 legs EQUAL at ${TOTAL / 16:.2f} (= ${TOTAL:,.2f}), "
          f"${TOTAL / 2:,.2f} per account")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
