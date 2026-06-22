"""Live-EXECUTOR parity harness.

test_parity.py proves the SIGNAL function matches the backtest. It never
exercises position management, so a divergence in the live bot's entry/exit
logic (like the missing signal-flip exit) sails straight through it.

This harness closes that gap. It drives the REAL Bot methods
(`enter_position`, `check_exit`, `close_position`) bar-by-bar over historical
data in paper mode — prices stubbed to each bar, decay disabled — and compares
the position the live bot holds on every bar against core.backtest.run_backtest
under matched conventions (slip=0, same risk / fees / max_bars).

The two should hold the same side on every bar EXCEPT inside divergence regions
triggered by a known-benign cause:
  * signal-flip — the backtest closes (and reverses) a position when the
                  opposite signal appears; the live bot deliberately holds, so
                  the two desync until they next realign (a cascade).
  * entry-skip  — the live bot rejects a malformed-geometry / sub-min-notional
                  entry the backtest takes.

Any divergence region NOT explained by those is UNEXPECTED and fails the test —
the regression guard for future changes to the live execution path.

Run:  python test_exec_parity.py
"""
from __future__ import annotations

import logging
import os
import tempfile

import pandas as pd

os.environ.setdefault("MODE", "paper")  # BotConfig reads env at import

import bot as botmod  # noqa: E402
from core.backtest import BTConfig, run_backtest  # noqa: E402
from core.data import fetch_ohlcv  # noqa: E402
from core.strategies import triple_confirm_bidir  # noqa: E402

BASE = dict(ema_fast=9, ema_slow=26, ema_trend=50, rsi_min=55.0, adx_min=22.0,
            atr_n=14, sl_mult=1.8, tp_mult=3.0)
RISK, LEV, MAX_BARS = 0.02, 5.0, 96


class _Noop:
    def __getattr__(self, _n):
        return lambda *a, **k: None


def _const(px):
    return lambda: float(px)


def _make_bot(symbol: str):
    tmp = tempfile.mkdtemp(prefix="rofl_exec_")
    cfg = botmod.BotConfig()
    cfg.mode = "paper"
    cfg.preset = "adaptive_bidir"          # triple_bidir, allow_short=True
    cfg.symbol_override = symbol
    cfg.state_file = os.path.join(tmp, "state.json")
    cfg.log_file = os.path.join(tmp, "bot.log")
    cfg.starting_equity = 100.0
    bot = botmod.Bot(cfg)
    bot.log.setLevel(logging.CRITICAL)     # silence per-trade INFO
    bot.state.equity = 100.0
    bot.events = _Noop()
    bot.notifier = _Noop()
    # Disable decay so sizing matches the no-decay reference (decay changes qty,
    # not lifecycle, but keeping them equal makes final equity comparable).
    bot._effective_risk = lambda: bot.cfg.risk_per_trade
    return bot


def replay_live(df: pd.DataFrame, sig: pd.DataFrame, symbol: str):
    """Drive the real bot exec methods bar-by-bar. Returns (trades, final_eq,
    skipped_entry_bars)."""
    bot = _make_bot(symbol)
    trades, skips, cur = [], [], {}
    for i in range(1, len(df)):
        bar = df.iloc[i]
        sig_prev = sig.iloc[i - 1]                # act on prior CLOSED bar
        # 1) manage open position (mirrors tick: bars++, then real check_exit)
        if bot.state.position is not None:
            bot.state.position.bars_open += 1
            reason = bot.check_exit(bar)
            if reason is not None:
                pos = bot.state.position
                bot.ex.fetch_price = _const(bar["close"])   # used only for 'time'
                bot.close_position(float(bar["close"]), reason)
                trades.append(dict(entry_bar=cur.get("entry_bar"), exit_bar=i,
                                   side=pos.side, reason=reason))
                cur.clear()
        # 2) enter if flat, prior-bar signal at THIS bar's open (bt convention)
        if bot.state.position is None:
            s = int(sig_prev["signal"])
            if s != 0:
                sl, tp = sig_prev["sl"], sig_prev["tp"]
                bot.ex.fetch_price = _const(bar["open"])
                bot.enter_position({"signal": s,
                                    "sl": float(sl) if pd.notna(sl) else None,
                                    "tp": float(tp) if pd.notna(tp) else None})
                if bot.state.position is not None:
                    cur["entry_bar"] = i
                else:
                    skips.append(i)               # geometry / min-notional reject
    return trades, bot.state.equity, skips


def backtest_run(df: pd.DataFrame, sig: pd.DataFrame):
    """Returns (trades, final_eq). Trades as dicts with bar indices."""
    cfg = BTConfig(starting_equity=100.0, risk_per_trade=RISK, max_leverage=LEV,
                   fee_rate=0.0006, slip_bps=0.0, max_bars_in_trade=MAX_BARS,
                   allow_short=True)
    res = run_backtest(df, sig, cfg, long_only=False)
    loc = {ts: k for k, ts in enumerate(df.index)}
    trades = [dict(entry_bar=loc[t.entry_time], exit_bar=loc[t.exit_time],
                   side=t.side, reason=t.reason)
              for t in res.trades if t.reason != "eod"]
    return trades, float(res.equity_curve.iloc[-1])


def _entry_timeline(trades, n):
    """The entry_bar of the trade open at the END of each bar (-1 if flat).

    Using the entry bar (not just the side) means two DIFFERENT same-side
    trades never look 'matched' — so a 1-bar offset inherited from a flip stays
    a single continuous divergence region instead of spuriously splitting."""
    tl = [-1] * n
    for t in trades:
        for b in range(t["entry_bar"], t["exit_bar"]):
            if 0 <= b < n:
                tl[b] = t["entry_bar"]
    return tl


def _diff_regions(a, b):
    regions, i, n = [], 0, len(a)
    while i < n:
        if a[i] != b[i]:
            j = i
            while j < n and a[j] != b[j]:
                j += 1
            regions.append((i, j - 1))
            i = j
        else:
            i += 1
    return regions


def run_case(pair: str, days: int = 180) -> int:
    df = fetch_ohlcv(pair, "1h", days=days)
    sig = triple_confirm_bidir(df, **BASE)
    bt, bt_eq = backtest_run(df, sig)
    live, live_eq, skips = replay_live(df, sig, pair.replace("-", "/"))
    n = len(df)
    bt_tl, live_tl = _entry_timeline(bt, n), _entry_timeline(live, n)
    regions = _diff_regions(bt_tl, live_tl)

    flip_bars = {t["exit_bar"] for t in bt if t["reason"] == "signal"}
    skip_set = set(skips)
    by_flip = by_skip = unexpected = 0
    unexpected_regions = []
    for (s, e) in regions:
        if s in flip_bars:
            by_flip += 1
        elif s in skip_set:
            by_skip += 1
        else:
            unexpected += 1
            unexpected_regions.append((s, e, bt_tl[s], live_tl[s]))

    drift = (live_eq / bt_eq - 1) * 100
    print(f"{pair} ({days}d, {n} bars): bt {len(bt)} / live {len(live)} trades")
    print(f"  divergence regions: {len(regions)}  "
          f"[flip-cascade {by_flip}, entry-skip {by_skip}, UNEXPECTED {unexpected}]")
    print(f"  final equity  bt {bt_eq:.2f}  live {live_eq:.2f}  "
          f"(live drift {drift:+.1f}% from holding through flips)")
    for (s, e, bs, ls) in unexpected_regions[:6]:
        print(f"     UNEXPECTED bars {s}-{e}: bt side={bs} live side={ls}")
    return unexpected


def main():
    cases = [("INJ-USDT", 180), ("SOL-USDT", 180), ("ADA-USDT", 180),
             ("ETH-USDT", 180), ("LINK-USDT", 180)]
    total_unexpected = sum(run_case(p, d) for p, d in cases)
    print("=" * 64)
    assert total_unexpected == 0, (
        f"{total_unexpected} UNEXPECTED exec-divergence region(s): the live "
        f"executor diverges from the backtest for a reason other than the known "
        f"signal-flip / entry-skip. Investigate before deploying.")
    print("EXEC PARITY OK - every divergence is an explained signal-flip "
          "cascade or entry-skip; no unexpected execution drift.")


if __name__ == "__main__":
    main()
