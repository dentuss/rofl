"""TIMESTOP-4H — audit of the INHERITED `max_bars_in_trade = 96` on the 4h book.

WHY THIS IS A LEGITIMATE EXPERIMENT AND NOT GRID MINING
-------------------------------------------------------
`BTConfig.max_bars_in_trade` defaults to 96 and its own comment dates it:
"96 bars = 24h on 15m, 4d on 1h" (core/backtest.py:26). It was calibrated in
the 15m/1h era and was never re-examined when the book moved to 4h bars,
where 96 bars is a forced exit at **16 days**. It is NOT dormant:
  * engine   — core/backtest_enhanced.py:172 (reason="time", taker close at
               the bar close, no cooldown armed)
  * live bot — bot.py:1068, env MAX_BARS default "96"
  * compose  — MAX_BARS is set in NO compose file, so live inherits 96
So every honest-era number in FINDINGS was measured WITH this cap, and L1
would trade with it. Parity between engine and bot holds; the parameter
itself is simply unvalidated on this timeframe.

The cap is in direct tension with the most-defended law in the book:
"winners must run" — partial TP + breakeven was rejected TWICE (artifact-era
and again on the honest 4h base), and TP widening improved MONOTONICALLY
(tight TP was an artifact selection). A 16-day guillotine on a 6-ATR target
truncates exactly the right tail those findings say the edge lives in.
Prior, stated BEFORE running: relaxing the cap helps, or is free.

PRE-REGISTERED CELLS (one dimension, monotone relaxation, four points)
----------------------------------------------------------------------
  T96     96 bars = 16d   -- the inherited default; CONTROL (what L1 runs)
  T180   180 bars = 30d
  T360   360 bars = 60d
  TOFF   disabled          -- no time stop at all
No interaction grid, no second dimension, no re-parameterisation of anything
else. Test budget: 4 cells. If this ladder is later widened that is a NEW
experiment with its own OOS.

EVERYTHING ELSE IS EXACTLY research/deploy_report.py (the deployed stack):
MAJORS8, BLEND50_CONF (0.5 triple_bidir tp6 + 0.5 pullback_in_trend), walk-
forward GMM regime mask, F&G 3-day persistence, 3-tier decay, CHOP half-size,
vol targeting 60% ann, GMM-confidence sizing, SL cooldown 3 engine bars,
maker entries (entry_style="maker_close"), TP-as-limit, real per-pair
funding, 365d warmup, common window from 2023-08-17, unit weights, daily
granularity.

PROMOTION BAR, DECLARED IN ADVANCE
-----------------------------------
To displace the inherited T96 a cell must clear ALL of:
  (a) Sh(mo) >= T96 + 0.10
  (b) G1 — IS and OOS both hold or improve vs T96 (no OOS decay)
  (c) G2 — sub-window thirds all positive
  (d) dMDD no worse than T96 by more than 1.0pp
Anything short of all four = INCONCLUSIVE, keep the inherited 96, and the
negative goes in FINDINGS with the same prominence as a win.

G3 (random-entry null) is N/A: this touches no entry logic. G4 (universe
generalization) and G5 (exec parity) are NOT run here, so the ceiling status
this script can produce is "Promising — under validation". It CANNOT produce
an "Adopted", and nothing here goes near capital.

DIAGNOSTIC (descriptive, not a cell): exit-reason mix and mean return by
reason on the control. If time-exits are a negligible share of trades the
whole question is moot and that is the finding.

Run:  PYTHONIOENCODING=utf-8 ./.venv/bin/python research/timestop_4h.py
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
WARMUP_D = 365
BPD = 6

# PRE-REGISTERED — do not edit after the first run.
CELLS: list[tuple[str, int]] = [
    ("T96  (16d, inherited)", 96),
    ("T180 (30d)", 180),
    ("T360 (60d)", 360),
    ("TOFF (disabled)", 10 ** 9),
]


def cell_stats(r: pd.Series) -> dict:
    """Daily-return series -> the deploy_report metric set + thirds."""
    eq = TOTAL * (1 + r).cumprod()
    yrs = max((r.index[-1] - r.index[0]).days, 1) / 365
    cagr = (float(eq.iloc[-1]) / TOTAL) ** (1 / yrs) - 1
    mdd = float((eq / eq.cummax() - 1).min())
    mo = eq.resample("ME").last().pct_change().dropna()
    sh = lambda s: float(s.mean() / s.std() * np.sqrt(12)) if len(s) > 2 and s.std() > 0 else 0.0
    wk = eq.resample("W").last().pct_change().dropna()
    n = len(mo)
    thirds = [sh(mo.iloc[int(n * a / 3):int(n * (a + 1) / 3)]) for a in range(3)]
    return dict(final=float(eq.iloc[-1]), cagr=cagr * 100, sh=sh(mo), mdd=mdd * 100,
                worst_d=float(r.min()) * 100, worst_w=float(wk.min()) * 100,
                worst_m=float(mo.min()) * 100, win=float((mo > 0).mean() * 100),
                is_sh=sh(mo.iloc[:int(n * 0.6)]), oos_sh=sh(mo.iloc[int(n * 0.6):]),
                thirds=thirds, ann_vol=float(r.std() * np.sqrt(365)) * 100)


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print(f"TIMESTOP-4H — inherited max_bars_in_trade audit, deposit ${TOTAL:.2f}")
    print(f"cells (pre-registered): {[c[0] for c in CELLS]}", flush=True)
    fng = fetch_fear_greed()

    # eq[cell_name][leg][pair]; diag holds control-cell trades for the mix table
    eq = {name: {"t": {}, "p": {}} for name, _ in CELLS}
    diag: list = []

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

        # Signals built ONCE per pair; only the engine's time cap varies.
        sigs = {}
        for tag, raw in [("t", triple_confirm_bidir(df, tp_mult=6.0)),
                         ("p", pullback_in_trend(df))]:
            s = fng_persist(regime_mask(raw, regs), fa)
            s["risk_mult"] = mult
            sigs[tag] = s[s.index >= cut]

        for name, mb in CELLS:
            for tag in ("t", "p"):
                cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                                       max_leverage=5.0,
                                       eq_decay_tiers=DEFAULT_DECAY_TIERS,
                                       cooldown_bars=3, fee_rate=FEE_TAKER,
                                       fee_maker=FEE_MAKER,
                                       entry_style="maker_close", tp_as_limit=True,
                                       max_bars_in_trade=mb)
                e, tr = run_backtest_enhanced(dfe, sigs[tag], cfg)
                eq[name][tag][p] = apply_funding_real(e, tr, fund)
                if name == CELLS[0][0]:          # control cell only
                    for t in tr:
                        stop = abs(t.entry_px - t.sl) / t.entry_px
                        diag.append(dict(pair=p, leg=tag, reason=t.reason,
                                         bars=t.bars_held,
                                         r=(t.pnl / (t.notional * stop)) if stop > 0 else 0.0))
        print(f"  {p:10s} {time.time()-t0:5.0f}s", flush=True)

    # ---- common window + unit-weight blend, identical to deploy_report ----
    idx = None
    for p in MAJORS8:
        e = eq[CELLS[0][0]]["t"][p]
        e = e[e.index >= COMMON_START]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    w = {p: 1 / 8 for p in MAJORS8}

    rows = {}
    for name, _ in CELLS:
        pt = build(eq[name]["t"], w, idx)
        pp = build(eq[name]["p"], w, idx)
        blend = 0.5 * pt / pt.iloc[0] + 0.5 * pp / pp.iloc[0]
        rows[name] = cell_stats(blend.resample("1D").last().pct_change().dropna())

    print("\n" + "=" * 112)
    print(f"BLEND50_CONF — unit weights, {idx[0].date()}..{idx[-1].date()}, "
          f"start ${TOTAL:.2f}")
    print("=" * 112)
    print(f"{'cell':24s}{'final$':>9s}{'CAGR%':>8s}{'Sh(mo)':>8s}{'dMDD%':>8s}"
          f"{'worstD%':>9s}{'worstW%':>9s}{'worstM%':>9s}{'win%':>7s}"
          f"{'IS':>7s}{'OOS':>7s}")
    for name, _ in CELLS:
        s = rows[name]
        print(f"{name:24s}{s['final']:>9.0f}{s['cagr']:>8.1f}{s['sh']:>8.2f}"
              f"{s['mdd']:>8.1f}{s['worst_d']:>9.1f}{s['worst_w']:>9.1f}"
              f"{s['worst_m']:>9.1f}{s['win']:>7.0f}{s['is_sh']:>7.2f}{s['oos_sh']:>7.2f}")

    print(f"\n{'cell':24s}{'third1':>9s}{'third2':>9s}{'third3':>9s}{'annVol%':>10s}")
    for name, _ in CELLS:
        s = rows[name]
        print(f"{name:24s}" + "".join(f"{t:>9.2f}" for t in s['thirds'])
              + f"{s['ann_vol']:>10.1f}")

    # ---- promotion bar, evaluated mechanically ----
    base = rows[CELLS[0][0]]
    print("\n" + "=" * 112)
    print("PRE-REGISTERED PROMOTION BAR vs T96 "
          "(a: dSh>=+0.10, b: IS&OOS hold, c: thirds all +, d: dMDD not worse by >1pp)")
    print("=" * 112)
    for name, _ in CELLS[1:]:
        s = rows[name]
        a = s['sh'] - base['sh'] >= 0.10
        b = s['is_sh'] >= base['is_sh'] - 1e-9 and s['oos_sh'] >= base['oos_sh'] - 1e-9
        c = all(t > 0 for t in s['thirds'])
        d = s['mdd'] >= base['mdd'] - 1.0
        verdict = "PASS -> promising" if (a and b and c and d) else "INCONCLUSIVE -> keep T96"
        print(f"{name:24s} dSh {s['sh']-base['sh']:+.2f}  "
              f"a={'Y' if a else 'N'} b={'Y' if b else 'N'} "
              f"c={'Y' if c else 'N'} d={'Y' if d else 'N'}   {verdict}")

    # ---- descriptive diagnostic on the control ----
    d = pd.DataFrame(diag)
    print("\n" + "=" * 112)
    print("EXIT-REASON MIX on the control cell T96 (descriptive, not a cell)")
    print("=" * 112)
    print(f"{'leg':6s}{'reason':10s}{'n':>7s}{'share%':>9s}{'meanR':>9s}"
          f"{'medR':>9s}{'meanBars':>10s}")
    for leg in ("t", "p"):
        sub = d[d.leg == leg]
        for reason, g in sub.groupby("reason"):
            print(f"{leg:6s}{reason:10s}{len(g):>7d}{len(g)/max(len(sub),1)*100:>9.1f}"
                  f"{g.r.mean():>9.2f}{g.r.median():>9.2f}{g.bars.mean():>10.1f}")
    tt = d[d.reason == "time"]
    print(f"\n  time-exits: {len(tt)} of {len(d)} trades "
          f"({len(tt)/max(len(d),1)*100:.1f}%), mean R {tt.r.mean() if len(tt) else 0:+.2f}, "
          f"total R {tt.r.sum() if len(tt) else 0:+.1f}")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
