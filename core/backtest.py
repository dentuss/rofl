"""Event-driven-ish vectorized backtester.

- Signals are read from a strategy DataFrame and applied on the *next* bar's
  open (no look-ahead).
- Stops and take-profits are checked intra-bar against high/low. If both are
  hit in the same bar, we conservatively assume the stop fills first.
- Position sizing is risk-based: notional = (equity * risk) / stop_distance.
- Leverage is capped (max_lev). Fees and slippage are applied on each fill.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class BTConfig:
    starting_equity: float = 100.0
    risk_per_trade: float = 0.015      # 1.5% of equity at risk per trade
    max_leverage: float = 5.0           # cap notional at 5x equity
    fee_rate: float = 0.0006            # 0.06% per side (taker, futures)
    slip_bps: float = 2.0               # 2bp = 0.02% slippage per side
    max_bars_in_trade: int = 96         # time stop (96 bars = 24h on 15m, 4d on 1h)
    allow_short: bool = True
    # Post-stop same-side re-entry cooldown: after a SL on side X, block new
    # side-X entries for this many bars. 0 disables (default — preserves all
    # prior results). Validated lift (see research/FINDINGS.md "cooldown").
    cooldown_bars: int = 0


@dataclass
class Trade:
    side: int
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_px: float
    exit_px: float
    qty: float
    notional: float
    sl: float
    tp: float
    pnl: float
    fees: float
    reason: str  # 'tp', 'sl', 'signal', 'time'
    bars_held: int


@dataclass
class BTResult:
    equity_curve: pd.Series
    trades: list[Trade]
    config: BTConfig

    def stats(self) -> dict:
        n = len(self.trades)
        eq = self.equity_curve
        ret = float(eq.iloc[-1] / eq.iloc[0] - 1)
        peak = eq.cummax()
        dd = float((eq / peak - 1).min())
        if n == 0:
            return {
                "trades": 0, "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "profit_factor": 0.0, "total_return": ret,
                "final_equity": round(float(eq.iloc[-1]), 2),
                "max_drawdown": round(dd, 4), "sharpe": 0.0,
            }
        pnls = np.array([t.pnl for t in self.trades])
        wins = pnls[pnls > 0]
        losses = pnls[pnls <= 0]
        wr = len(wins) / n
        avg_w = wins.mean() if len(wins) else 0.0
        avg_l = losses.mean() if len(losses) else 0.0
        gross_w = wins.sum()
        gross_l = -losses.sum()
        pf = (gross_w / gross_l) if gross_l > 0 else float("inf")
        # Sharpe on per-bar returns, annualized for 1h bars
        bar_ret = eq.pct_change().fillna(0)
        bars_per_year = 24 * 365
        sharpe = 0.0
        if bar_ret.std() > 0:
            sharpe = (bar_ret.mean() / bar_ret.std()) * np.sqrt(bars_per_year)
        return {
            "trades": n,
            "win_rate": round(wr, 3),
            "avg_win": round(float(avg_w), 4),
            "avg_loss": round(float(avg_l), 4),
            "profit_factor": round(float(pf), 3),
            "total_return": round(float(ret), 4),
            "final_equity": round(float(eq.iloc[-1]), 2),
            "max_drawdown": round(float(dd), 4),
            "sharpe": round(float(sharpe), 2),
        }


def _apply_slippage(price: float, side: int, slip_bps: float, is_entry: bool) -> float:
    """Slippage: entry pays up, exit gets less (worse fill in our direction)."""
    slip = slip_bps / 10_000.0
    if is_entry:
        return price * (1 + slip * side)  # side=+1 long entry: pay more
    else:
        return price * (1 - slip * side)  # exit a long (side=+1): receive less


def run_backtest(price_df: pd.DataFrame, sig_df: pd.DataFrame,
                 cfg: BTConfig = BTConfig(), long_only: bool = False) -> BTResult:
    """price_df: OHLCV. sig_df: from strategy (signal/sl/tp). Same index."""
    df = price_df.join(sig_df, how="inner").copy()
    # No look-ahead: act on next bar's open with previous bar's signal
    df["sig_next"] = df["signal"].shift(1).fillna(0).astype(int)
    df["sl_next"] = df["sl"].shift(1)
    df["tp_next"] = df["tp"].shift(1)

    if long_only or not cfg.allow_short:
        df["sig_next"] = df["sig_next"].clip(lower=0)

    equity = cfg.starting_equity
    eq_curve = []
    trades: list[Trade] = []

    pos_side = 0
    pos_qty = 0.0
    pos_entry = 0.0
    pos_sl = 0.0
    pos_tp = 0.0
    pos_open_time: Optional[pd.Timestamp] = None
    pos_bars = 0
    pos_notional = 0.0
    block_long_until = block_short_until = -1   # post-SL same-side cooldown

    for i, (ts, row) in enumerate(df.iterrows()):
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        sig_next = int(row["sig_next"])

        # 1) If a position is open, check intra-bar SL/TP using THIS bar's H/L
        if pos_side != 0:
            pos_bars += 1
            hit_sl = (l <= pos_sl) if pos_side == 1 else (h >= pos_sl)
            hit_tp = (h >= pos_tp) if pos_side == 1 else (l <= pos_tp)

            exit_px = None
            reason = ""
            if hit_sl and hit_tp:
                # Conservative: assume SL fills first
                exit_px = pos_sl
                reason = "sl"
            elif hit_sl:
                exit_px = pos_sl
                reason = "sl"
            elif hit_tp:
                exit_px = pos_tp
                reason = "tp"
            elif pos_bars >= cfg.max_bars_in_trade:
                exit_px = c
                reason = "time"
            elif sig_next != 0 and sig_next != pos_side:
                # signal flip closes the trade at this bar's open
                exit_px = o
                reason = "signal"

            if exit_px is not None:
                exit_fill = _apply_slippage(exit_px, pos_side, cfg.slip_bps, is_entry=False)
                gross_pnl = (exit_fill - pos_entry) * pos_qty * pos_side
                exit_fee = exit_fill * pos_qty * cfg.fee_rate
                pnl = gross_pnl - exit_fee  # entry fee was deducted at entry
                equity += pnl
                trades.append(Trade(
                    side=pos_side, entry_time=pos_open_time, exit_time=ts,
                    entry_px=pos_entry, exit_px=exit_fill, qty=pos_qty,
                    notional=pos_notional, sl=pos_sl, tp=pos_tp,
                    pnl=pnl, fees=exit_fee, reason=reason, bars_held=pos_bars,
                ))
                if reason == "sl" and cfg.cooldown_bars > 0:
                    if pos_side == 1:
                        block_long_until = i + cfg.cooldown_bars
                    else:
                        block_short_until = i + cfg.cooldown_bars
                pos_side = 0
                pos_qty = 0.0

        # 2) If flat and we have a fresh signal at this bar's open, enter
        blocked = (sig_next == 1 and i < block_long_until) or \
                  (sig_next == -1 and i < block_short_until)
        if pos_side == 0 and sig_next != 0 and not blocked:
            sl = row["sl_next"]
            tp = row["tp_next"]
            if pd.notna(sl) and pd.notna(tp) and equity > 0:
                entry_fill = _apply_slippage(o, sig_next, cfg.slip_bps, is_entry=True)
                stop_dist = abs(entry_fill - sl) / entry_fill
                if stop_dist > 1e-5:
                    risk_dollars = equity * cfg.risk_per_trade
                    notional = risk_dollars / stop_dist
                    notional = min(notional, equity * cfg.max_leverage)
                    qty = notional / entry_fill
                    entry_fee = notional * cfg.fee_rate
                    equity -= entry_fee  # pay entry fee immediately
                    pos_side = sig_next
                    pos_qty = qty
                    pos_entry = entry_fill
                    pos_sl = sl
                    pos_tp = tp
                    pos_open_time = ts
                    pos_bars = 0
                    pos_notional = notional

        # 3) Mark equity (use close price for unrealized PnL)
        if pos_side != 0:
            unreal = (c - pos_entry) * pos_qty * pos_side
            mark = equity + unreal
        else:
            mark = equity
        eq_curve.append((ts, mark))

    # Close any open position at the last bar
    if pos_side != 0:
        last_ts = df.index[-1]
        exit_fill = _apply_slippage(df["close"].iloc[-1], pos_side, cfg.slip_bps, is_entry=False)
        gross_pnl = (exit_fill - pos_entry) * pos_qty * pos_side
        exit_fee = exit_fill * pos_qty * cfg.fee_rate
        pnl = gross_pnl - exit_fee
        equity += pnl
        trades.append(Trade(
            side=pos_side, entry_time=pos_open_time, exit_time=last_ts,
            entry_px=pos_entry, exit_px=exit_fill, qty=pos_qty,
            notional=pos_notional, sl=pos_sl, tp=pos_tp,
            pnl=pnl, fees=exit_fee, reason="eod", bars_held=pos_bars,
        ))
        eq_curve[-1] = (last_ts, equity)

    eq = pd.Series([v for _, v in eq_curve], index=[t for t, _ in eq_curve], name="equity")
    return BTResult(equity_curve=eq, trades=trades, config=cfg)
