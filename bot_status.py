"""Print a status summary across all bot_state_*.json files.

Useful when running run_portfolio.sh — quickly see equity, position, and
PnL across all running bots.

Usage:
    python3 bot_status.py             # current dir
    python3 bot_status.py /path/dir   # custom dir
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def main(directory: str = "."):
    root = Path(directory)
    # Search the dir AND its state/ subdir (portfolio launchers write there).
    search_dirs = [root]
    if (root / "state").is_dir():
        search_dirs.append(root / "state")
    files = []
    for d in search_dirs:
        files.extend(d.glob("bot_state_*.json"))
    files = sorted(set(files))
    if not files:
        # also try the single-bot file in either location
        for d in search_dirs:
            single = d / "bot_state.json"
            if single.exists():
                files = [single]
                break
        if not files:
            print("No bot_state*.json found.")
            return

    print(f"{'name':12s}{'equity':>10s}{'realized':>11s}{'trades':>8s}"
          f"{'peak':>10s}  position")
    print("-" * 80)
    total_eq = 0.0
    total_pnl = 0.0
    total_trades = 0
    for f in files:
        try:
            d = json.loads(f.read_text())
        except Exception as e:
            print(f"{f.name}: parse error {e}")
            continue
        name = f.stem.replace("bot_state_", "").replace("bot_state", "default") or "default"
        eq = d.get("equity", 0.0)
        pnl = d.get("realised_pnl", 0.0)
        trades = d.get("realised_trades", 0)
        peak = d.get("equity_peak", eq)
        pos = d.get("position")
        if pos:
            side = "LONG" if pos["side"] == 1 else "SHORT"
            ts = pos.get("open_ts", 0)
            age_s = int(datetime.now(timezone.utc).timestamp() - ts) if ts else 0
            age_str = f"{age_s // 3600}h{(age_s % 3600) // 60}m"
            pos_str = (f"{side} qty={pos['qty']:.4f} "
                       f"entry={pos['entry_px']:.3f} "
                       f"sl={pos['sl']:.3f} tp={pos['tp']:.3f} "
                       f"age={age_str}")
        else:
            pos_str = "(flat)"
        dd_pct = ((eq / peak - 1) * 100) if peak > 0 else 0
        print(f"{name:12s}{eq:>10.2f}{pnl:>+10.2f} {trades:>7d}{peak:>10.2f}  "
              f"{pos_str}")
        if dd_pct < -5:
            print(f"{'':12s}{'':>10s}{'':>11s}{'':>8s}{'dd':>10s}  "
                  f"current dd: {dd_pct:.2f}%")
        total_eq += eq
        total_pnl += pnl
        total_trades += trades
    if len(files) > 1:
        print("-" * 80)
        print(f"{'TOTAL':12s}{total_eq:>10.2f}{total_pnl:>+10.2f} {total_trades:>7d}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
