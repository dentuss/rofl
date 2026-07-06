"""Unit tests for ENTRY_LIMIT_ORDERS (post-only maker entries).

Engine parity contract (entry_style="maker_close", the adopted cost model):
- limit at the SIGNAL bar's close, sized at that price
- rests for exactly ONE bar; unfilled -> cancelled (miss is missed)
- fill adopted promptly; attached SL/TP live from the first contract
- partial fills are closed, not adopted (v1 policy)
- the bar-based TP check is suppressed on the fill bar (maker never credits
  a same-bar TP; the exchange TP order is the source of truth)

All exchange interaction stubbed — no network.

Run:  ./.venv/Scripts/python.exe test_maker_entries.py
"""
from __future__ import annotations

import logging
import os
import tempfile

os.environ.setdefault("MODE", "paper")

import pandas as pd  # noqa: E402

import bot as botmod  # noqa: E402


class _Noop:
    def __getattr__(self, _n):
        return lambda *a, **k: None


class StubEx:
    """Scriptable exchange: queue of fetch_order_status results."""

    def __init__(self):
        self.paper = False
        self.placed = []            # (side, qty, price, sl, tp)
        self.cancelled = []
        self.closes = []            # (side_str, qty, reduce_only)
        self.status_queue = []
        self.raise_on_place = False
        self.cancel_all_called = 0

    def fetch_price(self):
        return 100.0

    def normalize_order(self, qty, entry_px):
        return qty, None

    def limit_entry(self, side, qty, price, sl, tp):
        if self.raise_on_place:
            raise RuntimeError("postonly would take liquidity")
        self.placed.append((side, qty, price, sl, tp))
        return {"id": f"ord-{len(self.placed)}"}

    def fetch_order_status(self, order_id):
        if self.status_queue:
            return self.status_queue.pop(0)
        return {"status": "open", "filled": 0.0, "avg_px": None}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True

    def cancel_all(self):
        self.cancel_all_called += 1

    def fetch_position_size(self):
        return 0.0

    def market_buy(self, qty, sl=None, tp=None, reduce_only=False):
        self.closes.append(("buy", qty, reduce_only))
        return {"id": "close", "price": 100.0, "amount": qty}

    def market_sell(self, qty, sl=None, tp=None, reduce_only=False):
        self.closes.append(("sell", qty, reduce_only))
        return {"id": "close", "price": 100.0, "amount": qty}


def _bot():
    tmp = tempfile.mkdtemp(prefix="rofl_maker_")
    cfg = botmod.BotConfig()
    cfg.mode = "paper"
    cfg.preset = "adaptive_bidir_4h"
    cfg.state_file = os.path.join(tmp, "state.json")
    cfg.log_file = os.path.join(tmp, "bot.log")
    b = botmod.Bot(cfg)
    b.log.setLevel(logging.CRITICAL)
    b.state.equity = 100.0
    b.state.equity_peak = 100.0
    b.events = _Noop()
    b.notifier = _Noop()
    b.cfg.mode = "live"                 # exercise the live maker path...
    b.cfg.entry_limit_orders = True
    b.ex = StubEx()                     # ...against the stub, never a network
    b._effective_risk = lambda: b.cfg.risk_per_trade
    return b


SIG = {"signal": 1, "sl": 90.0, "tp": 110.0, "close": 100.0}

# 1) placement: post-only limit at the signal close, sized at that price
b = _bot()
b.enter_position(dict(SIG), bar_ts=1000)
assert b.state.position is None and b.state.pending_entry is not None
side, qty, price, sl, tp = b.ex.placed[0]
assert side == 1 and price == 100.0 and sl == 90.0 and tp == 110.0
assert abs(qty - 0.2) < 1e-9, qty            # 2% x 100 / 10% stop / $100
assert b.state.pending_entry["signal_bar_ts"] == 1000
print("[1] placement at signal close, engine-parity sizing    OK")

# 2) no stacking while resting
b.enter_position(dict(SIG), bar_ts=1000)
assert len(b.ex.placed) == 1
print("[2] second signal while resting places nothing         OK")

# 3) mid-bar fill -> adopted promptly, maker_entry flagged
b.ex.status_queue = [{"status": "closed", "filled": 0.2, "avg_px": 100.0}]
b._check_pending_entry(rollover=False)
pos = b.state.position
assert pos is not None and pos.maker_entry and pos.entry_px == 100.0
assert b.state.pending_entry is None
print("[3] fill adopted mid-bar, maker_entry=True             OK")

# 4) rollover with no fill -> cancel, miss is missed
b = _bot()
b.enter_position(dict(SIG), bar_ts=1000)
b.ex.status_queue = [{"status": "open", "filled": 0.0, "avg_px": None},
                     {"status": "canceled", "filled": 0.0, "avg_px": None}]
b._check_pending_entry(rollover=True)
assert b.state.position is None and b.state.pending_entry is None
assert b.ex.cancelled, "must cancel the resting order at rollover"
print("[4] one-bar rest: unfilled -> cancelled                OK")

# 5) cancel/fill race at rollover -> the fill wins
b = _bot()
b.enter_position(dict(SIG), bar_ts=1000)
b.ex.status_queue = [{"status": "open", "filled": 0.0, "avg_px": None},
                     {"status": "closed", "filled": 0.2, "avg_px": 100.0}]
b._check_pending_entry(rollover=True)
assert b.state.position is not None and b.state.position.qty == 0.2
print("[5] cancel/fill race adopts the fill                   OK")

# 6) partial fill at rollover -> closed reduce-only, never adopted
b = _bot()
b.enter_position(dict(SIG), bar_ts=1000)
b.ex.status_queue = [{"status": "open", "filled": 0.08, "avg_px": 100.0},
                     {"status": "canceled", "filled": 0.08, "avg_px": 100.0}]
b._check_pending_entry(rollover=True)
assert b.state.position is None and b.state.pending_entry is None
assert ("sell", 0.08, True) in b.ex.closes, b.ex.closes
print("[6] partial closed reduce-only (v1 policy)             OK")

# 7) post-only reject at placement -> swept, skipped, no crash
b = _bot()
b.ex.raise_on_place = True
b.enter_position(dict(SIG), bar_ts=1000)
assert b.state.pending_entry is None and b.state.position is None
assert b.ex.cancel_all_called == 1
print("[7] placement reject: sweep + skip (retry next bar)    OK")

# 8) fill-bar TP suppression in check_exit (SL still honored)
b = _bot()
b.state.position = botmod.Position(side=1, qty=0.2, entry_px=100.0, sl=90.0,
                                   tp=110.0, open_ts=0, notional=20.0,
                                   bars_open=1, maker_entry=True)
bar = pd.Series({"high": 111.0, "low": 95.0, "close": 105.0})
assert b.check_exit(bar) is None, "fill-bar TP must be suppressed for maker"
bar_sl = pd.Series({"high": 111.0, "low": 89.0, "close": 95.0})
assert b.check_exit(bar_sl) == "sl", "fill-bar SL must still fire"
b.state.position.bars_open = 2
assert b.check_exit(bar) == "tp", "TP fires normally from the next bar"
b.state.position.maker_entry = False
b.state.position.bars_open = 1
assert b.check_exit(bar) == "tp", "taker entries keep the old behavior"
print("[8] fill-bar TP suppressed, SL honored, taker intact   OK")

# 9) pending entry survives a state save/load round-trip (restart safety)
b = _bot()
b.enter_position(dict(SIG), bar_ts=1000)
b.state.save(b.cfg.state_file)
st2 = botmod.State.load(b.cfg.state_file, 100.0)
assert st2.pending_entry == b.state.pending_entry
print("[9] pending entry survives restart (state round-trip)  OK")

print("\nALL MAKER-ENTRY TESTS PASSED")
