"""Breadth round 2 — honest name selection + REGSIZE/CD3 gate confirmation.

breadth_allin.py showed the edge is cross-sectionally heterogeneous: EW-23
dilutes (Sh 0.46) while majors (ETH/SOL/BTC/RUNE/NEAR) carry real edge and
legacy alts (LTC/ETC/FIL/ATOM...) bleed. Naive breadth is dead; SELECTED
breadth must be done without the 8-pair selection trap.

PRE-REGISTERED design:
  A. Walk-forward selection: rank the 23 qualifiers by IS-window (first 60%)
     pair Sharpe(mo) under the ALL_IN stack; freeze EW baskets of K=5/8/10;
     judge ONLY on the OOS 40%. Score each selected basket against 300 random
     K-subsets of the same 23 (OOS Sharpe pct-rank). Selection is honest iff
     the basket beats ~95th pct of random.
  B. Graveyard-survivor gates on the 10-name book (EW11 minus INJ, all with
     full common-window data): BASE vs REGSIZE (CHOP risk x0.5) vs
     REGSIZE+CD3 (pre-registered combo). Report IS/OOS + thirds.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/breadth_select.py
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
from core.regime_strategy import walk_forward_regimes
from core.risk import DEFAULT_DECAY_TIERS
from core.sentiment import align_to_bars, fetch_fear_greed
from core.strategies import triple_confirm_bidir
from research.cost_engine import (apply_funding_real, regime_mask, fng_persist,
                                  sharpe_m, stats, build, FEE_TAKER, FEE_MAKER)

QUAL23 = [f"{b}-USDT" for b in
          ["BTC", "ETH", "SOL", "ADA", "LINK", "AVAX", "NEAR", "AAVE", "GRT",
           "RUNE", "DOGE", "DOT", "ATOM", "LTC", "XRP", "BNB", "FIL", "OP",
           "UNI", "ETC", "BCH", "TRX", "SAND"]]
TEN = [f"{b}-USDT" for b in ["SOL", "ADA", "ETH", "LINK", "AVAX", "NEAR",
                             "AAVE", "GRT", "RUNE", "DOGE"]]
TOTAL = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))
WARMUP_D = 365
BPD = 6
COMMON_START = pd.Timestamp("2023-08-17", tz="UTC")
N_NULL = 300
RNG = np.random.default_rng(7)


def cfg_allin(cd=1):
    return EnhancedBTConfig(
        starting_equity=100.0, risk_per_trade=0.020, max_leverage=5.0,
        eq_decay_tiers=DEFAULT_DECAY_TIERS, cooldown_bars=cd,
        fee_rate=FEE_TAKER, fee_maker=FEE_MAKER, entry_style="maker_close")


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print(f"BREADTH r2  select-{len(QUAL23)} + REGSIZE gates", flush=True)
    fng = fetch_fear_greed()

    base_eq, reg_eq, regcd3_eq = {}, {}, {}
    for p in QUAL23:
        t0 = time.time()
        df = fetch_ohlcv_bybit(p, "4h", days=DAYS)
        regs = walk_forward_regimes(df, bars_per_day=BPD, train_days=365,
                                    step_days=30)
        fa = align_to_bars(fng, df.index)
        fund = fetch_funding(p, days=DAYS, source="auto")
        sig = fng_persist(regime_mask(triple_confirm_bidir(df, tp_mult=6.0),
                                      regs), fa)
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]
        eq, tr = run_backtest_enhanced(dfe, sig[sig.index >= cut], cfg_allin())
        base_eq[p] = apply_funding_real(eq, tr, fund)
        if p in TEN:
            a = regs.reindex(sig.index, method="ffill").fillna("CHOP")
            s2 = sig.copy()
            s2["risk_mult"] = np.where(a == "CHOP", 0.5, 1.0)
            eq2, tr2 = run_backtest_enhanced(dfe, s2[s2.index >= cut], cfg_allin())
            reg_eq[p] = apply_funding_real(eq2, tr2, fund)
            eq3, tr3 = run_backtest_enhanced(dfe, s2[s2.index >= cut], cfg_allin(cd=3))
            regcd3_eq[p] = apply_funding_real(eq3, tr3, fund)
        print(f"  {p:10s} {time.time()-t0:4.0f}s", flush=True)

    idx = None
    for p in QUAL23:
        e = base_eq[p][base_eq[p].index >= COMMON_START]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    split = idx[int(len(idx) * 0.6)]
    is_idx, oos_idx = idx[idx < split], idx[idx >= split]

    # --- A. IS-selection, OOS judgement -------------------------------------
    is_sh = {p: sharpe_m(base_eq[p].reindex(is_idx).ffill()) for p in QUAL23}
    ranked = sorted(QUAL23, key=lambda p: -is_sh[p])
    print("\nIS ranking (first 60%):")
    print("  " + ", ".join(f"{p.split('-')[0]} {is_sh[p]:+.2f}" for p in ranked))

    print("\n" + "=" * 88)
    print("A. IS-SELECTED BASKETS, JUDGED OOS ONLY (vs 300 random K-subsets of the 23)")
    print("=" * 88)
    for K in (5, 8, 10):
        picks = ranked[:K]
        w = {p: 1 / K for p in picks}
        o = stats(build(base_eq, w, oos_idx))
        nulls = []
        for _ in range(N_NULL):
            sub = RNG.choice(QUAL23, size=K, replace=False)
            nulls.append(sharpe_m(build(base_eq, {p: 1 / K for p in sub}, oos_idx)))
        nulls = np.array(nulls)
        pct = float((nulls < o["sh_m"]).mean() * 100)
        print(f"  K={K:2d}  OOS Sh {o['sh_m']:+.2f}  CAGR {o['cagr']*100:+.1f}%  "
              f"MDD {o['mdd']*100:.1f}%  | null med {np.median(nulls):+.2f} "
              f"p95 {np.percentile(nulls,95):+.2f}  -> pct-rank {pct:.0f}th")
        print(f"        picks: {', '.join(p.split('-')[0] for p in picks)}")

    # --- B. REGSIZE / REGSIZE+CD3 gates on the 10-name book ------------------
    print("\n" + "=" * 88)
    print("B. SURVIVOR GATES  EW10 (EW11 minus INJ), full common window")
    print("=" * 88)
    w10 = {p: 1 / len(TEN) for p in TEN}
    idx10 = None
    for p in TEN:
        e = base_eq[p][base_eq[p].index >= COMMON_START]
        idx10 = e.index if idx10 is None else idx10.intersection(e.index)
    idx10 = idx10.sort_values()
    sp = idx10[int(len(idx10) * 0.6)]
    i10, o10 = idx10[idx10 < sp], idx10[idx10 >= sp]
    b3 = [idx10[0] + (idx10[-1] - idx10[0]) * k / 3 for k in range(4)]
    print(f"{'cell':13s}{'final$':>8s}{'CAGR%':>8s}{'Sh(mo)':>8s}{'MDD%':>7s}"
          f"{'worst':>7s}{'IS Sh':>7s}{'OOS Sh':>8s}{'thirds':>22s}")
    for name, eqs in [("BASE", base_eq), ("REGSIZE", reg_eq),
                      ("REGSIZE+CD3", regcd3_eq)]:
        s = stats(build(eqs, w10, idx10))
        i = stats(build(eqs, w10, i10))
        o = stats(build(eqs, w10, o10))
        th = "  ".join(
            f"{sharpe_m(build(eqs, w10, idx10[(idx10 >= b3[k]) & (idx10 < b3[k+1])])):+.2f}"
            for k in range(3))
        print(f"{name:13s}{s['final']:8.0f}{s['cagr']*100:8.1f}{s['sh_m']:8.2f}"
              f"{s['mdd']*100:7.1f}{s['worst_mo']:7.1f}{i['sh_m']:7.2f}"
              f"{o['sh_m']:8.2f}{th:>22s}")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
