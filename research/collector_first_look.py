"""COLLECTOR FIRST LOOK — a REPORT, not an experiment.

No cells, nothing pre-registered, no strategy claim, and nothing here can
become one. At ~2.5 days the sample supports exactly two kinds of statement:

  (a) INFRASTRUCTURE facts — data rate, gap rate, disk projection, and how
      long until the tier-2 studies have enough events to be worth running.
  (b) STRUCTURAL microstructure that stabilises fast — chiefly the BID/ASK
      SPREAD, which is a property of each market's tick size and liquidity,
      not of a market regime. A median spread is well determined by a few
      hundred thousand snapshots; it does not need 60 days.

(b) matters because ROADMAP Phase 2 has an OPEN item: "Slippage/spread
measurement ... replace the flat 2 bps assumption with measured per-pair
values." The cost model currently assumes 2bp slip per taker side, chosen
a priori. book_1s now lets us check that number against reality.

What this CANNOT say, and will not be asked to say until A3 (~60 days):
anything about liquidation cascades, funding-settlement microstructure, book
imbalance as a signal, or any edge whatsoever. Two days spans one or two
regime states and zero stress events. Treating a 2-day median as an estimate
of anything conditional is precisely the error the same-bar artifact taught.

Run:  PYTHONIOENCODING=utf-8 ./.venv/bin/python research/collector_first_look.py
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
# The cost model's a-priori assumptions (research/cost_engine.py, ROADMAP §1).
SLIP_BPS_ASSUMED = 2.0
FEE_TAKER_BPS, FEE_MAKER_BPS = 6.0, 2.0


def inventory() -> None:
    days = tick_days()
    print("=" * 92)
    print(f"1) INVENTORY — {len(days)} day-dirs, {days[0]} .. {days[-1]}")
    print("=" * 92)
    tot = 0
    for kind in ("trades_1s", "book_1s", "ticker_1m", "liq"):
        df = load_ticks(kind, tz_index=False)
        n = len(df)
        tot += n
        span_h = 0.0
        if n:
            tcol = "ts_ms" if "ts_ms" in df.columns else "ts"
            t = pd.to_numeric(df[tcol], errors="coerce").dropna()
            if tcol == "ts_ms":
                t = t / 1000.0
            span_h = (t.max() - t.min()) / 3600.0
        print(f"  {kind:11s} {n:>10,} rows  {df['sym'].nunique() if n else 0:>3d} syms  "
              f"{span_h:6.1f}h observed  {n/max(span_h,1e-9)/3600:8.1f} rows/s")
    print(f"  {'TOTAL':11s} {tot:>10,} rows")


def spreads() -> None:
    """The one thing 2.5 days genuinely determines."""
    bk = load_ticks("book_1s", tz_index=False)
    print("\n" + "=" * 92)
    print("2) TOP-OF-BOOK SPREAD  (structural — this is what the sample supports)")
    print("=" * 92)
    if bk.empty:
        print("  no book data\n")
        return
    for c in ("bid", "ask"):
        bk[c] = pd.to_numeric(bk[c], errors="coerce")
    bk = bk.dropna(subset=["bid", "ask"])
    bk = bk[(bk.bid > 0) & (bk.ask > bk.bid)]
    mid = (bk.bid + bk.ask) / 2
    bk = bk.assign(spread_bps=(bk.ask - bk.bid) / mid * 1e4)

    print(f"  {'sym':7s}{'n':>10s}{'med':>8s}{'p25':>8s}{'p75':>8s}{'p95':>8s}"
          f"{'p99':>8s}   half-spread vs {SLIP_BPS_ASSUMED:.0f}bp slip assumption")
    rows = []
    for sym, g in bk.groupby("sym"):
        q = g.spread_bps.quantile([.25, .5, .75, .95, .99])
        half = q[.5] / 2
        verdict = ("conservative" if half < SLIP_BPS_ASSUMED
                   else "OPTIMISTIC" if half > SLIP_BPS_ASSUMED * 1.25 else "≈ right")
        rows.append((sym, len(g), q[.5], half, verdict))
        mark = "*" if sym in MAJORS8 else " "
        print(f" {mark}{sym:6s}{len(g):>10,}{q[.5]:>8.2f}{q[.25]:>8.2f}{q[.75]:>8.2f}"
              f"{q[.95]:>8.2f}{q[.99]:>8.2f}   half={half:5.2f}bp  {verdict}")
    print("  * = in the traded MAJORS8 book")

    maj = [r for r in rows if r[0] in MAJORS8]
    if maj:
        hs = np.median([r[3] for r in maj])
        print(f"\n  MAJORS8 median half-spread: {hs:.2f} bp vs the model's "
              f"{SLIP_BPS_ASSUMED:.0f} bp slip assumption")
        rt_taker = 2 * (FEE_TAKER_BPS + hs)
        rt_model = 2 * (FEE_TAKER_BPS + SLIP_BPS_ASSUMED)
        print(f"  implied taker round trip: {rt_taker:.1f} bp measured vs "
              f"{rt_model:.1f} bp modelled")
        print(f"  maker round trip (the deployed config): {2*FEE_MAKER_BPS:.1f} bp "
              f"+ adverse selection, which spread alone cannot measure")


def liquidations() -> None:
    """Event-rate only — how long until tier-2 has enough to study."""
    lq = load_ticks("liq", tz_index=False)
    print("\n" + "=" * 92)
    print("3) LIQUIDATION PRINTS — event rate, i.e. when is tier-2 worth running")
    print("=" * 92)
    if lq.empty:
        print("  none recorded yet\n")
        return
    t = pd.to_numeric(lq["ts_ms"], errors="coerce").dropna() / 1000.0
    span_d = max((t.max() - t.min()) / 86400.0, 1e-9)
    per_day = len(lq) / span_d
    print(f"  {len(lq):,} prints over {span_d:.2f} days = {per_day:.0f}/day "
          f"across {lq['sym'].nunique()} symbols")
    top = lq.groupby("sym").size().sort_values(ascending=False).head(6)
    print("  busiest: " + ", ".join(f"{s} {n}" for s, n in top.items()))
    print(f"\n  projection: ~{per_day*30:,.0f} by the A2 mark (30d), "
          f"~{per_day*60:,.0f} by A3 (60d)")
    print("  A cascade study needs CLUSTERED prints, not a raw count — and how")
    print("  many clusters exist depends entirely on whether a stress event")
    print("  happens to fall in the window. That is not something the current")
    print("  sample can forecast, and no amount of arithmetic here changes it.")


def what_this_cannot_say() -> None:
    days = tick_days()
    print("\n" + "=" * 92)
    print("4) WHAT THIS SAMPLE CANNOT SUPPORT")
    print("=" * 92)
    print(f"  Elapsed: {len(days)} calendar days. ROADMAP A2 wants 30, A3 wants 60.")
    print("  Off the table until then, without exception:")
    for s in ("liquidation-cascade fades (needs clustered stress events)",
              "funding-settlement microstructure (needs many 8h settlements)",
              "book-imbalance as a signal (needs regime variety, not one regime)",
              "ANY edge estimate — 2 days is one regime state and zero stress"):
        print(f"    - {s}")
    print("\n  The spread numbers above ARE usable now because a median spread is")
    print("  a structural property of tick size and liquidity. Its TAIL is not:")
    print("  stress-widening is exactly what this window has none of, so p95/p99")
    print("  here are calm-market numbers and must not be read as worst case.")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    inventory()
    spreads()
    liquidations()
    what_this_cannot_say()
