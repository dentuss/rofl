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


if __name__ == "__main__":
    test_chop_mult()
    print("PASS: CHOP_RISK_MULT scales risk only in CHOP; default 1.0 is a no-op")
    print("ALL CHOP-SIZING TESTS PASSED")
