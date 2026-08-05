"""DATA HEALTH — what did the boxes actually record, and where are the holes?

A report, not an experiment: no cells, nothing pre-registered, no strategy
claim. Run it after every `deploy/pull-data.sh` and before any study that
consumes tick data, so a gap is discovered here rather than silently baked
into a result.

Prints:
  1. tick coverage per day — rows and % of expected for the metronomic
     streams (book_1s ~86400/sym/day, ticker_1m ~1440/sym/day). trades_1s and
     liq are event-driven, so their counts are reported without a target.
  2. missing days in the tick range, and per-symbol coverage on the last day
  3. live legs: heartbeat age, equity, open position, trade count
  4. the live blotter tail + realised PnL by exit reason

Gaps are FACTS, not defects to be papered over. Never backfill or synthesise
a tick to close one — a fake row poisons every study built on it. Record the
gap and move on.

Run:  PYTHONIOENCODING=utf-8 ./.venv/bin/python research/data_health.py
"""
from __future__ import annotations

import sys as _sys, os as _os
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

import pandas as pd

from core.datastore import (DATA, TICK_KINDS, legs, live_blotter, load_events,
                            load_states, load_ticks, tick_days, ticks_root)

# Per-symbol rows/day for the clock-driven streams. Event-driven ones (trades,
# liquidations) have no meaningful target — volume is the market's business.
EXPECTED_PER_SYM = {"book_1s": 86400, "ticker_1m": 1440}


def _fmt(n) -> str:
    return "-" if n is None else f"{n:,}"


def ticks_report() -> None:
    days = tick_days()
    print("=" * 88)
    print(f"1) TICK COVERAGE   {ticks_root()}")
    print("=" * 88)
    if not days:
        print("  no tick data yet — has the collector box been pulled?\n")
        return
    print(f"  {len(days)} day-dirs, {days[0]} .. {days[-1]}\n")
    print(f"  {'day':12s}" + "".join(f"{k:>14s}" for k in TICK_KINDS))
    nsym = {}
    for day in days:
        line = f"  {day:12s}"
        for kind in TICK_KINDS:
            df = load_ticks(kind, start=day, end=day, tz_index=False)
            n = len(df)
            nsym[kind] = df["sym"].nunique() if n else nsym.get(kind, 0)
            if kind in EXPECTED_PER_SYM and n and nsym[kind]:
                pct = 100 * n / (EXPECTED_PER_SYM[kind] * nsym[kind])
                line += f"{n:>9,}{pct:>4.0f}%"
            else:
                line += f"{_fmt(n):>14s}"
        print(line)
    print(f"\n  symbols seen: " +
          ", ".join(f"{k}={nsym.get(k, 0)}" for k in TICK_KINDS))

    # calendar gaps
    idx = pd.date_range(days[0], days[-1], freq="D").strftime("%Y-%m-%d")
    missing = [d for d in idx if d not in set(days)]
    if missing:
        print(f"  ⚠ MISSING DAY DIRS ({len(missing)}): {', '.join(missing[:10])}"
              + (" ..." if len(missing) > 10 else ""))
    else:
        print("  no missing day directories")

    # per-symbol coverage on the most recent day
    last = days[-1]
    tk = load_ticks("ticker_1m", start=last, end=last, tz_index=False)
    if len(tk):
        cov = (tk.groupby("sym").size() / EXPECTED_PER_SYM["ticker_1m"] * 100)
        weak = cov[cov < 80].sort_values()
        print(f"\n  ticker_1m per-symbol coverage on {last} "
              f"(median {cov.median():.0f}%):")
        if len(weak):
            print("    under 80%: " +
                  ", ".join(f"{s} {v:.0f}%" for s, v in weak.items()))
        else:
            print("    all symbols >=80%")
    print()


def live_report() -> None:
    print("=" * 88)
    print(f"2) LIVE LEGS   {DATA / 'live'}")
    print("=" * 88)
    st = load_states()
    if st.empty:
        print("  no live legs yet — has the trading box been pulled?\n")
        return
    print(f"  {'leg':10s}{'hb age':>9s}{'equity':>10s}{'trades':>8s}"
          f"{'wins':>6s}{'pos':>6s}{'entry':>12s}")
    for r in st.to_dict("records"):
        hb = r.get("heartbeat_age_s")
        hb_s = "-" if hb is None else (f"{hb}s" if hb < 300 else f"{hb}s!")
        # pos_side is NaN for a flat leg, and `not NaN` is False in Python —
        # testing truthiness here would label every flat leg SHORT.
        pos = r.get("pos_side")
        pos_s = "-" if pos is None or pd.isna(pos) else ("LONG" if pos == 1 else "SHORT")
        ent = r.get("pos_entry")
        ent_s = "-" if ent is None or pd.isna(ent) else f"{ent:.4f}"
        eq = r.get("equity")
        eq_s = "-" if eq is None or pd.isna(eq) else f"{eq:.2f}"
        print(f"  {r['leg']:10s}{hb_s:>9s}{eq_s:>10s}"
              f"{_fmt(r.get('realised_trades')):>8s}"
              f"{_fmt(r.get('realised_wins')):>6s}{pos_s:>6s}{ent_s:>12s}")
    stale = st[st["heartbeat_age_s"].fillna(1e9) > 300] if "heartbeat_age_s" in st else st.iloc[:0]
    if len(stale):
        print(f"\n  ⚠ {len(stale)} leg(s) with heartbeat >300s — either the pull "
              f"is stale or those containers are down: "
              f"{', '.join(stale['leg'])}")

    errs = load_events(event="error")
    if len(errs):
        print(f"\n  ⚠ {len(errs)} error events; last 3:")
        for r in errs.tail(3).to_dict("records"):
            print(f"    {r.get('ts')} {r.get('leg')}: "
                  f"{str(r.get('message'))[:80]}")
    print()


def blotter_report() -> None:
    print("=" * 88)
    print("3) LIVE BLOTTER (core.backtest.Trade shape — the L2 reconcile input)")
    print("=" * 88)
    bl = live_blotter()
    if bl.empty:
        print("  no completed live trades yet\n")
        return
    print(f"  {len(bl)} trades, {bl['exit_time'].min()} .. {bl['exit_time'].max()}")
    print(f"  realised PnL {bl['pnl'].sum():+.2f}   fees {bl['fees'].sum():.2f}\n")
    g = bl.groupby("reason").agg(n=("pnl", "size"), pnl=("pnl", "sum"),
                                 mean=("pnl", "mean"))
    print(f"  {'reason':14s}{'n':>6s}{'pnl':>10s}{'mean':>9s}")
    for reason, r in g.iterrows():
        print(f"  {str(reason):14s}{int(r['n']):>6d}{r['pnl']:>10.2f}{r['mean']:>9.3f}")
    print(f"\n  last 5:")
    cols = ["leg", "side", "exit_time", "entry_px", "exit_px", "pnl", "reason"]
    print(bl[cols].tail(5).to_string(index=False).replace("\n", "\n  "))
    print()


def main() -> None:
    print(f"DATA HEALTH   root={DATA}\n")
    if not DATA.is_dir():
        raise SystemExit(f"no data dir at {DATA} — run deploy/pull-data.sh first")
    ticks_report()
    live_report()
    blotter_report()
    print("Gaps above are facts. Record them; never synthesise a tick to close one.")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
