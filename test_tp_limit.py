"""Unit tests for the tp_as_limit engine knob (TP as a resting maker limit).

Checks, on synthetic bars:
1. Default (off): TP fills on a TOUCH (h >= tp), taker fee + slippage.
2. On: a touch does NOT fill; the first bar trading THROUGH the target fills
   AT the target with maker fee and zero slip.
3. Same-bar SL+TP collision stays SL-first in both modes.
4. Entry-bar TP (taker entry): touch-only bar doesn't fill in limit mode;
   penetration fills at target with maker fee.

Run:  ./.venv/Scripts/python.exe test_tp_limit.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.backtest_enhanced import EnhancedBTConfig, run_backtest_enhanced

FEE_T, FEE_M, SLIP = 0.0006, 0.0002, 2.0


def frame(specials: dict[int, tuple]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """30 flat bars (o=c=100, h=100.5, l=99.5); specials: i -> (o,h,l,c)."""
    idx = pd.date_range("2025-01-01", periods=30, freq="4h", tz="UTC")
    df = pd.DataFrame({"open": 100.0, "high": 100.5, "low": 99.5,
                       "close": 100.0, "volume": 1.0}, index=idx)
    for i, (o, h, l, c) in specials.items():
        df.iloc[i, :4] = [o, h, l, c]
    sig = pd.DataFrame({"signal": 0, "sl": np.nan, "tp": np.nan}, index=idx)
    sig.iloc[25, 0] = 1
    sig.iloc[25, 1] = 90.0
    sig.iloc[25, 2] = 110.0
    return df, sig


def cfg(tp_limit: bool) -> EnhancedBTConfig:
    return EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.02,
                            max_leverage=5.0, fee_rate=FEE_T, slip_bps=SLIP,
                            entry_style="taker", tp_as_limit=tp_limit)


def run(specials, tp_limit):
    df, sig = frame(specials)
    eq, trades = run_backtest_enhanced(df, sig, cfg(tp_limit))
    return df, trades


# 1+2: touch bar 27 (h == 110), penetration bar 28 (h = 111)
specials = {27: (100.0, 110.0, 100.0, 105.0), 28: (105.0, 111.0, 104.0, 110.0)}

df, tr = run(specials, tp_limit=False)
assert len(tr) == 1 and tr[0].reason == "tp", tr
assert tr[0].exit_time == df.index[27], "off: TP must fill on the touch bar"
exp_fill = 110.0 * (1 - SLIP / 10_000 * 1)
assert abs(tr[0].exit_px - exp_fill) < 1e-9, (tr[0].exit_px, exp_fill)
assert abs(tr[0].fees - tr[0].exit_px * tr[0].qty * FEE_T) < 1e-9
print("[1] off: touch fills, taker fee + slip                 OK")

df, tr = run(specials, tp_limit=True)
assert len(tr) == 1 and tr[0].reason == "tp", tr
assert tr[0].exit_time == df.index[28], "on: touch must NOT fill; penetration does"
assert abs(tr[0].exit_px - 110.0) < 1e-12, tr[0].exit_px
assert abs(tr[0].fees - 110.0 * tr[0].qty * FEE_M) < 1e-12
print("[2] on: touch skipped, penetration fills at target, maker fee   OK")

# 3: same-bar SL+TP collision -> SL first in both modes
specials_c = {27: (100.0, 111.0, 89.0, 100.0)}
for lim in (False, True):
    _, tr = run(specials_c, tp_limit=lim)
    assert len(tr) == 1 and tr[0].reason == "sl", (lim, tr)
    assert abs(tr[0].exit_px - 90.0 * (1 - SLIP / 10_000)) < 1e-9
print("[3] SL-first on collision unchanged in both modes      OK")

# 4: entry-bar TP. Touch-only entry bar: off exits same bar, on holds.
specials_e = {26: (100.0, 110.0, 99.5, 105.0)}
_, tr = run(specials_e, tp_limit=False)
assert tr and tr[0].reason == "tp" and tr[0].bars_held == 0
_, tr = run(specials_e, tp_limit=True)
assert not tr or tr[0].bars_held > 0, "on: entry-bar touch must not fill"
# Penetrating entry bar: on exits same bar at target with maker fee.
specials_e2 = {26: (100.0, 111.0, 99.5, 105.0)}
_, tr = run(specials_e2, tp_limit=True)
assert tr and tr[0].reason == "tp" and tr[0].bars_held == 0
assert abs(tr[0].exit_px - 110.0) < 1e-12
assert abs(tr[0].fees - 110.0 * tr[0].qty * FEE_M) < 1e-12
print("[4] entry-bar TP: strict penetration + maker fee       OK")

print("\nALL tp_as_limit TESTS PASSED")
