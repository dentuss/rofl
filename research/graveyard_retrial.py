"""Graveyard re-trial — every artifact-era rejection, retried on the honest
ALL_IN base. The old engine paid a bounty per TP fired, so every idea that
reduced churn or cut winners short was structurally rigged to lose. All those
verdicts are void (FINDINGS 2026-07-05); this is the honest re-trial.

Base: ALL_IN on SOFT5 (4h, tp_mult=6.0, entry-bar check, maker entries,
cooldown_bars=1, decay tiers, REAL funding) — the scoreboard baseline.

PRE-REGISTERED cells (singles only; combos are a later round):
  PTP20 / PTP30  partial TP at 2.0 / 3.0 ATR, stop to breakeven
                 (artifact verdict: "catastrophic -27%")
  VOLF           ATR/price chop filter, min_pct=0.003 (with_vol_filter)
                 (artifact verdict: "hurts winners")
  ADX25          stricter entry gate adx_min 22 -> 25
  REGSIZE        risk x0.5 in CHOP regime via risk_mult
                 (artifact verdict: "+0.10 Sharpe but -36% return")
  HTF1D          soft HTF risk bias 1D EMA50, counter x0.5 (r2 hinted MDD cut)
  CD2 / CD3      SL cooldown 2 / 3 engine bars on 4h (currently effectively 0)

Judge on IS, confirm on OOS; deltas vs BASE reported for every cell.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/graveyard_retrial.py
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
from core.strategies_enhanced import with_htf_risk_bias, with_vol_filter
from research.cost_engine import (apply_funding_real, regime_mask, fng_persist,
                                  sharpe_m, stats, build, FEE_TAKER, FEE_MAKER)

SOFT5 = {"INJ-USDT": .25, "SOL-USDT": .1875, "ADA-USDT": .1875,
         "ETH-USDT": .1875, "LINK-USDT": .1875}
TOTAL = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))
WARMUP_D = 365
BPD = 6

# cell -> dict(adx=None|float, vol_filter=bool, htf=bool, regsize=bool,
#              partial=0.0, cd=1)
CELLS = {
    "BASE":   dict(),
    "PTP20":  dict(partial=2.0),
    "PTP30":  dict(partial=3.0),
    "VOLF":   dict(vol_filter=True),
    "ADX25":  dict(adx=25.0),
    "REGSIZE": dict(regsize=True),
    "HTF1D":  dict(htf=True),
    "CD2":    dict(cd=2),
    "CD3":    dict(cd=3),
}


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print(f"GRAVEYARD RE-TRIAL  ALL_IN 4h tp6 SOFT5  cells={list(CELLS)}", flush=True)
    fng = fetch_fear_greed()

    pair_eq = {c: {} for c in CELLS}
    counts = {c: 0 for c in CELLS}

    for p in SOFT5:
        t0 = time.time()
        df = fetch_ohlcv_bybit(p, "4h", days=DAYS)
        regs = walk_forward_regimes(df, bars_per_day=BPD, train_days=365,
                                    step_days=30)
        fa = align_to_bars(fng, df.index)
        fund = fetch_funding(p, days=DAYS, source="auto")
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]

        for c, spec in CELLS.items():
            kw = {"tp_mult": 6.0}
            if spec.get("adx"):
                kw["adx_min"] = spec["adx"]
            sig = fng_persist(regime_mask(triple_confirm_bidir(df, **kw), regs), fa)
            if spec.get("vol_filter"):
                sig = with_vol_filter(df, sig)
            if spec.get("htf"):
                sig = with_htf_risk_bias(df, sig, htf_rule="1D", ema_n=50,
                                         counter_mult=0.5)
            if spec.get("regsize"):
                a = regs.reindex(sig.index, method="ffill").fillna("CHOP")
                mult = np.where(a == "CHOP", 0.5, 1.0)
                base_m = sig["risk_mult"].to_numpy() if "risk_mult" in sig.columns else 1.0
                sig = sig.copy()
                sig["risk_mult"] = base_m * mult
            cfg = EnhancedBTConfig(
                starting_equity=100.0, risk_per_trade=0.020, max_leverage=5.0,
                eq_decay_tiers=DEFAULT_DECAY_TIERS,
                cooldown_bars=spec.get("cd", 1),
                fee_rate=FEE_TAKER, fee_maker=FEE_MAKER,
                entry_style="maker_close",
                partial_tp_atr=spec.get("partial", 0.0),
                partial_to_breakeven=True)
            eq, tr = run_backtest_enhanced(dfe, sig[sig.index >= cut], cfg)
            pair_eq[c][p] = apply_funding_real(eq, tr, fund)
            counts[c] += len(tr)
        print(f"  {p:11s} {time.time()-t0:5.0f}s", flush=True)

    idx = None
    for p in SOFT5:
        e = pair_eq["BASE"][p]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    split = idx[int(len(idx) * 0.6)]
    is_idx, oos_idx = idx[idx < split], idx[idx >= split]
    yrs = (idx[-1] - idx[0]).days / 365

    print("\n" + "=" * 88)
    print(f"GRAVEYARD RE-TRIAL  {idx[0].date()}..{idx[-1].date()} ({yrs:.2f}y)  "
          f"SOFT5 @ ${TOTAL:.0f}")
    print("=" * 88)
    print(f"{'cell':9s}{'final$':>8s}{'CAGR%':>8s}{'Sh(mo)':>8s}{'MDD%':>7s}"
          f"{'worst':>7s}{'pos%':>6s}{'trades':>8s}{'IS Sh':>7s}{'OOS Sh':>8s}")
    full = {}
    for c in CELLS:
        s = stats(build(pair_eq[c], SOFT5, idx)); full[c] = s
        i = stats(build(pair_eq[c], SOFT5, is_idx))
        o = stats(build(pair_eq[c], SOFT5, oos_idx))
        print(f"{c:9s}{s['final']:8.0f}{s['cagr']*100:8.1f}{s['sh_m']:8.2f}"
              f"{s['mdd']*100:7.1f}{s['worst_mo']:7.1f}{s['win_mo']:6.0f}"
              f"{counts[c]:8d}{i['sh_m']:7.2f}{o['sh_m']:8.2f}")

    print("\nDELTAS vs BASE:")
    b = full["BASE"]
    for c in CELLS:
        if c == "BASE":
            continue
        s = full[c]
        print(f"  {c:8s} dSh(mo) {s['sh_m']-b['sh_m']:+.2f}  "
              f"dCAGR {100*(s['cagr']-b['cagr']):+.1f}pp  "
              f"dMDD {100*(s['mdd']-b['mdd']):+.1f}pp  "
              f"dWorstMo {s['worst_mo']-b['worst_mo']:+.1f}pp")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
