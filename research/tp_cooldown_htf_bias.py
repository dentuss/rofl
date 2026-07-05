"""Post-TP re-entry cooldown and soft HTF risk bias — validation on the full
production stack, SOFT5 portfolio, Bybit perp data — PLUS the study that this
work uncovered: the same-bar re-entry ARTIFACT correction.

DISCOVERY (2026-07-05): the first run of this study showed that blocking only
the engine's same-bar post-TP re-entry (exit at TP intra-bar, re-enter at that
SAME bar's open — a fill from BEFORE the exit) collapses SOFT5's backtested
CAGR from ~213% to ~0%. The engine was manufacturing the edge: 2,538 such
trades (41% of all trades) at mean +0.55R with a mean +1.34% impossible fill
advantage (raw-ADA measurement). bot.py is bar-close gated and cannot make
these trades. The engines are now FIXED by default (legacy_same_bar_reentry
flag reproduces old numbers); every pre-2026-07-05 absolute backtest number is
inflated by this artifact.

Live cooldown mapping (bot.py gate compares the SIGNAL bar's ts, engine
compares the fill bar): live COOLDOWN_BARS=3 blocks entry fills on bars
[exit+1, exit+3] == FIXED engine cooldown_bars=4. So FIXED_K4 is "live as it
runs today"; FIXED_K3 is the literal adopted config.

Variants:
  LEGACY       old engine, SL K=3 — reproduces the adopted (inflated) numbers;
               sanity-checked against the frozen adopted wrapper
  FIXED_K3     fixed engine, SL K=3 (adopted config, honest measurement)
  FIXED_K4     fixed engine, SL K=4 (live-gate semantics) — HONEST BASELINE
  F_TPK2/3     + post-TP same-side cooldown 2/3 bars (K_tp=1 == the fix itself;
               K_tp=2 blocks the first real re-entry bar — the ADA 2026-07-03
               trade; K_tp=3 blocks two)
  F_HTF1D      + with_htf_risk_bias 1D EMA50, counter-trend risk x0.5
  F_HTF1D75    + same, x0.75
  F_HTF4H      + 4h EMA50, x0.5
  F_TPK2_HTF1D combo

Also prints post-TP same-side re-entry EV by gap (on the honest baseline).
Selection-bias guard: pick any winner on IS, confirm on OOS.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/tp_cooldown_htf_bias.py
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
from core.strategies_enhanced import with_htf_risk_bias
from research.test_reentry_cooldown_prod import (
    run_enhanced_with_cooldown, regime_filtered, fng_persist_3d, apply_funding)

SOFT5 = {"INJ-USDT": .25, "SOL-USDT": .1875, "ADA-USDT": .1875,
         "ETH-USDT": .1875, "LINK-USDT": .1875}
TOTAL = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))
WARMUP_D = 365
BPY = 24 * 365
BASELINE = "FIXED_K4"

# name -> (legacy, sl_k, tp_k, bias) with bias = (htf_rule, ema_n, counter_mult)
VARIANTS = {
    "LEGACY":       (True,  3, 0, None),
    "FIXED_K3":     (False, 3, 0, None),
    "FIXED_K4":     (False, 4, 0, None),
    "F_TPK2":       (False, 4, 2, None),
    "F_TPK3":       (False, 4, 3, None),
    "F_HTF1D":      (False, 4, 0, ("1D", 50, 0.5)),
    "F_HTF1D75":    (False, 4, 0, ("1D", 50, 0.75)),
    "F_HTF4H":      (False, 4, 0, ("4h", 50, 0.5)),
    "F_TPK2_HTF1D": (False, 4, 2, ("1D", 50, 0.5)),
}


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
                med_mo=float(mo.median()) if len(mo) else 0,
                win_mo=float((mo > 0).mean() * 100) if len(mo) else 0)


def build(pair_eq, w, idx):
    port = None
    for p, wt in w.items():
        e = pair_eq[p].reindex(idx).ffill()
        e = e / e.iloc[0] * wt
        port = e if port is None else port + e
    return port * TOTAL


def post_tp_reentry_diag(trades):
    """For each trade entered after a same-side TP exit: gap in bars,
    R-multiple, and whether it chased (entered worse than the prior TP exit)."""
    rows = []
    for prev, t in zip(trades, trades[1:]):
        if prev.reason != "tp" or prev.side != t.side:
            continue
        gap = int(round((t.entry_time - prev.exit_time) / pd.Timedelta(hours=1)))
        risk = t.notional * abs(t.entry_px - t.sl) / t.entry_px
        r = t.pnl / risk if risk > 0 else 0.0
        chase = t.entry_px > prev.exit_px if t.side == 1 else t.entry_px < prev.exit_px
        rows.append(dict(gap=gap, r=r, pnl=t.pnl, win=t.pnl > 0, chase=chase))
    return rows


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing — regime GMM would be a silent no-op")
    print(f"BYBIT PERP  SOFT5  TOTAL={TOTAL:.0f}  warmup={WARMUP_D}d  "
          f"variants={list(VARIANTS)}", flush=True)
    fng = fetch_fear_greed()

    pair_eq = {v: {} for v in VARIANTS}
    trade_counts = {v: 0 for v in VARIANTS}
    diag_rows = []
    sanity_done = False

    for p in SOFT5:
        t0 = time.time()
        df = fetch_ohlcv_bybit(p, "1h", days=DAYS)
        regs = walk_forward_regimes(df, bars_per_day=24, train_days=365, step_days=30)
        rc = regs.value_counts().to_dict()
        assert (rc.get("BULL", 0) + rc.get("BEAR", 0)) > 0, \
            f"{p}: regime all-CHOP {rc} — sklearn broken?"
        fa = align_to_bars(fng, df.index)
        sig = fng_persist_3d(regime_filtered(df, regs), fa)
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]

        if not sanity_done:
            cfg0 = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                                    max_leverage=5.0, eq_decay_tiers=DEFAULT_DECAY_TIERS,
                                    cooldown_bars=3, legacy_same_bar_reentry=True)
            eqA, _ = run_backtest_enhanced(dfe, sig[sig.index >= cut], cfg0)
            eqB, _ = run_enhanced_with_cooldown(dfe, sig[sig.index >= cut], cfg0, 3)
            d = abs(float(eqA.iloc[-1]) - float(eqB.iloc[-1]))
            assert d < 1e-6, f"sanity FAIL: LEGACY engine != adopted wrapper ({d})"
            print(f"sanity: legacy_same_bar_reentry=True reproduces the adopted "
                  f"wrapper at K=3 (delta={d:.2e}) OK", flush=True)
            sanity_done = True

        for v, (legacy, sl_k, tp_k, bias) in VARIANTS.items():
            sig_v = sig if bias is None else with_htf_risk_bias(
                df, sig, htf_rule=bias[0], ema_n=bias[1], counter_mult=bias[2])
            cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                                   max_leverage=5.0, eq_decay_tiers=DEFAULT_DECAY_TIERS,
                                   cooldown_bars=sl_k, cooldown_bars_tp=tp_k,
                                   legacy_same_bar_reentry=legacy)
            eq, tr = run_backtest_enhanced(dfe, sig_v[sig_v.index >= cut], cfg)
            pair_eq[v][p] = apply_funding(eq, tr)
            trade_counts[v] += len(tr)
            if v == BASELINE:
                diag_rows.extend(post_tp_reentry_diag(tr))
        print(f"  {p:11s} {time.time()-t0:5.0f}s  {dfe.index[0].date()}..{dfe.index[-1].date()}",
              flush=True)

    # --- post-TP re-entry EV diagnostic (honest baseline, all pairs) ---------
    print("\n" + "=" * 86)
    print(f"POST-TP SAME-SIDE RE-ENTRY EV ({BASELINE}, per-pair engine units, R = pnl/risk)")
    print("=" * 86)
    dd = pd.DataFrame(diag_rows)
    if len(dd):
        buckets = [("gap 1 (first live bar — blocked by K_tp>=2)", dd.gap == 1),
                   ("gap 2 (blocked by K_tp=3)", dd.gap == 2),
                   ("gap 3-5", (dd.gap >= 3) & (dd.gap <= 5)),
                   ("gap 6+", dd.gap >= 6)]
        print(f"{'bucket':44s}{'n':>5s}{'meanR':>8s}{'sumR':>8s}{'WR%':>6s}{'chase%':>8s}")
        for label, m in buckets:
            b = dd[m]
            if not len(b):
                print(f"{label:44s}{0:5d}")
                continue
            print(f"{label:44s}{len(b):5d}{b.r.mean():8.2f}{b.r.sum():8.1f}"
                  f"{b.win.mean()*100:6.0f}{b.chase.mean()*100:8.0f}")
        ch = dd[(dd.gap >= 1) & (dd.gap <= 2)]
        if len(ch):
            print(f"\n  blockable re-entries (gap 1-2): n={len(ch)}  "
                  f"meanR {ch.r.mean():+.2f}  sumR {ch.r.sum():+.1f}  "
                  f"chase meanR {ch[ch.chase].r.mean():+.2f} (n={int(ch.chase.sum())}) "
                  f"vs pullback meanR {ch[~ch.chase].r.mean():+.2f} (n={int((~ch.chase).sum())})")
    else:
        print("  no post-TP same-side re-entries found (?)")

    # --- portfolio tables -----------------------------------------------------
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
    print(f"FULL WINDOW  {idx[0].date()}..{idx[-1].date()} ({yrs:.2f}y)  SOFT5 @ ${TOTAL:.0f}, Bybit")
    print("=" * 86)
    print(f"{'variant':14s}{'final$':>8s}{'CAGR%':>8s}{'Sh(mo)':>8s}{'Sh(h)':>7s}"
          f"{'MDD%':>7s}{'worst':>7s}{'med':>6s}{'pos%':>6s}{'trades':>8s}")
    for v in VARIANTS:
        s = full[v]
        print(f"{v:14s}{s['final']:8.0f}{s['cagr']*100:8.1f}{s['sh_m']:8.2f}"
              f"{s['sh_h']:7.2f}{s['mdd']*100:7.1f}{s['worst_mo']:7.1f}"
              f"{s['med_mo']:6.2f}{s['win_mo']:6.0f}{trade_counts[v]:8d}")

    print("\n" + "=" * 86)
    print(f"IS/OOS 60-40  IS {is_idx[0].date()}..{is_idx[-1].date()} | "
          f"OOS {oos_idx[0].date()}..{oos_idx[-1].date()}")
    print("=" * 86)
    print(f"{'variant':14s}{'IS Sh(mo)':>10s}{'OOS Sh(mo)':>11s}{'IS CAGR%':>10s}"
          f"{'OOS CAGR%':>10s}{'OOS MDD%':>9s}{'OOS worst':>10s}")
    for v in VARIANTS:
        print(f"{v:14s}{isr[v]['sh_m']:10.2f}{oos[v]['sh_m']:11.2f}"
              f"{isr[v]['cagr']*100:10.1f}{oos[v]['cagr']*100:10.1f}"
              f"{oos[v]['mdd']*100:9.1f}{oos[v]['worst_mo']:10.1f}")

    print("\n" + "=" * 86)
    print(f"DELTAS vs {BASELINE} (full window)")
    print("=" * 86)
    b = full[BASELINE]
    for v in VARIANTS:
        if v == BASELINE:
            continue
        s = full[v]
        print(f"  {v:13s} dSh(mo) {s['sh_m']-b['sh_m']:+.2f}  dCAGR {100*(s['cagr']-b['cagr']):+.1f}pp  "
              f"dMDD {100*(s['mdd']-b['mdd']):+.1f}pp  dWorstMo {s['worst_mo']-b['worst_mo']:+.1f}pp  "
              f"dTrades {trade_counts[v]-trade_counts[BASELINE]:+d}")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
