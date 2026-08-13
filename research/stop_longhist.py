"""LONG-HISTORY GATE for sl_mult 3.0 — the gate that killed the sleeves.

sl 3.0 has now cleared G1 (IS 1.34->1.47, OOS 0.99->1.03), G2 (thirds all
positive) and G4 (dSh +0.15/+0.14/+0.14/+0.06 across MAJORS8/12/16/EW23).
Every one of those was measured from 2023-08-17. This is the test it has not
faced: the 2022-inclusive pseudo-OOS that rejected TSMOM-90 (-0.70) and carry
(-0.34), and that the deployed book itself passed only NARROWLY.

WHY THE MARGIN MATTERS HERE. trend_longhist.py (2026-07-06) measured the
DEPLOYED book at full +1.20, pre-2023-08 **+0.18**, post +1.68. The pre-2023
margin is 0.18, not miles of room. A change that improves post-2023 by +0.14
while costing anything meaningful pre-2023 would push the book through the
floor of the same gate we killed two sleeves with — and would be revealed as a
post-2023 regime artifact rather than a mechanism.

METHOD — identical to trend_longhist.py, so the control reproduces the
2026-07-06 numbers and any drift is visible. MAJORS8, full available Bybit
history (DAYS=2000), per-pair evaluable window = data start + 365d regime/VT
warmup, EXPANDING equal-weight book (>=3 names live), NOTHING clipped to the
common window. The only difference is a second cell.

PRE-REGISTERED CELLS — exactly two, no re-tuning, no new parameters:
    sl 1.8 (deployed CONTROL, = trend_longhist's configuration)
    sl 3.0 (the candidate)
Both legs at tp_mult 6.0, which is what the control already used.

PRE-REGISTERED CRITERIA, both declared before running:

  (1) ABSOLUTE — the standing gate, unchanged:
      PASS iff full-history Sh(mo) >= 0.5 AND pre-2023-08 Sh(mo) >= 0.0.

  (2) RELATIVE — required because this is a PARAMETER CHANGE, not a new
      sleeve. The absolute bar alone is not sufficient: sl 3.0 could clear it
      purely because the deployed book does. Declared in advance:
      **a NEGATIVE dSh on the pre-2023 sub-period, while post-2023 is +0.14,
      is the signature of a post-2023 regime artifact.** In that case the
      result is INCONCLUSIVE AT BEST regardless of an absolute pass, and the
      deployed 1.8 stands.

Per-leg decomposition is printed because the 2026-07-06 finding was in the
decomposition, not the headline: TRIPLE pre +0.57 carried PULL pre -2.57. A
stop-width change need not move both legs the same way, and if sl 3.0 helps
only the leg that was already healthy that is worth seeing.

This gate cannot ADOPT anything — G5 exec parity is still unrun. It can only
reject, or clear the way to G5.

Run:  PYTHONIOENCODING=utf-8 ./.venv/bin/python research/stop_longhist.py
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
WARMUP_D, BPD, MIN_NAMES = 365, 6, 3
CELLS = [("sl 1.8 CONTROL", 1.8), ("sl 3.0", 3.0)]


def sh(mo: pd.Series) -> float:
    return float(mo.mean() / mo.std() * np.sqrt(12)) \
        if len(mo) > 3 and mo.std() > 0 else float("nan")


def book_stats(rets: pd.DataFrame) -> dict:
    """Expanding EW book from a bar-returns matrix (NaN = name not live)."""
    n_live = rets.notna().sum(axis=1)
    r = rets.mean(axis=1)[n_live >= MIN_NAMES]
    eq = (1 + r.fillna(0.0)).cumprod()
    mo = eq.resample("ME").last().pct_change().dropna()
    return dict(full=sh(mo), pre=sh(mo[mo.index < SPLIT]),
                post=sh(mo[mo.index >= SPLIT]),
                mdd=float((eq / eq.cummax() - 1).min()) * 100,
                yearly={y: sh(g) for y, g in mo.groupby(mo.index.year)},
                start=r.index[0].date(), end=r.index[-1].date())


def line(label: str, s: dict) -> None:
    print(f"  {label:16s} {s['start']}..{s['end']}  full {s['full']:+5.2f}  "
          f"pre {s['pre']:+5.2f}  post {s['post']:+5.2f}  MDD {s['mdd']:5.1f}%")
    print("    yearly: " + "  ".join(f"{y} {v:+.1f}" for y, v in s['yearly'].items()))


def main() -> None:
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print(f"LONG-HISTORY GATE for sl 3.0 — MAJORS8, {len(CELLS)} cells, "
          f"pre-{SPLIT.date()} = pseudo-OOS", flush=True)
    fng = fetch_fear_greed()
    eq = {n: {"t": {}, "p": {}} for n, _ in CELLS}

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
        for name, slm in CELLS:
            # sl_mult sets the signal's sl column — signals rebuild per cell.
            for tag, raw in [("t", triple_confirm_bidir(df, tp_mult=6.0, sl_mult=slm)),
                             ("p", pullback_in_trend(df, tp_mult=6.0, sl_mult=slm))]:
                sig = fng_persist(regime_mask(raw, regs), fa)
                sig["risk_mult"] = mult
                cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                                       max_leverage=5.0,
                                       eq_decay_tiers=DEFAULT_DECAY_TIERS,
                                       cooldown_bars=3, fee_rate=FEE_TAKER,
                                       fee_maker=FEE_MAKER,
                                       entry_style="maker_close", tp_as_limit=True)
                e, tr = run_backtest_enhanced(dfe, sig[sig.index >= cut], cfg)
                eq[name][tag][p] = apply_funding_real(e, tr, fund)
        print(f"  {p:10s} {time.time()-t0:5.0f}s  data {df.index[0].date()}.."
              f"{df.index[-1].date()}  evaluable from {cut.date()}", flush=True)

    union = None
    for p in MAJORS8:
        u = eq[CELLS[0][0]]["t"][p].index
        union = u if union is None else union.union(u)
    union = union.sort_values()

    res = {}
    for name, _ in CELLS:
        rt = pd.DataFrame({p: eq[name]["t"][p].reindex(union).pct_change() for p in MAJORS8})
        rp = pd.DataFrame({p: eq[name]["p"][p].reindex(union).pct_change() for p in MAJORS8})
        res[name] = dict(t=book_stats(rt), p=book_stats(rp), b=book_stats((rt + rp) / 2))

    live = pd.DataFrame({p: eq[CELLS[0][0]]["t"][p].reindex(union).pct_change()
                         for p in MAJORS8}).notna().sum(axis=1)
    print("\n  names live (median by year): " + "  ".join(
        f"{y} {int(g.median())}" for y, g in live.groupby(live.index.year)))

    print("\n" + "=" * 100)
    print("EXPANDING BOOKS — full available history (gate applies to the BLEND)")
    print("=" * 100)
    for name, _ in CELLS:
        print(f"\n  --- {name} ---")
        line("TRIPLE_CONF", res[name]["t"])
        line("PULL_CONF", res[name]["p"])
        line("BLEND50_CONF", res[name]["b"])

    c, k = res[CELLS[0][0]]["b"], res[CELLS[1][0]]["b"]
    print("\n" + "=" * 100)
    print("VERDICT vs the two PRE-REGISTERED criteria")
    print("=" * 100)
    a_full, a_pre = k["full"] >= 0.5, k["pre"] >= 0.0
    d_full, d_pre, d_post = k["full"] - c["full"], k["pre"] - c["pre"], k["post"] - c["post"]
    print(f"  (1) ABSOLUTE  full {k['full']:+.2f} vs >=0.50  -> {'OK' if a_full else 'FAIL'}")
    print(f"                pre  {k['pre']:+.2f} vs >= 0.00  -> {'OK' if a_pre else 'FAIL'}")
    print(f"  (2) RELATIVE  dSh   full {d_full:+.2f}   pre {d_pre:+.2f}   post {d_post:+.2f}")
    print(f"                control was full {c['full']:+.2f} pre {c['pre']:+.2f} post {c['post']:+.2f}")
    print()
    if not (a_full and a_pre):
        print("  LONG-HISTORY GATE: FAIL. sl 3.0 does not survive the 2022-inclusive")
        print("  window — the same bar that rejected TSMOM-90 and carry. The post-2023")
        print("  +0.14 was a regime artifact. DEPLOYED 1.8 STANDS.")
    elif d_pre < 0:
        print("  LONG-HISTORY GATE: INCONCLUSIVE (pre-registered outcome). sl 3.0 clears")
        print("  the absolute bar, but it does so on the deployed book's margin while")
        print("  DEGRADING the pseudo-OOS period. Declared in advance as the signature")
        print("  of a post-2023 regime artifact. NOT promotable. DEPLOYED 1.8 STANDS.")
    else:
        print("  LONG-HISTORY GATE: PASS. sl 3.0 clears the absolute bar AND does not")
        print("  degrade the pseudo-OOS period. G5 exec parity is the last gate before")
        print("  any adoption discussion — this does NOT authorise a config change.")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
