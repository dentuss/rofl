"""1d arm + 4h/1d ensemble — the same triple_bidir stack on DAILY bars as an
independent return stream, then capital blends with the adopted 4h book.

Rationale: the 4h book's edge survived every gate; a 1d version trades ~6x
less, pays ~6x less in fees per unit of signal, and if its monthly returns
are imperfectly correlated with the 4h book the BLEND buys Sharpe without
new machinery (same strategy, same venue, different clock).

PRE-REGISTERED cells (1d stack: walk-forward regimes bars_per_day=1, F&G
persistence 3 DAILY bars = same 3 calendar days as the 4h stack's 18 bars,
CHOP half-size, VT, maker entries, real funding, cooldown_bars=1 which on
the fixed engine == no cooldown — 3 4h-bars has no honest 1d analog):
  D1_T6   triple_confirm_bidir on 1d, tp_mult=6.0 (canonical params)
  D1_T9   tp_mult=9.0 (daily trends run longer; single pre-registered alt)
Ensemble (using D1_T6 only — canonical; T9 reported for information):
  BLEND50   0.5 x 4h book + 0.5 x 1d book (normalized equities)
  BLEND75   0.75 x 4h + 0.25 x 1d
Baseline: the adopted 4h MAJORS8/RSCD3+VT book, recomputed here.

Adoption bar: the 1d arm must stand alone (OOS no collapse, >=5/8 names
profitable); a blend replaces 4h-only iff full Sh improves AND OOS holds.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/arm_1d.py
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
from research.cost_engine import (apply_funding_real, regime_mask,
                                  sharpe_m, stats, build, FEE_TAKER, FEE_MAKER)
from research.regime_cache import wf_regimes_cached
from research.vol_target import vt_mult

MAJORS8 = [f"{b}-USDT" for b in
           ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX"]]
TOTAL = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))
COMMON_START = pd.Timestamp("2023-08-17", tz="UTC")
WARMUP_D = 365
FNG_BARS_1D = 3


def fng_persist_1d(sig, fa):
    above = (fa >= 80).rolling(FNG_BARS_1D, min_periods=FNG_BARS_1D).sum() == FNG_BARS_1D
    below = (fa <= 20).rolling(FNG_BARS_1D, min_periods=FNG_BARS_1D).sum() == FNG_BARS_1D
    block = ((sig["signal"] == 1) & above.reindex(sig.index).fillna(False)) | \
            ((sig["signal"] == -1) & below.reindex(sig.index).fillna(False))
    s = sig.copy()
    s.loc[block, "signal"] = 0
    s.loc[block, ["sl", "tp"]] = np.nan
    return s


def run_book(tf, bpd, tp, cooldown, fng):
    eqs = {}
    for p in MAJORS8:
        t0 = time.time()
        df = fetch_ohlcv_bybit(p, tf, days=DAYS)
        regs = wf_regimes_cached(df, p, tf, bpd)
        fa = align_to_bars(fng, df.index)
        fund = fetch_funding(p, days=DAYS, source="auto")
        raw = triple_confirm_bidir(df, tp_mult=tp)
        if tf == "1d":
            sig = fng_persist_1d(regime_mask(raw, regs), fa)
        else:
            from research.cost_engine import fng_persist
            sig = fng_persist(regime_mask(raw, regs), fa)
        a = regs.reindex(sig.index, method="ffill").fillna("CHOP")
        sig["risk_mult"] = np.where(a == "CHOP", 0.5, 1.0) * \
            vt_mult(df).reindex(sig.index).fillna(1.0).to_numpy()
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                               max_leverage=5.0,
                               eq_decay_tiers=DEFAULT_DECAY_TIERS,
                               cooldown_bars=cooldown, fee_rate=FEE_TAKER,
                               fee_maker=FEE_MAKER, entry_style="maker_close")
        eq, tr = run_backtest_enhanced(df[df.index >= cut],
                                       sig[sig.index >= cut], cfg)
        eqs[p] = apply_funding_real(eq, tr, fund)
        print(f"    {p:10s} {time.time()-t0:4.0f}s  trades={len(tr)}", flush=True)
    return eqs


def report(name, port, i_idx, o_idx, b3, idx):
    s = stats(port)
    i = stats(port.reindex(i_idx).dropna())
    o = stats(port.reindex(o_idx).dropna())
    th = "  ".join(
        f"{sharpe_m(port.reindex(idx[(idx >= b3[k]) & (idx < b3[k+1])]).dropna()):+.2f}"
        for k in range(3))
    print(f"{name:10s}{s['final']:8.0f}{s['cagr']*100:8.1f}{s['sh_m']:8.2f}"
          f"{s['mdd']*100:7.1f}{s['worst_mo']:7.1f}{i['sh_m']:7.2f}"
          f"{o['sh_m']:8.2f}{th:>22s}")
    return s


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print("1D ARM + 4h/1d ENSEMBLE  MAJORS8", flush=True)
    fng = fetch_fear_greed()

    print("  [4h baseline]", flush=True)
    eq_4h = run_book("4h", 6, 6.0, 3, fng)
    print("  [1d T6]", flush=True)
    eq_d6 = run_book("1d", 1, 6.0, 1, fng)
    print("  [1d T9]", flush=True)
    eq_d9 = run_book("1d", 1, 9.0, 1, fng)

    w = {p: 1 / 8 for p in MAJORS8}
    # common daily grid: resample the 4h book to daily closes
    idx4 = None
    for p in MAJORS8:
        e = eq_4h[p][eq_4h[p].index >= COMMON_START]
        idx4 = e.index if idx4 is None else idx4.intersection(e.index)
    port4 = build(eq_4h, w, idx4.sort_values()).resample("1D").last().dropna()

    books = {"4H_ONLY": port4}
    for nm, eqd in [("D1_T6", eq_d6), ("D1_T9", eq_d9)]:
        idxd = None
        for p in MAJORS8:
            e = eqd[p][eqd[p].index >= COMMON_START]
            idxd = e.index if idxd is None else idxd.intersection(e.index)
        books[nm] = build(eqd, w, idxd.sort_values()).resample("1D").last().dropna()

    common = books["4H_ONLY"].index
    for nm in ("D1_T6", "D1_T9"):
        common = common.intersection(books[nm].index)
    books = {nm: b.reindex(common) for nm, b in books.items()}

    r4 = books["4H_ONLY"].pct_change().fillna(0.0)
    r6 = books["D1_T6"].pct_change().fillna(0.0)
    books["BLEND50"] = TOTAL * (1 + 0.5 * r4 + 0.5 * r6).cumprod()
    books["BLEND75"] = TOTAL * (1 + 0.75 * r4 + 0.25 * r6).cumprod()

    idx = common
    split = idx[int(len(idx) * 0.6)]
    i_idx, o_idx = idx[idx < split], idx[idx >= split]
    b3 = [idx[0] + (idx[-1] - idx[0]) * k / 3 for k in range(4)]

    print("\n" + "=" * 96)
    print(f"1D ARM + BLENDS  {idx[0].date()}..{idx[-1].date()}  "
          f"MAJORS8 EW @ ${TOTAL:.0f}  (daily grid)")
    print("=" * 96)
    print(f"{'book':10s}{'final$':>8s}{'CAGR%':>8s}{'Sh(mo)':>8s}{'MDD%':>7s}"
          f"{'worst':>7s}{'IS Sh':>7s}{'OOS Sh':>8s}{'thirds':>22s}")
    for nm, port in books.items():
        report(nm, port / port.iloc[0] * TOTAL, i_idx, o_idx, b3, idx)

    mo4 = books["4H_ONLY"].resample("ME").last().pct_change().dropna()
    mo6 = books["D1_T6"].resample("ME").last().pct_change().dropna()
    mo9 = books["D1_T9"].resample("ME").last().pct_change().dropna()
    print(f"\n  monthly corr(4h, 1d_T6) = {float(mo4.corr(mo6)):+.2f}   "
          f"corr(4h, 1d_T9) = {float(mo4.corr(mo9)):+.2f}")
    for nm, eqd in [("D1_T6", eq_d6), ("D1_T9", eq_d9)]:
        prof = sum(1 for p in MAJORS8
                   if float(eqd[p].reindex(common, method='ffill').iloc[-1])
                   > float(eqd[p].reindex(common, method='ffill').iloc[0]))
        print(f"  {nm}: {prof}/8 names profitable on common window")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
