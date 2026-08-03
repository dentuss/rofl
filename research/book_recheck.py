"""BOOK RECHECK — report, not an experiment. No new cells, nothing pre-registered.

Recomputes the DEPLOYED book (identical config to research/deploy_report.py)
and answers three questions that the canonical report does not surface:

  1. TRAILING PARTIAL MONTH. dstats() does `resample("ME").last().pct_change()`,
     so the current, incomplete month enters the monthly series as if it were a
     full month. Reported here both ways (all months vs complete months only)
     so the Sharpe is not quietly distorted by a 3-day stub.

  2. DOC DRIFT. CLAUDE.md / SESSIONHANDOFF quote CAGR 10.4 / Sh(mo) 1.50 /
     dMDD -4.5 / worst mo -1.7 for unit weights. Prints the current values
     next to those so the gap is explicit and datable.

  3. PULL DEMOTION TRIGGER (pre-registered in SESSIONHANDOFF §1 and ROADMAP
     Phase 6): "trailing-3-month forward Sharpe < 0 -> drop to BLEND75 or
     triple-only". Decomposes the blend into its -t and -p legs and prints the
     trailing 3/6/12-month Sharpe of each so the trigger can be evaluated on
     evidence instead of recollection. NOTE: the trigger is defined on the
     FORWARD (live+paper) record, which does not exist yet; this is the
     backtest analogue on the same window and is explicitly NOT the trigger
     firing. It is context for the L1 decision.

Run:  PYTHONIOENCODING=utf-8 ./.venv/bin/python research/book_recheck.py
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
WARMUP_D, BPD = 365, 6
DOC = dict(cagr=10.4, sh=1.50, mdd=-4.5, worst_m=-1.7)   # CLAUDE.md / handoff


def shm(m: pd.Series) -> float:
    return float(m.mean() / m.std() * np.sqrt(12)) if len(m) > 2 and m.std() > 0 else 0.0


def report(r: pd.Series, label: str, complete_only: bool) -> dict:
    eq = TOTAL * (1 + r).cumprod()
    mo = eq.resample("ME").last().pct_change().dropna()
    if complete_only and len(mo):
        last_bar = r.index[-1]
        # drop the trailing month if the data does not reach its month end
        if last_bar < (last_bar + pd.offsets.MonthEnd(0)).normalize():
            mo = mo.iloc[:-1]
    yrs = max((r.index[-1] - r.index[0]).days, 1) / 365
    out = dict(label=label, cagr=((float(eq.iloc[-1]) / TOTAL) ** (1 / yrs) - 1) * 100,
               sh=shm(mo), mdd=float((eq / eq.cummax() - 1).min()) * 100,
               worst_m=float(mo.min()) * 100 if len(mo) else 0.0, n_mo=len(mo))
    return out


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print("BOOK RECHECK — deployed BLEND50_CONF, partial-month + doc-drift + PULL trigger",
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
            s = fng_persist(regime_mask(raw, regs), fa)
            s["risk_mult"] = mult
            cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                                   max_leverage=5.0, eq_decay_tiers=DEFAULT_DECAY_TIERS,
                                   cooldown_bars=3, fee_rate=FEE_TAKER,
                                   fee_maker=FEE_MAKER, entry_style="maker_close",
                                   tp_as_limit=True)
            e, tr = run_backtest_enhanced(dfe, s[s.index >= cut], cfg)
            (eq_t if tag == "t" else eq_p)[p] = apply_funding_real(e, tr, fund)
        print(f"  {p:10s} {time.time()-t0:5.0f}s", flush=True)

    idx = None
    for p in MAJORS8:
        e = eq_t[p][eq_t[p].index >= COMMON_START]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    w = {p: 1 / 8 for p in MAJORS8}
    pt, pp = build(eq_t, w, idx), build(eq_p, w, idx)
    blend = 0.5 * pt / pt.iloc[0] + 0.5 * pp / pp.iloc[0]

    r_all = blend.resample("1D").last().pct_change().dropna()
    rt = (pt / pt.iloc[0]).resample("1D").last().pct_change().dropna()
    rp = (pp / pp.iloc[0]).resample("1D").last().pct_change().dropna()

    print("\n" + "=" * 92)
    print("1) TRAILING PARTIAL MONTH — same data, two conventions")
    print("=" * 92)
    print(f"{'convention':34s}{'nMo':>6s}{'CAGR%':>9s}{'Sh(mo)':>9s}{'dMDD%':>9s}{'worstM%':>10s}")
    a = report(r_all, "all months (deploy_report)", False)
    b = report(r_all, "complete months only", True)
    for s in (a, b):
        print(f"{s['label']:34s}{s['n_mo']:>6d}{s['cagr']:>9.1f}{s['sh']:>9.2f}"
              f"{s['mdd']:>9.1f}{s['worst_m']:>10.1f}")

    print("\n" + "=" * 92)
    print("2) DOC DRIFT — CLAUDE.md / SESSIONHANDOFF vs today")
    print("=" * 92)
    print(f"{'metric':16s}{'documented':>13s}{'today':>10s}{'delta':>10s}")
    for k, nm in (("cagr", "CAGR%"), ("sh", "Sh(mo)"), ("mdd", "dMDD%"),
                  ("worst_m", "worst mo%")):
        print(f"{nm:16s}{DOC[k]:>13.2f}{b[k]:>10.2f}{b[k]-DOC[k]:>+10.2f}")

    print("\n" + "=" * 92)
    print("3) LEG DECOMPOSITION + PULL demotion-trigger context (backtest analogue)")
    print("=" * 92)
    mo_t = (TOTAL * (1 + rt).cumprod()).resample("ME").last().pct_change().dropna()
    mo_p = (TOTAL * (1 + rp).cumprod()).resample("ME").last().pct_change().dropna()
    mo_b = (TOTAL * (1 + r_all).cumprod()).resample("ME").last().pct_change().dropna()
    for nm, m in (("TRIPLE (-t)", mo_t), ("PULL (-p)", mo_p), ("BLEND50", mo_b)):
        m = m.iloc[:-1] if len(m) else m       # complete months only
        print(f"{nm:14s} full Sh {shm(m):>6.2f}   last12 {shm(m.tail(12)):>6.2f}   "
              f"last6 {shm(m.tail(6)):>6.2f}   last3 {shm(m.tail(3)):>6.2f}   "
              f"last3 cum {float((1+m.tail(3)).prod()-1)*100:>6.1f}%")
    print("\n  last 6 complete months (%):")
    tail = pd.DataFrame({"TRIPLE": mo_t, "PULL": mo_p, "BLEND": mo_b}).iloc[:-1].tail(6) * 100
    print(tail.round(2).to_string())
    print("\n  NOTE: the pre-registered trigger is defined on the FORWARD live+paper")
    print("  record, which does not exist yet. This is the backtest analogue on the")
    print("  same window — context for L1, NOT the trigger firing.")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
