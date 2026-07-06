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

from core.backtest import BTConfig, Trade
from core.risk import decay_risk_scale


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
    # Optional multi-tier decay ladder: ((depth, mult), ...). If non-empty it
    # OVERRIDES the single eq_risk_decay/drawdown_for_decay pair. Deepest
    # breached tier wins; a 0.0 multiplier stops opening new trades.
    eq_decay_tiers: tuple = ()
    # Strategy-health gate (path-dependent): pause NEW entries when the bot's
    # own trailing equity return over health_lookback_bars is below
    # health_min_return. 0 lookback disables. Resumes automatically when the
    # trailing return recovers above health_resume_return (hysteresis).
    health_lookback_bars: int = 0
    health_min_return: float = -0.15
    health_resume_return: float = -0.05
    # ATR-multiple trailing stop (0 disables). Ratchets the stop toward price
    # using the ENTRY bar's ATR, updated from each bar's CLOSE only after that
    # bar's exit checks — so a tightened stop is first testable on the NEXT
    # bar (no look-ahead; matches a live bot amending its stop at bar close).
    # UNDER VALIDATION (research/honest_rebuild_r2.py).
    trail_atr: float = 0.0
    # Entry execution style. "taker": market at the bar open (fee_rate +
    # slippage). "maker_close": post-only limit at the SIGNAL bar's close,
    # filled only if this bar trades THROUGH the price (strict penetration —
    # a touch may not reach a resting order), at fee_maker with no slippage;
    # a missed fill is missed (a persisting signal simply retries next bar).
    # Models the adverse selection honestly: maker longs fill on downticks
    # and miss the runners. UNDER VALIDATION (research/cost_engine.py).
    entry_style: str = "taker"
    fee_maker: float = 0.0002      # Bybit non-VIP linear-perp maker fee
    # TP as a resting limit order (post-only on the profit side). Fill only
    # when the bar trades THROUGH the target (strict penetration — a touch
    # may not reach a resting order), at fee_maker with no slippage. SL-first
    # on a same-bar SL+TP collision is unchanged. Maker entries still never
    # credit a same-bar TP. UNDER VALIDATION (research/tp_limit.py).
    tp_as_limit: bool = False


def _slip(p, side, slip_bps, is_entry):
    s = slip_bps / 10_000
    return p * (1 + s * side) if is_entry else p * (1 - s * side)


def run_backtest_enhanced(price_df: pd.DataFrame, sig_df: pd.DataFrame,
                          cfg: EnhancedBTConfig | None = None,
                          long_only: bool = False):
    if cfg is None:
        cfg = EnhancedBTConfig()
    from core.indicators import atr as atr_fn

    df = price_df.join(sig_df, how="inner").copy()
    df["sig_next"] = df["signal"].shift(1).fillna(0).astype(int)
    df["sl_next"] = df["sl"].shift(1)
    df["tp_next"] = df["tp"].shift(1)
    df["close_prev"] = df["close"].shift(1)   # maker_close limit price
    df["atr14"] = atr_fn(df["high"], df["low"], df["close"], 14)
    # Optional per-bar risk multiplier (e.g. for per-regime sizing).
    # If absent, treated as 1.0. Shifted by 1 (no look-ahead, matches sig).
    if "risk_mult" in df.columns:
        df["risk_mult_next"] = df["risk_mult"].shift(1).fillna(1.0)
    else:
        df["risk_mult_next"] = 1.0
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
    # Strategy-health gate state
    marks: list[float] = []          # equity mark history for trailing lookback
    health_paused = False
    block_long_until = block_short_until = -1   # post-SL/TP same-side cooldown

    for i, (ts, row) in enumerate(df.iterrows()):
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        sig_next = int(row["sig_next"])
        exited_intrabar = False   # SL/TP/time exit this bar -> no same-bar entry
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

        # Equity-aware risk scaling (single-tier or multi-tier ladder)
        eq_peak = max(eq_peak, equity)
        risk_scale = 1.0
        cur_dd = (equity / eq_peak - 1) if eq_peak > 0 else 0
        if cfg.eq_decay_tiers:
            risk_scale = decay_risk_scale(cur_dd, cfg.eq_decay_tiers)
        elif cfg.eq_risk_decay > 0:
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
            if cfg.tp_as_limit:
                hit_tp = (h > pos_tp) if pos_side == 1 else (l < pos_tp)
            else:
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
                if reason == "tp" and cfg.tp_as_limit:
                    fill = exit_px                      # limit fill, no slip
                    fee = fill * pos_qty * cfg.fee_maker
                else:
                    fill = _slip(exit_px, pos_side, cfg.slip_bps, False)
                    fee = fill * pos_qty * cfg.fee_rate
                gross = (fill - pos_entry) * pos_qty * pos_side
                pnl = gross - fee
                equity += pnl
                trades.append(Trade(
                    side=pos_side, entry_time=pos_open_time,
                    exit_time=ts, entry_px=pos_entry, exit_px=fill,
                    qty=pos_qty, notional=pos_notional, sl=pos_sl,
                    tp=pos_tp, pnl=pnl, fees=fee, reason=reason,
                    bars_held=pos_bars,
                ))
                cd = cfg.cooldown_bars if reason == "sl" else \
                    cfg.cooldown_bars_tp if reason == "tp" else 0
                if cd > 0:
                    if pos_side == 1:
                        block_long_until = i + cd
                    else:
                        block_short_until = i + cd
                exited_intrabar = reason in ("sl", "tp", "time")
                pos_side = 0
                pos_qty = 0.0
                partial_done = False
            elif cfg.trail_atr > 0 and pos_atr > 0:
                # still in the trade: ratchet the stop from this bar's close
                if pos_side == 1:
                    pos_sl = max(pos_sl, c - cfg.trail_atr * pos_atr)
                else:
                    pos_sl = min(pos_sl, c + cfg.trail_atr * pos_atr)

        # Strategy-health gate: pause new entries when trailing return is poor.
        if cfg.health_lookback_bars > 0 and len(marks) > cfg.health_lookback_bars:
            past = marks[-cfg.health_lookback_bars]
            trailing = (equity / past - 1) if past > 0 else 0.0
            if not health_paused and trailing < cfg.health_min_return:
                health_paused = True
            elif health_paused and trailing >= cfg.health_resume_return:
                health_paused = False

        # 2) New entry — only if flat AND not blocked AND have signal AND
        #    risk scaling hasn't hit a 0.0 tier (deep-drawdown hard stop) AND
        #    the strategy-health gate isn't paused AND not in a post-SL cooldown
        cd_blocked = (sig_next == 1 and i < block_long_until) or \
                     (sig_next == -1 and i < block_short_until)
        if exited_intrabar and not cfg.legacy_same_bar_reentry:
            cd_blocked = True   # this bar's open predates the exit fill
        if (pos_side == 0 and sig_next != 0 and not day_blocked
                and risk_scale > 0 and not health_paused and not cd_blocked):
            sl = row["sl_next"]
            tp = row["tp_next"]
            bar_atr = row["atr14"]
            if pd.notna(sl) and pd.notna(tp) and equity > 0 and pd.notna(bar_atr):
                maker = cfg.entry_style == "maker_close"
                if maker:
                    lim = row["close_prev"]
                    fill_ok = bool(pd.notna(lim)) and \
                        ((l < lim) if sig_next == 1 else (h > lim))
                    entry_fill = float(lim) if fill_ok else 1.0
                    entry_fee_rate = cfg.fee_maker
                else:
                    fill_ok = True
                    entry_fill = _slip(o, sig_next, cfg.slip_bps, True)
                    entry_fee_rate = cfg.fee_rate
                stop_dist = abs(entry_fill - sl) / entry_fill
                rmult = float(row["risk_mult_next"])
                if fill_ok and stop_dist > 1e-5 and rmult > 0:
                    risk = equity * cfg.risk_per_trade * risk_scale * rmult
                    notional = min(risk / stop_dist, equity * cfg.max_leverage)
                    qty = notional / entry_fill
                    fee = notional * entry_fee_rate
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
                    # Entry-bar SL/TP check (taker fills at the open, so the
                    # whole bar's range is post-entry; SL-first if both hit).
                    # Maker fills happen mid-bar: a TP print may PREDATE the
                    # fill, so maker entries never credit a same-bar TP; the
                    # same-bar SL is always debited (for a maker long the
                    # limit sits above the stop, so any path to the stop
                    # passed through the fill first).
                    if cfg.entry_bar_exit_check:
                        e_sl = (l <= pos_sl) if pos_side == 1 else (h >= pos_sl)
                        if cfg.tp_as_limit:
                            e_tp = (not maker) and \
                                ((h > pos_tp) if pos_side == 1 else (l < pos_tp))
                        else:
                            e_tp = (not maker) and \
                                ((h >= pos_tp) if pos_side == 1 else (l <= pos_tp))
                        if e_sl or e_tp:
                            x_px = pos_sl if e_sl else pos_tp
                            reason0 = "sl" if e_sl else "tp"
                            if reason0 == "tp" and cfg.tp_as_limit:
                                x_fill = x_px
                                fee0 = x_fill * pos_qty * cfg.fee_maker
                            else:
                                x_fill = _slip(x_px, pos_side, cfg.slip_bps,
                                               False)
                                fee0 = x_fill * pos_qty * cfg.fee_rate
                            gross0 = (x_fill - pos_entry) * pos_qty * pos_side
                            pnl0 = gross0 - fee0
                            equity += pnl0
                            trades.append(Trade(
                                side=pos_side, entry_time=ts, exit_time=ts,
                                entry_px=pos_entry, exit_px=x_fill, qty=pos_qty,
                                notional=pos_notional, sl=pos_sl, tp=pos_tp,
                                pnl=pnl0, fees=fee0, reason=reason0,
                                bars_held=0))
                            cd0 = cfg.cooldown_bars if reason0 == "sl" else \
                                cfg.cooldown_bars_tp if reason0 == "tp" else 0
                            if cd0 > 0:
                                if pos_side == 1:
                                    block_long_until = max(block_long_until,
                                                           i + cd0)
                                else:
                                    block_short_until = max(block_short_until,
                                                            i + cd0)
                            pos_side = 0
                            pos_qty = 0.0
                            partial_done = False

        # 3) Mark equity
        if pos_side != 0:
            unreal = (c - pos_entry) * pos_qty * pos_side
            mark = equity + unreal
        else:
            mark = equity
        eq_curve.append((ts, mark))
        if cfg.health_lookback_bars > 0:
            marks.append(mark)

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
