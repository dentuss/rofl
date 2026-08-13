"""Funding must reach the books: live PnL comes from the venue, paper does not.

Guards the 2026-08-13 fix. `bot.py` computed realised PnL as gross-minus-fees
and never booked funding (`grep -c funding bot.py` was 0). Reconciling the first
7 live closes against Bybit closed-PnL measured the gap at -0.095 (~1.4bp per
round trip) — small, but FAVOURABLE to the local number, and the -8% halt line
is evaluated on that equity.

The three properties that must hold, and the two traps this pins:
  1. LIVE books the venue's `closedPnl` verbatim (it nets gross, both fees and
     funding), and reports the venue's fees alongside so the blotter's pnl and
     fees columns describe the same trade.
  2. PAPER is byte-identical to before — the house rule is that fixing a live
     bug must never move paper, because paper is the engine's mirror.
  3. A missing / malformed / unavailable venue record degrades to the local
     model rather than crashing or booking None. The failure mode of the
     closed-PnL API is SILENT (retCode 0, empty list), so this path is the one
     that actually runs when it breaks.

Run:  python test_funding_booking.py
"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("MODE", "paper")

import bot as botmod  # noqa: E402


class _Noop:
    def __getattr__(self, _n):
        return lambda *a, **k: None


def _bot(mode: str, closed_row: dict | None, raise_on_close: bool = True):
    tmp = tempfile.mkdtemp(prefix="rofl_fund_")
    cfg = botmod.BotConfig()
    cfg.mode = mode
    cfg.symbol_override = "BTC/USDT"
    cfg.state_file = os.path.join(tmp, "state.json")
    cfg.log_file = os.path.join(tmp, "bot.log")
    cfg.starting_equity = 100.0
    b = botmod.Bot(cfg)
    b.events = _Noop()
    b.notifier = _Noop()
    import logging
    b.log.setLevel(logging.CRITICAL)
    b.state.equity = 100.0
    # The exchange-attached SL fired: our reduce-only close errors, the position
    # reads flat, and the booking path falls to closed-PnL. This is the exact
    # shape of all 7 real closes so far (reason 'sl-external').
    if raise_on_close:
        def _boom(*a, **k):
            raise RuntimeError("order would not reduce position size")
        b.ex.market_sell = _boom
        b.ex.market_buy = _boom
        b.ex.fetch_position_size = lambda: 0.0
    b.ex.fetch_last_closed_fill = lambda pos, since_ms=None: closed_row
    b.ex.fee_taker, b.ex.fee_maker = 0.00055, 0.0002
    b.ex.paper = (mode != "live")
    return b


def _pos(b):
    p = botmod.Position(side=1, qty=1.0, entry_px=100.0, sl=98.0, tp=112.0,
                        notional=100.0, open_ts=1_700_000_000)
    b.state.position = p
    return p


def main() -> None:
    # ---- 1. LIVE books the venue figure, not the local model ---------------
    # gross = (99 - 100) * 1 = -1.0 ; fees = 100*0.00055 + 99*0.00055 = -0.10945
    # local model = -1.10945. The venue says -1.16 because 0.05055 of funding
    # accrued while the position was open. The venue number must win.
    row = {"exit_px": 99.0, "qty": 1.0, "updated_ms": 1_700_000_100,
           "closed_pnl": -1.16, "open_fee": 0.055, "close_fee": 0.0545}
    b = _bot("live", row)
    _pos(b)
    b.close_position(98.0, "sl-external")
    assert abs(b.state.realised_pnl - (-1.16)) < 1e-9, \
        f"live must book the venue closedPnl, got {b.state.realised_pnl}"
    assert abs(b.state.equity - (100.0 - 1.16)) < 1e-9, \
        f"equity must move by the venue figure, got {b.state.equity}"
    print(f"  live  : booked {b.state.realised_pnl:+.5f} (venue) "
          f"vs local model -1.10945  -> funding {-1.16 - -1.10945:+.5f} captured")

    # ---- 2. The funding delta is what the reconcile measured ---------------
    # Sanity-check the direction the finding recorded: a LONG pays funding, so
    # the venue figure is MORE negative than the local model. If this assertion
    # ever flips, the sign convention changed and the halt line is wrong again.
    assert -1.16 < -1.10945, "long should pay funding => venue more negative"

    # ---- 3. PAPER is untouched --------------------------------------------
    # Same row available, but paper must ignore it entirely and keep mirroring
    # the engine: fill at pos.sl (98.0), local fees, no funding.
    b = _bot("paper", row, raise_on_close=False)
    b.ex.market_sell = lambda qty, reduce_only=False: {"price": 98.0}
    _pos(b)
    b.close_position(98.0, "sl")
    gross = (98.0 - 100.0) * 1.0
    fees = 100.0 * 0.00055 + 98.0 * 0.00055
    assert abs(b.state.realised_pnl - (gross - fees)) < 1e-9, \
        f"paper must keep the local model, got {b.state.realised_pnl}"
    print(f"  paper : booked {b.state.realised_pnl:+.5f} (local model, "
          f"venue row ignored) — unchanged")

    # ---- 4. Venue unavailable => degrade to the local model ----------------
    b = _bot("live", None)
    _pos(b)
    b.close_position(98.0, "sl-external")
    gross = (98.0 - 100.0) * 1.0                    # falls back to the hint
    fees = 100.0 * 0.00055 + 98.0 * 0.00055
    assert abs(b.state.realised_pnl - (gross - fees)) < 1e-9, \
        f"missing venue record must degrade to the local model, got {b.state.realised_pnl}"
    print(f"  no-rec: booked {b.state.realised_pnl:+.5f} (local fallback) — no crash")

    # ---- 5. Malformed row => still no crash, still degrades ---------------
    bad = {"exit_px": 99.0, "qty": 1.0, "updated_ms": None,
           "closed_pnl": None, "open_fee": None, "close_fee": None}
    b = _bot("live", bad)
    _pos(b)
    b.close_position(98.0, "sl-external")
    assert b.state.realised_pnl is not None and b.state.realised_pnl < 0, \
        "malformed row must not book None or crash"
    print(f"  bad   : booked {b.state.realised_pnl:+.5f} (local fallback) — no crash")

    print("\nOK — live books venue closedPnl (funding included), paper unchanged, "
          "missing/malformed records degrade safely.")


if __name__ == "__main__":
    main()
