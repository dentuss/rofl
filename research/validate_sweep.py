"""Sweep the production preset across the longest-history KuCoin pairs.

For each pair: triple_bidir + directional regime filter + F&G filter +
equity decay + funding (1bp/8h modeled). Prints a comparison table that
shows where the framework works and where it doesn't.

Run from project root:
    python3 research/validate_sweep.py
    PAIRS="BTC-USDT,ETH-USDT" python3 research/validate_sweep.py
"""
from __future__ import annotations

import sys as _sys, os as _os
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

import time
import numpy as np
import pandas as pd

from core.backtest import Trade
from core.backtest_enhanced import EnhancedBTConfig, run_backtest_enhanced
from core.data import fetch_ohlcv
from core.regime_strategy import walk_forward_regimes
from core.sentiment import align_to_bars, fetch_fear_greed
from core.strategies import triple_confirm_bidir


DEFAULT_PAIRS = ["BTC-USDT", "ETH-USDT", "LTC-USDT", "SOL-USDT",
                 "LINK-USDT", "ADA-USDT", "INJ-USDT"]


def apply_funding(eq, trades, bps=1.0):
    if not trades: return eq, trades
    per_bar = (bps / 1e4) / 8
    new = []; deltas = []
    for t in trades:
        c = per_bar * t.notional * t.bars_held * t.side
        nt = Trade(side=t.side, entry_time=t.entry_time, exit_time=t.exit_time,
                   entry_px=t.entry_px, exit_px=t.exit_px, qty=t.qty,
                   notional=t.notional, sl=t.sl, tp=t.tp,
                   pnl=t.pnl - c, fees=t.fees + max(c,0), reason=t.reason,
                   bars_held=t.bars_held)
        new.append(nt); deltas.append((nt.exit_time, nt.pnl - t.pnl))
    eq2 = eq.copy(); off = pd.Series(0.0, index=eq2.index)
    for ts, d in deltas: off.loc[eq2.index >= ts] += d
    return eq2 + off, new


def make_signal(df, regimes, fng_aligned):
    sig = triple_confirm_bidir(df)
    a = regimes.reindex(sig.index, method="ffill").fillna("CHOP")
    block_reg = ((sig["signal"] ==  1) & (~a.isin(["BULL","CHOP"]))) | \
                ((sig["signal"] == -1) & (~a.isin(["BEAR","CHOP"])))
    sig.loc[block_reg, "signal"] = 0
    sig.loc[block_reg, ["sl","tp"]] = np.nan
    if fng_aligned is not None:
        block_fng = ((sig["signal"] ==  1) & (fng_aligned >= 80)) | \
                    ((sig["signal"] == -1) & (fng_aligned <= 20))
        sig.loc[block_fng, "signal"] = 0
        sig.loc[block_fng, ["sl","tp"]] = np.nan
    return sig


def run_one(symbol, fng):
    days = int(_os.environ.get("DAYS", 4000))
    print(f"\n--- {symbol} ---", flush=True)
    t0 = time.time()
    df = fetch_ohlcv(symbol, "1h", days=days)
    yrs = (df.index[-1] - df.index[0]).days / 365
    print(f"  fetched {len(df)} bars ({yrs:.1f}y) in {time.time()-t0:.0f}s", flush=True)

    t0 = time.time()
    regimes = walk_forward_regimes(df, bars_per_day=24, train_days=365, step_days=30)
    print(f"  regimes in {time.time()-t0:.0f}s  "
          f"dist={regimes.value_counts().to_dict()}", flush=True)

    fng_aligned = align_to_bars(fng, df.index) if fng is not None else None
    sig = make_signal(df, regimes, fng_aligned)

    cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                           max_leverage=5.0, eq_risk_decay=0.5,
                           drawdown_for_decay=0.20)
    eq, trades = run_backtest_enhanced(df, sig, cfg, long_only=False)
    eq, trades = apply_funding(eq, trades)

    final = float(eq.iloc[-1])
    ret = float(final / eq.iloc[0] - 1)
    mdd = float((eq / eq.cummax() - 1).min())
    bar_ret = eq.pct_change().fillna(0)
    sharpe = float(bar_ret.mean() / bar_ret.std() * np.sqrt(24 * 365)) if bar_ret.std() > 0 else 0
    days_held = (eq.index[-1] - eq.index[0]).days
    cagr = (final / 100) ** (365 / max(days_held, 1)) - 1
    months = eq.resample("ME").last().pct_change().dropna() * 100
    n_long = sum(1 for t in trades if t.side == 1)
    n_short = sum(1 for t in trades if t.side == -1)
    long_pnl = sum(t.pnl for t in trades if t.side == 1)
    short_pnl = sum(t.pnl for t in trades if t.side == -1)
    # losing streak
    is_loss = (months <= 0).astype(int).values
    streaks = []; cur = 0
    for x in is_loss:
        if x: cur += 1
        else:
            if cur > 0: streaks.append(cur); cur = 0
    if cur > 0: streaks.append(cur)
    worst_streak = max(streaks) if streaks else 0

    print(f"  final ${final:.0f}  ret +{ret*100:.0f}%  cagr {cagr*100:+.1f}%  "
          f"mdd {mdd*100:.1f}%  sharpe {sharpe:.2f}", flush=True)

    return {
        "pair": symbol,
        "years": round(yrs, 1),
        "final$": round(final, 0),
        "ret%": round(ret * 100, 0),
        "cagr%": round(cagr * 100, 1),
        "mdd%": round(mdd * 100, 1),
        "sharpe": round(sharpe, 2),
        "trades": len(trades),
        "n_long": n_long,
        "n_short": n_short,
        "long$": round(long_pnl, 0),
        "short$": round(short_pnl, 0),
        "med_mo%": round(float(months.median()), 2),
        "mean_mo%": round(float(months.mean()), 2),
        "win_mo%": round(float((months > 0).mean() * 100), 0),
        "best_mo%": round(float(months.max()), 1),
        "worst_mo%": round(float(months.min()), 1),
        "worst_streak": worst_streak,
    }


def main():
    pairs = _os.environ.get("PAIRS", ",".join(DEFAULT_PAIRS)).split(",")

    print(f"Sweeping {len(pairs)} pairs: {pairs}\n", flush=True)
    print("F&G fetch ...", flush=True)
    try:
        fng = fetch_fear_greed()
        print(f"  OK ({len(fng)} rows)", flush=True)
    except Exception as e:
        print(f"  fail: {e}; running without F&G", flush=True)
        fng = None

    results = []
    for p in pairs:
        try:
            r = run_one(p, fng)
            results.append(r)
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"  ERROR on {p}: {e}", flush=True)

    print("\n\n" + "=" * 140)
    print("CROSS-ASSET VALIDATION SUMMARY")
    print("(production preset: triple_bidir + dir-regime + F&G + decay, funding 1bp/8h)")
    print("=" * 140)
    df = pd.DataFrame(results)
    pd.set_option("display.width", 240)
    pd.set_option("display.max_columns", 30)
    print(df.to_string(index=False))

    # Aggregate confidence stats
    print("\n--- Framework robustness stats (across all pairs) ---")
    print(f"  pairs profitable (final > $100):   {(df['final$'] > 100).sum()} / {len(df)}")
    print(f"  pairs with Sharpe > 1.0:           {(df['sharpe'] > 1.0).sum()} / {len(df)}")
    print(f"  pairs with positive median month:  {(df['med_mo%'] > 0).sum()} / {len(df)}")
    print(f"  shorts contributed positively:     {(df['short$'] > 0).sum()} / {len(df)}")
    print(f"  CAGR  median {df['cagr%'].median():+.1f}%  mean {df['cagr%'].mean():+.1f}%  "
          f"range [{df['cagr%'].min():+.1f}%, {df['cagr%'].max():+.1f}%]")
    print(f"  MDD   median {df['mdd%'].median():+.1f}%  worst {df['mdd%'].min():+.1f}%")
    print(f"  Sharpe median {df['sharpe'].median():.2f}  range [{df['sharpe'].min():.2f}, {df['sharpe'].max():.2f}]")


if __name__ == "__main__":
    main()
