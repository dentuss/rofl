"""Unified loader for everything the boxes produce — ticks and live trading.

The two Oracle boxes bind-mount their output to `<repo>/data` (see
docker-compose.*.yml `${ROFL_DATA:-./data}`), so `deploy/pull-data.sh` can
rsync that tree straight down here and the layout is identical in both places:

    data/
      ticks/YYYY-MM-DD/{trades_1s,liq,book_1s,ticker_1m}.csv[.gz]
      live/<sym>-<leg>/state/{bot_state.json,heartbeat}
      live/<sym>-<leg>/logs/{bot.log,events-YYYY-MM-DD.jsonl}

Everything here is READ-ONLY and tolerant of missing/partial data — a box
that has been up for an hour, a day with a gap, or a leg that never traded
must all load without raising. Missing is normal; NEVER synthesise a row to
fill a hole (a fake tick poisons every study built on it).

The point of `live_blotter()` is ROADMAP L2: it returns live fills in the same
shape as `core.backtest.Trade`, so reconciling live against the fixed engine
is a DataFrame join instead of an afternoon of eyeballing.

Usage:
    from core.datastore import load_ticks, live_blotter, load_states
    liq = load_ticks("liq", start="2026-08-05")
    bl  = live_blotter()
"""
from __future__ import annotations

import gzip
import io
import json
import os
from pathlib import Path

import pandas as pd

DATA = Path(os.getenv("ROFL_DATA", Path(__file__).resolve().parent.parent / "data"))

# Headers are defined by collector.py's Sink() calls — keep in lockstep.
TICK_KINDS: dict[str, list[str]] = {
    "trades_1s": ["ts", "sym", "n", "vol", "vwap", "buy_vol", "sell_vol"],
    "liq":       ["ts_ms", "sym", "side", "price", "amount"],
    "book_1s":   ["ts", "sym", "bid", "bid_sz", "ask", "ask_sz"],
    "ticker_1m": ["ts", "sym", "last", "mark", "index", "funding", "oi"],
    # depth_1s: cumulative notional within N bps of mid, per side. MAJORS8
    # only — depth is an execution question and we execute only there.
    "depth_1s":  ["ts", "sym", "mid",
                  "bid_1bp", "bid_5bp", "bid_10bp", "bid_25bp",
                  "ask_1bp", "ask_5bp", "ask_10bp", "ask_25bp"],
}

# Ops metadata, not tick data: no `sym` column, so it is deliberately outside
# TICK_KINDS (load_ticks groups and filters on sym).
SESSION_COLS = ["ts", "n_symbols", "n_depth_symbols", "free_mb"]


# --------------------------------------------------------------------- ticks
def ticks_root() -> Path:
    return DATA / "ticks"


def tick_days() -> list[str]:
    """Sorted YYYY-MM-DD directory names present locally."""
    root = ticks_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and len(p.name) == 10 and p.name[4] == "-")


def _read_csv(path: Path, cols: list[str]) -> pd.DataFrame:
    """Read a (possibly gzipped, possibly truncated) collector CSV."""
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
        # Drop malformed lines BEFORE parsing. pandas' on_bad_lines="skip" only
        # catches lines with too MANY fields; a line with too few is silently
        # NaN-padded — and too few is exactly what an rsync of a file the
        # collector is still appending to produces. These CSVs contain no
        # quoted fields (plain numerics + a base-symbol string), so counting
        # separators is a sound check.
        lines = text.splitlines()
        good = [ln for ln in lines if ln.count(",") == len(cols) - 1]
        if not good:
            return pd.DataFrame(columns=cols)
        df = pd.read_csv(io.StringIO("\n".join(good)), on_bad_lines="skip")
    except (OSError, EOFError, pd.errors.EmptyDataError):
        return pd.DataFrame(columns=cols)
    if df.empty:
        return pd.DataFrame(columns=cols)
    # A file written before a header fix, or a header-less append, still loads.
    if list(df.columns) != cols and len(df.columns) == len(cols):
        df.columns = cols
    return df


def load_ticks(kind: str = "trades_1s", start: str | None = None,
               end: str | None = None, symbols: list[str] | None = None,
               tz_index: bool = True) -> pd.DataFrame:
    """Concatenate one tick stream across days.

    kind    : one of TICK_KINDS
    start/end: inclusive 'YYYY-MM-DD' bounds on the DAY DIRECTORY
    symbols : base symbols to keep, e.g. ["BTC", "ETH"]
    tz_index: index by UTC timestamp instead of a RangeIndex
    """
    if kind not in TICK_KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {list(TICK_KINDS)}")
    cols = TICK_KINDS[kind]
    frames = []
    for day in tick_days():
        if start and day < start:
            continue
        if end and day > end:
            continue
        d = ticks_root() / day
        # PREFER .gz, never load both. The collector gzips yesterday's file at
        # UTC midnight and removes the original, but rsync without --delete
        # leaves the stale .csv behind locally — so a day pulled both before
        # and after midnight ends up with BOTH files, and loading each gave
        # ~45% duplicate rows (found 2026-08-07). We deliberately do not use
        # rsync --delete: the local tree is the redundant copy, and a wiped
        # box must not be able to wipe it. So the loader resolves it instead.
        gz, plain = d / f"{kind}.csv.gz", d / f"{kind}.csv"
        p = gz if gz.exists() else plain
        if p.exists():
            df = _read_csv(p, cols)
            if not df.empty:
                df["day"] = day
                frames.append(df)
    if not frames:
        return pd.DataFrame(columns=cols + ["day"])
    out = pd.concat(frames, ignore_index=True)
    if symbols:
        out = out[out["sym"].isin(symbols)]
    tcol = "ts_ms" if "ts_ms" in out.columns else "ts"
    out[tcol] = pd.to_numeric(out[tcol], errors="coerce")
    out = out.dropna(subset=[tcol])
    if tz_index:
        unit = "ms" if tcol == "ts_ms" else "s"
        out.index = pd.to_datetime(out[tcol], unit=unit, utc=True)
        out = out.sort_index()
    return out


def load_sessions(start: str | None = None) -> pd.DataFrame:
    """The collector's own minute heartbeat — makes gaps explicit rather than
    inferred, and carries free-disk so pressure is visible in hindsight."""
    frames = []
    for day in tick_days():
        if start and day < start:
            continue
        d = ticks_root() / day
        gz, plain = d / "session_1m.csv.gz", d / "session_1m.csv"
        p = gz if gz.exists() else plain
        if p.exists():
            df = _read_csv(p, SESSION_COLS)
            if not df.empty:
                frames.append(df)
    if not frames:
        return pd.DataFrame(columns=SESSION_COLS)
    out = pd.concat(frames, ignore_index=True)
    out["ts"] = pd.to_numeric(out["ts"], errors="coerce")
    out = out.dropna(subset=["ts"]).sort_values("ts")
    out.index = pd.to_datetime(out["ts"], unit="s", utc=True)
    return out


# ---------------------------------------------------------------- live legs
def live_root() -> Path:
    return DATA / "live"


def legs() -> list[str]:
    """Leg directory names present locally, e.g. ['ada-p', 'ada-t', ...]."""
    root = live_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def load_states() -> pd.DataFrame:
    """One row per leg from bot_state.json, plus heartbeat age in seconds."""
    import time
    rows = []
    for leg in legs():
        sd = live_root() / leg / "state"
        row: dict = {"leg": leg}
        sf = sd / "bot_state.json"
        if sf.exists():
            try:
                st = json.loads(sf.read_text())
                pos = st.get("position") or {}
                row.update(
                    equity=st.get("equity"),
                    realised_pnl=st.get("realised_pnl"),
                    realised_trades=st.get("realised_trades"),
                    realised_wins=st.get("realised_wins"),
                    pos_side=pos.get("side"), pos_qty=pos.get("qty"),
                    pos_entry=pos.get("entry_px"), pos_sl=pos.get("sl"),
                    pos_tp=pos.get("tp"),
                )
            except (json.JSONDecodeError, OSError):
                row["error"] = "unreadable bot_state.json"
        else:
            row["error"] = "no bot_state.json yet"
        hb = sd / "heartbeat"
        row["heartbeat_age_s"] = round(time.time() - hb.stat().st_mtime) \
            if hb.exists() else None
        rows.append(row)
    return pd.DataFrame(rows)


def load_events(leg: str | None = None, event: str | None = None,
                start: str | None = None) -> pd.DataFrame:
    """All JSONL events, optionally filtered by leg / event type / start day."""
    rows = []
    for lg in ([leg] if leg else legs()):
        ld = live_root() / lg / "logs"
        if not ld.is_dir():
            continue
        for p in sorted(ld.glob("events-*.jsonl")):
            day = p.stem.replace("events-", "")
            if start and day < start:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue          # partial final line while the bot writes
                r["leg"] = lg
                rows.append(r)
    if not rows:
        return pd.DataFrame(columns=["ts", "event", "leg"])
    df = pd.DataFrame(rows)
    if event:
        df = df[df["event"] == event]
    if "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
        df = df.sort_values("ts")
    return df.reset_index(drop=True)


# Columns of core.backtest.Trade, so live and engine line up 1:1.
TRADE_COLS = ["side", "entry_time", "exit_time", "entry_px", "exit_px", "qty",
              "notional", "sl", "tp", "pnl", "fees", "reason", "bars_held"]


def live_blotter() -> pd.DataFrame:
    """Live fills shaped like `core.backtest.Trade` — the L2 reconcile input.

    Built from `exit` events (which carry entry_px, pnl, fees, reason and
    bars_held), with entry_time / sl / tp joined from the matching `entry`
    event: within a leg, entries and exits alternate, so the last entry before
    an exit is that exit's opening. A leg whose first exit predates any entry
    (bot restarted mid-position) simply gets NaT/NaN there rather than a guess.
    """
    ev = load_events()
    if ev.empty or "event" not in ev.columns:
        return pd.DataFrame(columns=TRADE_COLS + ["leg", "symbol"])
    out = []
    for lg, g in ev.groupby("leg"):
        g = g.sort_values("ts")
        pending: dict | None = None
        for r in g.to_dict("records"):
            if r.get("event") == "entry":
                pending = r
            elif r.get("event") == "exit":
                out.append({
                    "leg": lg,
                    "symbol": r.get("symbol"),
                    "side": r.get("side"),
                    "entry_time": pending.get("ts") if pending else pd.NaT,
                    "exit_time": r.get("ts"),
                    "entry_px": r.get("entry_px"),
                    "exit_px": r.get("exit_px"),
                    "qty": r.get("qty"),
                    "notional": r.get("notional"),
                    "sl": pending.get("sl") if pending else float("nan"),
                    "tp": pending.get("tp") if pending else float("nan"),
                    "pnl": r.get("pnl"),
                    "fees": r.get("fees"),
                    "reason": r.get("reason"),
                    "bars_held": r.get("bars_held"),
                })
                pending = None
    if not out:
        return pd.DataFrame(columns=TRADE_COLS + ["leg", "symbol"])
    df = pd.DataFrame(out)
    return df[["leg", "symbol"] + TRADE_COLS].sort_values("exit_time") \
             .reset_index(drop=True)


def summary() -> dict:
    """Cheap one-call overview — what is present, how fresh, how big."""
    days = tick_days()
    bl = live_blotter()
    st = load_states()
    return {
        "data_dir": str(DATA),
        "tick_days": len(days),
        "tick_first": days[0] if days else None,
        "tick_last": days[-1] if days else None,
        "legs": len(legs()),
        "live_trades": len(bl),
        "live_pnl": float(bl["pnl"].sum()) if len(bl) and "pnl" in bl else 0.0,
        "stale_legs": int((st["heartbeat_age_s"] > 300).sum())
                      if len(st) and st["heartbeat_age_s"].notna().any() else 0,
    }
