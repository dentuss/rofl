"""Vol-targeted sizing on the promoted EW5/RSCD3 baseline.

Fixed 2% risk ignores that a pair's ATR-stop distance does not equal its vol
contribution: in high-vol regimes the same 2% risk produces much larger P&L
swings. Classic CTA construction scales exposure inversely to trailing vol.

PRE-REGISTERED design (single parameterization, no grid):
- vol_i(t) = 30d std of daily close returns, annualized, computed through the
  prior day only (shifted; the engine additionally shifts risk_mult one bar).
- risk_mult_vt = clip(TARGET / vol_i(t), 0.5, 1.5), TARGET = 60% annualized
  (a round pre-registered constant near these alts' long-run median).
- Cells: BASE (RSCD3) vs VT (RSCD3 x vol-target mult), EW5 book,
  full/IS/OOS + thirds.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/vol_target.py
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
TARGET_ANN = 0.60
CLIP_LO, CLIP_HI = 0.5, 1.5


def vt_mult(df: pd.DataFrame) -> pd.Series:
    daily = df["close"].resample("1D").last()
    vol = daily.pct_change().rolling(30, min_periods=20).std() * np.sqrt(365)
    m = (TARGET_ANN / vol).clip(CLIP_LO, CLIP_HI)
    # yesterday's completed vol estimate, forward-filled onto 4h bars
    return m.shift(1).reindex(df.index, method="ffill").fillna(1.0)


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print("VOL TARGETING  EW5/RSCD3 base vs +VT", flush=True)
    fng = fetch_fear_greed()

    eqs = {"RSCD3": {}, "RSCD3+VT": {}}
    for p in SOFT5:
        t0 = time.time()
        df = fetch_ohlcv_bybit(p, "4h", days=DAYS)
        regs = walk_forward_regimes(df, bars_per_day=BPD, train_days=365, step_days=30)
        fa = align_to_bars(fng, df.index)
        fund = fetch_funding(p, days=DAYS, source="auto")
        sig = fng_persist(regime_mask(triple_confirm_bidir(df, tp_mult=6.0), regs), fa)
        a = regs.reindex(sig.index, method="ffill").fillna("CHOP")
        chop = np.where(a == "CHOP", 0.5, 1.0)
        vt = vt_mult(df).reindex(sig.index).fillna(1.0).to_numpy()
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]
        for name, mult in [("RSCD3", chop), ("RSCD3+VT", chop * vt)]:
            s = sig.copy()
            s["risk_mult"] = mult
            cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                                   max_leverage=5.0, eq_decay_tiers=DEFAULT_DECAY_TIERS,
                                   cooldown_bars=3, fee_rate=FEE_TAKER,
                                   fee_maker=FEE_MAKER, entry_style="maker_close")
            eq, tr = run_backtest_enhanced(dfe, s[s.index >= cut], cfg)
            eqs[name][p] = apply_funding_real(eq, tr, fund)
        print(f"  {p:10s} {time.time()-t0:4.0f}s", flush=True)

    idx = None
    for p in SOFT5:
        e = eqs["RSCD3"][p]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    split = idx[int(len(idx) * 0.6)]
    i_idx, o_idx = idx[idx < split], idx[idx >= split]
    b3 = [idx[0] + (idx[-1] - idx[0]) * k / 3 for k in range(4)]
    w = {p: 0.2 for p in SOFT5}

    print("\n" + "=" * 88)
    print(f"VOL TARGETING  {idx[0].date()}..{idx[-1].date()}  EW5 @ ${TOTAL:.0f}  "
          f"(target {TARGET_ANN:.0%} ann, clip [{CLIP_LO},{CLIP_HI}])")
    print("=" * 88)
    print(f"{'cell':10s}{'final$':>8s}{'CAGR%':>8s}{'Sh(mo)':>8s}{'MDD%':>7s}"
          f"{'worst':>7s}{'IS Sh':>7s}{'OOS Sh':>8s}{'thirds':>22s}")
    for name in eqs:
        s = stats(build(eqs[name], w, idx))
        i = stats(build(eqs[name], w, i_idx))
        o = stats(build(eqs[name], w, o_idx))
        th = "  ".join(
            f"{sharpe_m(build(eqs[name], w, idx[(idx >= b3[k]) & (idx < b3[k+1])])):+.2f}"
            for k in range(3))
        print(f"{name:10s}{s['final']:8.0f}{s['cagr']*100:8.1f}{s['sh_m']:8.2f}"
              f"{s['mdd']*100:7.1f}{s['worst_mo']:7.1f}{i['sh_m']:7.2f}"
              f"{o['sh_m']:8.2f}{th:>22s}")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
