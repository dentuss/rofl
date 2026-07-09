"""Paper executor for the cross-sectional sleeves: XSMOM-21 + XSBAB-60.

Deterministic forward test (sleeves_paper.py pattern): each run recomputes
both sleeves from public data using the EXACT pre-registered constructions
(research/sleeve_battery.py S3 k=21, research/sleeve_battery2.py XSBAB-60 —
signals lagged >= 1 day, weekly Monday stamps, base_w inverse-vol sizing,
8bp/turnover + real funding via port_returns), then reports the track since
the anchor and TODAY'S TARGET BOOKS. Signals are lagged, so past values
never revise: the track file IS the forward record once the anchor is set.

This is the gating forward stage for the assembly-v4 proposal
(BOOK50/XSMOM25/XSBAB25, Sh 1.80 / OOS 2.01) — the headline question is
whether XSMOM's recent fade (last third +0.14) is a rough patch or decay.

Run daily (cron / Task Scheduler / by hand):
    PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe xs_paper.py
First run stamps state/xs_paper.json (XS_ANCHOR=YYYY-MM-DD overrides).
No keys, no exchange writes — read-only public data.
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
from research.sleeve_diagnosis import port_returns, base_w
from research.tsmom_sleeve import QUAL23

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
STATE_FILE = os.path.join(STATE_DIR, "xs_paper.json")
TRACK_FILE = os.path.join(STATE_DIR, "xs_paper_track.csv")
DAYS = int(os.environ.get("DAYS", 500))


def weekly(raw: pd.DataFrame) -> pd.DataFrame:
    is_reb = pd.Series(raw.index.dayofweek == 0, index=raw.index)
    return raw.where(is_reb).ffill().fillna(0.0)


def xsmom21_positions(C: pd.DataFrame) -> pd.DataFrame:
    mom = C.pct_change(21).sub(C["BTC-USDT"].pct_change(21), axis=0) \
        .shift(1).drop(columns=["BTC-USDT"])
    rank = mom.rank(axis=1, pct=True)
    raw = pd.DataFrame(0.0, index=C.index, columns=mom.columns)
    raw[rank >= 0.8] = 1.0
    raw[rank <= 0.2] = -1.0
    pos = (weekly(raw) * base_w(C).drop(columns=["BTC-USDT"])).fillna(0.0)
    return pos.reindex(columns=C.columns).fillna(0.0)


def xsbab60_positions(C: pd.DataFrame) -> pd.DataFrame:
    r1 = C.pct_change()
    rb = r1["BTC-USDT"]
    beta = r1.rolling(60, min_periods=40).cov(rb) \
        .div(rb.rolling(60, min_periods=40).var(), axis=0).shift(1) \
        .drop(columns=["BTC-USDT"])
    rank = beta.rank(axis=1, pct=True)
    raw = pd.DataFrame(0.0, index=C.index, columns=beta.columns)
    raw[rank <= 0.2] = 1.0
    raw[rank >= 0.8] = -1.0
    pos = (weekly(raw) * base_w(C).drop(columns=["BTC-USDT"])).fillna(0.0)
    return pos.reindex(columns=C.columns).fillna(0.0)


def show_book(name: str, pos_row: pd.Series) -> None:
    longs = {s.split("-")[0]: v for s, v in pos_row.items() if v > 1e-9}
    shorts = {s.split("-")[0]: v for s, v in pos_row.items() if v < -1e-9}
    fmt = lambda d: "  ".join(f"{k} {v:+.2f}" for k, v in
                              sorted(d.items(), key=lambda x: -abs(x[1])))
    print(f"  {name} target book (position = fraction of sleeve capital):")
    print(f"    LONG : {fmt(longs) or '-'}")
    print(f"    SHORT: {fmt(shorts) or '-'}")


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
        fund_d[p] = f["funding_rate"].resample("1D").sum() \
            if f is not None and len(f) else pd.Series(dtype=float)
    C = pd.DataFrame(closes)
    F = pd.DataFrame(fund_d)

    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as fh:
            state = json.load(fh)
    else:
        anchor = os.environ.get("XS_ANCHOR", str(C.index[-1].date()))
        state = {"anchor": anchor}
        with open(STATE_FILE, "w") as fh:
            json.dump(state, fh)
        print(f"anchor set: {anchor}")
    anchor = pd.Timestamp(state["anchor"], tz="UTC")

    pos_m = xsmom21_positions(C)
    pos_b = xsbab60_positions(C)
    r_m = port_returns(C, F, pos_m)
    r_b = port_returns(C, F, pos_b)
    track = pd.DataFrame({"xsmom": r_m, "xsbab": r_b}).loc[anchor:] \
        .fillna(0.0)
    track["xsmom_eq"] = 100 * (1 + track["xsmom"]).cumprod()
    track["xsbab_eq"] = 100 * (1 + track["xsbab"]).cumprod()
    track.to_csv(TRACK_FILE)

    print(f"XS PAPER TRACK since {anchor.date()} ({len(track)} days, "
          f"through {track.index[-1].date()}):")
    for name in ("xsmom", "xsbab"):
        r = track[name]
        s = float(r.mean() / r.std() * np.sqrt(365)) if r.std() > 0 else 0.0
        print(f"  {name:6s} total {float(track[name + '_eq'].iloc[-1]) - 100:+6.2f}%   "
              f"ann Sh {s:+.2f}   worst day {float(r.min())*100:+.2f}%")
    print()
    show_book("XSMOM-21", pos_m.iloc[-1])
    show_book("XSBAB-60", pos_b.iloc[-1])
    print("\n(forward stage for the BOOK50/XS25/BAB25 proposal — "
          ">=8 weeks before any capital discussion)")


if __name__ == "__main__":
    main()
