"""FILL CALIBRATION — did our maker entries actually fill? Measured, not assumed.

The engine fills a resting limit whenever the bar trades through it by ANY
amount. research/maker_fill_depth.py (2026-08-08) could only bound the
exposure with a GUESSED bp ladder, because nothing better existed. Bybit's
free archives (verified 2026-08-09) now make the real question answerable:

    a passive order at price P fills once the volume trading through P
    exceeds the QUEUE AHEAD of it at P.

Both terms are now observable:
    Q = resting size at the limit price when the bar opens   (ob200 book archive)
    V = volume that traded at/through the limit in that bar  (tick trades archive)
    S = our own order size                                   (the engine)

PRE-REGISTERED, fixed before the first download:

  SAMPLE. 359 engine entries fall in the book-archive overlap
  (2025-09-01 .. present) across 356 unique symbol-days. A census is ~21 GB
  and hours of JSONL replay, so: N=64 entries, drawn as EIGHT PER PAIR with
  numpy default_rng(SEED=20260809). Stratifying by pair prevents a single
  liquid name dominating; the seed is fixed so the draw is reproducible and
  cannot be re-rolled toward a nicer answer.

  CRITERIA (evaluated per sampled entry, no thresholds tuned):
    C0  engine        filled (this is definitionally true for every sampled
                      entry — the engine only produces entries it filled)
    C1  lenient       V >= Q            queue cleared, ignoring our own size
    C2  strict        V >= Q + S        queue cleared AND our size absorbed
    C3  paranoid      V >= 2*(Q + S)    twice the required volume

  PRIMARY OUTPUT. The share of engine-assumed fills that FAIL C1/C2/C3, and
  the distribution of the coverage ratio V/(Q+S). A high pass rate VALIDATES
  the engine's optimistic assumption; a low one means a chunk of the
  backtested edge rests on fills that never happened.

  This CANNOT promote anything and is not a strategy change. It is evidence
  about an assumption. Any resulting fill model owes the full gate battery.

CAVEAT, stated in advance: the book archive starts 2025-09, i.e. the last ~30%
of the 2023-08..2026-08 backtest window. Nothing measured here is evidence
about 2023-2024, and applying it there would be extrapolation.

Run:  PYTHONIOENCODING=utf-8 ./.venv/bin/python research/fill_calibration.py
"""
from __future__ import annotations

import sys as _sys, os as _os, time
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

from pathlib import Path

import numpy as np
import pandas as pd

from core.bybit_archive import book_at, load_trades, volume_through

SEED = 20260809
PER_PAIR = int(_os.environ.get("PER_PAIR", 8))
ENTRIES = _os.environ.get("ENTRIES", "/tmp/entries_overlap.parquet")
CACHE = Path(_os.environ.get("ARCH_CACHE", "/tmp/arch_cache"))
BAR = pd.Timedelta(hours=4)


def main() -> None:
    e = pd.read_parquet(ENTRIES)
    e["sym"] = e["pair"].str.replace("-", "", regex=False)
    rng = np.random.default_rng(SEED)
    picks = []
    for pair, g in e.groupby("pair"):
        g = g.sort_values("entry_time").reset_index(drop=True)
        idx = rng.choice(len(g), size=min(PER_PAIR, len(g)), replace=False)
        picks.append(g.iloc[sorted(idx)])
    smp = pd.concat(picks).reset_index(drop=True)
    print(f"FILL CALIBRATION — {len(smp)} entries "
          f"({PER_PAIR}/pair, seed {SEED}), cache {CACHE}", flush=True)

    rows = []
    for i, r in smp.iterrows():
        sym, day = r["sym"], str(r["entry_time"].date())
        # The limit rests at the SIGNAL bar's close, i.e. the entry price the
        # engine used, from the START of the entry bar.
        t0 = pd.Timestamp(r["entry_time"]).tz_convert("UTC")
        lim, side, qty = float(r["entry_px"]), int(r["side"]), float(r["qty"])
        t = time.time()
        try:
            bk = book_at(sym, day, [t0], levels=200, cache=CACHE)
            tr = load_trades(sym, day, cache=CACHE)
        except Exception as ex:
            print(f"  [{i+1:2d}/{len(smp)}] {sym:9s} {day}  SKIP {type(ex).__name__}",
                  flush=True)
            continue
        if bk.empty:
            print(f"  [{i+1:2d}/{len(smp)}] {sym:9s} {day}  SKIP no book row", flush=True)
            continue
        b = bk.iloc[0]
        # Queue ahead of us at the limit: the resting size on OUR side at the
        # touch. A passive BUY joins the bid; a passive SELL joins the ask.
        Q = float(b["bid_sz"] if side == 1 else b["ask_sz"])
        V = volume_through(tr, t0, t0 + BAR, lim, side)
        rows.append(dict(sym=sym, day=day, side=side, limit=lim, S=qty, Q=Q, V=V,
                         cover=V / (Q + qty) if (Q + qty) > 0 else np.nan))
        print(f"  [{i+1:2d}/{len(smp)}] {sym:9s} {day} side={side:+d} "
              f"Q={Q:>12,.0f} V={V:>14,.0f} S={qty:>10,.2f} "
              f"cover={rows[-1]['cover']:>8.1f}x  {time.time()-t:4.0f}s", flush=True)

    d = pd.DataFrame(rows)
    if d.empty:
        print("\nno rows collected")
        return
    d.to_parquet("/tmp/fill_calibration.parquet")

    print("\n" + "=" * 92)
    print(f"RESULTS  n={len(d)} sampled engine entries")
    print("=" * 92)
    c1 = d.V >= d.Q
    c2 = d.V >= (d.Q + d.S)
    c3 = d.V >= 2 * (d.Q + d.S)
    for lab, c in (("C1 lenient   V >= Q", c1), ("C2 strict    V >= Q+S", c2),
                   ("C3 paranoid  V >= 2(Q+S)", c3)):
        print(f"  {lab:26s} pass {c.sum():3d}/{len(d)}  ({100*c.mean():5.1f}%)   "
              f"FAIL {100*(~c).mean():5.1f}%")
    q = d.cover.quantile([.01, .05, .10, .25, .50, .75, .95])
    print(f"\n  coverage ratio V/(Q+S) percentiles:")
    print("    " + "  ".join(f"p{int(k*100)}={v:,.1f}x" for k, v in q.items()))
    print(f"    min {d.cover.min():,.2f}x   median {d.cover.median():,.1f}x   "
          f"max {d.cover.max():,.0f}x")
    print(f"\n  our size vs queue ahead (S/Q): median {(d.S/d.Q).median():.4f} "
          f"({100*(d.S/d.Q).median():.2f}% of the queue)")
    worst = d.nsmallest(5, "cover")[["sym", "day", "Q", "V", "S", "cover"]]
    print(f"\n  thinnest 5 by coverage:")
    print(worst.to_string(index=False, float_format=lambda v: f"{v:,.2f}"))

    print("\n" + "=" * 92)
    print("READING")
    print("=" * 92)
    fail2 = 100 * (~c2).mean()
    if fail2 == 0:
        print("  EVERY sampled entry clears the strict criterion. The engine's")
        print("  'any penetration fills' assumption is VALIDATED on this sample —")
        print("  the fills it books are fills that had the volume to happen.")
    else:
        print(f"  {fail2:.1f}% of engine fills FAIL the strict criterion — that share")
        print("  of the backtested edge rests on fills that may not have occurred.")
    print("  Scope: book archive begins 2025-09, so this says NOTHING about")
    print("  2023-2024. Applying it there would be extrapolation.")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
