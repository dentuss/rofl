"""Unit test: CHOP_RISK_MULT scales _effective_risk only in CHOP, default off.

Run:  ./.venv/Scripts/python.exe test_chop_sizing.py
"""
from __future__ import annotations

import logging
import os
import tempfile

os.environ.setdefault("MODE", "paper")

import bot as botmod  # noqa: E402


def _bot(chop_mult=1.0):
    tmp = tempfile.mkdtemp(prefix="rofl_chop_")
    cfg = botmod.BotConfig()
    cfg.mode = "paper"
    cfg.preset = "adaptive_bidir_4h"
    cfg.state_file = os.path.join(tmp, "state.json")
    cfg.log_file = os.path.join(tmp, "bot.log")
    # decay is isolated by keeping equity == equity_peak (dd=0 -> scale 1.0)
    cfg.chop_risk_mult = chop_mult
    b = botmod.Bot(cfg)
    b.log.setLevel(logging.CRITICAL)
    b.state.equity = 100.0
    b.state.equity_peak = 100.0
    return b


def test_chop_mult():
    b = _bot(chop_mult=0.5)
    base = b.cfg.risk_per_trade
    b._last_regime = "BULL"
    assert abs(b._effective_risk() - base) < 1e-12, "BULL must be full size"
    b._last_regime = "CHOP"
    assert abs(b._effective_risk() - base * 0.5) < 1e-12, "CHOP must halve"
    b._last_regime = None
    assert abs(b._effective_risk() - base) < 1e-12, "unknown regime = full size"

    b1 = _bot(chop_mult=1.0)         # default off: CHOP changes nothing
    b1._last_regime = "CHOP"
    assert abs(b1._effective_risk() - b1.cfg.risk_per_trade) < 1e-12, \
        "default 1.0 must be a no-op"


def test_vol_target_mult():
    import numpy as np
    import pandas as pd
    idx = pd.date_range("2025-01-01", periods=60 * 6, freq="4h", tz="UTC")
    # ~constant daily vol series: alternate +2%/-2% daily moves -> ann vol ~38%
    daily_ret = np.where(np.arange(61) % 2 == 0, 0.02, -0.02)
    daily_px = 100 * np.cumprod(1 + daily_ret)
    px = pd.Series(np.repeat(daily_px[:60], 6)[: len(idx)], index=idx)
    m = botmod.vol_target_mult(px, target_ann=0.60)
    vol = float(pd.Series(daily_px).pct_change().tail(30).std()) * (365 ** 0.5)
    assert abs(m - min(max(0.60 / vol, 0.5), 1.5)) < 0.05, (m, vol)
    # not enough history -> neutral
    assert botmod.vol_target_mult(px.iloc[: 6 * 10], 0.60) == 1.0
    # risk path: _effective_risk applies the stashed multiplier
    b = _bot(chop_mult=1.0)
    b.cfg.vol_target_ann = 0.60
    b._vt_mult = 0.7
    b._last_regime = "BULL"
    assert abs(b._effective_risk() - b.cfg.risk_per_trade * 0.7) < 1e-12
    b.cfg.vol_target_ann = 0.0               # off -> multiplier ignored
    assert abs(b._effective_risk() - b.cfg.risk_per_trade) < 1e-12


if __name__ == "__main__":
    test_chop_mult()
    print("PASS: CHOP_RISK_MULT scales risk only in CHOP; default 1.0 is a no-op")
    test_vol_target_mult()
    print("PASS: vol_target_mult parity math; VOL_TARGET_ANN gates the risk path")
    print("ALL CHOP/VT SIZING TESTS PASSED")
