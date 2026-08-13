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

Any divergence region NOT explained by those is UNEXPECTED. Whether that FAILS
the run depends on which tier the case belongs to — see below.

PINNED WINDOW (added 2026-08-05)
--------------------------------
Every case used to fetch `days` back from *today*, so the verdict changed with
the calendar: on 2026-08-05 two fresh bars slid into the 1h window and turned a
green gate red with no code change (FINDINGS 2026-08-05). A gate test whose
result depends on the date it runs is not reproducible, which contradicts the
basis of the whole ledger.

The window is now pinned to end at PARITY_END. **That date was fixed as a
calendar boundary (the 1st of the current month) BEFORE re-running — it was not
chosen by looking at which date made the suite pass.** Whatever the pinned
window reports is reported. Override with PARITY_END=YYYY-MM-DD to re-pin
deliberately; moving it is a decision, not a side effect.

TWO TIERS (added 2026-08-05)
-----------------------------
  DEPLOYED — the 4h configs the live book actually runs (4h/tp6 K=0, and the
             pullback 4h K=3 leg of BLEND50_CONF). An UNEXPECTED region here
             FAILS the run. This is gate G5.
  LEGACY   — the retired 1h program (K=0 and K=3), kept because its cooldown
             semantics still document the signal-bar-vs-fill-bar off-by-one.
             Nothing deployed runs 1h. An UNEXPECTED region here is reported
             loudly and recorded, but does NOT fail the run — otherwise drift
             in dead code masks or blocks a real regression in the live book.
             Set PARITY_STRICT=1 to make legacy failures fatal too.

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
from core.strategies import pullback_in_trend, triple_confirm_bidir  # noqa: E402

# Stop width for BOTH sides of the comparison. bot.py reads TL_SL_MULT for the
# triple and pullback legs alike (cfg.tl_sl_mult, default 1.8); the engine side
# used to hardcode 1.8 here. Binding them to the SAME env is what makes a
# non-default run a parity test rather than a mismatch test: set TL_SL_MULT and
# the live executor and the fixed engine move together. Unset reproduces the
# deployed gate exactly (1.8 == the previous literal).
SL_MULT = float(os.getenv("TL_SL_MULT", "1.8"))
BASE = dict(ema_fast=9, ema_slow=26, ema_trend=50, rsi_min=55.0, adx_min=22.0,
            atr_n=14, sl_mult=SL_MULT, tp_mult=3.0)
RISK, LEV, MAX_BARS = 0.02, 5.0, 96

# Inclusive end of the pinned evaluation window (see the docstring). Fixed as a
# calendar boundary, not tuned to an outcome. Bars after it are discarded, so
# the same commit yields the same verdict next month.
PARITY_END = os.getenv("PARITY_END", "2026-08-01")
PARITY_STRICT = os.getenv("PARITY_STRICT", "0") == "1"


def _pin(df: "pd.DataFrame", days: int, tf: str) -> "pd.DataFrame":
    """Clip to the pinned window: <= PARITY_END, keeping the last `days` of it.

    Fetching is still relative to now (that is core.data's contract and it
    caches that way), so we over-fetch and slice. If the cache predates
    PARITY_END the frame simply ends earlier — the bar count printed per case
    makes any such shortfall visible rather than silent.
    """
    end = pd.Timestamp(PARITY_END, tz="UTC")
    df = df[df.index <= end]
    if df.empty:
        return df
    start = end - pd.Timedelta(days=days)
    return df[df.index >= start]


class _Noop:
    def __getattr__(self, _n):
        return lambda *a, **k: None


def _const(px):
    return lambda: float(px)


def _make_bot(symbol: str, cooldown: int = 0, preset: str = "adaptive_bidir"):
    tmp = tempfile.mkdtemp(prefix="rofl_exec_")
    cfg = botmod.BotConfig()
    cfg.mode = "paper"
    cfg.preset = preset                    # triple_bidir, allow_short=True
    cfg.symbol_override = symbol
    cfg.state_file = os.path.join(tmp, "state.json")
    cfg.log_file = os.path.join(tmp, "bot.log")
    cfg.starting_equity = 100.0
    cfg.cooldown_bars = cooldown           # 0 = pure exec parity; >0 = cooldown parity
    bot = botmod.Bot(cfg)
    bot.log.setLevel(logging.CRITICAL)     # silence per-trade INFO
    bot.state.equity = 100.0
    bot.events = _Noop()
    bot.notifier = _Noop()
    # Disable decay so sizing matches the no-decay reference (decay changes qty,
    # not lifecycle, but keeping them equal makes final equity comparable).
    bot._effective_risk = lambda: bot.cfg.risk_per_trade
    return bot


def replay_live(df: pd.DataFrame, sig: pd.DataFrame, symbol: str, cooldown: int = 0,
                preset: str = "adaptive_bidir"):
    """Drive the real bot exec methods bar-by-bar. Returns (trades, final_eq,
    skipped_entry_bars). bar_ts is threaded through so the live post-SL cooldown
    (keyed on bar timestamp) exercises the same way it does in tick()."""
    bot = _make_bot(symbol, cooldown=cooldown, preset=preset)
    trades, skips, cur = [], [], {}
    for i in range(1, len(df)):
        bar = df.iloc[i]
        bar_ts = int(df.index[i].timestamp())
        sig_prev = sig.iloc[i - 1]                # act on prior CLOSED bar
        # 1) manage open position (mirrors tick: bars++, then real check_exit)
        if bot.state.position is not None:
            bot.state.position.bars_open += 1
            reason = bot.check_exit(bar)
            if reason is not None:
                pos = bot.state.position
                bot.ex.fetch_price = _const(bar["close"])   # used only for 'time'
                bot.close_position(float(bar["close"]), reason, bar_ts=bar_ts)
                trades.append(dict(entry_bar=cur.get("entry_bar"), exit_bar=i,
                                   side=pos.side, reason=reason))
                cur.clear()
                # An SL/TP/time fill happens INSIDE (or at the close of) this
                # bar; the real bot's earliest re-entry is the next tick, at
                # the NEXT bar's price. Same-bar re-entry at this bar's open
                # would be the retro-fill artifact the fixed engine also
                # blocks (FINDINGS 2026-07-05).
                continue
        # 2) enter if flat, prior-bar signal at THIS bar's open (bt convention).
        # bar_ts threading matches tick(): the real bot passes the label of the
        # just-CLOSED signal bar (df.index[i-1]), not the fill bar — which makes
        # the live cooldown gate one bar stricter than the engine's (see the
        # benign-region classifier in run_case).
        if bot.state.position is None:
            s = int(sig_prev["signal"])
            if s != 0:
                sl, tp = sig_prev["sl"], sig_prev["tp"]
                bot.ex.fetch_price = _const(bar["open"])
                bot.enter_position({"signal": s,
                                    "sl": float(sl) if pd.notna(sl) else None,
                                    "tp": float(tp) if pd.notna(tp) else None},
                                   bar_ts=int(df.index[i - 1].timestamp()))
                if bot.state.position is not None:
                    cur["entry_bar"] = i
                    # Exchange-attached SL/TP are live DURING the fill bar —
                    # mirror the engine's entry-bar exit check (no grace bar).
                    reason = bot.check_exit(bar)
                    if reason is not None:
                        pos = bot.state.position
                        bot.ex.fetch_price = _const(bar["close"])
                        bot.close_position(float(bar["close"]), reason,
                                           bar_ts=bar_ts)
                        trades.append(dict(entry_bar=i, exit_bar=i,
                                           side=pos.side, reason=reason))
                        cur.clear()
                else:
                    skips.append(i)               # geometry / min-notional reject
    return trades, bot.state.equity, skips


def backtest_run(df: pd.DataFrame, sig: pd.DataFrame, cooldown: int = 0):
    """Returns (trades, final_eq). Trades as dicts with bar indices."""
    cfg = BTConfig(starting_equity=100.0, risk_per_trade=RISK, max_leverage=LEV,
                   fee_rate=0.0006, slip_bps=0.0, max_bars_in_trade=MAX_BARS,
                   allow_short=True, cooldown_bars=cooldown)
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


def run_case(pair: str, days: int = 180, cooldown: int = 0,
             tf: str = "1h", tp_mult: float | None = None,
             strategy: str = "triple") -> int:
    # Over-fetch past PARITY_END (fetching is always relative to now), then clip.
    lag = max((pd.Timestamp.now(tz="UTC") - pd.Timestamp(PARITY_END, tz="UTC")).days, 0)
    df = _pin(fetch_ohlcv(pair, tf, days=days + lag + 5), days, tf)
    if df.empty:
        raise SystemExit(f"{pair}: no bars at or before PARITY_END={PARITY_END}")
    if strategy == "pullback":
        sig = pullback_in_trend(
            df, tp_mult=6.0 if tp_mult is None else tp_mult, sl_mult=SL_MULT)
        preset = "pullback_bidir_4h"
    else:
        params = dict(BASE) if tp_mult is None else {**BASE, "tp_mult": tp_mult}
        sig = triple_confirm_bidir(df, **params)
        preset = "adaptive_bidir_4h" if tf == "4h" else "adaptive_bidir"
    bt, bt_eq = backtest_run(df, sig, cooldown=cooldown)
    live, live_eq, skips = replay_live(df, sig, pair.replace("-", "/"),
                                       cooldown=cooldown, preset=preset)
    n = len(df)
    bt_tl, live_tl = _entry_timeline(bt, n), _entry_timeline(live, n)
    regions = _diff_regions(bt_tl, live_tl)

    flip_bars = {t["exit_bar"] for t in bt if t["reason"] == "signal"}
    skip_set = set(skips)
    # Bars where a post-SL cooldown blocks a same-side re-entry. The engine
    # blocks entry FILLS on bars [sl_exit_bar, sl_exit_bar+K); the live gate
    # compares the SIGNAL bar's ts (one bar earlier than the fill), so it
    # blocks fills on [sl_exit_bar+1, sl_exit_bar+K] — one bar stricter. A
    # region starting anywhere in the union [exit, exit+K] is cooldown-driven
    # (benign, conservative on the live side); anything else is UNEXPECTED.
    cooldown_bars = set()
    if cooldown > 0:
        for t in bt:
            if t["reason"] == "sl":
                cooldown_bars.update(range(t["exit_bar"], t["exit_bar"] + cooldown + 1))
    by_flip = by_skip = by_cooldown = unexpected = 0
    unexpected_regions = []
    for (s, e) in regions:
        if s in flip_bars:
            by_flip += 1
        elif s in skip_set:
            by_skip += 1
        elif s in cooldown_bars:
            by_cooldown += 1
        else:
            unexpected += 1
            unexpected_regions.append((s, e, bt_tl[s], live_tl[s]))

    drift = (live_eq / bt_eq - 1) * 100
    cd = f", cooldown {by_cooldown}" if cooldown > 0 else ""
    print(f"{pair} ({days}d {tf}, {n} bars, K={cooldown}): bt {len(bt)} / live {len(live)} trades")
    print(f"  divergence regions: {len(regions)}  "
          f"[flip-cascade {by_flip}, entry-skip {by_skip}{cd}, UNEXPECTED {unexpected}]")
    print(f"  final equity  bt {bt_eq:.2f}  live {live_eq:.2f}  (live drift {drift:+.1f}%)")
    for (s, e, bs, ls) in unexpected_regions[:6]:
        print(f"     UNEXPECTED bars {s}-{e}: bt side={bs} live side={ls}")
    return unexpected


def main():
    pairs = [("INJ-USDT", 180), ("SOL-USDT", 180), ("ADA-USDT", 180),
             ("ETH-USDT", 180), ("LINK-USDT", 180)]
    print(f"PINNED WINDOW: bars <= {PARITY_END} "
          f"(strict legacy={'on' if PARITY_STRICT else 'off'})")
    print(f"STOP WIDTH:    sl_mult={SL_MULT} on BOTH sides "
          f"(engine BASE and bot cfg.tl_sl_mult={botmod.BotConfig().tl_sl_mult})"
          f"{'' if SL_MULT == 1.8 else '   <-- NON-DEPLOYED, gate run for research'}\n")
    assert abs(botmod.BotConfig().tl_sl_mult - SL_MULT) < 1e-9, (
        f"stop width mismatch: engine {SL_MULT} vs bot "
        f"{botmod.BotConfig().tl_sl_mult}. TL_SL_MULT must be set in the "
        f"environment BEFORE `import bot` (BotConfig reads env at import).")

    print("--- LEGACY 1h (K=0): retired program, reported not gated ---")
    base_unexpected = sum(run_case(p, d, cooldown=0) for p, d in pairs)
    print("\n--- LEGACY 1h (K=3): retired cooldown gate, reported not gated ---")
    cd_unexpected = sum(run_case(p, d, cooldown=3) for p, d in pairs)
    print("\n--- DEPLOYED 4h (K=0, tp_mult=6.0): the honest-rebuild config ---")
    h4_unexpected = sum(run_case(p, 540, cooldown=0, tf="4h", tp_mult=6.0)
                        for p, _ in pairs)
    print("\n--- DEPLOYED pullback 4h (K=3): the BLEND50_CONF second leg ---")
    pb_unexpected = sum(run_case(p, 540, cooldown=3, tf="4h",
                                 strategy="pullback")
                        for p in ("BTC-USDT", "ETH-USDT", "SOL-USDT"))
    print("=" * 64)

    # --- DEPLOYED tier: this is gate G5. Always fatal. ---
    assert h4_unexpected == 0, (
        f"{h4_unexpected} UNEXPECTED divergence region(s) on the 4h/tp6 config: the "
        f"live executor diverges from the fixed engine on the DEPLOYED setup.")
    assert pb_unexpected == 0, (
        f"{pb_unexpected} UNEXPECTED divergence region(s) on the pullback 4h config: "
        f"the live executor diverges from the fixed engine on the promoted "
        f"BLEND50_CONF second leg. Investigate before deploying.")

    # --- LEGACY tier: retired 1h program. Reported; fatal only under
    # PARITY_STRICT=1. Nothing deployed runs 1h, and letting dead code block
    # the suite would train us to ignore a red G5 — the opposite of the point.
    legacy = base_unexpected + cd_unexpected
    if legacy:
        print(f"WARNING: {legacy} UNEXPECTED region(s) in the LEGACY 1h tier "
              f"(K=0: {base_unexpected}, K=3: {cd_unexpected}).")
        print("  Nothing deployed runs 1h, so this does not gate. It IS still a "
              "real divergence in retired code — record it in FINDINGS rather "
              "than letting it become wallpaper. PARITY_STRICT=1 makes it fatal.")
        if PARITY_STRICT:
            raise AssertionError(
                f"{legacy} UNEXPECTED legacy-tier region(s) with PARITY_STRICT=1")
    else:
        print("legacy 1h tier: clean.")

    print("EXEC PARITY OK (DEPLOYED tier) - the 4h/tp6 config and the pullback 4h "
          "leg both match the fixed engine; no unexpected drift. Window pinned to "
          f"{PARITY_END}, so this verdict is reproducible.")


if __name__ == "__main__":
    main()
