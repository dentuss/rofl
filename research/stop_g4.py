"""G4 UNIVERSE GENERALIZATION for sl_mult 3.0 — the gate, not a new search.

`research/stop_geometry.py` (2026-08-13) found sl 3.0 clearing the promotion
bar on MAJORS8 (ΔSh +0.12, all four criteria), but with three warnings the bar
did not encode: the ladder was NON-MONOTONE (1.8 a local peak, 2.2 dipping
−0.14, 3.0 rising), +0.12 sits at 2–4x the documented jitter, and IS improved
more than OOS. Those are the fingerprints of a fit to one book.

G4 is the test that separates the two readings. Per the standing gate —
"universe generalization, zero re-tuning" — a REAL mechanism must improve the
other structural books too. Vol-targeting, the last change to pass G4, moved
ALL four (EW10 1.04→1.36, MAJORS8 1.17→1.42, EW23 0.58→0.86). A change that
lifts MAJORS8 alone is fitted to MAJORS8.

NO NEW CELLS. Exactly one comparison, sl 1.8 (deployed) vs sl 3.0, carried
unchanged onto books built the structural way: QUAL23 ranked by EX-ANTE median
daily dollar volume, top 8 / 12 / 16 / all 23. Universe is never picked by
performance (law 4), so the ranking uses volume only.

PASS REQUIRES: ΔSh > 0 on ALL FOUR books, and the MAJORS8 gain not to be an
outlier among them. Anything else means the +0.12 was noise and the deployed
1.8 stands.

Run:  PYTHONIOENCODING=utf-8 ./.venv/bin/python research/stop_g4.py
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
from research.tsmom_sleeve import QUAL23
from research.vol_target import vt_mult

TOTAL = float(_os.environ.get("TOTAL_EQUITY", 1800.00))
DAYS = int(_os.environ.get("DAYS", 2000))
COMMON_START = pd.Timestamp("2023-08-17", tz="UTC")
WARMUP_D, BPD = 365, 6
CELLS = [("sl 1.8 (deployed)", 1.8), ("sl 3.0", 3.0)]


def sh_mo(r: pd.Series) -> tuple[float, float, float, float]:
    eq = TOTAL * (1 + r).cumprod()
    yrs = max((r.index[-1] - r.index[0]).days, 1) / 365
    mo = eq.resample("ME").last().pct_change().dropna()
    f = lambda s: float(s.mean() / s.std() * np.sqrt(12)) if len(s) > 2 and s.std() > 0 else 0.0
    n = len(mo)
    return (f(mo), ((float(eq.iloc[-1]) / TOTAL) ** (1 / yrs) - 1) * 100,
            float((eq / eq.cummax() - 1).min()) * 100,
            f(mo.iloc[int(n * .6):]))


def main() -> None:
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print(f"G4 for sl_mult 3.0 — {len(QUAL23)} names, 2 cells, no re-tuning",
          flush=True)
    fng = fetch_fear_greed()
    eq = {n: {"t": {}, "p": {}} for n, _ in CELLS}
    dvol, ok = {}, []

    for p in QUAL23:
        t0 = time.time()
        try:
            df = fetch_ohlcv_bybit(p, "4h", days=DAYS)
            regs, conf = wf_labels_conf(df, BPD)
            fa = align_to_bars(fng, df.index)
            fund = fetch_funding(p, days=DAYS, source="auto")
        except Exception as e:
            print(f"  {p:10s} SKIP {type(e).__name__}", flush=True)
            continue
        # EX-ANTE structural ranking input: median daily dollar volume.
        d = (df["close"] * df["volume"]).resample("1D").sum()
        dvol[p] = float(d.median())
        a = regs.reindex(df.index, method="ffill").fillna("CHOP")
        mult = np.where(a == "CHOP", 0.5, 1.0) * \
            vt_mult(df).reindex(df.index).fillna(1.0).to_numpy() * \
            (0.5 + 0.5 * conf.reindex(df.index).fillna(1.0).to_numpy())
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]
        for name, slm in CELLS:
            for tag, raw in [("t", triple_confirm_bidir(df, tp_mult=6.0, sl_mult=slm)),
                             ("p", pullback_in_trend(df, tp_mult=6.0, sl_mult=slm))]:
                s = fng_persist(regime_mask(raw, regs), fa)
                s["risk_mult"] = mult
                cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                                       max_leverage=5.0,
                                       eq_decay_tiers=DEFAULT_DECAY_TIERS,
                                       cooldown_bars=3, fee_rate=FEE_TAKER,
                                       fee_maker=FEE_MAKER,
                                       entry_style="maker_close", tp_as_limit=True)
                e, tr = run_backtest_enhanced(dfe, s[s.index >= cut], cfg)
                eq[name][tag][p] = apply_funding_real(e, tr, fund)
        ok.append(p)
        print(f"  {p:10s} {time.time()-t0:5.0f}s  medDV ${dvol[p]/1e6:6.1f}M", flush=True)

    ranked = sorted(ok, key=lambda p: -dvol[p])
    print("\n  liquidity ranking: " + " ".join(p.split("-")[0] for p in ranked))
    books = {"MAJORS8": ranked[:8], "MAJORS12": ranked[:12],
             "MAJORS16": ranked[:16], f"EW{len(ranked)}": ranked}

    idx = None
    for p in ok:
        e = eq[CELLS[0][0]]["t"][p]
        e = e[e.index >= COMMON_START]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()

    print("\n" + "=" * 96)
    print(f"G4 — does sl 3.0 carry across structural books?  {idx[0].date()}..{idx[-1].date()}")
    print("=" * 96)
    print(f"  {'book':10s}{'n':>4s}" + "".join(
        f"{c:>26s}" for c, _ in CELLS) + f"{'dSh':>8s}{'dCAGR':>8s}")
    res = {}
    for bname, names in books.items():
        w = {p: 1 / len(names) for p in names}
        out = []
        for cname, _ in CELLS:
            pt, pp = build({p: eq[cname]["t"][p] for p in names}, w, idx), \
                     build({p: eq[cname]["p"][p] for p in names}, w, idx)
            bl = 0.5 * pt / pt.iloc[0] + 0.5 * pp / pp.iloc[0]
            out.append(sh_mo(bl.resample("1D").last().pct_change().dropna()))
        res[bname] = out
        cells = "".join(f"   Sh {o[0]:5.2f} CAGR {o[1]:5.1f}" for o in out)
        print(f"  {bname:10s}{len(names):>4d}{cells}"
              f"{out[1][0]-out[0][0]:>+8.2f}{out[1][1]-out[0][1]:>+8.1f}")

    print("\n" + "=" * 96)
    print("VERDICT — pass requires dSh > 0 on ALL books, MAJORS8 not an outlier")
    print("=" * 96)
    ds = {b: res[b][1][0] - res[b][0][0] for b in books}
    allpos = all(v > 0 for v in ds.values())
    others = [v for b, v in ds.items() if b != "MAJORS8"]
    print("  " + "   ".join(f"{b} {v:+.2f}" for b, v in ds.items()))
    print(f"  all books positive: {'YES' if allpos else 'NO'}")
    print(f"  MAJORS8 {ds['MAJORS8']:+.2f} vs mean of others {np.mean(others):+.2f}")
    if allpos and ds["MAJORS8"] <= max(others) + 0.10:
        print("\n  G4 PASS — the effect generalises; sl 3.0 earns further validation.")
    else:
        print("\n  G4 FAIL — the MAJORS8 result does not carry. Consistent with the")
        print("  non-monotone ladder: +0.12 was a fit to one book. DEPLOYED 1.8 STANDS.")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
