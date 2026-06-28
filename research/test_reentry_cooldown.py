"""Research: does a post-stop SAME-SIDE re-entry cooldown help?

After a stop-loss on side X, block NEW side-X entries for K bars. This is
narrower than the rejected fresh-crossover gating (which blocks ALL non-fresh
entries) — it only pauses after a *loss*, only on the *same side*. Motivation:
the INJ V-bounce whipsaw on 2026-06-24 (short TP'd, then two immediate re-shorts
into the bounce, both stopped, -5.25).

K=0 must exactly reproduce core.backtest.run_backtest (sanity check). Research
only — nothing here touches the live path.

Run:  python research/test_reentry_cooldown.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.backtest import BTConfig, BTResult, Trade, _apply_slippage, run_backtest  # noqa: E402
from core.data import fetch_ohlcv  # noqa: E402
from core.strategies import triple_confirm_bidir  # noqa: E402

BASE = dict(ema_fast=9, ema_slow=26, ema_trend=50, rsi_min=55.0, adx_min=22.0,
            atr_n=14, sl_mult=1.8, tp_mult=3.0)
PAIRS = ["INJ-USDT", "SOL-USDT", "ADA-USDT", "ETH-USDT", "LINK-USDT"]
DAYS = 400
KS = [0, 3, 6, 12, 24]


def run_with_cooldown(price_df, sig_df, cfg, K):
    """core.backtest.run_backtest + a post-SL same-side re-entry cooldown of K bars."""
    df = price_df.join(sig_df, how="inner").copy()
    df["sig_next"] = df["signal"].shift(1).fillna(0).astype(int)
    df["sl_next"] = df["sl"].shift(1)
    df["tp_next"] = df["tp"].shift(1)
    if not cfg.allow_short:
        df["sig_next"] = df["sig_next"].clip(lower=0)
    equity = cfg.starting_equity
    eq_curve, trades = [], []
    pos_side = 0
    pos_qty = pos_entry = pos_sl = pos_tp = pos_notional = 0.0
    pos_open = None
    pos_bars = 0
    block_long_until = block_short_until = -1

    for i, (ts, row) in enumerate(df.iterrows()):
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        sig_next = int(row["sig_next"])
        if pos_side != 0:
            pos_bars += 1
            hit_sl = (l <= pos_sl) if pos_side == 1 else (h >= pos_sl)
            hit_tp = (h >= pos_tp) if pos_side == 1 else (l <= pos_tp)
            exit_px = None
            reason = ""
            if hit_sl and hit_tp:
                exit_px, reason = pos_sl, "sl"
            elif hit_sl:
                exit_px, reason = pos_sl, "sl"
            elif hit_tp:
                exit_px, reason = pos_tp, "tp"
            elif pos_bars >= cfg.max_bars_in_trade:
                exit_px, reason = c, "time"
            elif sig_next != 0 and sig_next != pos_side:
                exit_px, reason = o, "signal"
            if exit_px is not None:
                fill = _apply_slippage(exit_px, pos_side, cfg.slip_bps, False)
                fee = fill * pos_qty * cfg.fee_rate
                pnl = (fill - pos_entry) * pos_qty * pos_side - fee
                equity += pnl
                trades.append(Trade(pos_side, pos_open, ts, pos_entry, fill, pos_qty,
                                    pos_notional, pos_sl, pos_tp, pnl, fee, reason, pos_bars))
                if reason == "sl" and K > 0:           # arm the same-side cooldown
                    if pos_side == 1:
                        block_long_until = i + K
                    else:
                        block_short_until = i + K
                pos_side = 0
                pos_qty = 0.0

        if pos_side == 0 and sig_next != 0:
            blocked = (sig_next == 1 and i < block_long_until) or \
                      (sig_next == -1 and i < block_short_until)
            if not blocked:
                sl, tp = row["sl_next"], row["tp_next"]
                if pd.notna(sl) and pd.notna(tp) and equity > 0:
                    entry = _apply_slippage(o, sig_next, cfg.slip_bps, True)
                    sd = abs(entry - sl) / entry
                    if sd > 1e-5:
                        notional = min(equity * cfg.risk_per_trade / sd, equity * cfg.max_leverage)
                        qty = notional / entry
                        equity -= notional * cfg.fee_rate
                        pos_side, pos_qty, pos_entry = sig_next, qty, entry
                        pos_sl, pos_tp, pos_open, pos_bars, pos_notional = sl, tp, ts, 0, notional

        mark = equity + ((c - pos_entry) * pos_qty * pos_side if pos_side else 0.0)
        eq_curve.append((ts, mark))

    if pos_side != 0:
        last_ts = df.index[-1]
        fill = _apply_slippage(df["close"].iloc[-1], pos_side, cfg.slip_bps, False)
        fee = fill * pos_qty * cfg.fee_rate
        pnl = (fill - pos_entry) * pos_qty * pos_side - fee
        equity += pnl
        trades.append(Trade(pos_side, pos_open, last_ts, pos_entry, fill, pos_qty,
                            pos_notional, pos_sl, pos_tp, pnl, fee, "eod", pos_bars))
        eq_curve[-1] = (last_ts, equity)

    eq = pd.Series([v for _, v in eq_curve], index=[t for t, _ in eq_curve], name="equity")
    return BTResult(equity_curve=eq, trades=trades, config=cfg)


def main():
    cfg = BTConfig(starting_equity=100.0, risk_per_trade=0.02, max_leverage=5.0,
                   max_bars_in_trade=96, allow_short=True)
    # K=0 sanity check vs the canonical backtester on INJ
    inj = fetch_ohlcv("INJ-USDT", "1h", days=DAYS)
    sig = triple_confirm_bidir(inj, **BASE)
    a = run_backtest(inj, sig, cfg, long_only=False).stats()
    b = run_with_cooldown(inj, sig, cfg, 0).stats()
    assert abs(a["final_equity"] - b["final_equity"]) < 1e-6, (a, b)
    print(f"sanity: K=0 reproduces run_backtest on INJ "
          f"(final_eq {b['final_equity']}) ✓\n")

    print(f"Post-stop same-side cooldown — {DAYS}d 1h, 5 pairs\n")
    agg = {K: {"ret": [], "sharpe": [], "mdd": []} for K in KS}
    for pair in PAIRS:
        df = fetch_ohlcv(pair, "1h", days=DAYS)
        sig = triple_confirm_bidir(df, **BASE)
        print(f"{pair}:")
        for K in KS:
            s = run_with_cooldown(df, sig, cfg, K).stats()
            agg[K]["ret"].append(s["total_return"])
            agg[K]["sharpe"].append(s["sharpe"])
            agg[K]["mdd"].append(s["max_drawdown"])
            tag = " (baseline)" if K == 0 else ""
            print(f"  K={K:<2d} ret {s['total_return']*100:+7.0f}%  "
                  f"Sharpe {s['sharpe']:.2f}  MDD {s['max_drawdown']*100:5.0f}%  "
                  f"trades {s['trades']:3d}  WR {s['win_rate']*100:.0f}%{tag}")
        print()

    print("=" * 60)
    print("Mean across pairs (vs K=0 baseline):")
    base_ret = np.mean(agg[0]["ret"]); base_sh = np.mean(agg[0]["sharpe"])
    for K in KS:
        mret, msh, mmdd = (np.mean(agg[K]["ret"]), np.mean(agg[K]["sharpe"]),
                           np.mean(agg[K]["mdd"]))
        d = "" if K == 0 else f"  (Δret {(mret-base_ret)*100:+.0f}pp, ΔSharpe {msh-base_sh:+.2f})"
        print(f"  K={K:<2d} mean ret {mret*100:+.0f}%  mean Sharpe {msh:.2f}  "
              f"mean MDD {mmdd*100:.0f}%{d}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
