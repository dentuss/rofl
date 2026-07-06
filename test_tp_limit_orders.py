"""Unit test: TP_LIMIT_ORDERS builds the Bybit V5 limit-TP attach params.

Checks (no network; _ccxt forced None so price/amount rounding pass through):
1. Paper mode: no attach params ever (paper fills are simulated).
2. Live path, flag OFF (default): stopLoss/takeProfit only — behavior
   identical to before the change.
3. Live path, flag ON: tpslMode=Partial, tpOrderType=Limit, tpLimitPrice,
   tpSize=slSize=qty — the exchange rests the TP as a maker limit while the
   SL stays a market conditional.
4. Flag ON but qty unknown: falls back to plain attach (no Partial keys).

Run:  ./.venv/Scripts/python.exe test_tp_limit_orders.py
"""
from __future__ import annotations

import logging
import os

os.environ.setdefault("MODE", "paper")

import bot as botmod  # noqa: E402


def _ex(tp_limit: bool):
    cfg = botmod.BotConfig()
    cfg.mode = "paper"
    cfg.preset = "adaptive_bidir_4h"
    cfg.tp_limit_orders = tp_limit
    ex = botmod.Exchange(cfg, logging.getLogger("tp_limit_test"))
    ex._ccxt = None          # no network: rounding passes through
    return ex


# 1) paper mode -> never attaches
ex = _ex(tp_limit=True)
assert ex._attached_sltp_params(90.0, 110.0, qty=0.5) == {}
print("[1] paper mode attaches nothing                        OK")

# 2) live path, flag off -> unchanged plain attach
ex = _ex(tp_limit=False)
ex.paper = False
p = ex._attached_sltp_params(90.0, 110.0, qty=0.5)
assert p == {"stopLoss": 90.0, "takeProfit": 110.0}, p
print("[2] flag off: plain stopLoss/takeProfit (unchanged)    OK")

# 3) live path, flag on -> Bybit V5 limit-TP params
ex = _ex(tp_limit=True)
ex.paper = False
p = ex._attached_sltp_params(90.0, 110.0, qty=0.5)
assert p["stopLoss"] == 90.0 and p["takeProfit"] == 110.0
assert p["tpslMode"] == "Partial" and p["tpOrderType"] == "Limit"
assert p["tpLimitPrice"] == 110.0
assert p["tpSize"] == 0.5 and p["slSize"] == 0.5
print("[3] flag on: Partial/Limit/tpLimitPrice/tpSize/slSize  OK")

# 4) flag on, qty unknown -> graceful plain attach
p = ex._attached_sltp_params(90.0, 110.0)
assert p == {"stopLoss": 90.0, "takeProfit": 110.0}, p
# SL-only attach never gains Partial keys either
p = ex._attached_sltp_params(90.0, None, qty=0.5)
assert p == {"stopLoss": 90.0}, p
print("[4] qty-unknown / SL-only fall back to plain attach    OK")

print("\nALL TP_LIMIT_ORDERS PARAM TESTS PASSED")
