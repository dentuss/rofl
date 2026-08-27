"""DOES THE COST MODEL HOLD DURING VOL EVENTS? — our own collector data.

The deployed book assumes a fixed round trip: 3.6bp maker entry + 10.0bp taker
exit, plus a 2bp slippage allowance. Every one of those numbers was calibrated
on CALM data (fill_calibration.py, 2026-08). The trend book, by construction,
trades hardest when volatility expands — so if execution cost blows out during
vol events, the edge is overstated exactly where the book does its business.

The collector caught a genuine event: liquidations went from ~1,000/day to
8,275 on 2026-08-21 while BTC ran 63.8k -> 78.3k. That is a natural experiment
we did not have when the cost model was built.

WHY book_1s AND NOT depth_1s. The depth_1s band buckets are degenerate for the
whole v1 era (2026-08-27 FINDINGS: limit=50 never reached the 1bp band, all
four buckets carry the identical top-50 total). This study therefore uses
book_1s — real top-of-book bid/ask/size, unaffected by that bug — and treats
depth_1s v1 only as a single aggregate liquidity series, never as a gradient.

PRE-REGISTERED, fixed before any spread number was computed:

  WINDOW SPLIT — mechanical, and keyed on a series INDEPENDENT of the metric.
  Stress = calendar days whose total liquidation count >= 3x the median daily
  liquidation count over the sample. Everything else is calm. The split is
  computed from liq.csv.gz, never from spreads, so it cannot be tuned to the
  answer. (This rule selects 2026-08-19..22 on the current sample.)

  METRICS, per symbol, median over each window:
    M1  relative spread   (ask - bid) / mid, in bp   <- the slippage floor
    M2  top-of-book notional  min(bid_sz*bid, ask_sz*ask)  <- impact proxy
    M3  share of SECONDS with spread > the 2bp slip allowance

  BAR — the cost model needs a regime term if EITHER:
    (a) median stress spread > 2x median calm spread on >= 5 of MAJORS8, OR
    (b) M3 during stress > 25% on >= 5 of MAJORS8.
  Anything less and the flat cost model survives the event, which is itself a
  publishable negative — the 4h/6-ATR design's defence against the cost floor
  is that 200-400bp moves dwarf a ~13bp round trip, and that defence is only
  as good as the round trip staying ~13bp when it matters.

UNIVERSE: MAJORS8 (where we execute) reported as the gate; QUAL23 reported
alongside for generalisation. Universe is structural, never performance-picked.

Run:  PYTHONIOENCODING=utf-8 ./.venv/bin/python research/stress_cost.py
"""
from __future__ import annotations

import sys as _sys, os as _os
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

import numpy as np
import pandas as pd

from core.datastore import load_ticks, tick_days

MAJORS8 = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX"]
SLIP_BP = 2.0          # the deployed slippage allowance
LIQ_MULT = 3.0         # stress = daily liq count >= LIQ_MULT x median


def main() -> None:
    days = sorted(tick_days())
    print(f"STRESS COST STUDY — {len(days)} days, {days[0]}..{days[-1]}", flush=True)

    # ---- 1. split the sample using LIQUIDATIONS ONLY -----------------------
    liq_n = {}
    for d in days:
        try:
            l = load_ticks("liq", start=d, end=d)
            liq_n[d] = len(l)
        except Exception:
            liq_n[d] = 0
    med = float(np.median([v for v in liq_n.values() if v > 0]))
    stress = sorted(d for d, v in liq_n.items() if v >= LIQ_MULT * med)
    calm = sorted(d for d in days if d not in stress)
    print(f"  median daily liquidations {med:,.0f} -> stress bar "
          f"{LIQ_MULT * med:,.0f}")
    print(f"  STRESS days ({len(stress)}): {', '.join(stress)}")
    print(f"  CALM   days ({len(calm)})")
    if not stress or not calm:
        raise SystemExit("split produced an empty window — nothing to compare")

    # ---- 2. per-day, per-symbol book metrics -------------------------------
    rows = []
    for d in days:
        try:
            b = load_ticks("book_1s", start=d, end=d)
        except Exception as e:
            print(f"  {d} book load failed: {type(e).__name__}", flush=True)
            continue
        if b.empty:
            continue
        b = b[(b["bid"] > 0) & (b["ask"] > 0) & (b["ask"] >= b["bid"])]
        mid = (b["bid"] + b["ask"]) / 2
        b = b.assign(spread_bp=(b["ask"] - b["bid"]) / mid * 1e4,
                     top_notional=np.minimum(b["bid_sz"] * b["bid"],
                                             b["ask_sz"] * b["ask"]))
        g = b.groupby("sym")
        for sym, gg in g:
            rows.append(dict(day=d, sym=sym,
                             spread_bp=float(gg["spread_bp"].median()),
                             top_notional=float(gg["top_notional"].median()),
                             wide_share=float((gg["spread_bp"] > SLIP_BP).mean()),
                             n=len(gg)))
        print(f"  {d}  {len(b):>9,} book rows  {b['sym'].nunique():>2} syms",
              flush=True)
    df = pd.DataFrame(rows)
    if df.empty:
        raise SystemExit("no book data loaded")
    df["regime"] = np.where(df["day"].isin(stress), "stress", "calm")

    # ---- 3. compare --------------------------------------------------------
    piv = df.pivot_table(index="sym", columns="regime",
                         values=["spread_bp", "top_notional", "wide_share"],
                         aggfunc="median")

    def show(names: list[str], label: str) -> dict:
        print("\n" + "=" * 92)
        print(f"{label}   median spread (bp) | median top-of-book ($) | "
              f"share of s with spread > {SLIP_BP}bp")
        print("=" * 92)
        print(f"  {'sym':6s}{'calm':>8s}{'stress':>8s}{'x':>7s}"
              f"{'calm$':>11s}{'stress$':>11s}{'x':>7s}"
              f"{'calm%':>8s}{'stress%':>8s}")
        out = {}
        for s in names:
            if s not in piv.index:
                continue
            try:
                sc, ss = piv.loc[s, ("spread_bp", "calm")], piv.loc[s, ("spread_bp", "stress")]
                tc, ts = piv.loc[s, ("top_notional", "calm")], piv.loc[s, ("top_notional", "stress")]
                wc, ws = piv.loc[s, ("wide_share", "calm")], piv.loc[s, ("wide_share", "stress")]
            except KeyError:
                continue
            out[s] = (sc, ss, ws)
            print(f"  {s:6s}{sc:>8.2f}{ss:>8.2f}{ss / sc:>7.2f}"
                  f"{tc:>11,.0f}{ts:>11,.0f}{ts / tc:>7.2f}"
                  f"{100 * wc:>8.1f}{100 * ws:>8.1f}")
        return out

    m8 = show(MAJORS8, "MAJORS8 — THE GATE")
    others = sorted(set(df["sym"]) - set(MAJORS8))
    show(others, "REST OF QUAL23 — generalisation, reported not gated")

    # ---- 4. verdict --------------------------------------------------------
    print("\n" + "=" * 92)
    print("VERDICT vs the pre-registered bar")
    print("=" * 92)
    a = [s for s, (sc, ss, _) in m8.items() if ss > 2 * sc]
    b = [s for s, (_, _, ws) in m8.items() if ws > 0.25]
    print(f"  (a) stress spread > 2x calm : {len(a)}/8  {a if a else ''}")
    print(f"  (b) >25% of stress seconds above the {SLIP_BP}bp allowance : "
          f"{len(b)}/8  {b if b else ''}")
    if len(a) >= 5 or len(b) >= 5:
        print("\n  BAR MET — execution cost is regime-dependent. The flat cost")
        print("  model understates the true round trip during exactly the")
        print("  windows the trend book trades most. A regime term is owed.")
    else:
        print("\n  BAR NOT MET — the flat cost model SURVIVES the event. The")
        print("  4h/6-ATR defence holds: a ~13bp round trip stayed ~13bp while")
        print("  liquidations went 8x. Recorded as a negative, and it is the")
        print("  result that lets the deployed cost numbers stand.")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
