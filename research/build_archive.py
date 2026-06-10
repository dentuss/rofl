"""Build a portable archive of a portfolio run for the record.

Given a directory containing per-bot subdirectories with {bot_state.json,
bot.log, events-*.jsonl}, produces:
  - summary.md  : markdown report (per-bot equity, PnL, trade list, totals)
  - trades.csv  : every entry/exit event, one row per trade
  - kept files  : the raw state + logs + events for reference

Used by `./portfolio.sh archive` — it docker-execs into each container to
copy the volume contents under archives/<timestamp>/<bot>/, then calls
this script to produce the report.

Run standalone:
    python3 research/build_archive.py archives/run_2026-06-10_120000
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _read_state(p: Path) -> dict | None:
    f = p / "bot_state.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def _read_events(p: Path) -> list[dict]:
    out = []
    for f in sorted(p.glob("events-*.jsonl")):
        with f.open() as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def _trade_table(events: list[dict]) -> list[dict]:
    """Pair entry+exit events into trade rows."""
    open_pos = None
    rows = []
    for e in events:
        ev = e.get("event")
        if ev == "entry":
            open_pos = e
        elif ev == "exit" and open_pos is not None:
            rows.append({
                "entry_ts": open_pos.get("ts"),
                "exit_ts": e.get("ts"),
                "side": "LONG" if open_pos.get("side") == 1 else "SHORT",
                "qty": open_pos.get("qty"),
                "entry_px": open_pos.get("entry_px"),
                "exit_px": e.get("exit_px"),
                "pnl": e.get("pnl"),
                "pnl_pct": e.get("pnl_pct"),
                "reason": e.get("reason"),
                "bars_held": e.get("bars_held"),
            })
            open_pos = None
    return rows


def _fmt_money(x): return f"{x:+.2f}" if x is not None else "-"
def _fmt_pct(x):   return f"{x:+.2f}%" if x is not None else "-"


def build(root: Path) -> None:
    bots = sorted([d for d in root.iterdir() if d.is_dir()])
    if not bots:
        print(f"no bot subdirs in {root}")
        return

    per_bot = []
    all_trades_rows = []
    total_eq = total_pnl = total_trades = 0.0
    for bd in bots:
        name = bd.name
        st = _read_state(bd)
        evs = _read_events(bd)
        trades = _trade_table(evs)
        if st is None:
            per_bot.append({"name": name, "missing": True})
            continue
        wins = [t for t in trades if (t.get("pnl") or 0) > 0]
        wr = len(wins) / len(trades) if trades else 0.0
        per_bot.append({
            "name": name,
            "equity": st.get("equity", 0),
            "pnl": st.get("realised_pnl", 0),
            "realised_trades": st.get("realised_trades", 0),
            "equity_peak": st.get("equity_peak", st.get("equity", 0)),
            "open_position": st.get("position"),
            "trades_logged": len(trades),
            "win_rate": wr,
            "best": max((t.get("pnl") or 0 for t in trades), default=0),
            "worst": min((t.get("pnl") or 0 for t in trades), default=0),
        })
        for t in trades:
            t["bot"] = name
            all_trades_rows.append(t)
        total_eq += st.get("equity", 0)
        total_pnl += st.get("realised_pnl", 0)
        total_trades += st.get("realised_trades", 0)

    # Summary markdown
    md = []
    md.append(f"# Portfolio archive — {root.name}\n")
    md.append(f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")
    md.append("## Per-bot summary\n")
    md.append("| Bot | Equity | Realized PnL | Trades | Win rate | Peak | Best trade | Worst trade | Open position |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for b in per_bot:
        if b.get("missing"):
            md.append(f"| {b['name']} | (no state) | | | | | | | |")
            continue
        pos = b["open_position"]
        if pos:
            side = "LONG" if pos.get("side") == 1 else "SHORT"
            pos_s = (f"{side} qty={pos.get('qty', 0):.4f} entry={pos.get('entry_px', 0):.4f} "
                     f"sl={pos.get('sl', 0):.4f} tp={pos.get('tp', 0):.4f}")
        else:
            pos_s = "(flat)"
        md.append(f"| {b['name']} | {b['equity']:.2f} | {_fmt_money(b['pnl'])} | "
                  f"{b['realised_trades']} | {b['win_rate']*100:.0f}% | "
                  f"{b['equity_peak']:.2f} | {_fmt_money(b['best'])} | "
                  f"{_fmt_money(b['worst'])} | {pos_s} |")
    md.append(f"| **TOTAL** | **{total_eq:.2f}** | **{_fmt_money(total_pnl)}** | "
              f"**{int(total_trades)}** | | | | | |\n")

    if all_trades_rows:
        md.append("## All trades (chronological)\n")
        md.append("| Bot | Entry | Exit | Side | Entry px | Exit px | PnL | PnL % | Reason | Bars |")
        md.append("|---|---|---|---|---:|---:|---:|---:|---|---:|")
        for t in sorted(all_trades_rows, key=lambda r: r.get("entry_ts") or 0):
            e_ts = datetime.fromtimestamp(t["entry_ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if t.get("entry_ts") else "-"
            x_ts = datetime.fromtimestamp(t["exit_ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if t.get("exit_ts") else "-"
            md.append(f"| {t['bot']} | {e_ts} | {x_ts} | {t.get('side')} | "
                      f"{t.get('entry_px', 0):.4f} | {t.get('exit_px', 0):.4f} | "
                      f"{_fmt_money(t.get('pnl'))} | {_fmt_pct(t.get('pnl_pct'))} | "
                      f"{t.get('reason', '-')} | {t.get('bars_held', '-')} |")
        md.append("")

    (root / "summary.md").write_text("\n".join(md))

    # CSV of trades (Excel/Sheets-friendly)
    if all_trades_rows:
        import csv
        cols = ["bot", "entry_ts", "exit_ts", "side", "qty", "entry_px", "exit_px",
                "pnl", "pnl_pct", "reason", "bars_held"]
        with (root / "trades.csv").open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for t in sorted(all_trades_rows, key=lambda r: r.get("entry_ts") or 0):
                w.writerow(t)

    print(f"  wrote {root}/summary.md  ({len(per_bot)} bots, {len(all_trades_rows)} trades)")
    if all_trades_rows:
        print(f"  wrote {root}/trades.csv")
    print(f"  TOTAL equity ${total_eq:.2f}   PnL ${total_pnl:+.2f}   trades {int(total_trades)}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: build_archive.py <archive_dir>")
        sys.exit(2)
    build(Path(sys.argv[1]).resolve())
