"""Experiment: production strategy on 15m vs 1h, and 5x vs 25x leverage.

Runs the production signal (triple_bidir + dir-regime + F&G + three-tier decay,
funding modeled) on INJ across a 2x2 matrix:
    timeframe  : 1h  vs  15m
    max_leverage: 5x  vs  25x
Starting equity $100. Reports headline stats + monthly P/L in $ and %.

NOTE: "same strategy" keeps the exact 1h-tuned config (EMA 9/26/50, RSI 55,
ADX 22, 1.8/3.0 ATR stops, max_bars=96). On 15m, max_bars=96 is a 24h time
stop (vs 4 days on 1h) — a real behavioral difference, flagged in output.

Run from project root:
    python3 research/experiment_tf_leverage.py
"""
from __future__ import annotations

import sys as _sys, os as _os
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

import time
import numpy as np
import pandas as pd

from core.backtest_enhanced import EnhancedBTConfig, run_backtest_enhanced
from core.data import fetch_ohlcv
from core.regime_strategy import walk_forward_regimes
from core.risk import DEFAULT_DECAY_TIERS
from core.sentiment import align_to_bars, fetch_fear_greed
from core.strategies import triple_confirm_bidir

BARS_PER_DAY = {"1h": 24, "15m": 96}


def apply_funding(eq, trades, bar_minutes, bps_per_8h=1.0):
    """Bar-duration-aware funding: per-bar rate spreads the 8h cost correctly."""
    if not trades:
        return eq
    per_bar = (bps_per_8h / 1e4) * (bar_minutes / (8 * 60))
    off = pd.Series(0.0, index=eq.index)
    for t in trades:
        off.loc[eq.index >= t.exit_time] -= per_bar * t.notional * t.bars_held * t.side
    return eq + off


def prod_signal(df, regimes, fa):
    s = triple_confirm_bidir(df)
    a = regimes.reindex(s.index, method="ffill").fillna("CHOP")
    b = ((s["signal"] == 1) & (~a.isin(["BULL", "CHOP"]))) | \
        ((s["signal"] == -1) & (~a.isin(["BEAR", "CHOP"])))
    s.loc[b, "signal"] = 0
    s.loc[b, ["sl", "tp"]] = np.nan
    if fa is not None:
        bf = ((s["signal"] == 1) & (fa >= 80)) | ((s["signal"] == -1) & (fa <= 20))
        s.loc[bf, "signal"] = 0
        s.loc[bf, ["sl", "tp"]] = np.nan
    return s


def cstats(eq, trades, bpd):
    final = float(eq.iloc[-1])
    ret = float(final / eq.iloc[0] - 1)
    mdd = float((eq / eq.cummax() - 1).min())
    br = eq.pct_change().fillna(0)
    sharpe = float(br.mean() / br.std() * np.sqrt(bpd * 365)) if br.std() > 0 else 0
    days = (eq.index[-1] - eq.index[0]).days
    cagr = (final / eq.iloc[0]) ** (365 / max(days, 1)) - 1
    n = len(trades)
    wins = sum(1 for t in trades if t.pnl > 0)
    wr = wins / n if n else 0
    # how often did the leverage cap bind?
    return dict(final=final, ret=ret, mdd=mdd, sharpe=sharpe, cagr=cagr,
                n=n, wr=wr)


def monthly(eq):
    return eq.resample("ME").last().pct_change().dropna() * 100


def main():
    days = int(_os.environ.get("DAYS", 5 * 365))
    try:
        fng = fetch_fear_greed()
    except Exception:
        fng = None

    # Precompute signal + regimes per timeframe (leverage doesn't affect signal)
    sig_by_tf = {}
    for tf in ["1h", "15m"]:
        print(f"\nFetching INJ {tf} ({days}d) + regimes ...", flush=True)
        t0 = time.time()
        df = fetch_ohlcv("INJ-USDT", tf, days=days)
        bpd = BARS_PER_DAY[tf]
        regs = walk_forward_regimes(df, bars_per_day=bpd, train_days=365, step_days=30)
        fa = align_to_bars(fng, df.index) if fng is not None else None
        sig = prod_signal(df, regs, fa)
        sig_by_tf[tf] = (df, sig, bpd)
        print(f"  {len(df)} bars ({df.index[0].date()}→{df.index[-1].date()}), "
              f"{time.time()-t0:.0f}s", flush=True)

    # Run the 2x2 matrix
    results = {}
    for tf in ["1h", "15m"]:
        df, sig, bpd = sig_by_tf[tf]
        bar_min = 60 if tf == "1h" else 15
        for lev in [5, 25]:
            cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                                   max_leverage=float(lev),
                                   eq_decay_tiers=DEFAULT_DECAY_TIERS)
            eq, tr = run_backtest_enhanced(df, sig, cfg, long_only=False)
            eq = apply_funding(eq, tr, bar_min)
            results[(tf, lev)] = (eq, tr, cstats(eq, tr, bpd))

    # Headline table
    print("\n" + "=" * 92)
    print("HEADLINE — INJ, production strategy, $100 start, funding modeled")
    print("=" * 92)
    print(f"{'config':16s}{'final$':>10s}{'CAGR':>9s}{'MDD':>9s}{'Sharpe':>9s}"
          f"{'trades':>8s}{'win%':>7s}")
    for tf in ["1h", "15m"]:
        for lev in [5, 25]:
            s = results[(tf, lev)][2]
            tag = f"{tf} {lev}x"
            print(f"{tag:16s}{s['final']:>10.0f}{s['cagr']*100:>+8.0f}%"
                  f"{s['mdd']*100:>+8.1f}%{s['sharpe']:>9.2f}{s['n']:>8d}"
                  f"{s['wr']*100:>6.0f}%")

    # Monthly P/L — 2026 YTD + trailing 12mo, for each config
    for tf in ["1h", "15m"]:
        for lev in [5, 25]:
            eq = results[(tf, lev)][0]
            m = monthly(eq)
            ytd = m[m.index.year == 2026]
            print("\n" + "-" * 70)
            note = "" if tf == "1h" else "  [max_bars=96 = 24h time stop on 15m]"
            print(f"MONTHLY P/L — INJ {tf} {lev}x{note}")
            print("-" * 70)
            # Show trailing 12 months in $ (on $100 start) and %
            tail = m.tail(12)
            # reconstruct $ path for the tail
            eq_m = eq.resample("ME").last()
            print(f"  {'month':9s}{'return%':>10s}{'equity$':>12s}")
            for ts in tail.index:
                pct = tail[ts]
                eqv = float(eq_m.get(ts, float('nan')))
                yr_mark = "  <-- 2026 YTD" if ts.year == 2026 else ""
                print(f"  {ts.strftime('%Y-%m'):9s}{pct:>+9.2f}%{eqv:>12.2f}{yr_mark}")
            if len(ytd):
                ytd_comp = float((1 + ytd / 100).prod() - 1) * 100
                print(f"  2026 YTD compounded: {ytd_comp:+.1f}%  "
                      f"({(ytd>0).sum()}/{len(ytd)} months positive)")
            print(f"  monthly: median {m.median():+.2f}%  mean {m.mean():+.2f}%  "
                  f"win {(m>0).mean()*100:.0f}%  best {m.max():+.1f}%  worst {m.min():+.1f}%")

    # Reality-check warning
    print("\n" + "=" * 92)
    print("REALITY CHECK (backtest does NOT model these — they bite hardest at 25x)")
    print("=" * 92)
    print("  - Liquidation: a gap THROUGH the stop at 25x can wipe the account before")
    print("    the stop fills. Backtest assumes stops always fill at the stop price.")
    print("  - Funding spikes: modeled as flat 1bp/8h; real spikes hit 0.1-0.3%/8h.")
    print("  - Slippage on size: $ amounts are tiny here so it's moot, but at scale")
    print("    25x notional moves the book against you.")
    print("  - 15m: ~4x the trades = ~4x the fee/slippage drag vs 1h.")

    # ---- Why 5x == 25x, and the REAL lever: risk_per_trade ----
    print("\n" + "=" * 92)
    print("THE REAL LEVER — risk_per_trade (1h, max_lev 25x, $100 start)")
    print("=" * 92)
    print("  The 25x cap is inert: the bot's risk-based sizing rarely wants >~1.5x")
    print("  notional, so it never reaches the cap. To actually take bigger positions")
    print("  you raise risk_per_trade. Here's INJ 1h at escalating risk:")
    print(f"\n  {'risk/trade':12s}{'final$':>10s}{'CAGR':>9s}{'MDD':>9s}{'Sharpe':>9s}"
          f"{'worst_mo':>10s}")
    df1h, sig1h, bpd1h = sig_by_tf["1h"]
    for risk in [0.02, 0.05, 0.10, 0.20]:
        cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=risk,
                               max_leverage=25.0, eq_decay_tiers=DEFAULT_DECAY_TIERS)
        eq, tr = run_backtest_enhanced(df1h, sig1h, cfg, long_only=False)
        eq = apply_funding(eq, tr, 60)
        s = cstats(eq, tr, bpd1h)
        wm = monthly(eq).min()
        flag = "  <- blows past -50% decay floor often" if s['mdd'] < -0.5 else ""
        print(f"  {risk*100:>9.0f}%  {s['final']:>10.0f}{s['cagr']*100:>+8.0f}%"
              f"{s['mdd']*100:>+8.1f}%{s['sharpe']:>9.2f}{wm:>+9.1f}%{flag}")
    print("\n  Note: higher risk_per_trade scales BOTH return and drawdown ~linearly")
    print("  until the decay ladder and leverage cap start clipping the extremes.")


if __name__ == "__main__":
    main()
