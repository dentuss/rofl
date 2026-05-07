"""Enhanced backtester with optional partial profit taking and daily loss limit.

Wraps the original event-style logic but adds two features:
  1. Partial TP: when profit reaches partial_tp_atr * ATR, close half the
     position at market, move stop to entry (breakeven).
  2. Daily loss limit: if cumulative realised PnL since UTC midnight is
     <= -daily_loss_pct * starting_equity, stop opening new positions for
     the rest of the day. Existing positions still run their stops.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from backtest import BTConfig, Trade


@dataclass
class EnhancedBTConfig(BTConfig):
    # Partial profit taking (0 disables)
    partial_tp_atr: float = 0.0          # take half off at this multiple of ATR
    partial_to_breakeven: bool = True    # move stop to breakeven after partial
    # Daily loss limit (0 disables)
    daily_loss_pct: float = 0.0          # e.g. 0.05 = stop opening when -5% on day
    # Equity-aware risk scaling
    eq_risk_decay: float = 0.0           # 0 disables. e.g. 0.5 = halve risk after
                                          # equity DD reaches drawdown_for_decay
    drawdown_for_decay: float = 0.15      # DD threshold for risk halving


def _slip(p, side, slip_bps, is_entry):
    s = slip_bps / 10_000
    return p * (1 + s * side) if is_entry else p * (1 - s * side)


def run_backtest_enhanced(price_df: pd.DataFrame, sig_df: pd.DataFrame,
                          cfg: EnhancedBTConfig | None = None,
                          long_only: bool = False):
    if cfg is None:
        cfg = EnhancedBTConfig()
    from indicators import atr as atr_fn

    df = price_df.join(sig_df, how="inner").copy()
    df["sig_next"] = df["signal"].shift(1).fillna(0).astype(int)
    df["sl_next"] = df["sl"].shift(1)
    df["tp_next"] = df["tp"].shift(1)
    df["atr14"] = atr_fn(df["high"], df["low"], df["close"], 14)
    if long_only or not cfg.allow_short:
        df["sig_next"] = df["sig_next"].clip(lower=0)

    equity = cfg.starting_equity
    eq_peak = cfg.starting_equity
    eq_curve = []
    trades: list[Trade] = []
    pos_side = 0
    pos_qty = pos_entry = pos_sl = pos_tp = pos_atr = 0.0
    pos_open_time = None
    pos_bars = 0
    pos_notional = 0.0
    partial_done = False
    # Daily loss tracking
    day_start_eq = equity
    day = None
    day_blocked = False

    for ts, row in df.iterrows():
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        sig_next = int(row["sig_next"])
        cur_day = ts.normalize()
        if cur_day != day:
            day = cur_day
            day_start_eq = equity
            day_blocked = False

        # Daily loss check (only blocks new entries)
        if cfg.daily_loss_pct > 0:
            day_pnl_pct = (equity - day_start_eq) / day_start_eq if day_start_eq > 0 else 0
            if day_pnl_pct <= -cfg.daily_loss_pct:
                day_blocked = True

        # Equity-aware risk scaling
        eq_peak = max(eq_peak, equity)
        risk_scale = 1.0
        if cfg.eq_risk_decay > 0:
            cur_dd = (equity / eq_peak - 1) if eq_peak > 0 else 0
            if cur_dd <= -cfg.drawdown_for_decay:
                risk_scale = cfg.eq_risk_decay

        # 1) Manage open position
        if pos_side != 0:
            pos_bars += 1

            # Partial TP check
            if cfg.partial_tp_atr > 0 and not partial_done and pos_atr > 0:
                pt_hit = ((h - pos_entry) >= cfg.partial_tp_atr * pos_atr) if pos_side == 1 \
                    else ((pos_entry - l) >= cfg.partial_tp_atr * pos_atr)
                if pt_hit:
                    half_qty = pos_qty / 2
                    fill_px = pos_entry + cfg.partial_tp_atr * pos_atr * pos_side
                    fill_px = _slip(fill_px, pos_side, cfg.slip_bps, False)
                    gross = (fill_px - pos_entry) * half_qty * pos_side
                    fee = fill_px * half_qty * cfg.fee_rate
                    equity += gross - fee
                    pos_qty -= half_qty
                    if cfg.partial_to_breakeven:
                        pos_sl = pos_entry  # move to BE
                    partial_done = True

            # Stop / TP / time / signal-flip exit
            hit_sl = (l <= pos_sl) if pos_side == 1 else (h >= pos_sl)
            hit_tp = (h >= pos_tp) if pos_side == 1 else (l <= pos_tp)
            exit_px = None
            reason = ""
            if hit_sl and hit_tp:
                exit_px = pos_sl; reason = "sl"
            elif hit_sl:
                exit_px = pos_sl; reason = "sl"
            elif hit_tp:
                exit_px = pos_tp; reason = "tp"
            elif pos_bars >= cfg.max_bars_in_trade:
                exit_px = c; reason = "time"
            elif sig_next != 0 and sig_next != pos_side:
                exit_px = o; reason = "signal"
            if exit_px is not None:
                fill = _slip(exit_px, pos_side, cfg.slip_bps, False)
                gross = (fill - pos_entry) * pos_qty * pos_side
                fee = fill * pos_qty * cfg.fee_rate
                pnl = gross - fee
                equity += pnl
                trades.append(Trade(
                    side=pos_side, entry_time=pos_open_time,
                    exit_time=ts, entry_px=pos_entry, exit_px=fill,
                    qty=pos_qty, notional=pos_notional, sl=pos_sl,
                    tp=pos_tp, pnl=pnl, fees=fee, reason=reason,
                    bars_held=pos_bars,
                ))
                pos_side = 0
                pos_qty = 0.0
                partial_done = False

        # 2) New entry — only if flat AND not blocked AND have signal
        if pos_side == 0 and sig_next != 0 and not day_blocked:
            sl = row["sl_next"]
            tp = row["tp_next"]
            bar_atr = row["atr14"]
            if pd.notna(sl) and pd.notna(tp) and equity > 0 and pd.notna(bar_atr):
                entry_fill = _slip(o, sig_next, cfg.slip_bps, True)
                stop_dist = abs(entry_fill - sl) / entry_fill
                if stop_dist > 1e-5:
                    risk = equity * cfg.risk_per_trade * risk_scale
                    notional = min(risk / stop_dist, equity * cfg.max_leverage)
                    qty = notional / entry_fill
                    fee = notional * cfg.fee_rate
                    equity -= fee
                    pos_side = sig_next
                    pos_qty = qty
                    pos_entry = entry_fill
                    pos_sl = sl
                    pos_tp = tp
                    pos_atr = float(bar_atr)
                    pos_open_time = ts
                    pos_bars = 0
                    pos_notional = notional
                    partial_done = False

        # 3) Mark equity
        if pos_side != 0:
            unreal = (c - pos_entry) * pos_qty * pos_side
            mark = equity + unreal
        else:
            mark = equity
        eq_curve.append((ts, mark))

    # Close any open position at end
    if pos_side != 0:
        last_ts = df.index[-1]
        fill = _slip(df["close"].iloc[-1], pos_side, cfg.slip_bps, False)
        gross = (fill - pos_entry) * pos_qty * pos_side
        fee = fill * pos_qty * cfg.fee_rate
        pnl = gross - fee
        equity += pnl
        trades.append(Trade(
            side=pos_side, entry_time=pos_open_time, exit_time=last_ts,
            entry_px=pos_entry, exit_px=fill, qty=pos_qty,
            notional=pos_notional, sl=pos_sl, tp=pos_tp,
            pnl=pnl, fees=fee, reason="eod", bars_held=pos_bars,
        ))
        eq_curve[-1] = (last_ts, equity)

    eq = pd.Series([v for _, v in eq_curve], index=[t for t, _ in eq_curve])
    return eq, trades
