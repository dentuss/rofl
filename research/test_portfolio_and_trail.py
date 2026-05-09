"""Test multi-pair portfolio and trailing-stop enhancements vs the current best.

Reference:
  Single-pair sentiment Donchian on ETH/USDT 1h:
    +93%/y, Sharpe 2.41, MDD -10.84%, 83% months win
"""
from __future__ import annotations

import sys as _sys, os as _os
_THIS_DIR_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _THIS_DIR_PARENT not in _sys.path: _sys.path.insert(0, _THIS_DIR_PARENT)

import numpy as np
import pandas as pd

from core.backtest import BTConfig, Trade, run_backtest
from core.data import fetch_ohlcv
from core.portfolio import run_portfolio
from core.sentiment import fetch_fear_greed
from core.strategies_sentiment import donchian_skip_fear

PARAMS = dict(entry_n=20, exit_n=10, adx_n=14, adx_min=20.0,
              atr_n=14, sl_mult=2.5, tp_mult=5.0)


# -------------------- Trailing-stop variant of backtest ---------------------
def run_backtest_trail(price_df, sig_df, cfg=BTConfig(), long_only=False,
                       trail_after_atr: float = 1.5,
                       trail_distance_atr: float = 1.5):
    """Backtest with a trailing stop:
       - After unrealised PnL >= trail_after_atr * ATR_at_entry, move stop up
         (long) / down (short) to (current_close +/- trail_distance_atr * ATR).
    """
    from indicators import atr as atr_fn
    df = price_df.join(sig_df, how="inner").copy()
    df["sig_next"] = df["signal"].shift(1).fillna(0).astype(int)
    df["sl_next"] = df["sl"].shift(1)
    df["tp_next"] = df["tp"].shift(1)
    df["atr14"] = atr_fn(df["high"], df["low"], df["close"], 14)
    if long_only or not cfg.allow_short:
        df["sig_next"] = df["sig_next"].clip(lower=0)

    equity = cfg.starting_equity
    eq_curve = []
    trades: list[Trade] = []
    pos_side = 0
    pos_qty = pos_entry = pos_sl = pos_tp = pos_atr = 0.0
    pos_open_time = None
    pos_bars = 0
    pos_notional = 0.0

    def slip(p, side, slip_bps, is_entry):
        s = slip_bps / 10_000
        return p * (1 + s * side) if is_entry else p * (1 - s * side)

    for ts, row in df.iterrows():
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        sig_next = int(row["sig_next"])

        if pos_side != 0:
            pos_bars += 1
            # update trailing stop on this bar (using the close)
            if pos_atr > 0:
                profit = (c - pos_entry) * pos_side
                if profit >= trail_after_atr * pos_atr:
                    new_sl = (c - trail_distance_atr * pos_atr) if pos_side == 1 \
                        else (c + trail_distance_atr * pos_atr)
                    if pos_side == 1:
                        pos_sl = max(pos_sl, new_sl)
                    else:
                        pos_sl = min(pos_sl, new_sl)

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
                fill = slip(exit_px, pos_side, cfg.slip_bps, False)
                gross = (fill - pos_entry) * pos_qty * pos_side
                fee = fill * pos_qty * cfg.fee_rate
                pnl = gross - fee
                equity += pnl
                trades.append(Trade(side=pos_side, entry_time=pos_open_time,
                                    exit_time=ts, entry_px=pos_entry, exit_px=fill,
                                    qty=pos_qty, notional=pos_notional, sl=pos_sl,
                                    tp=pos_tp, pnl=pnl, fees=fee, reason=reason,
                                    bars_held=pos_bars))
                pos_side = 0
                pos_qty = 0.0

        if pos_side == 0 and sig_next != 0:
            sl = row["sl_next"]; tp = row["tp_next"]; bar_atr = row["atr14"]
            if pd.notna(sl) and pd.notna(tp) and equity > 0 and pd.notna(bar_atr):
                entry_fill = slip(o, sig_next, cfg.slip_bps, True)
                stop_dist = abs(entry_fill - sl) / entry_fill
                if stop_dist > 1e-5:
                    risk = equity * cfg.risk_per_trade
                    notional = min(risk / stop_dist, equity * cfg.max_leverage)
                    qty = notional / entry_fill
                    fee = notional * cfg.fee_rate
                    equity -= fee
                    pos_side = sig_next; pos_qty = qty; pos_entry = entry_fill
                    pos_sl = sl; pos_tp = tp; pos_atr = float(bar_atr)
                    pos_open_time = ts; pos_bars = 0; pos_notional = notional

        mark = equity + ((c - pos_entry) * pos_qty * pos_side if pos_side != 0 else 0)
        eq_curve.append((ts, mark))

    if pos_side != 0:
        last_ts = df.index[-1]
        fill = slip(df["close"].iloc[-1], pos_side, cfg.slip_bps, False)
        gross = (fill - pos_entry) * pos_qty * pos_side
        fee = fill * pos_qty * cfg.fee_rate
        equity += gross - fee
        eq_curve[-1] = (last_ts, equity)

    eq = pd.Series([v for _, v in eq_curve], index=[t for t, _ in eq_curve])
    peak = eq.cummax()
    dd = float((eq / peak - 1).min())
    ret = float(eq.iloc[-1] / eq.iloc[0] - 1)
    pnls = np.array([t.pnl for t in trades]) if trades else np.array([0.0])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    bar_ret = eq.pct_change().fillna(0)
    sharpe = 0.0
    if bar_ret.std() > 0:
        sharpe = (bar_ret.mean() / bar_ret.std()) * np.sqrt(24 * 365)
    return {
        "trades": len(trades),
        "win_rate": round(len(wins) / max(len(trades), 1), 3),
        "avg_win": round(float(wins.mean()) if len(wins) else 0, 4),
        "avg_loss": round(float(losses.mean()) if len(losses) else 0, 4),
        "profit_factor": round(float(wins.sum() / max(-losses.sum(), 1e-9)), 3),
        "total_return": round(ret, 4),
        "final_equity": round(float(eq.iloc[-1]), 2),
        "max_drawdown": round(dd, 4),
        "sharpe": round(float(sharpe), 2),
        "_eq": eq,
    }


def rolling_win_pct(eq, win_days=30, bars_per_day=24):
    w = win_days * bars_per_day
    rets = []
    for i in range(0, len(eq) - w + 1, w):
        sub = eq.iloc[i:i + w]
        rets.append(sub.iloc[-1] / sub.iloc[0] - 1)
    rs = pd.Series(rets)
    return {
        "n": len(rs),
        "win_pct": round((rs > 0).mean(), 3),
        "median": round(rs.median(), 4),
        "min": round(rs.min(), 4),
        "max": round(rs.max(), 4),
    }


def main():
    pd.set_option("display.width", 220)
    fng = fetch_fear_greed()

    print("=" * 80)
    print("PART 1: Single-pair sentiment Donchian + trailing stop")
    print("=" * 80)
    df = fetch_ohlcv("ETH-USDT", "1h", days=365)
    sig = donchian_skip_fear(df, fng, fear_min=25, **PARAMS)
    base = run_backtest(df, sig, BTConfig()).stats()
    print(f"baseline (no trail):           {base}")
    print(f"  rolling 30d:                 {rolling_win_pct(_make_eq(df, sig))}")
    for ta in (1.0, 1.5, 2.0, 2.5):
        for td in (1.0, 1.5, 2.0):
            r = run_backtest_trail(df, sig, BTConfig(),
                                   trail_after_atr=ta, trail_distance_atr=td)
            eq = r.pop("_eq")
            print(f"trail after {ta}xATR, dist {td}xATR: ret={r['total_return']:+.3f} "
                  f"sharpe={r['sharpe']:.2f} mdd={r['max_drawdown']:+.3f} "
                  f"trades={r['trades']} wr={r['win_rate']:.3f} "
                  f"rolling30d={rolling_win_pct(eq)}")

    print("\n" + "=" * 80)
    print("PART 2: Multi-pair portfolio (sentiment Donchian on each)")
    print("=" * 80)
    def signal_fn(local_df):
        return donchian_skip_fear(local_df, fng, fear_min=25, **PARAMS)

    for pairs, weights in [
        (["ETH-USDT"],                       [1.0]),
        (["ETH-USDT", "SOL-USDT"],           [0.5, 0.5]),
        (["ETH-USDT", "SOL-USDT"],           [0.7, 0.3]),
        (["ETH-USDT", "BTC-USDT", "SOL-USDT"], [0.5, 0.25, 0.25]),
    ]:
        r = run_portfolio(pairs, "1h", 365, signal_fn, weights=weights)
        print(f"{pairs} {weights}:")
        print(f"  totals: {r.stats}")
        print(f"  rolling 30d: {rolling_win_pct(r.equity_curve)}")
        for p, eq in r.per_asset.items():
            ret = eq.iloc[-1] / eq.iloc[0] - 1
            mdd = (eq / eq.cummax() - 1).min()
            print(f"    {p}: ret={ret:+.3f} mdd={mdd:+.3f}")


def _make_eq(df, sig):
    return run_backtest(df, sig, BTConfig()).equity_curve


if __name__ == "__main__":
    main()
