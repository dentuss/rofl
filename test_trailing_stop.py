"""Unit tests for the ATR trailing stop (EnhancedBTConfig.trail_atr).

Run:  ./.venv/Scripts/python.exe test_trailing_stop.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.backtest_enhanced import EnhancedBTConfig, run_backtest_enhanced


def _df(n=40):
    idx = pd.date_range("2025-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"open": 100.0, "high": 100.4, "low": 99.6,
                         "close": 100.0, "volume": 1.0}, index=idx)


def _sig(df, from_bar=20, sl=90.0, tp=1e6):
    s = pd.DataFrame({"signal": 0, "sl": np.nan, "tp": np.nan}, index=df.index)
    s.iloc[from_bar:, s.columns.get_loc("signal")] = 1
    s.iloc[from_bar:, s.columns.get_loc("sl")] = sl
    s.iloc[from_bar:, s.columns.get_loc("tp")] = tp
    return s


def test_trail_locks_in_profit():
    # Entry bar 21 @100; ramp to ~120 by bar 30; crash at bar 31. With a
    # 2.5-ATR trail the stop rides up and the crash exits far above entry;
    # without it, the crash exits at the original 90 stop.
    df = _df(45)
    for j, b in enumerate(range(21, 31)):          # closes 102..120
        c = 100.0 + 2.0 * (j + 1)
        df.iloc[b] = [c - 1.0, c + 0.4, c - 1.4, c, 1.0]
    df.iloc[31] = [118.0, 118.0, 80.0, 82.0, 1.0]  # crash bar
    sig = _sig(df)
    cfg = EnhancedBTConfig(trail_atr=2.5)
    _, tr = run_backtest_enhanced(df, sig, cfg)
    assert tr and tr[0].reason == "sl"
    assert tr[0].exit_px > 110, f"trail should lock profit, exited {tr[0].exit_px}"
    _, tr0 = run_backtest_enhanced(df, sig, EnhancedBTConfig())
    assert tr0[0].exit_px < 91, "without trail the crash should hit the 90 stop"


def test_trail_no_lookahead_same_bar():
    # Bar 25 dips to 99 then closes at 110. The stop entering bar 25 is based
    # on bar 24's close (~98 with a 2.5-ATR trail on a flat tape) — the dip
    # must NOT be stopped by the trail level implied by bar 25's OWN close
    # (~108). Exit becomes possible from bar 26 on.
    df = _df(45)
    df.iloc[25] = [100.0, 111.0, 99.0, 110.0, 1.0]
    df.iloc[26] = [110.0, 110.5, 99.0, 100.0, 1.0]
    sig = _sig(df)
    cfg = EnhancedBTConfig(trail_atr=2.5)
    _, tr = run_backtest_enhanced(df, sig, cfg)
    assert tr and tr[0].reason == "sl"
    assert tr[0].exit_time == df.index[26], \
        f"exited {tr[0].exit_time}, want bar 26 (bar-25 exit = look-ahead)"
    assert tr[0].exit_px > 105   # filled at the trailed stop, not the old 90


if __name__ == "__main__":
    test_trail_locks_in_profit()
    print("PASS: trail ratchets the stop and locks in profit")
    test_trail_no_lookahead_same_bar()
    print("PASS: trail from a bar's close is only testable on the next bar")
    print("ALL TRAILING-STOP TESTS PASSED")
