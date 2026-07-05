"""Unit tests for the post-TP re-entry cooldown knob (cooldown_bars_tp) and the
soft HTF risk bias (with_htf_risk_bias). Both are UNDER VALIDATION — these tests
pin engine semantics, not strategy quality.

Run:  ./.venv/Scripts/python.exe test_tp_cooldown_htf_bias.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.backtest import BTConfig, run_backtest
from core.backtest_enhanced import EnhancedBTConfig, run_backtest_enhanced
from core.strategies_enhanced import with_htf_risk_bias

H = pd.Timedelta(hours=1)


def _flat_df(n=40, tp_bar=None, sl_bar=None):
    """Flat tape: o=c=100, h=100.4, l=99.6. Optionally spike one bar's high
    through TP (102) or drop one bar's low through SL (90)."""
    idx = pd.date_range("2025-01-01", periods=n, freq="1h", tz="UTC")
    df = pd.DataFrame({"open": 100.0, "high": 100.4, "low": 99.6,
                       "close": 100.0, "volume": 1.0}, index=idx)
    if tp_bar is not None:
        df.iloc[tp_bar, df.columns.get_loc("high")] = 103.0
    if sl_bar is not None:
        df.iloc[sl_bar, df.columns.get_loc("low")] = 85.0
    return df


def _sig(df, signal=1, sl=90.0, tp=102.0, from_bar=20):
    s = pd.DataFrame({"signal": 0, "sl": np.nan, "tp": np.nan}, index=df.index)
    s.iloc[from_bar:, s.columns.get_loc("signal")] = signal
    s.iloc[from_bar:, s.columns.get_loc("sl")] = sl
    s.iloc[from_bar:, s.columns.get_loc("tp")] = tp
    return s


# --- cooldown_bars_tp + same-bar re-entry fix: enhanced engine ----------------
# Signal=1 from bar 20 -> entry at bar 21 open; bar 22 high=103 hits TP=102.
# FIXED engine (default): the exit bar itself never re-enters (its open
# predates the exit fill), so earliest re-entry is bar 23. K_tp blocks bars
# i..i+K-1 after exit bar i, so K_tp<=1 adds nothing beyond the fix and
# K_tp=2 blocks the first real re-entry bar. legacy_same_bar_reentry=True
# restores the pre-2026-07-05 same-bar artifact (re-entry at bar 22).

def test_tp_cooldown_blocks_reentry():
    df = _flat_df(tp_bar=22)
    sig = _sig(df)
    for k, reentry_bar in [(0, 23), (1, 23), (2, 24), (3, 25)]:
        cfg = EnhancedBTConfig(cooldown_bars=3, cooldown_bars_tp=k)
        _, tr = run_backtest_enhanced(df, sig, cfg)
        assert tr[0].reason == "tp", (k, tr[0].reason)
        assert len(tr) >= 2, k
        assert tr[1].entry_time == df.index[reentry_bar], \
            f"K_tp={k}: re-entry at {tr[1].entry_time}, want bar {reentry_bar}"


def test_legacy_flag_restores_same_bar_artifact():
    df = _flat_df(tp_bar=22)
    sig = _sig(df)
    cfg = EnhancedBTConfig(cooldown_bars=3, legacy_same_bar_reentry=True)
    _, tr = run_backtest_enhanced(df, sig, cfg)
    assert tr[0].reason == "tp" and tr[1].entry_time == df.index[22]
    # entry booked at the bar's open, BELOW the TP exit it follows — the
    # chronologically impossible fill the fix removes
    assert tr[1].entry_px < tr[0].exit_px


def test_signal_flip_exit_still_reverses_same_bar():
    # Flip exits fill AT the open, so a same-bar reversal is time-consistent
    # and must NOT be blocked by the fix.
    df = _flat_df()
    sig = _sig(df)
    sig.iloc[22:, sig.columns.get_loc("signal")] = -1
    sig.iloc[22:, sig.columns.get_loc("sl")] = 110.0
    sig.iloc[22:, sig.columns.get_loc("tp")] = 95.0
    _, tr = run_backtest_enhanced(df, sig, EnhancedBTConfig(cooldown_bars=3))
    assert tr[0].reason == "signal"
    assert tr[1].side == -1 and tr[1].entry_time == tr[0].exit_time


def test_tp_cooldown_default_off_and_sl_arming_unchanged():
    # Default config: knobs exist, tp cooldown off, fix on
    assert EnhancedBTConfig().cooldown_bars_tp == 0
    assert BTConfig().cooldown_bars_tp == 0
    assert EnhancedBTConfig().legacy_same_bar_reentry is False
    # SL exit still armed by cooldown_bars (=3): SL at bar 22 -> re-entry bar 25
    df = _flat_df(sl_bar=22)
    sig = _sig(df)
    cfg = EnhancedBTConfig(cooldown_bars=3, cooldown_bars_tp=0)
    _, tr = run_backtest_enhanced(df, sig, cfg)
    assert tr[0].reason == "sl"
    assert tr[1].entry_time == df.index[25]
    # TP exit with tp knob off arms no cooldown: re-entry on the NEXT bar
    # (the exit bar itself is excluded by the same-bar fix)
    df2 = _flat_df(tp_bar=22)
    _, tr2 = run_backtest_enhanced(df2, _sig(df2),
                                   EnhancedBTConfig(cooldown_bars=3))
    assert tr2[0].reason == "tp" and tr2[1].entry_time == df2.index[23]


def test_tp_cooldown_same_side_only():
    # Long TP at bar 22 with K_tp=2 blocks LONGS until bar 24, but a short
    # signal appearing at bar 22 enters normally at bar 23.
    df = _flat_df(tp_bar=22)
    sig = _sig(df)
    sig.iloc[22:, sig.columns.get_loc("signal")] = -1
    sig.iloc[22:, sig.columns.get_loc("sl")] = 110.0
    sig.iloc[22:, sig.columns.get_loc("tp")] = 95.0
    cfg = EnhancedBTConfig(cooldown_bars=3, cooldown_bars_tp=2)
    _, tr = run_backtest_enhanced(df, sig, cfg)
    assert tr[0].reason == "tp"
    assert tr[1].side == -1 and tr[1].entry_time == df.index[23]


def test_tp_cooldown_core_engine_matches():
    # Same knobs in the plain core engine (kept in sync with enhanced).
    df = _flat_df(tp_bar=22)
    sig = _sig(df)
    res0 = run_backtest(df, sig, BTConfig(cooldown_bars=3, cooldown_bars_tp=0))
    res2 = run_backtest(df, sig, BTConfig(cooldown_bars=3, cooldown_bars_tp=2))
    resL = run_backtest(df, sig, BTConfig(cooldown_bars=3,
                                          legacy_same_bar_reentry=True))
    assert res0.trades[1].entry_time == df.index[23]
    assert res2.trades[1].entry_time == df.index[24]
    assert resL.trades[1].entry_time == df.index[22]


# --- with_htf_risk_bias -------------------------------------------------------

def _trend_df(days_up=60, days_down=40):
    n = (days_up + days_down) * 24
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    up = np.linspace(100, 200, days_up * 24)
    down = np.linspace(200, 120, days_down * 24)
    px = np.concatenate([up, down])
    return pd.DataFrame({"open": px, "high": px * 1.001, "low": px * 0.999,
                         "close": px, "volume": 1.0}, index=idx)


def test_htf_bias_direction_and_warmup():
    df = _trend_df()
    sig = pd.DataFrame({"signal": 1, "sl": 1.0, "tp": 1e6}, index=df.index)
    out = with_htf_risk_bias(df, sig, htf_rule="1D", ema_n=5,
                             with_mult=1.0, counter_mult=0.5)
    # Warmup (EMA not formed): neutral 1.0
    assert (out["risk_mult"].iloc[: 4 * 24] == 1.0).all()
    # Deep in the uptrend: long is with-trend -> 1.0
    assert (out["risk_mult"].iloc[40 * 24: 60 * 24] == 1.0).all()
    # Deep in the downtrend (well after daily close < EMA): long counter -> 0.5
    assert (out["risk_mult"].iloc[-10 * 24:] == 0.5).all()
    # Mirror for shorts
    sig_s = pd.DataFrame({"signal": -1, "sl": 1e6, "tp": 1.0}, index=df.index)
    out_s = with_htf_risk_bias(df, sig_s, htf_rule="1D", ema_n=5,
                               with_mult=1.0, counter_mult=0.5)
    assert (out_s["risk_mult"].iloc[40 * 24: 60 * 24] == 0.5).all()
    assert (out_s["risk_mult"].iloc[-10 * 24:] == 1.0).all()


def test_htf_bias_composes_and_ignores_inprogress_bar():
    df = _trend_df()
    sig = pd.DataFrame({"signal": 1, "sl": 1.0, "tp": 1e6,
                        "risk_mult": 0.8}, index=df.index)
    out = with_htf_risk_bias(df, sig, htf_rule="1D", ema_n=5,
                             counter_mult=0.5)
    assert np.isclose(out["risk_mult"].iloc[-1], 0.8 * 0.5)
    # In-progress HTF bar unused: a price jump on the LAST day (whose daily
    # close would flip the trend) must not change any bias values, because
    # shift(1) only exposes it the following day.
    df2 = _trend_df()
    base = with_htf_risk_bias(
        df2, pd.DataFrame({"signal": 1, "sl": 1.0, "tp": 1e6}, index=df2.index),
        htf_rule="1D", ema_n=5, counter_mult=0.5)["risk_mult"]
    df3 = df2.copy()
    df3.iloc[-24:, df3.columns.get_loc("close")] = 500.0  # moon on last day
    jump = with_htf_risk_bias(
        df3, pd.DataFrame({"signal": 1, "sl": 1.0, "tp": 1e6}, index=df3.index),
        htf_rule="1D", ema_n=5, counter_mult=0.5)["risk_mult"]
    assert (base == jump).all()


def test_engine_consumes_risk_mult_sizing():
    # notional scales linearly with risk_mult (risk-based sizing, cap not hit)
    df = _flat_df(n=60)
    sig = _sig(df, from_bar=20)
    full = sig.copy(); full["risk_mult"] = 1.0
    half = sig.copy(); half["risk_mult"] = 0.5
    cfg = EnhancedBTConfig(risk_per_trade=0.02, max_leverage=5.0)
    _, tr_f = run_backtest_enhanced(df, full, cfg)
    _, tr_h = run_backtest_enhanced(df, half, cfg)
    assert tr_f and tr_h
    assert np.isclose(tr_h[0].notional, tr_f[0].notional * 0.5, rtol=1e-9)
    # risk_mult == 0 blocks the entry entirely (hard-gate degenerate case)
    zero = sig.copy(); zero["risk_mult"] = 0.0
    _, tr_z = run_backtest_enhanced(df, zero, cfg)
    assert not tr_z


if __name__ == "__main__":
    test_tp_cooldown_blocks_reentry()
    print("PASS: same-bar fix + TP cooldown block re-entry (K=0/1/2/3)")
    test_legacy_flag_restores_same_bar_artifact()
    print("PASS: legacy flag reproduces the pre-fix same-bar artifact")
    test_signal_flip_exit_still_reverses_same_bar()
    print("PASS: signal-flip exits still reverse same-bar (fill at open)")
    test_tp_cooldown_default_off_and_sl_arming_unchanged()
    print("PASS: knob defaults; SL arming unchanged; TP arms nothing at K=0")
    test_tp_cooldown_same_side_only()
    print("PASS: TP cooldown is same-side only")
    test_tp_cooldown_core_engine_matches()
    print("PASS: core engine knob matches enhanced engine")
    test_htf_bias_direction_and_warmup()
    print("PASS: HTF bias direction + warmup neutrality")
    test_htf_bias_composes_and_ignores_inprogress_bar()
    print("PASS: HTF bias composes with existing risk_mult; in-progress HTF bar unused")
    test_engine_consumes_risk_mult_sizing()
    print("PASS: engine sizes by risk_mult; 0 blocks entry")
    print("ALL TP-COOLDOWN / HTF-BIAS TESTS PASSED")
