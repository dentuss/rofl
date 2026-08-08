"""MAKER-FILL FRAGILITY — how much of the edge rests on "any touch fills"?

NOT a proposed improvement, and NOT a book-consuming model. A FRAGILITY TEST
of an assumption the whole adopted stack depends on.

THE ASSUMPTION. `entry_style="maker_close"` fills a resting limit whenever the
bar trades through it by ANY amount:

    fill_ok = (low < limit) if long else (high > limit)

Physically, a resting order fills only once enough volume trades PAST your
price to clear the queue ahead of you. A 0.01 bp penetration means almost
nothing traded past you, so counting it as a fill is the most optimistic
assumption available — and it is precisely why FINDINGS reports "ZERO TP fills
lost, only 6/1048 entry fills missed". Those numbers are a consequence of the
assumption, not independent evidence for it.

WHY NOT A REAL BOOK MODEL. We hold 3 days of book data against a 3-year
backtest window, so a genuinely book-consuming entry model cannot be
backtested at all yet, and fitting one on 3 days would be fitting noise. What
CAN be done now is bound the exposure: require deeper penetration before
counting a fill, and watch how fast the edge decays. That is answerable with
the OHLCV we already have for the full window.

PRE-REGISTERED CELLS (one dimension, monotone, five points). Penetration is
expressed in bp of the limit price; the ladder deliberately spans "far below
one tick" to "several ticks" for the majors, whose measured one-tick spreads
run 0.02 bp (BTC) to 5.22 bp (ADA) — so the SAME bp threshold is a different
number of ticks per symbol, and that asymmetry is a stated limitation, not a
hidden one.

    F0    0.0 bp   any penetration fills — the CONTROL, today's engine
    F1    1.0 bp
    F2    2.0 bp
    F5    5.0 bp
    F10  10.0 bp

Raising the gate can ONLY remove fills, so every cell is a lower bound on the
control. There is no way for this experiment to flatter the book, which is why
it is safe to run without a fresh OOS split.

Everything else is exactly research/deploy_report.py (MAJORS8, BLEND50_CONF,
WF regime mask, F&G persistence, decay tiers, CHOP half-size, VT, CONF sizing,
TP-as-limit, real funding, MEASURED fees 10.0/3.6 bp, 365d warmup, common
window from 2023-08-17, unit weights).

WHAT A RESULT MEANS. This CANNOT promote anything — no cell here is more
"correct" than the control, because we do not know the true fill threshold.
It measures sensitivity only:
  * shallow decay  -> the edge does not live on marginal touches; reassuring
  * steep decay    -> a large share of the backtested edge comes from fills
                      that may never have happened, and the honest expectation
                      should be marked down accordingly

Also reports the DISTRIBUTION of realised penetration depths on the control,
so the ladder can be read against what the data actually does.

Run:  PYTHONIOENCODING=utf-8 ./.venv/bin/python research/maker_fill_depth.py
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

# PRE-REGISTERED — do not edit after the first run.
CELLS = [("F0   0.0bp (control)", 0.0), ("F1   1.0bp", 1.0),
         ("F2   2.0bp", 2.0), ("F5   5.0bp", 5.0), ("F10 10.0bp", 10.0)]


def stats(r: pd.Series) -> dict:
    eq = TOTAL * (1 + r).cumprod()
    yrs = max((r.index[-1] - r.index[0]).days, 1) / 365
    mo = eq.resample("ME").last().pct_change().dropna()
    sh = lambda s: float(s.mean() / s.std() * np.sqrt(12)) if len(s) > 2 and s.std() > 0 else 0.0
    n = len(mo)
    return dict(final=float(eq.iloc[-1]),
                cagr=((float(eq.iloc[-1]) / TOTAL) ** (1 / yrs) - 1) * 100,
                sh=sh(mo), mdd=float((eq / eq.cummax() - 1).min()) * 100,
                worst_m=float(mo.min()) * 100,
                is_sh=sh(mo.iloc[:int(n * .6)]), oos_sh=sh(mo.iloc[int(n * .6):]))


def main() -> None:
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print("MAKER-FILL FRAGILITY — cells:", [c[0] for c in CELLS], flush=True)
    fng = fetch_fear_greed()
    eq = {n: {"t": {}, "p": {}} for n, _ in CELLS}
    ntr = {n: 0 for n, _ in CELLS}
    pens: list[float] = []

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
        sigs = {}
        for tag, raw in [("t", triple_confirm_bidir(df, tp_mult=6.0)),
                         ("p", pullback_in_trend(df))]:
            s = fng_persist(regime_mask(raw, regs), fa)
            s["risk_mult"] = mult
            sigs[tag] = s[s.index >= cut]

        # Descriptive: how deep does a filling bar actually penetrate?
        for tag in ("t", "p"):
            s = sigs[tag]
            j = dfe.join(s[["signal"]], how="inner")
            lim = j["close"].shift(1)
            sg = j["signal"].shift(1)
            long_pen = (lim - j["low"]) / lim * 1e4
            short_pen = (j["high"] - lim) / lim * 1e4
            pen = np.where(sg == 1, long_pen, np.where(sg == -1, short_pen, np.nan))
            pens.extend([float(x) for x in pen if np.isfinite(x) and x > 0])

        for name, minbp in CELLS:
            for tag in ("t", "p"):
                cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                                       max_leverage=5.0,
                                       eq_decay_tiers=DEFAULT_DECAY_TIERS,
                                       cooldown_bars=3, fee_rate=FEE_TAKER,
                                       fee_maker=FEE_MAKER,
                                       entry_style="maker_close", tp_as_limit=True,
                                       maker_fill_min_bp=minbp)
                e, tr = run_backtest_enhanced(dfe, sigs[tag], cfg)
                eq[name][tag][p] = apply_funding_real(e, tr, fund)
                ntr[name] += len(tr)
        print(f"  {p:10s} {time.time()-t0:5.0f}s", flush=True)

    idx = None
    for p in MAJORS8:
        e = eq[CELLS[0][0]]["t"][p]
        e = e[e.index >= COMMON_START]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    w = {p: 1 / 8 for p in MAJORS8}

    rows = {}
    for name, _ in CELLS:
        pt, pp = build(eq[name]["t"], w, idx), build(eq[name]["p"], w, idx)
        blend = 0.5 * pt / pt.iloc[0] + 0.5 * pp / pp.iloc[0]
        rows[name] = stats(blend.resample("1D").last().pct_change().dropna())

    base, base_n = rows[CELLS[0][0]], ntr[CELLS[0][0]]
    print("\n" + "=" * 104)
    print(f"BLEND50_CONF, unit weights, {idx[0].date()}..{idx[-1].date()}, "
          f"measured fees {FEE_TAKER*1e4:.1f}/{FEE_MAKER*1e4:.1f}bp")
    print("=" * 104)
    print(f"{'cell':22s}{'trades':>8s}{'kept%':>7s}{'CAGR%':>8s}{'Sh(mo)':>8s}"
          f"{'dSh':>7s}{'dMDD%':>8s}{'worstM%':>9s}{'IS':>7s}{'OOS':>7s}")
    for name, _ in CELLS:
        s = rows[name]
        print(f"{name:22s}{ntr[name]:>8d}{100*ntr[name]/max(base_n,1):>7.0f}"
              f"{s['cagr']:>8.1f}{s['sh']:>8.2f}{s['sh']-base['sh']:>+7.2f}"
              f"{s['mdd']:>8.1f}{s['worst_m']:>9.1f}{s['is_sh']:>7.2f}{s['oos_sh']:>7.2f}")

    pa = np.array(pens)
    print("\n" + "=" * 104)
    print(f"REALISED PENETRATION DEPTH on filling bars (n={len(pa):,})")
    print("=" * 104)
    qs = [1, 5, 10, 25, 50, 75, 95]
    print("  percentile " + "".join(f"{q:>9d}" for q in qs))
    print("  bp         " + "".join(f"{np.percentile(pa, q):>9.1f}" for q in qs))
    for thr in (1.0, 2.0, 5.0, 10.0):
        print(f"  share of filling bars penetrating < {thr:4.1f} bp: "
              f"{100*(pa < thr).mean():5.1f}%")

    print("\n" + "=" * 104)
    print("READING")
    print("=" * 104)
    print("  No cell here can be PROMOTED — none is more correct than the control,")
    print("  because the true fill threshold is unknown. This bounds exposure only.")
    worst = min(rows[n]["sh"] for n, _ in CELLS)
    print(f"  Sharpe across the whole ladder: {base['sh']:.2f} (control) .. {worst:.2f}")
    print(f"  Edge retained at the harshest 10bp gate: "
          f"{100*rows[CELLS[-1][0]]['cagr']/base['cagr']:.0f}% of control CAGR "
          f"on {100*ntr[CELLS[-1][0]]/max(base_n,1):.0f}% of the trades.")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
