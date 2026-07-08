"""Unit tests for the cost-engine realism knobs: entry_bar_exit_check
(default ON) and maker_close entries.

Run:  ./.venv/Scripts/python.exe test_cost_engine_knobs.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.backtest import BTConfig, run_backtest
from core.backtest_enhanced import EnhancedBTConfig, run_backtest_enhanced


def _flat_df(n=40):
    idx = pd.date_range("2025-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"open": 100.0, "high": 100.4, "low": 99.6,
                         "close": 100.0, "volume": 1.0}, index=idx)


def _sig(df, from_bar=20, sl=90.0, tp=102.0):
    s = pd.DataFrame({"signal": 0, "sl": np.nan, "tp": np.nan}, index=df.index)
    s.iloc[from_bar:, s.columns.get_loc("signal")] = 1
    s.iloc[from_bar:, s.columns.get_loc("sl")] = sl
    s.iloc[from_bar:, s.columns.get_loc("tp")] = tp
    return s


# --- entry-bar exit check ------------------------------------------------------
# Signal from bar 20 -> entry at bar 21 open (100). Bar 21's own range can now
# hit the stop/target: no more one-bar grace period.

def test_entry_bar_sl_is_debited_and_arms_cooldown():
    df = _flat_df()
    df.iloc[21, df.columns.get_loc("low")] = 85.0     # crashes through sl=90
    sig = _sig(df)
    cfg = EnhancedBTConfig(cooldown_bars=3)
    _, tr = run_backtest_enhanced(df, sig, cfg)
    assert tr[0].reason == "sl" and tr[0].bars_held == 0
    assert tr[0].exit_time == df.index[21]
    # cooldown armed from the entry bar: blocked 21+3=24 -> re-entry at 24
    assert tr[1].entry_time == df.index[24]
    # legacy behavior (check off): the bar-21 crash is invisible, position
    # survives on the flat tape until the end of data
    cfg_off = EnhancedBTConfig(cooldown_bars=3, entry_bar_exit_check=False)
    _, tr_off = run_backtest_enhanced(df, sig, cfg_off)
    assert tr_off[0].reason == "eod"


def test_entry_bar_tp_is_credited_for_taker():
    df = _flat_df()
    df.iloc[21, df.columns.get_loc("high")] = 103.0   # through tp=102
    _, tr = run_backtest_enhanced(df, _sig(df), EnhancedBTConfig(cooldown_bars=3))
    assert tr[0].reason == "tp" and tr[0].bars_held == 0
    # SL-first when both hit on the entry bar (conservative)
    df2 = _flat_df()
    df2.iloc[21, df2.columns.get_loc("high")] = 103.0
    df2.iloc[21, df2.columns.get_loc("low")] = 85.0
    _, tr2 = run_backtest_enhanced(df2, _sig(df2), EnhancedBTConfig(cooldown_bars=3))
    assert tr2[0].reason == "sl" and tr2[0].bars_held == 0


def test_entry_bar_check_core_engine_matches():
    df = _flat_df()
    df.iloc[21, df.columns.get_loc("low")] = 85.0
    res = run_backtest(df, _sig(df), BTConfig(cooldown_bars=3))
    assert res.trades[0].reason == "sl" and res.trades[0].bars_held == 0
    assert res.trades[1].entry_time == df.index[24]


# --- maker_close entries --------------------------------------------------------

def test_maker_fills_at_limit_without_slippage():
    # limit = signal bar's close (100); bar 21 low 99.6 < 100 -> filled AT 100
    df = _flat_df()
    sig = _sig(df, tp=1e6)
    cfg = EnhancedBTConfig(entry_style="maker_close")
    _, tr = run_backtest_enhanced(df, sig, cfg)
    assert tr and tr[0].entry_px == 100.0            # no slip
    # taker reference pays slippage: entry strictly above 100
    _, tr_t = run_backtest_enhanced(df, sig, EnhancedBTConfig())
    assert tr_t[0].entry_px > 100.0
    # maker fee < taker fee on the same notional (compare entry-fee impact
    # via equity after the first entry is not directly exposed; use fees on a
    # same-bar SL round trip instead)


def test_maker_misses_runaway_entries():
    # steadily rising tape: every bar's low stays above the previous close ->
    # a long limit at close_prev never fills -> zero trades (adverse selection
    # modeled honestly: maker misses the runners)
    n = 60
    idx = pd.date_range("2025-01-01", periods=n, freq="1h", tz="UTC")
    op = 100 + 0.5 * np.arange(n)
    df = pd.DataFrame({"open": op, "high": op + 0.45, "low": op - 0.1,
                       "close": op + 0.3, "volume": 1.0}, index=idx)
    sig = pd.DataFrame({"signal": 1, "sl": 50.0, "tp": 1e6}, index=idx)
    _, tr = run_backtest_enhanced(df, sig,
                                  EnhancedBTConfig(entry_style="maker_close"))
    assert not tr, f"maker limit should never fill on a runaway tape, got {len(tr)}"


def test_maker_never_credits_same_bar_tp_but_debits_same_bar_sl():
    # fill bar also prints through the TP: order of events unknowable, so the
    # maker entry must NOT book the same-bar TP (position stays open)
    df = _flat_df()
    df.iloc[21, df.columns.get_loc("low")] = 99.0     # fills limit 100
    df.iloc[21, df.columns.get_loc("high")] = 103.0   # through tp=102 — ignored
    _, tr = run_backtest_enhanced(df, _sig(df),
                                  EnhancedBTConfig(entry_style="maker_close"))
    assert tr[0].reason == "eod"                      # held, not TP'd
    # ...but a same-bar SL is debited (limit sits above the stop)
    df2 = _flat_df()
    df2.iloc[21, df2.columns.get_loc("low")] = 85.0   # fills 100 then breaks 90
    _, tr2 = run_backtest_enhanced(df2, _sig(df2),
                                   EnhancedBTConfig(entry_style="maker_close",
                                                    cooldown_bars=3))
    assert tr2[0].reason == "sl" and tr2[0].bars_held == 0
    assert tr2[0].entry_px == 100.0


if __name__ == "__main__":
    test_entry_bar_sl_is_debited_and_arms_cooldown()
    print("PASS: entry-bar SL debited, cooldown armed, legacy flag preserves old behavior")
    test_entry_bar_tp_is_credited_for_taker()
    print("PASS: entry-bar TP credited for taker; SL-first when both hit")
    test_entry_bar_check_core_engine_matches()
    print("PASS: core engine entry-bar check matches enhanced")
    test_maker_fills_at_limit_without_slippage()
    print("PASS: maker fills at the limit, no slippage")
    test_maker_misses_runaway_entries()
    print("PASS: maker misses runaway entries (adverse selection modeled)")
    test_maker_never_credits_same_bar_tp_but_debits_same_bar_sl()
    print("PASS: maker same-bar TP suppressed, same-bar SL debited")
    print("ALL COST-ENGINE KNOB TESTS PASSED")
