"""Summarise the bot's event log after a paper-trading run.

Usage:
    python3 research/analyze_logs.py [path_to_logs_dir]

Reads events-YYYY-MM-DD.jsonl, prints:
  - run overview (sessions, total elapsed time)
  - per-trade summary: count, win rate, avg win/loss, profit factor
  - per-day PnL
  - regime distribution (from signal events)
  - signal-vs-fill summary (how many signals fired vs trades opened)
  - errors / regime changes / bot-restarts
  - equity curve as a tiny ASCII chart

Designed so we can paste its output back into the conversation later
to decide whether to tune any parameters.
"""
from __future__ import annotations

import sys as _sys, os as _os
_THIS_DIR_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _THIS_DIR_PARENT not in _sys.path:
    _sys.path.insert(0, _THIS_DIR_PARENT)

import glob
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd


def load_events(logs_dir: Path) -> pd.DataFrame:
    files = sorted(logs_dir.glob("events-*.jsonl"))
    if not files:
        # fallback: try logs/ or current dir
        files = sorted(Path(".").glob("events-*.jsonl"))
        if not files:
            print(f"No events-*.jsonl in {logs_dir} or .")
            sys.exit(1)
    rows = []
    for f in files:
        for line in open(f):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    if not rows:
        print("No events found.")
        sys.exit(1)
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    return df


def ascii_chart(series: pd.Series, width: int = 60, height: int = 10) -> str:
    """Tiny ASCII line chart of a numeric series."""
    s = series.dropna().values
    if len(s) < 2:
        return "(not enough data)"
    if len(s) > width:
        idx = (pd.Series(range(len(s))) * width // len(s)).values
        bucketed = pd.Series(s).groupby(idx).mean().values
    else:
        bucketed = s
    lo, hi = bucketed.min(), bucketed.max()
    if hi == lo:
        return f"flat at {lo:.2f}"
    rows = []
    for level in range(height, -1, -1):
        threshold = lo + (hi - lo) * level / height
        line = "".join("*" if v >= threshold else " " for v in bucketed)
        prefix = f"{threshold:>8.2f} |"
        rows.append(prefix + line)
    rows.append(" " * 9 + "+" + "-" * len(bucketed))
    rows.append(f"         {series.index[0].strftime('%m-%d')}"
                + " " * (len(bucketed) - 10) + series.index[-1].strftime('%m-%d'))
    return "\n".join(rows)


def main():
    logs_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "logs")
    print(f"Reading events from: {logs_dir.resolve()}")
    df = load_events(logs_dir)
    print(f"Loaded {len(df)} events across {df['ts'].dt.date.nunique()} days "
          f"({df['ts'].min()} → {df['ts'].max()})")

    pd.set_option("display.width", 200)
    pd.set_option("display.max_colwidth", 60)

    # --- Sessions (each bot_start = new session) -----------------------------
    starts = df[df["event"] == "bot_start"]
    stops = df[df["event"] == "bot_stop"]
    print()
    print("=" * 80)
    print(f"SESSIONS")
    print("=" * 80)
    print(f"  bot_start events: {len(starts)}")
    print(f"  bot_stop events:  {len(stops)}")
    if not starts.empty:
        last = starts.iloc[-1]
        print(f"  last start at:    {last['ts']}")
        print(f"  config:           mode={last.get('mode')} exchange={last.get('exchange')} "
              f"symbol={last.get('symbol')} tf={last.get('tf')} preset={last.get('preset')} "
              f"risk={last.get('risk_per_trade')}")

    # --- Trade summary -------------------------------------------------------
    trades = df[df["event"] == "exit"].copy()
    print()
    print("=" * 80)
    print(f"TRADES — {len(trades)} closed")
    print("=" * 80)
    if not trades.empty:
        trades["pnl"] = trades["pnl"].astype(float)
        trades["pnl_pct"] = trades["pnl_pct"].astype(float)
        trades["bars_held"] = trades["bars_held"].astype(int)
        wins = trades[trades["pnl"] > 0]
        losses = trades[trades["pnl"] <= 0]
        gross_w = wins["pnl"].sum() if len(wins) else 0.0
        gross_l = -losses["pnl"].sum() if len(losses) else 0.0
        pf = (gross_w / gross_l) if gross_l > 0 else float("inf")
        print(f"  win rate:        {len(wins)/len(trades)*100:.1f}%   "
              f"({len(wins)}W / {len(losses)}L)")
        print(f"  avg win:         {wins['pnl'].mean():+.3f}" if len(wins) else "  avg win:         —")
        print(f"  avg loss:        {losses['pnl'].mean():+.3f}" if len(losses) else "  avg loss:        —")
        print(f"  profit factor:   {pf:.2f}")
        print(f"  total realised:  {trades['pnl'].sum():+.3f}")
        print(f"  avg holding:     {trades['bars_held'].mean():.1f} bars")
        print(f"  exit reasons:    {dict(Counter(trades['reason']))}")
        print()
        print("  Last 10 trades:")
        cols = ["ts", "side", "qty", "entry_px", "exit_px", "pnl",
                "pnl_pct", "reason", "bars_held"]
        cols = [c for c in cols if c in trades.columns]
        print(trades[cols].tail(10).to_string(index=False))

    # --- Per-day PnL --------------------------------------------------------
    if not trades.empty:
        print()
        print("=" * 80)
        print("PER-DAY PnL")
        print("=" * 80)
        trades["date"] = trades["ts"].dt.date
        daily = trades.groupby("date")["pnl"].agg(["sum", "count"])
        daily.columns = ["pnl", "trades"]
        print(daily.tail(30).to_string())

    # --- Signal-vs-fill summary ---------------------------------------------
    sigs = df[df["event"] == "signal"]
    entries = df[df["event"] == "entry"]
    print()
    print("=" * 80)
    print("SIGNAL ACTIVITY")
    print("=" * 80)
    if not sigs.empty:
        nonzero = sigs[sigs["signal"] != 0]
        print(f"  total signal-checks:  {len(sigs)}")
        print(f"  non-zero signals:     {len(nonzero)} ({len(nonzero)/len(sigs)*100:.1f}%)")
        print(f"  entries opened:       {len(entries)}")
        if len(nonzero) > 0:
            print(f"  signal->entry rate:   {len(entries)/len(nonzero)*100:.1f}%")
            # (some signals are skipped because a position is already open)

    # --- Regime distribution -------------------------------------------------
    if "regime" in df.columns:
        regs = df[df["regime"].notna() & (df["regime"] != "")]["regime"]
        if len(regs) > 0:
            print()
            print("=" * 80)
            print("REGIME DISTRIBUTION (from signal events)")
            print("=" * 80)
            for label, count in regs.value_counts().items():
                pct = count / len(regs) * 100
                print(f"  {label:6s}  {count:>6d}  ({pct:5.1f}%)")

    rchanges = df[df["event"] == "regime_change"]
    if not rchanges.empty:
        print(f"\n  Regime transitions ({len(rchanges)} observed):")
        for _, r in rchanges.tail(10).iterrows():
            print(f"    {r['ts']}  {r['from_regime']} → {r['to_regime']}  "
                  f"(equity={r.get('equity', '?')})")

    # --- Errors -------------------------------------------------------------
    errs = df[df["event"] == "error"]
    if not errs.empty:
        print()
        print("=" * 80)
        print(f"ERRORS — {len(errs)} (last 5)")
        print("=" * 80)
        for _, e in errs.tail(5).iterrows():
            print(f"  {e['ts']}  {e.get('exception_type', '')}: {str(e.get('message', ''))[:120]}")

    # --- Equity curve (from signal + exit events) ---------------------------
    eq_events = df[df["event"].isin(["signal", "exit", "daily_summary"])].copy()
    if "equity" in eq_events.columns:
        eq_events = eq_events.dropna(subset=["equity"])
        if not eq_events.empty:
            eq_series = pd.Series(eq_events["equity"].astype(float).values,
                                  index=pd.DatetimeIndex(eq_events["ts"]))
            print()
            print("=" * 80)
            print("EQUITY CURVE")
            print("=" * 80)
            print(f"  starting: {eq_series.iloc[0]:.2f}")
            print(f"  current:  {eq_series.iloc[-1]:.2f}")
            print(f"  return:   {(eq_series.iloc[-1]/eq_series.iloc[0]-1)*100:+.2f}%")
            peak = eq_series.cummax()
            dd = (eq_series / peak - 1).min()
            print(f"  peak:     {peak.iloc[-1]:.2f}")
            print(f"  max DD:   {dd*100:+.2f}%")
            print()
            print(ascii_chart(eq_series, width=70, height=8))

    print()
    print("=" * 80)
    print("Bring this output back to me + the events-*.jsonl files and we'll")
    print("decide whether parameters need tuning.")
    print("=" * 80)


if __name__ == "__main__":
    main()
