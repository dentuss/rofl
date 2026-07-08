"""Paper executor for the TSMOM-90 and funding-carry sleeves.

Deterministic forward test: each run recomputes both sleeve return series
from public data using the EXACT research formulas (research/tsmom_sleeve.py,
research/carry_sleeve.py — signals lagged >= 1 day, 8bp/turnover, real
funding), then reports the track record since the anchor date and today's
TARGET BOOK for each sleeve. No keys, no exchange writes — read-only data.

Because every signal is lagged, past values never revise: the track file IS
the forward record once the anchor is set.

Run daily (Task Scheduler / cron / by hand):
    PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe sleeves_paper.py
First run stamps the anchor into state/sleeves_paper.json
(SLEEVES_ANCHOR=YYYY-MM-DD env overrides).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

from core.data import fetch_ohlcv_bybit
from core.funding import fetch_funding
from research.tsmom_sleeve import (sleeve_returns, eq_from_rets, QUAL23,
                                   VOL_TARGET_D, POS_CAP)
from research.carry_sleeve import carry_returns

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
STATE_FILE = os.path.join(STATE_DIR, "sleeves_paper.json")
TRACK_FILE = os.path.join(STATE_DIR, "sleeves_paper_track.csv")
DAYS = int(os.environ.get("DAYS", 400))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    os.makedirs(STATE_DIR, exist_ok=True)

    closes, fund_d = {}, {}
    for p in QUAL23:
        closes[p] = fetch_ohlcv_bybit(p, "1d", days=DAYS)["close"]
        f = fetch_funding(p, days=DAYS, source="auto")
        fund_d[p] = f["funding_rate"].resample("1D").sum() if f is not None and len(f) \
            else pd.Series(dtype=float)
    closes = pd.DataFrame(closes)
    fund_daily = pd.DataFrame(fund_d)

    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as fh:
            state = json.load(fh)
    else:
        anchor = os.environ.get("SLEEVES_ANCHOR",
                                str(closes.index[-1].date()))
        state = {"anchor": anchor}
        with open(STATE_FILE, "w") as fh:
            json.dump(state, fh)
        print(f"anchor set: {anchor}")
    anchor = pd.Timestamp(state["anchor"], tz="UTC")

    r_t = sleeve_returns(closes, fund_daily, 90)
    r_c = carry_returns(closes, fund_daily)
    track = pd.DataFrame({"tsmom": r_t, "carry": r_c}).loc[anchor:].fillna(0.0)
    track["tsmom_eq"] = 100 * (1 + track["tsmom"]).cumprod()
    track["carry_eq"] = 100 * (1 + track["carry"]).cumprod()
    track.to_csv(TRACK_FILE)

    print(f"SLEEVES PAPER TRACK since {anchor.date()} "
          f"({len(track)} days, through {track.index[-1].date()}):")
    for name in ("tsmom", "carry"):
        r = track[name]
        sh = float(r.mean() / r.std() * np.sqrt(365)) if r.std() > 0 else 0.0
        print(f"  {name:6s} total {float(track[name + '_eq'].iloc[-1]) - 100:+6.2f}%  "
              f"Sh(d) {sh:+5.2f}  worst day {float(r.min()) * 100:+5.2f}%")

    # --- today's TARGET books (positions to hold next; research formulas) ----
    rets = closes.pct_change()
    vol = rets.shift(1).rolling(60, min_periods=40).std()
    w = (VOL_TARGET_D / vol).clip(upper=POS_CAP)
    sig_t = np.sign(closes.shift(1) / closes.shift(91) - 1)
    pos_t = (sig_t * w).iloc[-1].dropna()
    print("\nTSMOM-90 target book (weight per name):")
    for p, v in pos_t[pos_t != 0].sort_values().items():
        print(f"  {p.split('-')[0]:6s} {v:+.2f}")

    f7 = fund_daily.reindex(closes.index).fillna(0.0).rolling(7).sum().shift(1)
    rank = f7.rank(axis=1, pct=True).iloc[-1]
    longs = rank[rank <= 0.2].index
    shorts = rank[rank >= 0.8].index
    wl = w.iloc[-1]
    print("\nCARRY target book (as of the last weekly rebalance snapshot):")
    print("  LONG  (cheap funding): " +
          ", ".join(f"{p.split('-')[0]} {wl[p]:+.2f}" for p in longs))
    print("  SHORT (crowded longs): " +
          ", ".join(f"{p.split('-')[0]} {-wl[p]:+.2f}" for p in shorts))


if __name__ == "__main__":
    main()
