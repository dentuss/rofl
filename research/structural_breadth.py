"""Structural breadth — does widening the ex-ante liquidity basket beyond
MAJORS8 add Sharpe, or does the edge dilute below the top-8?

The breadth laws forbid performance-picking names. The only legal knob is the
STRUCTURAL cutoff: rank the 23 structural qualifiers by median daily dollar
volume (computed from the common window, strategy-independent) and take the
top K. MAJORS8 (K=8) is the adopted baseline; this study prices K=12 and
K=16, with EW23 as the known-diluted reference.

PRE-REGISTERED cells: books {MAJORS8, MAJORS12, MAJORS16, EW23}, single
stack RSCD3+VT (the adopted one). Adoption bar: a wider book replaces
MAJORS8 only if full Sh(mo) improves AND OOS holds AND tails (MDD, worst
month) do not degrade materially.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/structural_breadth.py
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
from research.tsmom_sleeve import QUAL23
from research.vol_target import vt_mult

TOTAL = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))
COMMON_START = pd.Timestamp("2023-08-17", tz="UTC")
WARMUP_D = 365
BPD = 6


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print(f"STRUCTURAL BREADTH  QUAL23 -> top-K by median daily $vol", flush=True)
    fng = fetch_fear_greed()

    eqs, dollar_vol = {}, {}
    for p in QUAL23:
        t0 = time.time()
        df = fetch_ohlcv_bybit(p, "4h", days=DAYS)
        dollar_vol[p] = float((df["close"] * df["volume"])
                              .loc[df.index >= COMMON_START]
                              .resample("1D").sum().median())
        regs = wf_regimes_cached(df, p, "4h", BPD)
        fa = align_to_bars(fng, df.index)
        fund = fetch_funding(p, days=DAYS, source="auto")
        sig = fng_persist(regime_mask(triple_confirm_bidir(df, tp_mult=6.0),
                                      regs), fa)
        a = regs.reindex(sig.index, method="ffill").fillna("CHOP")
        sig["risk_mult"] = np.where(a == "CHOP", 0.5, 1.0) * \
            vt_mult(df).reindex(sig.index).fillna(1.0).to_numpy()
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                               max_leverage=5.0,
                               eq_decay_tiers=DEFAULT_DECAY_TIERS,
                               cooldown_bars=3, fee_rate=FEE_TAKER,
                               fee_maker=FEE_MAKER, entry_style="maker_close")
        eq, tr = run_backtest_enhanced(df[df.index >= cut],
                                       sig[sig.index >= cut], cfg)
        eqs[p] = apply_funding_real(eq, tr, fund)
        print(f"  {p:10s} {time.time()-t0:5.0f}s  medDV ${dollar_vol[p]/1e6:.1f}M",
              flush=True)

    ranked = sorted(QUAL23, key=lambda p: -dollar_vol[p])
    print("\nliquidity ranking: " +
          " ".join(p.split("-")[0] for p in ranked))
    books = {
        "MAJORS8": {p: 1 / 8 for p in ranked[:8]},
        "MAJORS12": {p: 1 / 12 for p in ranked[:12]},
        "MAJORS16": {p: 1 / 16 for p in ranked[:16]},
        "EW23": {p: 1 / 23 for p in QUAL23},
    }

    idx = None
    for p in QUAL23:
        e = eqs[p][eqs[p].index >= COMMON_START]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    split = idx[int(len(idx) * 0.6)]
    i_idx, o_idx = idx[idx < split], idx[idx >= split]
    b3 = [idx[0] + (idx[-1] - idx[0]) * k / 3 for k in range(4)]

    print("\n" + "=" * 96)
    print(f"BOOKS (RSCD3+VT)  {idx[0].date()}..{idx[-1].date()} @ ${TOTAL:.0f}")
    print("=" * 96)
    print(f"{'book':10s}{'final$':>8s}{'CAGR%':>8s}{'Sh(mo)':>8s}{'MDD%':>7s}"
          f"{'worst':>7s}{'IS Sh':>7s}{'OOS Sh':>8s}{'prof':>7s}{'thirds':>22s}")
    for bname, w in books.items():
        s = stats(build(eqs, w, idx))
        i = stats(build(eqs, w, i_idx))
        o = stats(build(eqs, w, o_idx))
        prof = sum(1 for p in w
                   if float(eqs[p].reindex(idx).ffill().iloc[-1])
                   > float(eqs[p].reindex(idx).ffill().iloc[0]))
        th = "  ".join(
            f"{sharpe_m(build(eqs, w, idx[(idx >= b3[k]) & (idx < b3[k+1])])):+.2f}"
            for k in range(3))
        print(f"{bname:10s}{s['final']:8.0f}{s['cagr']*100:8.1f}{s['sh_m']:8.2f}"
              f"{s['mdd']*100:7.1f}{s['worst_mo']:7.1f}{i['sh_m']:7.2f}"
              f"{o['sh_m']:8.2f}{prof:>4d}/{len(w):<2d}{th:>22s}")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
