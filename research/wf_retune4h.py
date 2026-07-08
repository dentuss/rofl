"""Honest walk-forward param re-tune on 4h — the current triple_bidir params
(ema 9/26, rsi_min 55) were frozen in the 1h artifact era. This tests whether
PERIODIC RE-SELECTION on trailing data beats the frozen set, with zero
look-ahead: at each refit date the grid is scored ONLY on the prior 365d,
and the winner is applied to the NEXT 90d. The deployed curve is the stitched
sequence of those choices — a strategy you could actually have run.

PRE-REGISTERED:
- Grid (same as the old retune.py — no widening): ema_fast {7,9,12},
  ema_slow {21,26,34}, rsi_min {50,55,60}, fast<slow -> 27 combos.
  tp_mult stays 6.0 (adopted; not re-tuned here).
- Refit every 90d, trailing 365d selection window, score = bar-level Sharpe
  of the full regime+F&G-masked signal under taker fees, cooldown 3, flat
  risk (no chop/VT in selection — they rescale P&L without reordering
  combos; deployment applies the full adopted stack).
- Deployment stack: RSCD3+VT, maker entries, real funding, MAJORS8 EW.
- Baseline: FIXED (9, 26, 55) through the identical machinery.
- Caveat (honest, unavoidable): the FIRST selection window predates the
  first walk-forward regime prediction, so its regimes are all-CHOP; this
  uses only past information and is applied identically to every combo.

Adoption bar: WF replaces FIXED only if full Sh improves AND OOS holds AND
the param timeline is STABLE (a new combo nearly every refit = the retune.py
overfit lesson again -> reject regardless of headline Sharpe).

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/wf_retune4h.py
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
                                  sharpe_m, stats, build, FEE_TAKER, FEE_MAKER)
from research.regime_cache import wf_regimes_cached
from research.vol_target import vt_mult

MAJORS8 = [f"{b}-USDT" for b in
           ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX"]]
TOTAL = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))
COMMON_START = pd.Timestamp("2023-08-17", tz="UTC")
WARMUP_D = 365
BPD = 6
TRAIN_D = 365
REFIT_D = 90
FIXED = (9, 26, 55)

GRID = [(ef, es, rm)
        for ef in (7, 9, 12) for es in (21, 26, 34) for rm in (50, 55, 60)
        if ef < es]


def bar_sharpe(eq: pd.Series) -> float:
    r = eq.pct_change().dropna()
    return float(r.mean() / r.std() * np.sqrt(BPD * 365)) \
        if len(r) > 50 and r.std() > 0 else -99.0


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print(f"WALK-FORWARD RETUNE 4h  grid={len(GRID)} combos  "
          f"train {TRAIN_D}d / refit {REFIT_D}d  MAJORS8", flush=True)
    fng = fetch_fear_greed()

    cfg_sel = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                               max_leverage=5.0,
                               eq_decay_tiers=DEFAULT_DECAY_TIERS,
                               cooldown_bars=3, fee_rate=FEE_TAKER)
    cfg_dep = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                               max_leverage=5.0,
                               eq_decay_tiers=DEFAULT_DECAY_TIERS,
                               cooldown_bars=3, fee_rate=FEE_TAKER,
                               fee_maker=FEE_MAKER, entry_style="maker_close")

    eqs = {"FIXED": {}, "WF": {}}
    timelines = {}
    for p in MAJORS8:
        t0 = time.time()
        df = fetch_ohlcv_bybit(p, "4h", days=DAYS)
        regs = wf_regimes_cached(df, p, "4h", BPD)
        fa = align_to_bars(fng, df.index)
        fund = fetch_funding(p, days=DAYS, source="auto")
        a = regs.reindex(df.index, method="ffill").fillna("CHOP")
        mult = pd.Series(np.where(a == "CHOP", 0.5, 1.0)
                         * vt_mult(df).reindex(df.index).fillna(1.0).to_numpy(),
                         index=df.index)
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)

        # full-stack signal per combo, built once on the whole history
        sigs = {}
        for c in GRID:
            ef, es, rm = c
            sigs[c] = fng_persist(regime_mask(
                triple_confirm_bidir(df, ema_fast=ef, ema_slow=es,
                                     rsi_min=float(rm), tp_mult=6.0),
                regs), fa)

        # walk-forward selection
        chosen, rows = [], []
        t = cut
        end = df.index[-1]
        while t < end:
            lo, hi = t - pd.Timedelta(days=TRAIN_D), t
            m_tr = (df.index >= lo) & (df.index < hi)
            best, best_s = FIXED, -1e9
            for c in GRID:
                eq, tr = run_backtest_enhanced(df[m_tr], sigs[c][m_tr], cfg_sel)
                s = bar_sharpe(eq) if len(tr) >= 5 else -99.0
                if s > best_s:
                    best, best_s = c, s
            nxt = t + pd.Timedelta(days=REFIT_D)
            m_ap = (df.index >= t) & (df.index < nxt)
            rows.append(sigs[best][m_ap])
            chosen.append((t, best))
            t = nxt
        timelines[p] = chosen
        wf_sig = pd.concat(rows).sort_index()
        wf_sig["risk_mult"] = mult.reindex(wf_sig.index).to_numpy()

        fixed_sig = sigs[FIXED].copy()
        fixed_sig["risk_mult"] = mult.to_numpy()

        dfe = df[df.index >= cut]
        for name, s in [("FIXED", fixed_sig[fixed_sig.index >= cut]),
                        ("WF", wf_sig)]:
            eq, tr = run_backtest_enhanced(dfe, s, cfg_dep)
            eqs[name][p] = apply_funding_real(eq, tr, fund)
        churn = sum(1 for k in range(1, len(chosen))
                    if chosen[k][1] != chosen[k - 1][1])
        print(f"  {p:10s} {time.time()-t0:5.0f}s  refits={len(chosen)} "
              f"changes={churn}", flush=True)

    idx = None
    for p in MAJORS8:
        e = eqs["FIXED"][p][eqs["FIXED"][p].index >= COMMON_START]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    split = idx[int(len(idx) * 0.6)]
    i_idx, o_idx = idx[idx < split], idx[idx >= split]
    b3 = [idx[0] + (idx[-1] - idx[0]) * k / 3 for k in range(4)]
    w = {p: 1 / 8 for p in MAJORS8}

    print("\n" + "=" * 96)
    print(f"FIXED vs WALK-FORWARD  {idx[0].date()}..{idx[-1].date()}  "
          f"MAJORS8 EW @ ${TOTAL:.0f}")
    print("=" * 96)
    print(f"{'cell':8s}{'final$':>8s}{'CAGR%':>8s}{'Sh(mo)':>8s}{'MDD%':>7s}"
          f"{'worst':>7s}{'IS Sh':>7s}{'OOS Sh':>8s}{'thirds':>22s}")
    for name in eqs:
        s = stats(build(eqs[name], w, idx))
        i = stats(build(eqs[name], w, i_idx))
        o = stats(build(eqs[name], w, o_idx))
        th = "  ".join(
            f"{sharpe_m(build(eqs[name], w, idx[(idx >= b3[k]) & (idx < b3[k+1])])):+.2f}"
            for k in range(3))
        print(f"{name:8s}{s['final']:8.0f}{s['cagr']*100:8.1f}{s['sh_m']:8.2f}"
              f"{s['mdd']*100:7.1f}{s['worst_mo']:7.1f}{i['sh_m']:7.2f}"
              f"{o['sh_m']:8.2f}{th:>22s}")

    print("\nparam timelines (refit date -> combo; * = change):")
    for p in MAJORS8:
        parts, prev = [], None
        for ts, c in timelines[p]:
            mark = "*" if prev is not None and c != prev else ""
            parts.append(f"{ts.date()} ({c[0]},{c[1]},{c[2]}){mark}")
            prev = c
        print(f"  {p.split('-')[0]:5s} " + " | ".join(parts))


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
