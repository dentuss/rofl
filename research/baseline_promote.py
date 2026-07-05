"""Baseline promotion run — the REGSIZE+CD3 stack (CHOP risk x0.5 + SL
cooldown K=3), which beat BASE on every metric on the EW10 book
(breadth_select.py part B), measured on the SOFT5-family books for the
scoreboard promotion decision.

PRE-REGISTERED: 3 stacks x 3 books, all reported:
  stacks:  BASE (ALL_IN) | RS (+CHOP half-size) | RSCD3 (+CHOP half-size, K=3)
  books:   SOFT5 (25/18.75x4) | EW5 (equal, incl INJ) | EW4 (equal, ex-INJ)
INJ note: the breadth study suggests INJ drags honestly — but dropping a name
AFTER seeing its result is performance selection. EW4 is reported for
information; any INJ decision waits for structural criteria + forward paper
evidence, not this table.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/baseline_promote.py
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

SOFT5 = {"INJ-USDT": .25, "SOL-USDT": .1875, "ADA-USDT": .1875,
         "ETH-USDT": .1875, "LINK-USDT": .1875}
TOTAL = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))
WARMUP_D = 365
BPD = 6


def cfg_allin(cd=1):
    return EnhancedBTConfig(
        starting_equity=100.0, risk_per_trade=0.020, max_leverage=5.0,
        eq_decay_tiers=DEFAULT_DECAY_TIERS, cooldown_bars=cd,
        fee_rate=FEE_TAKER, fee_maker=FEE_MAKER, entry_style="maker_close")


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print("BASELINE PROMOTION  SOFT5-family x {BASE, RS, RSCD3}", flush=True)
    fng = fetch_fear_greed()

    eqs = {"BASE": {}, "RS": {}, "RSCD3": {}}
    for p in SOFT5:
        t0 = time.time()
        df = fetch_ohlcv_bybit(p, "4h", days=DAYS)
        regs = walk_forward_regimes(df, bars_per_day=BPD, train_days=365,
                                    step_days=30)
        fa = align_to_bars(fng, df.index)
        fund = fetch_funding(p, days=DAYS, source="auto")
        sig = fng_persist(regime_mask(triple_confirm_bidir(df, tp_mult=6.0),
                                      regs), fa)
        a = regs.reindex(sig.index, method="ffill").fillna("CHOP")
        sig_rs = sig.copy()
        sig_rs["risk_mult"] = np.where(a == "CHOP", 0.5, 1.0)
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]
        for name, s, cd in [("BASE", sig, 1), ("RS", sig_rs, 1),
                            ("RSCD3", sig_rs, 3)]:
            eq, tr = run_backtest_enhanced(dfe, s[s.index >= cut], cfg_allin(cd))
            eqs[name][p] = apply_funding_real(eq, tr, fund)
        print(f"  {p:10s} {time.time()-t0:4.0f}s", flush=True)

    idx = None
    for p in SOFT5:
        e = eqs["BASE"][p]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    split = idx[int(len(idx) * 0.6)]
    i_idx, o_idx = idx[idx < split], idx[idx >= split]
    b3 = [idx[0] + (idx[-1] - idx[0]) * k / 3 for k in range(4)]
    yrs = (idx[-1] - idx[0]).days / 365

    books = {"SOFT5": SOFT5,
             "EW5": {p: 0.2 for p in SOFT5},
             "EW4-exINJ": {p: 0.25 for p in SOFT5 if p != "INJ-USDT"}}
    print("\n" + "=" * 88)
    print(f"PROMOTION TABLE  {idx[0].date()}..{idx[-1].date()} ({yrs:.2f}y) @ ${TOTAL:.0f}")
    print("=" * 88)
    print(f"{'book/stack':20s}{'final$':>8s}{'CAGR%':>8s}{'Sh(mo)':>8s}{'MDD%':>7s}"
          f"{'worst':>7s}{'IS Sh':>7s}{'OOS Sh':>8s}{'thirds':>22s}")
    for bname, w in books.items():
        for sname in ("BASE", "RS", "RSCD3"):
            s = stats(build(eqs[sname], w, idx))
            i = stats(build(eqs[sname], w, i_idx))
            o = stats(build(eqs[sname], w, o_idx))
            th = "  ".join(
                f"{sharpe_m(build(eqs[sname], w, idx[(idx >= b3[k]) & (idx < b3[k+1])])):+.2f}"
                for k in range(3))
            print(f"{bname + '/' + sname:20s}{s['final']:8.0f}{s['cagr']*100:8.1f}"
                  f"{s['sh_m']:8.2f}{s['mdd']*100:7.1f}{s['worst_mo']:7.1f}"
                  f"{i['sh_m']:7.2f}{o['sh_m']:8.2f}{th:>22s}")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
