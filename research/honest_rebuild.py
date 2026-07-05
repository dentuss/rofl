"""Honest rebuild, round 1 — re-validate the strategy family from zero on the
FIXED engine (post same-bar-re-entry correction, FINDINGS 2026-07-05).

Three questions, all on Bybit perp data, SOFT5 pairs, fixed engine:

A. LAYER DECOMPOSITION — which overlays add real value?
     L0  raw triple_confirm_bidir (no overlays, no decay, K=0)
     L1  + directional regime mask (walk-forward GMM)
     L2  + F&G 3-day persistence
     L3  + three-tier decay
     L4  + SL cooldown K=4 (== live COOLDOWN_BARS=3)  <- production stack
   Funding applied to all. If L4 ~ 0 while L0 < 0, the overlays are real and
   the bleed is in the raw entry/exit scheme.

B. EXIT AUTOPSY (on L4 trades) — where does the money actually go?
   Per exit reason: n, win rate, mean/sum R, mean bars held; plus gross PnL
   vs total fees vs funding drag.

C. EXIT-SCHEME PROBES (pre-registered, deliberately few — no grid search):
   the artifact rewarded tight TPs (each TP fired a free same-bar re-entry),
   so the adopted tp_mult=3.0 was likely selected BY the bug. Probes, all on
   the full L4 stack: tp_mult 4.5, 6.0, and none (1e6 => exit on flip/time).
   Judge on IS, confirm on OOS 60/40.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/honest_rebuild.py
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
from research.test_reentry_cooldown_prod import fng_persist_3d, apply_funding

SOFT5 = {"INJ-USDT": .25, "SOL-USDT": .1875, "ADA-USDT": .1875,
         "ETH-USDT": .1875, "LINK-USDT": .1875}
TOTAL = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))
WARMUP_D = 365
BPY = 24 * 365
FEE = 0.0006

# variant -> (tp_mult or None for default 3.0, use_regime, use_fng, decay, k_sl)
VARIANTS = {
    "L0_RAW":    (None, False, False, False, 0),
    "L1_REGIME": (None, True,  False, False, 0),
    "L2_FNG":    (None, True,  True,  False, 0),
    "L3_DECAY":  (None, True,  True,  True,  0),
    "L4_PROD":   (None, True,  True,  True,  4),
    "TP45":      (4.5,  True,  True,  True,  4),
    "TP60":      (6.0,  True,  True,  True,  4),
    "TPINF":     (1e6,  True,  True,  True,  4),
}
BASELINE = "L4_PROD"


def regime_mask(sig, regs):
    """Directional regime mask (same rule as research/test_reentry_cooldown_prod)."""
    a = regs.reindex(sig.index, method="ffill").fillna("CHOP")
    b = ((sig["signal"] == 1) & (~a.isin(["BULL", "CHOP"]))) | \
        ((sig["signal"] == -1) & (~a.isin(["BEAR", "CHOP"])))
    s = sig.copy()
    s.loc[b, "signal"] = 0
    s.loc[b, ["sl", "tp"]] = np.nan
    return s


def sharpe_m(eq):
    m = eq.resample("ME").last().pct_change().dropna()
    return float(m.mean() / m.std() * np.sqrt(12)) if len(m) > 2 and m.std() > 0 else 0.0


def stats(eq):
    final = float(eq.iloc[-1]); ret = final / float(eq.iloc[0]) - 1
    mdd = float((eq / eq.cummax() - 1).min())
    days = max((eq.index[-1] - eq.index[0]).days, 1)
    cagr = (final / float(eq.iloc[0])) ** (365 / days) - 1
    br = eq.pct_change().fillna(0)
    sh_h = float(br.mean() / br.std() * np.sqrt(BPY)) if br.std() > 0 else 0
    mo = eq.resample("ME").last().pct_change().dropna() * 100
    return dict(final=final, ret=ret, cagr=cagr, mdd=mdd, sh_h=sh_h,
                sh_m=sharpe_m(eq),
                worst_mo=float(mo.min()) if len(mo) else 0,
                win_mo=float((mo > 0).mean() * 100) if len(mo) else 0)


def build(pair_eq, w, idx):
    port = None
    for p, wt in w.items():
        e = pair_eq[p].reindex(idx).ffill()
        e = e / e.iloc[0] * wt
        port = e if port is None else port + e
    return port * TOTAL


def autopsy(trades):
    rows = []
    for t in trades:
        risk = t.notional * abs(t.entry_px - t.sl) / t.entry_px if t.sl else 0
        r = t.pnl / risk if risk > 0 else 0.0
        entry_fee = t.notional * FEE
        gross = t.pnl + t.fees + entry_fee
        rows.append(dict(reason=t.reason, r=r, pnl=t.pnl, win=t.pnl > 0,
                         bars=t.bars_held, gross=gross,
                         fees=t.fees + entry_fee))
    return pd.DataFrame(rows)


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print(f"HONEST REBUILD r1  BYBIT  SOFT5  TOTAL={TOTAL:.0f}  warmup={WARMUP_D}d  "
          f"variants={list(VARIANTS)}", flush=True)
    fng = fetch_fear_greed()

    pair_eq = {v: {} for v in VARIANTS}
    counts = {v: 0 for v in VARIANTS}
    aut = []

    for p in SOFT5:
        t0 = time.time()
        df = fetch_ohlcv_bybit(p, "1h", days=DAYS)
        regs = walk_forward_regimes(df, bars_per_day=24, train_days=365, step_days=30)
        rc = regs.value_counts().to_dict()
        assert (rc.get("BULL", 0) + rc.get("BEAR", 0)) > 0, f"{p}: all-CHOP {rc}"
        fa = align_to_bars(fng, df.index)
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]

        sig_cache = {}
        for v, (tpm, use_reg, use_fng, decay, k_sl) in VARIANTS.items():
            key = tpm
            if key not in sig_cache:
                kw = {} if tpm is None else {"tp_mult": tpm}
                sig_cache[key] = triple_confirm_bidir(df, **kw)
            sig = sig_cache[key]
            if use_reg:
                sig = regime_mask(sig, regs)
            if use_fng:
                sig = fng_persist_3d(sig, fa)
            cfg = EnhancedBTConfig(
                starting_equity=100.0, risk_per_trade=0.020, max_leverage=5.0,
                eq_decay_tiers=DEFAULT_DECAY_TIERS if decay else (),
                cooldown_bars=k_sl)
            eq, tr = run_backtest_enhanced(dfe, sig[sig.index >= cut], cfg)
            pair_eq[v][p] = apply_funding(eq, tr)
            counts[v] += len(tr)
            if v == BASELINE:
                a = autopsy(tr)
                a["pair"] = p
                a["fund_drag"] = float(pair_eq[v][p].iloc[-1]) - float(eq.iloc[-1])
                aut.append(a)
        print(f"  {p:11s} {time.time()-t0:5.0f}s", flush=True)

    # --- B. exit autopsy ------------------------------------------------------
    A = pd.concat(aut, ignore_index=True)
    print("\n" + "=" * 86)
    print(f"EXIT AUTOPSY ({BASELINE}, all pairs, per-pair engine units)")
    print("=" * 86)
    print(f"{'reason':10s}{'n':>6s}{'WR%':>6s}{'meanR':>8s}{'sumR':>9s}{'mean bars':>10s}")
    for reason, g in A.groupby("reason"):
        print(f"{reason:10s}{len(g):6d}{g.win.mean()*100:6.0f}{g.r.mean():8.2f}"
              f"{g.r.sum():9.1f}{g.bars.mean():10.1f}")
    fund = sum(a["fund_drag"].iloc[0] for a in aut)
    print(f"\n  gross PnL {A.gross.sum():+9.1f}   fees {-A.fees.sum():+9.1f}   "
          f"net {A.pnl.sum():+9.1f}   funding {fund:+7.1f}   (per-100 units, 5 pairs)")

    # --- A/C. tables ------------------------------------------------------------
    idx = None
    for p in SOFT5:
        e = pair_eq[BASELINE][p]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    split = idx[int(len(idx) * 0.6)]
    is_idx, oos_idx = idx[idx < split], idx[idx >= split]
    yrs = (idx[-1] - idx[0]).days / 365

    full = {v: stats(build(pair_eq[v], SOFT5, idx)) for v in VARIANTS}
    isr = {v: stats(build(pair_eq[v], SOFT5, is_idx)) for v in VARIANTS}
    oos = {v: stats(build(pair_eq[v], SOFT5, oos_idx)) for v in VARIANTS}

    print("\n" + "=" * 86)
    print(f"LAYERS + PROBES  {idx[0].date()}..{idx[-1].date()} ({yrs:.2f}y)  SOFT5 @ ${TOTAL:.0f}")
    print("=" * 86)
    print(f"{'variant':11s}{'final$':>8s}{'CAGR%':>8s}{'Sh(mo)':>8s}{'MDD%':>7s}"
          f"{'worst':>7s}{'pos%':>6s}{'trades':>8s}{'IS Sh':>7s}{'OOS Sh':>8s}")
    for v in VARIANTS:
        s = full[v]
        print(f"{v:11s}{s['final']:8.0f}{s['cagr']*100:8.1f}{s['sh_m']:8.2f}"
              f"{s['mdd']*100:7.1f}{s['worst_mo']:7.1f}{s['win_mo']:6.0f}"
              f"{counts[v]:8d}{isr[v]['sh_m']:7.2f}{oos[v]['sh_m']:8.2f}")

    print("\nPER-PAIR final (per-100 units), key variants:")
    keyv = ["L0_RAW", "L4_PROD", "TP60", "TPINF"]
    print(f"{'pair':11s}" + "".join(f"{v:>10s}" for v in keyv))
    for p in SOFT5:
        print(f"{p:11s}" + "".join(
            f"{float(pair_eq[v][p].reindex(idx).ffill().iloc[-1]):10.1f}" for v in keyv))


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
