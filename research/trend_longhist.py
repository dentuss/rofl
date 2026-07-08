"""Trend-book LONG-HISTORY GATE — the test we killed the sleeves with,
applied to our own promoted book. The BLEND50_CONF stack has only ever been
measured from 2023-08-17; every layer was designed on post-2023 data, so
everything BEFORE that date is pseudo-OOS — including most of the 2022 bear
for the majors with deep Bybit history.

PRE-REGISTERED:
- MAJORS8, full available Bybit history (DAYS=2000), per-pair evaluable
  window = data start + 365d regime/VT warmup. NOTHING clipped to
  COMMON_START.
- Books: TRIPLE_CONF leg, PULL_CONF leg, BLEND50_CONF (the promoted stack,
  incl. tp_as_limit) — each as an EXPANDING equal-weight book: at every bar,
  average the bar returns of all names whose window has started (>= 3 names
  required; earlier bars dropped). Names-live-by-year reported (the sleeve
  post-mortem lesson: thinness is a real caveat, say it out loud).
- GATE (same bar the sleeves failed): PASS iff full-history Sh(mo) >= 0.5
  AND pre-2023-08 Sh(mo) >= 0.0. Yearly Sharpes printed. Per-leg results
  are diagnostic; the gate verdict applies to the BLEND.
- Also reported: the COMMON-window book (intersection of all 8 names) for
  continuity with every prior table.

If the blend FAILS pre-2023, the assembly-v2 vol-dial table is overstated
and the go-live sizing must be cut or the book redesigned — same standard we
applied to TSMOM/carry.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/trend_longhist.py
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
                                  FEE_TAKER, FEE_MAKER)
from research.regime_upgrades import wf_labels_conf
from research.vol_target import vt_mult

MAJORS8 = [f"{b}-USDT" for b in
           ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX"]]
DAYS = int(_os.environ.get("DAYS", 2000))
SPLIT = pd.Timestamp("2023-08-17", tz="UTC")
WARMUP_D = 365
BPD = 6
MIN_NAMES = 3


def sh(mo: pd.Series) -> float:
    return float(mo.mean() / mo.std() * np.sqrt(12)) \
        if len(mo) > 3 and mo.std() > 0 else float("nan")


def book_line(rets: pd.DataFrame, label: str):
    """Expanding EW book from a bar-returns matrix (NaN = name not live)."""
    n_live = rets.notna().sum(axis=1)
    r = rets.mean(axis=1)[n_live >= MIN_NAMES]
    eq = (1 + r.fillna(0.0)).cumprod()
    mo = eq.resample("ME").last().pct_change().dropna()
    pre, post = mo[mo.index < SPLIT], mo[mo.index >= SPLIT]
    yl = "  ".join(f"{y} {sh(g):+.1f}" for y, g in mo.groupby(mo.index.year))
    mdd = float((eq / eq.cummax() - 1).min())
    print(f"  {label:12s} {r.index[0].date()}..{r.index[-1].date()}  "
          f"full {sh(mo):+5.2f}  pre {sh(pre):+5.2f}  post {sh(post):+5.2f}  "
          f"MDD {mdd*100:5.1f}%")
    print(f"    yearly: {yl}")
    return sh(mo), sh(pre), sh(post)


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print("TREND-BOOK LONG-HISTORY GATE  MAJORS8  (pre-2023-08 = pseudo-OOS)",
          flush=True)
    fng = fetch_fear_greed()

    eq_t, eq_p = {}, {}
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
        for tag, raw in [("t", triple_confirm_bidir(df, tp_mult=6.0)),
                         ("p", pullback_in_trend(df))]:
            sig = fng_persist(regime_mask(raw, regs), fa)
            sig["risk_mult"] = mult
            cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                                   max_leverage=5.0,
                                   eq_decay_tiers=DEFAULT_DECAY_TIERS,
                                   cooldown_bars=3, fee_rate=FEE_TAKER,
                                   fee_maker=FEE_MAKER,
                                   entry_style="maker_close", tp_as_limit=True)
            eq, tr = run_backtest_enhanced(dfe, sig[sig.index >= cut], cfg)
            (eq_t if tag == "t" else eq_p)[p] = apply_funding_real(eq, tr, fund)
        print(f"  {p:10s} {time.time()-t0:5.0f}s  data {df.index[0].date()}.."
              f"{df.index[-1].date()}  evaluable from {cut.date()}", flush=True)

    # bar-returns matrices on the union index (NaN outside a name's window)
    union = None
    for p in MAJORS8:
        union = eq_t[p].index if union is None else union.union(eq_t[p].index)
    union = union.sort_values()
    rt = pd.DataFrame({p: eq_t[p].reindex(union).pct_change()
                       for p in MAJORS8})
    rp = pd.DataFrame({p: eq_p[p].reindex(union).pct_change()
                       for p in MAJORS8})

    live = rt.notna().sum(axis=1)
    print("\n  names live (median by year): " + "  ".join(
        f"{y} {int(g.median())}" for y, g in live.groupby(live.index.year)))

    print("\n" + "=" * 96)
    print("EXPANDING BOOKS (full available history; gate applies to BLEND)")
    print("=" * 96)
    book_line(rt, "TRIPLE_CONF")
    book_line(rp, "PULL_CONF")
    full, pre, post = book_line((rt + rp) / 2, "BLEND50_CONF")

    # continuity: common-window book
    common = None
    for p in MAJORS8:
        idx = eq_t[p].dropna().index
        common = idx if common is None else common.intersection(idx)
    common = common.sort_values()
    print("\n" + "=" * 96)
    print(f"COMMON-WINDOW BOOK (all 8 names; from {common[0].date()} — "
          f"continuity with prior tables)")
    print("=" * 96)
    book_line(((rt + rp) / 2).loc[common], "BLEND (com)")

    ok = (full >= 0.5) and (pre >= 0.0)
    print(f"\nGATE VERDICT: {'PASS' if ok else 'FAIL'}  "
          f"(full {full:+.2f} vs >=0.5, pre-2023-08 {pre:+.2f} vs >=0.0)")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
