"""STOP GEOMETRY — SL width and trailing stop, both never honestly tested.

PROMPTED BY, BUT NOT JUSTIFIED BY, a live run of 7 consecutive stop-outs.
That streak is NOT evidence: with the backtested exit mix (sl 67.6% / tp
23.6%), P(no TP in 7 trades) = 15%, about one stretch in seven, and the 95%
CI on the true TP-rate given 0/7 is [0%, 41%] — which contains 23.6%. Seven
trades cannot reject the design. ~20 would start to.

The experiment is justified instead by an AUDIT finding: two parameters in
the deployed geometry have never been examined.

  * `sl_mult = 1.8` — TP width WAS swept (tp_mult 3→6, monotone improvement,
    honest_rebuild r2). Stop width never was; it simply appears as 1.8
    everywhere. Same class as the inherited `max_bars_in_trade = 96`.
  * `trail_atr` — present in the engine since the honest rebuild, carries a
    mechanics test, is commented "UNDER VALIDATION", and has NO verdict in
    FINDINGS. An orphaned knob.

MECHANISM WORTH TESTING (from the live fills): a fixed ~12 bp round trip is a
larger fraction of a narrower stop. Live R by stop width: 1.14% stop → −1.152R;
4.33% stop → −1.027R. Cost drag in R terms scales as 1/stop_width, so wider
stops should lose less per stop — offset against fewer winners reaching a
fixed 6-ATR target and smaller positions per unit risk.

PRE-REGISTERED CELLS — two SEPARATE one-dimensional ladders, not a grid.
Test budget 9 cells; at a 5% bar expect ~0-1 false positives, so the
promotion bar below is deliberately demanding.

  LADDER A — stop width (tp_mult fixed at 6.0):
      A1.2  A1.5  A1.8 (CONTROL, deployed)  A2.2  A3.0
    NOTE, stated in advance: widening SL with TP fixed also changes the
    reward:risk ratio (3.33 at 1.8 → 2.00 at 3.0). This is deliberately the
    test of "the stop is too tight", NOT of "scale both" — the confound is
    named rather than hidden, and a scale-both study would be a separate
    experiment with its own budget.

  LADDER B — trailing stop (sl 1.8 / tp 6.0 fixed):
      B0 (OFF, CONTROL)  B2.0  B3.0  B4.0   (ATR multiples)
    Prior, written before running: "winners must run" (partial-TP rejected
    TWICE) predicts a trailing stop HURTS. Weak counter-evidence: the
    2026-08-03 timestop study found meandering positions cut at +1.07R were
    fine to cut, so the law has a boundary.

PROMOTION BAR (identical to the timestop audit, which proved it can say no):
  (a) ΔSh ≥ +0.10 vs control, (b) IS and OOS both hold or improve,
  (c) sub-window thirds all positive, (d) dMDD not worse by >1.0pp.
Anything less is INCONCLUSIVE and the deployed value stands. G3 N/A (no entry
logic touched); G4/G5 not run, so nothing here is promotable to Adopted.

Everything else is exactly research/deploy_report.py at MEASURED fees.

Run:  PYTHONIOENCODING=utf-8 ./.venv/bin/python research/stop_geometry.py
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
from research.vol_target import vt_mult

MAJORS8 = [f"{b}-USDT" for b in
           ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX"]]
TOTAL = float(_os.environ.get("TOTAL_EQUITY", 1800.00))
DAYS = int(_os.environ.get("DAYS", 2000))
COMMON_START = pd.Timestamp("2023-08-17", tz="UTC")
WARMUP_D, BPD = 365, 6

# PRE-REGISTERED. (label, sl_mult, trail_atr)
CELLS = [("A1.2  sl 1.2", 1.2, 0.0), ("A1.5  sl 1.5", 1.5, 0.0),
         ("A1.8  sl 1.8 CONTROL", 1.8, 0.0),
         ("A2.2  sl 2.2", 2.2, 0.0), ("A3.0  sl 3.0", 3.0, 0.0),
         ("B0    trail off", 1.8, 0.0),
         ("B2.0  trail 2.0", 1.8, 2.0), ("B3.0  trail 3.0", 1.8, 3.0),
         ("B4.0  trail 4.0", 1.8, 4.0)]
CONTROL = "A1.8  sl 1.8 CONTROL"


def stats(r: pd.Series) -> dict:
    eq = TOTAL * (1 + r).cumprod()
    yrs = max((r.index[-1] - r.index[0]).days, 1) / 365
    mo = eq.resample("ME").last().pct_change().dropna()
    sh = lambda s: float(s.mean() / s.std() * np.sqrt(12)) if len(s) > 2 and s.std() > 0 else 0.0
    n = len(mo)
    thirds = [sh(mo.iloc[int(n*a/3):int(n*(a+1)/3)]) for a in range(3)]
    return dict(cagr=((float(eq.iloc[-1]) / TOTAL) ** (1 / yrs) - 1) * 100,
                sh=sh(mo), mdd=float((eq / eq.cummax() - 1).min()) * 100,
                worst_m=float(mo.min()) * 100, thirds=thirds,
                is_sh=sh(mo.iloc[:int(n*.6)]), oos_sh=sh(mo.iloc[int(n*.6):]))


def main() -> None:
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print(f"STOP GEOMETRY — {len(CELLS)} cells, fees {FEE_TAKER*1e4:.1f}/"
          f"{FEE_MAKER*1e4:.1f}bp", flush=True)
    fng = fetch_fear_greed()
    eq = {n: {"t": {}, "p": {}} for n, _, _ in CELLS}
    rec = {n: [] for n, _, _ in CELLS}

    for p in MAJORS8:
        t0 = time.time()
        df = fetch_ohlcv_bybit(p, "4h", days=DAYS)
        regs, conf = wf_labels_conf(df, BPD)
        fa = align_to_bars(fng, df.index)
        fund = fetch_funding(p, days=DAYS, source="auto")
        a = regs.reindex(df.index, method="ffill").fillna("CHOP")
        mult = np.where(a == "CHOP", 0.5, 1.0) * \
            vt_mult(df).reindex(df.index).fillna(1.0).to_numpy() * \
            (0.5 + 0.5 * conf.reindex(df.index).fillna(1.0).to_numpy())
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]
        for name, slm, tr_atr in CELLS:
            # sl_mult lives in the SIGNAL (it sets the sl column), so signals
            # must be rebuilt per cell — not just the engine config.
            sigs = {}
            for tag, raw in [("t", triple_confirm_bidir(df, tp_mult=6.0, sl_mult=slm)),
                             ("p", pullback_in_trend(df, tp_mult=6.0, sl_mult=slm))]:
                s = fng_persist(regime_mask(raw, regs), fa)
                s["risk_mult"] = mult
                sigs[tag] = s[s.index >= cut]
            for tag in ("t", "p"):
                cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                                       max_leverage=5.0,
                                       eq_decay_tiers=DEFAULT_DECAY_TIERS,
                                       cooldown_bars=3, fee_rate=FEE_TAKER,
                                       fee_maker=FEE_MAKER, entry_style="maker_close",
                                       tp_as_limit=True, trail_atr=tr_atr)
                e, t = run_backtest_enhanced(dfe, sigs[tag], cfg)
                eq[name][tag][p] = apply_funding_real(e, t, fund)
                rec[name].extend(t)
        print(f"  {p:10s} {time.time()-t0:5.0f}s", flush=True)

    idx = None
    for p in MAJORS8:
        e = eq[CONTROL]["t"][p]
        e = e[e.index >= COMMON_START]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    w = {p: 1 / 8 for p in MAJORS8}
    rows = {}
    for name, _, _ in CELLS:
        pt, pp = build(eq[name]["t"], w, idx), build(eq[name]["p"], w, idx)
        bl = 0.5 * pt / pt.iloc[0] + 0.5 * pp / pp.iloc[0]
        rows[name] = stats(bl.resample("1D").last().pct_change().dropna())

    base = rows[CONTROL]
    print("\n" + "=" * 108)
    print(f"BLEND50_CONF unit weights {idx[0].date()}..{idx[-1].date()}")
    print("=" * 108)
    print(f"{'cell':24s}{'trades':>8s}{'tp%':>7s}{'sl%':>7s}{'CAGR%':>8s}"
          f"{'Sh':>7s}{'dSh':>7s}{'dMDD%':>8s}{'IS':>7s}{'OOS':>7s}")
    for name, _, _ in CELLS:
        s, tr = rows[name], rec[name]
        n = len(tr)
        tp = 100 * sum(1 for x in tr if x.reason == "tp") / max(n, 1)
        sl = 100 * sum(1 for x in tr if x.reason == "sl") / max(n, 1)
        print(f"{name:24s}{n:>8d}{tp:>7.1f}{sl:>7.1f}{s['cagr']:>8.1f}"
              f"{s['sh']:>7.2f}{s['sh']-base['sh']:>+7.2f}"
              f"{s['mdd']-base['mdd']:>+8.1f}{s['is_sh']:>7.2f}{s['oos_sh']:>7.2f}")

    print("\n" + "=" * 108)
    print("PROMOTION BAR vs CONTROL  (a dSh>=+0.10  b IS&OOS hold  c thirds all +  d dMDD not -1pp)")
    print("=" * 108)
    for name, _, _ in CELLS:
        if name == CONTROL:
            continue
        s = rows[name]
        a = s['sh'] - base['sh'] >= 0.10
        b = s['is_sh'] >= base['is_sh'] - 1e-9 and s['oos_sh'] >= base['oos_sh'] - 1e-9
        c = all(t > 0 for t in s['thirds'])
        d = s['mdd'] >= base['mdd'] - 1.0
        print(f"  {name:24s} dSh {s['sh']-base['sh']:+.2f}  "
              f"a={'Y' if a else 'N'} b={'Y' if b else 'N'} c={'Y' if c else 'N'} "
              f"d={'Y' if d else 'N'}  -> "
              f"{'PASS' if (a and b and c and d) else 'INCONCLUSIVE, keep deployed'}")

    print("\n" + "=" * 108)
    print("MECHANISM — mean R by stop width (does cost drag explain it?)")
    print("=" * 108)
    for name, slm, tr_atr in CELLS:
        if tr_atr:
            continue
        tr = [x for x in rec[name] if x.reason == "sl"]
        if not tr:
            continue
        rs = [x.pnl / (x.notional * abs(x.entry_px - x.sl) / x.entry_px) for x in tr]
        sd = np.mean([abs(x.entry_px - x.sl) / x.entry_px for x in tr]) * 100
        print(f"  sl_mult {slm:.1f}: mean stop {sd:5.2f}%  mean R on stops "
              f"{np.mean(rs):+.3f}  (cost drag ~{12.1/ (sd*100):.3f}R)")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
