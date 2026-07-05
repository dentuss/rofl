"""Honest rebuild, round 2 — attack the fee churn and the exit scheme.

Round 1 (honest_rebuild.py) found: overlays are real (+~21pp CAGR combined),
the raw signal bleeds (−20%/y), fees eat 73% of gross edge (−290 of +398 per-100
units), and widening/removing the TP improves monotonically (L4 +0.6% → TPINF
+6.1% CAGR) but with no stable OOS edge yet. The artifact had selected tight
TPs and high churn; this round tests the two pre-registered anti-churn levers:

  1h arm (bars_per_day=24, K_sl=4):
    H1_TPINF   no TP, exit on flip/time            (r1 reference cell)
    H1_TRL25   no TP + 2.5-ATR trailing stop
    H1_TRL40   no TP + 4.0-ATR trailing stop
  4h arm (bars_per_day=6, K_sl=1 ≈ same calendar cooldown, F&G persistence
          scaled to 18 bars, funding scaled ×4/bar; ~4× fewer bars => ~4× less
          fee drag if trade counts scale):
    H4_BASE    default tp_mult=3.0
    H4_TP60    tp_mult=6.0
    H4_TPINF   no TP
    H4_TRL40   no TP + 4.0-ATR trail

No other cells — judge on IS, confirm on OOS 60/40, per arm.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/honest_rebuild_r2.py
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
from core.risk import DEFAULT_DECAY_TIERS
from core.data import fetch_ohlcv_bybit
from core.regime_strategy import walk_forward_regimes
from core.sentiment import align_to_bars, fetch_fear_greed
from core.strategies import triple_confirm_bidir
from research.test_reentry_cooldown_prod import apply_funding

SOFT5 = {"INJ-USDT": .25, "SOL-USDT": .1875, "ADA-USDT": .1875,
         "ETH-USDT": .1875, "LINK-USDT": .1875}
TOTAL = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))
WARMUP_D = 365
FEE = 0.0006

# variant -> (tf, bars_per_day, tp_mult|None, trail, k_sl)
VARIANTS = {
    "H1_TPINF": ("1h", 24, 1e6, 0.0, 4),
    "H1_TRL25": ("1h", 24, 1e6, 2.5, 4),
    "H1_TRL40": ("1h", 24, 1e6, 4.0, 4),
    "H4_BASE":  ("4h", 6,  None, 0.0, 1),
    "H4_TP60":  ("4h", 6,  6.0,  0.0, 1),
    "H4_TPINF": ("4h", 6,  1e6,  0.0, 1),
    "H4_TRL40": ("4h", 6,  1e6,  4.0, 1),
}


def regime_mask(sig, regs):
    a = regs.reindex(sig.index, method="ffill").fillna("CHOP")
    b = ((sig["signal"] == 1) & (~a.isin(["BULL", "CHOP"]))) | \
        ((sig["signal"] == -1) & (~a.isin(["BEAR", "CHOP"])))
    s = sig.copy()
    s.loc[b, "signal"] = 0
    s.loc[b, ["sl", "tp"]] = np.nan
    return s


def fng_persist(sig, fa, bpd):
    """F&G 3-DAY persistence, timeframe-aware (production rule is 3*24 1h bars)."""
    bars = 3 * bpd
    above = (fa >= 80).rolling(bars, min_periods=bars).sum() == bars
    below = (fa <= 20).rolling(bars, min_periods=bars).sum() == bars
    bl = (sig["signal"] == 1) & above.fillna(False)
    bs = (sig["signal"] == -1) & below.fillna(False)
    block = bl | bs
    s = sig.copy()
    s.loc[block, "signal"] = 0
    s.loc[block, ["sl", "tp"]] = np.nan
    return s


def sharpe_m(eq):
    m = eq.resample("ME").last().pct_change().dropna()
    return float(m.mean() / m.std() * np.sqrt(12)) if len(m) > 2 and m.std() > 0 else 0.0


def stats(eq, bpy):
    final = float(eq.iloc[-1])
    mdd = float((eq / eq.cummax() - 1).min())
    days = max((eq.index[-1] - eq.index[0]).days, 1)
    cagr = (final / float(eq.iloc[0])) ** (365 / days) - 1
    br = eq.pct_change().fillna(0)
    sh_h = float(br.mean() / br.std() * np.sqrt(bpy)) if br.std() > 0 else 0
    mo = eq.resample("ME").last().pct_change().dropna() * 100
    return dict(final=final, cagr=cagr, mdd=mdd, sh_h=sh_h, sh_m=sharpe_m(eq),
                worst_mo=float(mo.min()) if len(mo) else 0,
                win_mo=float((mo > 0).mean() * 100) if len(mo) else 0)


def build(pair_eq, w, idx):
    port = None
    for p, wt in w.items():
        e = pair_eq[p].reindex(idx).ffill()
        e = e / e.iloc[0] * wt
        port = e if port is None else port + e
    return port * TOTAL


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print(f"HONEST REBUILD r2  BYBIT  SOFT5  variants={list(VARIANTS)}", flush=True)
    fng = fetch_fear_greed()

    pair_eq = {v: {} for v in VARIANTS}
    counts = {v: 0 for v in VARIANTS}
    fees = {v: 0.0 for v in VARIANTS}

    for p in SOFT5:
        t0 = time.time()
        per_tf = {}
        for tf, bpd in [("1h", 24), ("4h", 6)]:
            df = fetch_ohlcv_bybit(p, tf, days=DAYS)
            regs = walk_forward_regimes(df, bars_per_day=bpd, train_days=365,
                                        step_days=30)
            rc = regs.value_counts().to_dict()
            assert (rc.get("BULL", 0) + rc.get("BEAR", 0)) > 0, f"{p} {tf}: all-CHOP"
            fa = align_to_bars(fng, df.index)
            per_tf[tf] = (df, regs, fa)

        for v, (tf, bpd, tpm, trail, k_sl) in VARIANTS.items():
            df, regs, fa = per_tf[tf]
            kw = {} if tpm is None else {"tp_mult": tpm}
            sig = fng_persist(regime_mask(triple_confirm_bidir(df, **kw), regs),
                              fa, bpd)
            cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
            dfe = df[df.index >= cut]
            cfg = EnhancedBTConfig(
                starting_equity=100.0, risk_per_trade=0.020, max_leverage=5.0,
                eq_decay_tiers=DEFAULT_DECAY_TIERS, cooldown_bars=k_sl,
                trail_atr=trail)
            eq, tr = run_backtest_enhanced(dfe, sig[sig.index >= cut], cfg)
            pair_eq[v][p] = apply_funding(eq, tr, bps=1.0 * (24 // bpd))
            counts[v] += len(tr)
            fees[v] += sum(t.fees + t.notional * FEE for t in tr)
        print(f"  {p:11s} {time.time()-t0:5.0f}s", flush=True)

    for arm, bpy in [("H1", 24 * 365), ("H4", 6 * 365)]:
        vs = [v for v in VARIANTS if v.startswith(arm)]
        idx = None
        for p in SOFT5:
            e = pair_eq[vs[0]][p]
            idx = e.index if idx is None else idx.intersection(e.index)
        idx = idx.sort_values()
        split = idx[int(len(idx) * 0.6)]
        is_idx, oos_idx = idx[idx < split], idx[idx >= split]
        yrs = (idx[-1] - idx[0]).days / 365
        print("\n" + "=" * 86)
        print(f"{arm} ARM  {idx[0].date()}..{idx[-1].date()} ({yrs:.2f}y)  SOFT5 @ ${TOTAL:.0f}")
        print("=" * 86)
        print(f"{'variant':10s}{'final$':>8s}{'CAGR%':>8s}{'Sh(mo)':>8s}{'MDD%':>7s}"
              f"{'worst':>7s}{'pos%':>6s}{'trades':>8s}{'fees':>7s}{'IS Sh':>7s}{'OOS Sh':>8s}")
        for v in vs:
            s = stats(build(pair_eq[v], SOFT5, idx), bpy)
            i = stats(build(pair_eq[v], SOFT5, is_idx), bpy)
            o = stats(build(pair_eq[v], SOFT5, oos_idx), bpy)
            print(f"{v:10s}{s['final']:8.0f}{s['cagr']*100:8.1f}{s['sh_m']:8.2f}"
                  f"{s['mdd']*100:7.1f}{s['worst_mo']:7.1f}{s['win_mo']:6.0f}"
                  f"{counts[v]:8d}{fees[v]:7.0f}{i['sh_m']:7.2f}{o['sh_m']:8.2f}")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
