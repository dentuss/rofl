"""Structured JSONL event log — append-only, easy to parse later.

Every meaningful event the bot processes is written as a single JSON line
to <state-dir>/events.jsonl. After running for a few weeks, run
research/analyze_logs.py to summarise what happened.

Event types:
  bot_start         {ts, mode, preset, symbol, tf, equity, ...}
  tick              {ts, equity, position_side, position_pnl_unrealised}
  signal            {ts, signal, sl, tp, regime, close_price}
  entry             {ts, side, qty, entry_px, sl, tp, notional, risk, equity}
  exit              {ts, side, qty, exit_px, pnl, reason, bars_held, equity}
  daily_summary     {ts, equity, day_pnl, day_trades, ...}
  regime_change     {ts, from, to, equity}
  error             {ts, message, traceback?}
  bot_stop          {ts, trades, pnl, equity}

The file rotates by day to keep individual files manageable:
  events-2026-05-18.jsonl
A simple `cat events-*.jsonl` reconstructs the full timeline.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


class EventLog:
    def __init__(self, path: str | None = None):
        """path: directory for event files. Defaults to dir of LOG_FILE or 'logs/'."""
        if path is None:
            log_file = os.getenv("LOG_FILE", "bot.log")
            path = str(Path(log_file).parent)
        self.dir = Path(path)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _current_file(self) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.dir / f"events-{day}.jsonl"

    def emit(self, event_type: str, **fields) -> None:
        """Write one event. Never raises — best-effort logging."""
        try:
            row = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "event": event_type,
                **fields,
            }
            # Coerce non-serializable values to strings
            line = json.dumps(row, default=str, separators=(",", ":"))
            with open(self._current_file(), "a") as f:
                f.write(line + "\n")
        except Exception:
            pass  # logging must not crash the bot

    # ---- convenience wrappers (typed signatures for editor help) ----------
    def bot_start(self, **kw):           self.emit("bot_start", **kw)
    def bot_stop(self, **kw):            self.emit("bot_stop", **kw)
    def tick(self, **kw):                self.emit("tick", **kw)
    def signal(self, **kw):              self.emit("signal", **kw)
    def entry(self, **kw):               self.emit("entry", **kw)
    def exit(self, **kw):                self.emit("exit", **kw)
    def daily_summary(self, **kw):       self.emit("daily_summary", **kw)
    def regime_change(self, **kw):       self.emit("regime_change", **kw)
    def error(self, **kw):               self.emit("error", **kw)
