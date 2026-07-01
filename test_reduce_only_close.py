"""Regression test for the reduce-only close fix.

Bug (found live 2026-06-22): close_position() placed plain, NON-reduce-only
market orders to flatten. When the exchange had already closed the position
(its attached SL/TP fired autonomously), the "close" order instead OPENED a
brand-new reversed, UNPROTECTED position — observed live as an ETH short with
no SL/TP after the ETH long stopped out.

This test fakes the ccxt client and reproduces the scenario:
  * state holds a LONG, exchange is already FLAT  -> must NOT re-open a short
  * state holds a LONG, exchange still holds it   -> must flatten via reduceOnly

It deliberately avoids sklearn / network so it runs anywhere pandas is present.
Run:  python test_reduce_only_close.py
"""
import os
import tempfile
import time

import pandas as pd

# BotConfig reads env as dataclass defaults at import time, so set before import.
_TMP = tempfile.mkdtemp(prefix="rofl_ro_test_")
os.environ.update({
    "MODE": "live",
    "EXCHANGE": "bybit",
    "STRATEGY_PRESET": "adaptive_bidir",   # triple_bidir, allow_short=True
    "STATE_FILE": os.path.join(_TMP, "state.json"),
    "LOG_FILE": os.path.join(_TMP, "bot.log"),
    "STARTING_EQUITY": "100",
})

import bot as botmod  # noqa: E402


class _Noop:
    """Swallow events/notifier side effects during the test."""
    def __getattr__(self, _name):
        return lambda *a, **k: None


class FakeCcxt:
    """Minimal stand-in for the ccxt client, modelling Bybit's reduce-only rule:
    a reduce-only order is rejected when there is nothing to reduce (flat book)
    or when it would add to / flip the position; otherwise it reduces and never
    flips past zero. A plain order just adds to the net position."""

    def __init__(self, net_position=0.0):
        self.position = float(net_position)
        self.orders = []  # every attempt recorded, even rejected ones
        self.markets = {"_": 1}      # truthy so _load_market skips load_markets
        self.closed_fill = None      # set to a closed-PnL row to exercise real-fill booking

    def _order(self, side, symbol, qty, params):
        reduce_only = bool((params or {}).get("reduceOnly"))
        self.orders.append({"side": side, "qty": qty, "reduceOnly": reduce_only})
        signed = qty if side == "buy" else -qty
        if reduce_only:
            same_side = (signed > 0) == (self.position > 0)
            if abs(self.position) < 1e-12 or same_side:
                raise Exception("110017: reduce-only order rejected (nothing to reduce)")
            new = self.position + signed
            if new != 0 and (new > 0) != (self.position > 0):
                new = 0.0  # reduce-only caps at flat, never flips
            self.position = new
        else:
            self.position += signed
        return {"id": f"fake-{len(self.orders)}", "price": 100.0,
                "amount": qty, "filled": qty}

    def create_market_buy_order(self, symbol, qty, params=None):
        return self._order("buy", symbol, qty, params)

    def create_market_sell_order(self, symbol, qty, params=None):
        return self._order("sell", symbol, qty, params)

    def fetch_ticker(self, symbol):
        return {"last": 100.0}

    def fetch_positions(self, symbols, params=None):
        sym = symbols[0]
        if abs(self.position) < 1e-12:
            return [{"symbol": sym, "contracts": 0, "side": ""}]
        return [{"symbol": sym, "contracts": abs(self.position),
                 "side": "long" if self.position > 0 else "short"}]

    def market(self, symbol):
        return {"id": "ETHUSDT", "limits": {}}

    def private_get_v5_position_closed_pnl(self, params=None):
        return {"result": {"list": [self.closed_fill] if self.closed_fill else []}}


def _make_bot(net_on_exchange):
    b = botmod.Bot(botmod.BotConfig())
    assert b.cfg.mode == "live", "test must run in live mode to exercise the path"
    b.ex._ccxt = FakeCcxt(net_on_exchange)
    b.ex.paper = False
    b.events = _Noop()
    b.notifier = _Noop()
    return b, b.ex._ccxt


def test_flat_exchange_does_not_reopen():
    """The bug: state=LONG, exchange already FLAT (autonomous SL fired)."""
    b, fake = _make_bot(net_on_exchange=0.0)
    b.state.position = botmod.Position(side=1, qty=0.04, entry_px=1732.91,
                                       sl=1710.50, tp=1771.09, open_ts=0,
                                       notional=69.0)
    b.close_position(exit_px_hint=1710.50, reason="sl-external")
    assert abs(fake.position) < 1e-9, f"REOPENED a position! net={fake.position}"
    assert fake.orders, "no close attempt was made"
    assert all(o["reduceOnly"] for o in fake.orders), \
        f"close order was not reduce-only: {fake.orders}"
    assert b.state.position is None, "state position should be booked closed"
    print("PASS: flat exchange -> no reopen, reduce-only used, close booked")


def test_open_exchange_gets_flattened():
    """Normal close: state=LONG, exchange still holds it -> reduce to flat."""
    b, fake = _make_bot(net_on_exchange=0.04)
    b.state.position = botmod.Position(side=1, qty=0.04, entry_px=1732.91,
                                       sl=1710.50, tp=1771.09, open_ts=0,
                                       notional=69.0)
    b.close_position(exit_px_hint=1771.09, reason="tp")
    assert abs(fake.position) < 1e-9, f"did not flatten; net={fake.position}"
    assert fake.orders[0]["reduceOnly"] is True, "close must be reduce-only"
    assert b.state.position is None
    print("PASS: open exchange -> flattened via reduce-only")


def test_short_state_flat_exchange_does_not_reopen_long():
    """Mirror case for a SHORT in state vs a flat exchange."""
    b, fake = _make_bot(net_on_exchange=0.0)
    b.state.position = botmod.Position(side=-1, qty=10.0, entry_px=4.85,
                                       sl=4.98, tp=4.66, open_ts=0, notional=48.5)
    b.close_position(exit_px_hint=4.98, reason="sl-external")
    assert abs(fake.position) < 1e-9, f"REOPENED a long! net={fake.position}"
    assert all(o["reduceOnly"] for o in fake.orders), fake.orders
    assert b.state.position is None
    print("PASS: short state + flat exchange -> no reopen")


def test_external_close_books_real_fill():
    """Live autonomous close: when the exchange's SL fired, the close must be
    booked at the REAL fill from closed-PnL history, NOT the theoretical SL
    price (a market stop slips past its trigger). This is the fix for the
    booked-vs-exchange equity gap."""
    b, fake = _make_bot(net_on_exchange=0.0)
    # Theoretical SL is 1710.50, but the stop really filled WORSE, at 1705.20.
    fake.closed_fill = {"qty": "0.04", "avgExitPrice": "1705.20",
                        "closedPnl": "-1.11", "updatedTime": "1000"}
    b.state.position = botmod.Position(side=1, qty=0.04, entry_px=1732.91,
                                       sl=1710.50, tp=1771.09, open_ts=1,
                                       notional=69.32)
    eq0 = b.state.equity
    b.close_position(exit_px_hint=1710.50, reason="sl-external")
    booked = b.state.equity - eq0
    real_gross = (1705.20 - 1732.91) * 0.04          # -1.1084  (real fill)
    theo_gross = (1710.50 - 1732.91) * 0.04          # -0.8964  (theoretical stop)
    assert abs(booked - real_gross) < 0.15, \
        f"expected booking near real fill ({real_gross:.3f}), got {booked:.3f}"
    assert booked < theo_gross - 0.1, \
        f"booked {booked:.3f} not meaningfully worse than theoretical {theo_gross:.3f}"
    assert abs(fake.position) < 1e-9, "must not reopen"
    assert b.state.position is None
    print(f"PASS: external close booked at REAL fill (pnl {booked:.3f}, "
          f"theoretical would book ~{theo_gross:.3f})")


def test_external_close_falls_back_when_history_unavailable():
    """If closed-PnL history is unavailable (closed_fill=None), booking must
    fall back to the theoretical hint — never crash, never reopen."""
    b, fake = _make_bot(net_on_exchange=0.0)  # closed_fill stays None
    b.state.position = botmod.Position(side=1, qty=0.04, entry_px=1732.91,
                                       sl=1710.50, tp=1771.09, open_ts=1,
                                       notional=69.32)
    eq0 = b.state.equity
    b.close_position(exit_px_hint=1710.50, reason="sl-external")
    booked = b.state.equity - eq0
    theo_gross = (1710.50 - 1732.91) * 0.04
    assert abs(booked - theo_gross) < 0.15, \
        f"fallback should book ~theoretical ({theo_gross:.3f}), got {booked:.3f}"
    assert b.state.position is None
    print("PASS: no closed-PnL -> falls back to theoretical price, no crash")


def test_resume_close_arms_cooldown_from_fill_ts():
    """Fix #1: an SL that fired while the bot was DOWN must arm the same-side
    re-entry cooldown on resume, keyed on the REAL fill's timestamp (not skipped
    as before). A recent fill => cooldown still active."""
    b, fake = _make_bot(net_on_exchange=0.0)
    assert b.cfg.cooldown_bars > 0, "cooldown must be on for this test"
    bar_sec = botmod.Exchange.TF_SECONDS.get(b.cfg.timeframe, 3600)
    now = int(time.time())
    updated_ms = (now - bar_sec) * 1000            # stop filled ~1 bar ago
    fake.closed_fill = {"qty": "0.04", "avgExitPrice": "1705.20",
                        "closedPnl": "-1.11", "updatedTime": str(updated_ms)}
    b.state.position = botmod.Position(side=1, qty=0.04, entry_px=1732.91,
                                       sl=1710.50, tp=1771.09, open_ts=1, notional=69.32)
    booked = b._book_resume_autonomous_close(b.state.position)
    assert booked is True, "should have booked the real close"
    assert b.state.position is None, "position must be cleared"
    snap = (updated_ms // 1000) // bar_sec * bar_sec
    expected = snap + b.cfg.cooldown_bars * bar_sec
    assert b.state.block_long_until_ts == expected, \
        f"cooldown until {b.state.block_long_until_ts} != expected {expected}"
    assert b.state.block_long_until_ts > now, "recent stop => cooldown still active"
    print("PASS: resume books real close AND arms same-side cooldown from fill ts")


def test_resume_stale_fill_expires_cooldown():
    """Fix #1 corollary: a stop that fired long ago yields an already-expired
    cooldown (blocks nothing) — the correct behaviour the old None-bar_ts path
    approximated but couldn't distinguish from a fresh stop."""
    b, fake = _make_bot(net_on_exchange=0.0)
    now = int(time.time())
    updated_ms = (now - 40 * 86400) * 1000          # 40 days ago
    fake.closed_fill = {"qty": "0.04", "avgExitPrice": "1705.20",
                        "closedPnl": "-1.11", "updatedTime": str(updated_ms)}
    b.state.position = botmod.Position(side=1, qty=0.04, entry_px=1732.91,
                                       sl=1710.50, tp=1771.09, open_ts=1, notional=69.32)
    assert b._book_resume_autonomous_close(b.state.position) is True
    assert b.state.block_long_until_ts < now, "stale stop must not block re-entry"
    print("PASS: stale resume stop leaves cooldown expired (no spurious block)")


def test_ambiguous_external_upgraded_to_sl_arms_cooldown():
    """Fix #2: reconcile finds the exchange flat but the fallback bar's H/L does
    NOT confirm the SL touch (spot-vs-perp wick). If the real closed-PnL fill
    landed nearer the stop, classify as sl-external so the cooldown still arms."""
    b, fake = _make_bot(net_on_exchange=0.0)
    bar_sec = botmod.Exchange.TF_SECONDS.get(b.cfg.timeframe, 3600)
    bar_ts = int(time.time()) // bar_sec * bar_sec
    # Real fill (1709.00) is right at the stop (1710.50), far from TP (1771.09).
    fake.closed_fill = {"qty": "0.04", "avgExitPrice": "1709.00",
                        "closedPnl": "-0.96", "updatedTime": str(bar_ts * 1000)}
    b.state.position = botmod.Position(side=1, qty=0.04, entry_px=1732.91,
                                       sl=1710.50, tp=1771.09, open_ts=1, notional=69.32)
    # Bar H/L straddle neither SL nor TP -> confirmed branch would say "external".
    last_bar = pd.Series({"high": 1745.0, "low": 1735.0, "close": 1740.0})
    acted = b._reconcile_position_with_exchange(last_bar, bar_ts=bar_ts)
    assert acted is True and b.state.position is None
    assert b.state.block_long_until_ts == bar_ts + b.cfg.cooldown_bars * bar_sec, \
        "ambiguous close near the stop must arm the same-side cooldown"
    print("PASS: ambiguous external near stop -> sl-external -> cooldown armed")


def test_bot_initiated_close_books_real_fill():
    """Fix #3: a bot-initiated live close (exchange still holds it, reduce-only
    succeeds) must book at the REAL fill from closed-PnL history, not the
    bar-close hint or the ccxt order's placeholder price."""
    b, fake = _make_bot(net_on_exchange=0.04)     # exchange still holds -> order fills
    fake.closed_fill = {"qty": "0.04", "avgExitPrice": "1770.00",
                        "closedPnl": "1.40", "updatedTime": str(int(time.time()) * 1000)}
    b.state.position = botmod.Position(side=1, qty=0.04, entry_px=1732.91,
                                       sl=1710.50, tp=1771.09, open_ts=1, notional=69.32)
    eq0 = b.state.equity
    b.close_position(exit_px_hint=1750.0, reason="time")   # hint deliberately != real fill
    booked = b.state.equity - eq0
    real_gross = (1770.00 - 1732.91) * 0.04       # +1.4836 at the real fill
    hint_gross = (1750.00 - 1732.91) * 0.04       # +0.6836 at the bar-close hint
    assert abs(fake.position) < 1e-9, "must flatten via reduce-only"
    assert booked > (hint_gross + real_gross) / 2, \
        f"booked {booked:.3f} should track real fill (~{real_gross:.3f}), not hint (~{hint_gross:.3f})"
    assert b.state.position is None
    print(f"PASS: bot-initiated close booked at REAL fill (pnl {booked:.3f}, "
          f"hint would book ~{hint_gross:.3f})")


if __name__ == "__main__":
    test_flat_exchange_does_not_reopen()
    test_open_exchange_gets_flattened()
    test_short_state_flat_exchange_does_not_reopen_long()
    test_external_close_books_real_fill()
    test_external_close_falls_back_when_history_unavailable()
    test_resume_close_arms_cooldown_from_fill_ts()
    test_resume_stale_fill_expires_cooldown()
    test_ambiguous_external_upgraded_to_sl_arms_cooldown()
    test_bot_initiated_close_books_real_fill()
    print("ALL REDUCE-ONLY REGRESSION TESTS PASSED")
