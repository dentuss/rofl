"""FILL QUALITY — join live fills to the tick record and measure execution.

A REPORT, not an experiment, and deliberately the ONLY direction tick data is
allowed to flow right now.

WHAT THIS IS FOR. ROADMAP Phase 2 carries an open item: "Slippage/spread
measurement from paper+live fill logs -> replace the flat 2 bps assumption
with measured per-pair values." The collector now gives book_1s and depth_1s
at 1-second resolution, and the live blotter gives real fills. Joining them
turns the 2 bp slip assumption from a guess into a measurement, and it does so
without touching a single live decision.

WHAT THIS IS NOT FOR, and why. Tick data must not reach the trading path:

  * as a SIGNAL — blocked by ROADMAP A3 (tier-2 unlocks at ~60 days) and, more
    fundamentally, mismatched: the book decides on a 4h clock ~40x/leg/year
    while tick information has a horizon of seconds.
  * as an EXECUTION input — the engine prices `maker_close` (limit at the
    signal bar's close, strict penetration, a miss is missed) and G5 exec
    parity is what makes the edge estimate meaningful. Smarter live placement
    would make live stop matching the engine, trading a measurable edge for an
    unmeasurable one. The order must be engine-first: model it, gate it, then
    wire it.

MEASURES, per completed fill:
  spread_bp     top-of-book spread at fill time
  top_usd       notional resting on the thinner side at fill time
  size_pct      our order as a % of that — >100% means we walked the book
  depth_5/25bp  cumulative notional within those bands (MAJORS8 only)
  slip_bp       ADVERSE slippage vs the SL/TP trigger the bot intended
                (positive = worse than intended; negative = better)

Run:  PYTHONIOENCODING=utf-8 ./.venv/bin/python research/fill_quality.py
"""
from __future__ import annotations

import sys as _sys, os as _os
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

import numpy as np
import pandas as pd

from core.datastore import live_blotter, load_ticks

WINDOW_S = 30          # tick rows to consider either side of the fill
SLIP_BPS_ASSUMED = 2.0  # what the cost model charges per taker side


def _near(df: pd.DataFrame, ts, secs: int = WINDOW_S) -> pd.DataFrame:
    if df.empty:
        return df
    return df[(df.index >= ts - pd.Timedelta(seconds=secs)) &
              (df.index <= ts + pd.Timedelta(seconds=secs))]


def main() -> None:
    bl = live_blotter()
    print("=" * 100)
    print("FILL QUALITY — live fills vs the tick record")
    print("=" * 100)
    if bl.empty:
        print("  no completed fills yet\n")
        return

    day0 = str(bl.exit_time.min().date())
    book = load_ticks("book_1s", start=day0)
    depth = load_ticks("depth_1s", start=day0)
    for c in ("bid", "bid_sz", "ask", "ask_sz"):
        if c in book.columns:
            book[c] = pd.to_numeric(book[c], errors="coerce")

    rows = []
    for _, r in bl.iterrows():
        sym = str(r["leg"]).split("-")[0].upper()
        ts, side = r["exit_time"], r["side"]
        bk = _near(book[book.sym == sym], ts) if not book.empty else book
        rec: dict = {"leg": r["leg"], "reason": r["reason"],
                     "exit_px": r["exit_px"], "notional": r["notional"]}
        if len(bk):
            mid = (bk.bid + bk.ask) / 2
            rec["spread_bp"] = float(((bk.ask - bk.bid) / mid * 1e4).median())
            top = np.minimum(bk.bid_sz * bk.bid, bk.ask_sz * bk.ask)
            rec["top_usd"] = float(top.median())
            rec["size_pct"] = 100 * float(r["notional"]) / rec["top_usd"] \
                if rec["top_usd"] else float("nan")
        if not depth.empty:
            dp = _near(depth[depth.sym == sym], ts)
            if len(dp):
                # the side we must HIT to close: a long exits into bids
                pfx = "bid" if side == 1 else "ask"
                for b in ("5", "25"):
                    col = f"{pfx}_{b}bp"
                    if col in dp.columns:
                        rec[f"depth_{b}bp"] = float(
                            pd.to_numeric(dp[col], errors="coerce").median())
        # Adverse slippage against the trigger the bot intended to exit at.
        trig = r["sl"] if str(r["reason"]).startswith("sl") else r["tp"]
        if pd.notna(trig) and trig:
            # long exits by SELLING (lower = worse); short exits by BUYING
            adverse = (trig - r["exit_px"]) if side == 1 else (r["exit_px"] - trig)
            rec["trigger"] = float(trig)
            rec["slip_bp"] = float(adverse / trig * 1e4)
        rows.append(rec)

    d = pd.DataFrame(rows)
    print(f"\n  {len(d)} fill(s), {bl.exit_time.min()} .. {bl.exit_time.max()}\n")
    cols = [c for c in ("leg", "reason", "notional", "spread_bp", "top_usd",
                        "size_pct", "depth_5bp", "depth_25bp", "trigger",
                        "exit_px", "slip_bp") if c in d.columns]
    print(d[cols].to_string(index=False, float_format=lambda v: f"{v:,.4f}"))

    if "slip_bp" in d.columns and d.slip_bp.notna().any():
        s = d.slip_bp.dropna()
        print("\n" + "=" * 100)
        print("SLIPPAGE vs THE COST MODEL")
        print("=" * 100)
        print(f"  model charges {SLIP_BPS_ASSUMED:.1f} bp per taker side")
        print(f"  measured: median {s.median():+.2f} bp   mean {s.mean():+.2f} bp   "
              f"worst {s.max():+.2f} bp   n={len(s)}")
        verdict = ("CONSERVATIVE — the model charges slip that is not occurring"
                   if s.median() < SLIP_BPS_ASSUMED else
                   "OPTIMISTIC — real slippage exceeds the assumption")
        print(f"  -> {verdict}")
        print(f"\n  DO NOT act on this yet. n={len(s)} is far too few, every fill so far")
        print("  is a STOP (taker, market) on a mid-depth name, and none occurred in")
        print("  stressed conditions. Lowering a cost on this evidence would be exactly")
        print("  the flattering adjustment the ledger exists to prevent. Revisit with")
        print("  n>=30 spanning both TP (maker) and SL (taker) exits.")

    if "size_pct" in d.columns and d.size_pct.notna().any():
        big = d[d.size_pct > 100]
        print("\n  order size vs top-of-book: "
              f"median {d.size_pct.median():.1f}%, worst {d.size_pct.max():.1f}%")
        if len(big):
            print(f"  ⚠ {len(big)} fill(s) EXCEEDED top-of-book — these walked the "
                  f"book: {', '.join(big.leg)}")
        else:
            print("  no fill exceeded top-of-book depth")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
