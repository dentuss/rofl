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
    print(f"  {'day':12s}" + "".join(f"{k:>14s}" for k in TICK_KINDS) + f"{'window':>16s}")
    nsym = {}
    for day in days:
        line = f"  {day:12s}"
        span_min = None
        for kind in TICK_KINDS:
            df = load_ticks(kind, start=day, end=day, tz_index=False)
            n = len(df)
            nsym[kind] = df["sym"].nunique() if n else nsym.get(kind, 0)
            # Coverage is measured against the OBSERVED window, not the
            # calendar day. The collector started mid-day on its first day and
            # the current day is always partial, so dividing by 1440 made every
            # healthy day read 35-60% and would have cried wolf on every cron
            # run. What we actually want to know is: of the minutes we were
            # running, how many did we record?
            if kind in EXPECTED_PER_SYM and n and nsym[kind]:
                tcol = "ts_ms" if "ts_ms" in df.columns else "ts"
                t = pd.to_numeric(df[tcol], errors="coerce").dropna()
                if tcol == "ts_ms":
                    t = t / 1000.0
                span = max((t.max() - t.min()) / 60.0, 1.0)
                if kind == "ticker_1m":
                    span_min = span
                expected = span * nsym[kind] * (60 if kind == "book_1s" else 1)
                line += f"{n:>9,}{100 * n / expected:>4.0f}%"
            else:
                line += f"{_fmt(n):>14s}"
        line += f"{(f'{span_min/60:.1f}h' if span_min else '-'):>16s}"
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
        t = pd.to_numeric(tk["ts"], errors="coerce").dropna()
        span = max((t.max() - t.min()) / 60.0, 1.0)   # observed window, see above
        cov = (tk.groupby("sym").size() / span * 100)
        weak = cov[cov < 80].sort_values()
        print(f"\n  ticker_1m per-symbol coverage on {last} over the "
              f"{span/60:.1f}h observed window (median {cov.median():.0f}%):")
        if len(weak):
            print("    under 80%: " +
                  ", ".join(f"{s} {v:.0f}%" for s, v in weak.items()))
        else:
            print("    all symbols >=80%")
    print()


def sessions_report() -> None:
    """The collector's own heartbeat. A gap here is a RECORDED gap — which is
    the point: an unrecorded one is indistinguishable from a quiet market."""
    from core.datastore import load_sessions
    ss = load_sessions()
    print("=" * 88)
    print("1b) COLLECTOR SESSIONS (session_1m — restarts and disk pressure)")
    print("=" * 88)
    if ss.empty:
        print("  no session log yet (predates the 2026-08-07 collector upgrade)\n")
        return
    gaps = ss.index.to_series().diff().dt.total_seconds().div(60)
    big = gaps[gaps > 3]
    print(f"  {len(ss):,} minute-rows, {ss.index[0]} .. {ss.index[-1]}")
    print(f"  symbols {int(ss.n_symbols.iloc[-1])} ({int(ss.n_depth_symbols.iloc[-1])} "
          f"with depth)   free disk {float(ss.free_mb.iloc[-1]):,.0f} MB")
    if len(big):
        print(f"  ⚠ {len(big)} gap(s) > 3 min — each is a real hole, not noise:")
        for ts, m in big.tail(5).items():
            print(f"    {ts}  {m:.0f} min")
    else:
        print("  no gaps > 3 min")
    lo = float(ss.free_mb.min())
    if lo < 5000:
        print(f"  ⚠ lowest free disk seen: {lo:,.0f} MB")
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


# L1 baseline: 16 legs x LEG4H_LIVE_EQUITY. The book halt line is a HUMAN
# control (ROADMAP IF->THEN) — nothing in bot.py enforces it, and per-leg
# decay does not fire until -20% of a leg, ~2.5x past it. This surfaces it.
LEG_EQUITY = float(_os.environ.get("LEG4H_LIVE_EQUITY", 112.20))
HALT_PCT = 0.08


def book_report() -> None:
    st = load_states()
    print("=" * 88)
    print("2b) BOOK vs THE -8% HALT LINE (realised only — see the caveat)")
    print("=" * 88)
    if st.empty or "equity" not in st:
        print("  no leg states yet\n")
        return
    eq = st["equity"].dropna()
    if eq.empty:
        print("  no equity in any state file\n")
        return
    base = LEG_EQUITY * len(st)
    book, pnl = float(eq.sum()), float(eq.sum()) - base
    pct = 100 * pnl / base if base else 0.0
    halt_at = -HALT_PCT * base
    print(f"  baseline  {len(st)} legs x {LEG_EQUITY:.2f} = {base:,.2f}")
    print(f"  now       {book:,.2f}   realised PnL {pnl:+.2f} ({pct:+.2f}%)")
    print(f"  halt line {halt_at:+,.2f} ({-HALT_PCT*100:.0f}%)   "
          f"headroom {pnl - halt_at:+,.2f}")
    if pnl <= halt_at:
        print("  *** BOOK HALT LINE BREACHED — flatten and post-mortem before "
              "any restart (ROADMAP IF->THEN) ***")
    elif pnl <= halt_at * 0.5:
        print(f"  ⚠ past HALF the halt budget — watch closely")
    print("\n  CAVEAT: bot equity is REALISED only; open positions are not marked")
    print("  to market here, so this LAGS. It is a slow control (worst backtested")
    print("  month is -3.6%), which is why 12-hourly detection is adequate — but")
    print("  it is a HUMAN control: nothing in bot.py enforces it.\n")


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
    sessions_report()
    live_report()
    book_report()
    blotter_report()
    print("Gaps above are facts. Record them; never synthesise a tick to close one.")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
