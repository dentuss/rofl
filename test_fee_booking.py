"""Regression test for per-side maker/taker fee booking in close_position().

Bug (found 2026-08-03, research/FINDINGS.md): close_position() charged a flat
    fees = (notional + exit_notional) * 0.0006
i.e. FEE_TAKER on BOTH sides unconditionally. The deployed stack runs post-only
maker entries (ENTRY_LIMIT_ORDERS) and TP-as-limit exits (TP_LIMIT_ORDERS), so
a maker-in / TP-out round trip really costs ~4bp but was booked at 12bp. That
biases state.equity DOWN, and state.equity drives risk sizing, vol targeting
and the drawdown decay ladder — plus it would surface in L2's live-vs-engine
reconcile as an unexplained cost gap against the >0.2 Sh HALT criterion.

Cases (fees are asserted to the cent against research/cost_engine.py rates):
  1. taker in / taker out   (no maker flags)            -> 6bp + 6bp
  2. maker in / taker out   (ENTRY_LIMIT_ORDERS only)   -> 2bp + 6bp
  3. maker in / maker out   (both, exchange TP filled)  -> 2bp + 2bp
  4. maker in / TAKER out   (TP_LIMIT on, but the BOT market-closed the tp)
  5. SL exits are never maker even with TP_LIMIT_ORDERS on

Run:  ./.venv/bin/python test_fee_booking.py
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="rofl_fee_test_")
os.environ.update({
    "MODE": "live",
    "EXCHANGE": "bybit",
    "STRATEGY_PRESET": "adaptive_bidir",
    "STATE_FILE": os.path.join(_TMP, "state.json"),
    "LOG_FILE": os.path.join(_TMP, "bot.log"),
    "STARTING_EQUITY": "100",
    "TP_LIMIT_ORDERS": "0",
    "ENTRY_LIMIT_ORDERS": "0",
})

import bot as botmod  # noqa: E402

TAKER, MAKER = 0.0006, 0.0002


class _Noop:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class FakeCcxt:
    """Holds the position; `reject_close` models the exchange having already
    flattened us (reduce-only rejected) -> close_position sees order=None."""

    def __init__(self, net=0.0, reject_close=False):
        self.position = float(net)
        self.reject_close = reject_close
        self.markets = {"_": 1}

    def _order(self, side, symbol, qty, params):
        if self.reject_close:
            raise Exception("110017: reduce-only order rejected")
        signed = qty if side == "buy" else -qty
        new = self.position + signed
        if new != 0 and (new > 0) != (self.position > 0):
            new = 0.0
        self.position = new
        # price=None so close_position falls back to exit_px_hint (a real ccxt
        # Bybit market order carries no fill price either) — keeps the fee the
        # only variable under test.
        return {"id": "fake", "price": None, "amount": qty, "filled": qty}

    def create_market_buy_order(self, s, q, params=None):
        return self._order("buy", s, q, params)

    def create_market_sell_order(self, s, q, params=None):
        return self._order("sell", s, q, params)

    def fetch_ticker(self, s):
        return {"last": 100.0}

    def fetch_positions(self, symbols, params=None):
        sym = symbols[0]
        if abs(self.position) < 1e-12:
            return [{"symbol": sym, "contracts": 0, "side": ""}]
        return [{"symbol": sym, "contracts": abs(self.position),
                 "side": "long" if self.position > 0 else "short"}]

    def market(self, s):
        return {"id": "ETHUSDT", "limits": {}}

    def private_get_v5_position_closed_pnl(self, params=None):
        return {"result": {"list": []}}


def _bot(tp_limit, entry_limit, net, reject_close=False):
    cfg = botmod.BotConfig()
    cfg.tp_limit_orders = tp_limit
    cfg.entry_limit_orders = entry_limit
    b = botmod.Bot(cfg)
    b.ex._ccxt = FakeCcxt(net, reject_close)
    b.ex.paper = False
    b.events = _Noop()
    b.notifier = _Noop()
    return b


def _run(tp_limit, entry_limit, maker_entry, reason, reject_close, exit_px):
    """Close one position and return the fee actually booked."""
    qty, entry_px = 0.5, 100.0
    notional = qty * entry_px
    # reject_close models "the resting TP limit already filled": the exchange
    # is FLAT, which is exactly why the reduce-only close gets rejected.
    b = _bot(tp_limit, entry_limit, net=0.0 if reject_close else qty,
             reject_close=reject_close)
    eq0 = b.state.equity
    b.state.position = botmod.Position(side=1, qty=qty, entry_px=entry_px,
                                       sl=98.0, tp=106.0, open_ts=0,
                                       notional=notional, maker_entry=maker_entry)
    b.close_position(exit_px_hint=exit_px, reason=reason)
    gross = (exit_px - entry_px) * qty
    return gross - (b.state.equity - eq0), notional, exit_px * qty


def _expect(name, got, notional, exit_notional, er, xr):
    want = notional * er + exit_notional * xr
    assert abs(got - want) < 1e-9, f"{name}: booked {got:.6f}, expected {want:.6f}"
    print(f"PASS {name:34s} fee {got:.4f}  ({er*1e4:.0f}bp in / {xr*1e4:.0f}bp out)")


def test_taker_in_taker_out():
    f, n, x = _run(False, False, False, "sl", False, 98.0)
    _expect("taker in / taker out", f, n, x, TAKER, TAKER)


def test_maker_in_taker_out():
    f, n, x = _run(False, True, True, "sl", False, 98.0)
    _expect("maker in / taker out", f, n, x, MAKER, TAKER)


def test_maker_in_maker_out():
    # reject_close=True -> exchange already flat -> the resting TP limit filled
    f, n, x = _run(True, True, True, "tp", True, 106.0)
    _expect("maker in / maker out (TP limit)", f, n, x, MAKER, MAKER)


def test_bot_initiated_tp_is_taker():
    # TP_LIMIT on, but OUR market close filled -> exit is taker, not maker
    f, n, x = _run(True, True, True, "tp", False, 106.0)
    _expect("maker in / bot market tp = taker", f, n, x, MAKER, TAKER)


def test_sl_never_maker():
    f, n, x = _run(True, True, True, "sl-external", True, 98.0)
    _expect("sl-external is never maker", f, n, x, MAKER, TAKER)


def test_round_trip_saving_is_material():
    """The whole point: the old flat-taker booking overstated a full maker
    round trip by 8bp of notional. Assert the corrected number is 4bp."""
    f, n, x = _run(True, True, True, "tp", True, 100.0)   # flat price, fee only
    old = (n + x) * TAKER
    assert abs(f - (n + x) * MAKER) < 1e-9
    assert old - f > 0, "corrected fee must be lower than the old flat-taker fee"
    print(f"PASS round-trip: old {old:.4f} -> new {f:.4f} "
          f"({(old-f)/n*1e4:.0f}bp of notional recovered)")


if __name__ == "__main__":
    test_taker_in_taker_out()
    test_maker_in_taker_out()
    test_maker_in_maker_out()
    test_bot_initiated_tp_is_taker()
    test_sl_never_maker()
    test_round_trip_saving_is_material()
    print("\nAll fee-booking tests passed.")
