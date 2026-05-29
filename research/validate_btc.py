"""Cross-asset validation: run the production preset on BTC-USDT 8.7y.

Same setup as adaptive_inj_bidir (triple_bidir + directional regime filter
+ F&G extreme-zone filter + equity decay + funding cost). Answers:
does the framework generalize, or is it INJ-curve-fit?

Run from project root:
    python3 research/validate_btc.py
    SYMBOL=ETH-USDT python3 research/validate_btc.py
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


def make_signal(df, regimes, fng_aligned,
                ema_fast=9, ema_slow=26, rsi_min=55.0,
                fng_greed_max=80, fng_fear_min=20):
    sig = triple_confirm_bidir(df, ema_fast=ema_fast, ema_slow=ema_slow,
                                rsi_min=rsi_min)
    a = regimes.reindex(sig.index, method="ffill").fillna("CHOP")
    block_reg = ((sig["signal"] ==  1) & (~a.isin(["BULL","CHOP"]))) | \
                ((sig["signal"] == -1) & (~a.isin(["BEAR","CHOP"])))
    sig.loc[block_reg, "signal"] = 0
    sig.loc[block_reg, ["sl","tp"]] = np.nan
    if fng_aligned is not None:
        block_fng = ((sig["signal"] ==  1) & (fng_aligned >= fng_greed_max)) | \
                    ((sig["signal"] == -1) & (fng_aligned <= fng_fear_min))
        sig.loc[block_fng, "signal"] = 0
        sig.loc[block_fng, ["sl","tp"]] = np.nan
    return sig


def yearly(eq):
    rows = []
    for y in sorted({d.year for d in eq.index}):
        s = eq[eq.index.year == y]
        if len(s) < 100: continue
        ret = s.iloc[-1] / s.iloc[0] - 1
        mdd = (s / s.cummax() - 1).min()
        rows.append({"year": y, "ret%": round(ret*100,1), "mdd%": round(float(mdd)*100,1)})
    return pd.DataFrame(rows)


def main():
    symbol = _os.environ.get("SYMBOL", "BTC-USDT")
    days = int(_os.environ.get("DAYS", 4000))  # 4000d = ~11y, capped by data
    print(f"\nFetching {symbol} 1h, up to {days}d ...")
    df = fetch_ohlcv(symbol, "1h", days=days)
    actual_years = (df.index[-1] - df.index[0]).days / 365
    print(f"  {len(df)} bars  ({df.index[0]} → {df.index[-1]}, {actual_years:.1f}y)")

    print("Walk-forward regimes ...")
    t0 = time.time()
    regimes = walk_forward_regimes(df, bars_per_day=24, train_days=365, step_days=30)
    print(f"  {time.time()-t0:.0f}s  dist={regimes.value_counts().to_dict()}")

    print("F&G fetch ...")
    try:
        fng = fetch_fear_greed()
        fng_aligned = align_to_bars(fng, df.index)
        # F&G only goes back to 2018-02-01; older bars get 50 (neutral) — OK
        print(f"  OK ({len(fng)} rows)")
    except Exception as e:
        print(f"  fail: {e}")
        fng_aligned = None

    cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                           max_leverage=5.0, eq_risk_decay=0.5,
                           drawdown_for_decay=0.20)

    print("\nBuilding production signal (triple_bidir + dir-regime + F&G) ...")
    sig = make_signal(df, regimes, fng_aligned)
    n_long = int((sig["signal"] == 1).sum())
    n_short = int((sig["signal"] == -1).sum())
    print(f"  signal counts: long={n_long}  short={n_short}")

    print("\nBacktesting ...")
    eq, tr = run_backtest_enhanced(df, sig, cfg, long_only=False)
    eq, tr, = apply_funding(eq, tr)

    # ===== headline =====
    n_long = sum(1 for t in tr if t.side == 1)
    n_short = sum(1 for t in tr if t.side == -1)
    long_pnl = sum(t.pnl for t in tr if t.side == 1)
    short_pnl = sum(t.pnl for t in tr if t.side == -1)
    final = float(eq.iloc[-1])
    ret = float(final / eq.iloc[0] - 1)
    mdd = float((eq / eq.cummax() - 1).min())
    bar_ret = eq.pct_change().fillna(0)
    sharpe = float(bar_ret.mean() / bar_ret.std() * np.sqrt(24 * 365)) if bar_ret.std() > 0 else 0
    days_held = (eq.index[-1] - eq.index[0]).days
    cagr = (final / 100) ** (365 / days_held) - 1 if days_held > 30 else 0

    print("\n" + "=" * 95)
    print(f"HEADLINE — {symbol} ({actual_years:.1f}y, production preset, $100 start, funding modeled)")
    print("=" * 95)
    print(f"  final ${final:.0f}  ret +{ret*100:.0f}%  cagr {cagr*100:+.1f}%  "
          f"mdd {mdd*100:.1f}%  sharpe {sharpe:.2f}")
    print(f"  trades n={len(tr)}  long={n_long} (${long_pnl:+.0f})  "
          f"short={n_short} (${short_pnl:+.0f})")

    # ===== yearly =====
    print("\n" + "=" * 95)
    print("YEAR-BY-YEAR")
    print("=" * 95)
    yr = yearly(eq)
    print(yr.to_string(index=False))

    # ===== month distribution =====
    months = eq.resample("ME").last().pct_change().dropna() * 100
    print("\n" + "=" * 95)
    print("MONTHLY DISTRIBUTION")
    print("=" * 95)
    pct = lambda p: float(np.percentile(months.values, p))
    print(f"  n_months = {len(months)}")
    print(f"  p10/p25/median/p75/p90 = "
          f"{pct(10):+.2f}% / {pct(25):+.2f}% / {pct(50):+.2f}% / "
          f"{pct(75):+.2f}% / {pct(90):+.2f}%")
    print(f"  mean = {months.mean():+.2f}%/mo   std = {months.std():.2f}pp")
    print(f"  win_rate = {(months > 0).mean()*100:.0f}%   "
          f"best = {months.max():+.1f}%   worst = {months.min():+.1f}%")
    # losing-streak
    is_loss = (months <= 0).astype(int).values
    streaks = []; cur = 0
    for x in is_loss:
        if x: cur += 1
        else:
            if cur > 0: streaks.append(cur); cur = 0
    if cur > 0: streaks.append(cur)
    if streaks:
        print(f"  worst losing streak: {max(streaks)} consecutive months")

    # ===== last 12 months =====
    print("\n" + "=" * 95)
    print("LAST 12 MONTHS")
    print("=" * 95)
    for ts, val in months.tail(12).items():
        print(f"  {ts.strftime('%Y-%m')}:  {val:+6.2f}%")


if __name__ == "__main__":
    main()
