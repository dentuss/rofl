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


if __name__ == "__main__":
    test_flat_exchange_does_not_reopen()
    test_open_exchange_gets_flattened()
    test_short_state_flat_exchange_does_not_reopen_long()
    test_external_close_books_real_fill()
    test_external_close_falls_back_when_history_unavailable()
    print("ALL REDUCE-ONLY REGRESSION TESTS PASSED")
