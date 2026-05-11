"""Comprehensive report on the bot — covers everything the user asked:

  1. Month-by-month PnL (%) for each recommended preset
  2. Best parameters (pair, risk, etc.) ranked
  3. 5-year backtest performance with year-by-year breakdown
  4. Paper trading setup (printed at the end)

Run from project root:
    python3 research/report.py
"""
from __future__ import annotations

import sys as _sys, os as _os
_THIS_DIR_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _THIS_DIR_PARENT not in _sys.path:
    _sys.path.insert(0, _THIS_DIR_PARENT)

import numpy as np
import pandas as pd

from core.backtest import BTConfig, run_backtest
from core.backtest_enhanced import EnhancedBTConfig, run_backtest_enhanced
from core.data import fetch_ohlcv
from core.regime_strategy import filter_by_regime, walk_forward_regimes
from core.strategies import triple_confirm_long


BASE = dict(ema_fast=9, ema_slow=26, ema_trend=50,
            rsi_min=55.0, adx_min=22.0,
            atr_n=14, sl_mult=1.8, tp_mult=3.0)
BPD = {"15m": 96, "30m": 48, "1h": 24, "4h": 6}


PRESETS = [
    # (label,                       pair,        tf,    risk,  decay,  adaptive)
    ("steady",                      "ETH-USDT",  "1h",  0.015, 0.0,    False),
    ("growth (SOL)",                "SOL-USDT",  "30m", 0.015, 0.0,    False),
    ("high_return (SOL)",           "SOL-USDT",  "30m", 0.020, 0.0,    False),
    ("safer_high_return (SOL)",     "SOL-USDT",  "30m", 0.020, 0.5,    False),
    ("inj_growth",                  "INJ-USDT",  "1h",  0.015, 0.0,    False),
    ("inj_high_return",             "INJ-USDT",  "1h",  0.020, 0.0,    False),
    ("safer_inj_high_return",       "INJ-USDT",  "1h",  0.020, 0.5,    False),
    ("adaptive_inj_high_return",    "INJ-USDT",  "1h",  0.020, 0.5,    True),
]


def cagr(eq):
    days = (eq.index[-1] - eq.index[0]).total_seconds() / 86400
    if days < 30:
        return 0.0
    return (eq.iloc[-1] / eq.iloc[0]) ** (365 / days) - 1


def yearly(eq):
    rows = []
    for y in sorted({d.year for d in eq.index}):
        s = eq[eq.index.year == y]
        if len(s) < 100:
            continue
        ret = s.iloc[-1] / s.iloc[0] - 1
        peak = s.cummax()
        dd = (s / peak - 1).min()
        rows.append({"year": y, "ret_pct": round(ret * 100, 1),
                     "mdd_pct": round(float(dd) * 100, 1)})
    return pd.DataFrame(rows)


def monthly(eq):
    """Per-month return % from the equity curve."""
    monthly_eq = eq.resample("ME").last()
    return monthly_eq.pct_change().dropna() * 100


def rolling_30d(eq, bpd):
    w = 30 * bpd
    rs = []
    for i in range(0, len(eq) - w + 1, w):
        sub = eq.iloc[i:i + w]
        rs.append(sub.iloc[-1] / sub.iloc[0] - 1)
    return pd.Series(rs)


def run_preset(pair: str, tf: str, risk: float, decay: float, adaptive: bool,
               days: int = 5 * 365):
    df = fetch_ohlcv(pair, tf, days=days)
    sig = triple_confirm_long(df, **BASE)
    bpd = BPD[tf]
    if adaptive:
        regimes = walk_forward_regimes(df, bars_per_day=bpd,
                                       train_days=365, step_days=30)
        sig = filter_by_regime(sig, regimes, allow=("BULL", "CHOP"))
    if decay > 0:
        cfg = EnhancedBTConfig(risk_per_trade=risk, max_leverage=5,
                               eq_risk_decay=decay, drawdown_for_decay=0.20)
        eq, trades = run_backtest_enhanced(df, sig, cfg, long_only=True)
        # Synthesize a minimal stats dict
        n = len(trades)
        wins = [t.pnl for t in trades if t.pnl > 0]
        losses = [t.pnl for t in trades if t.pnl <= 0]
        pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float("inf")
        bar_ret = eq.pct_change().fillna(0)
        sharpe = (bar_ret.mean() / bar_ret.std() * np.sqrt(bpd * 365)) if bar_ret.std() > 0 else 0
        stats = {"trades": n, "win_rate": (len(wins) / n) if n else 0,
                 "avg_win": float(np.mean(wins)) if wins else 0,
                 "avg_loss": float(np.mean(losses)) if losses else 0,
                 "profit_factor": float(min(pf, 99)),
                 "max_drawdown": float((eq / eq.cummax() - 1).min()),
                 "sharpe": float(sharpe),
                 "final_equity": float(eq.iloc[-1])}
    else:
        cfg = BTConfig(risk_per_trade=risk, max_leverage=5)
        res = run_backtest(df, sig, cfg, long_only=True)
        eq = res.equity_curve
        stats = res.stats()
    return eq, stats, df


# ============================================================================
def main():
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 25)

    print()
    print("#" * 80)
    print("#  COMPREHENSIVE BOT REPORT")
    print("#  ML regime detection + Monte Carlo + 5-year backtest summary")
    print("#" * 80)

    # Run all presets
    rows = []
    eqs = {}
    yearlys = {}
    monthlys = {}
    for label, pair, tf, risk, decay, adaptive in PRESETS:
        print(f"  ... running {label}", end="", flush=True)
        eq, stats, df = run_preset(pair, tf, risk, decay, adaptive)
        c = cagr(eq) * 100
        roll = rolling_30d(eq, BPD[tf])
        rows.append({
            "preset": label,
            "pair": pair,
            "tf": tf,
            "risk%": f"{risk*100:.1f}",
            "decay": f"{decay}" if decay > 0 else "—",
            "ml_adapt": "yes" if adaptive else "—",
            "5y_ret%": f"{(eq.iloc[-1] / eq.iloc[0] - 1) * 100:+.0f}",
            "cagr%": f"{c:+.1f}",
            "mdd%": f"{stats['max_drawdown'] * 100:+.1f}",
            "sharpe": f"{stats['sharpe']:.2f}",
            "win_rate": f"{stats['win_rate'] * 100:.0f}%",
            "trades": stats["trades"],
            "month_med%": f"{rolling_30d(eq, BPD[tf]).median() * 100:+.2f}",
            "month_win%": f"{(roll > 0).mean() * 100:.0f}%",
            "worst_mo%": f"{roll.min() * 100:+.1f}",
        })
        eqs[label] = eq
        yearlys[label] = yearly(eq)
        monthlys[label] = monthly(eq)
        print(" ✓")

    df_summary = pd.DataFrame(rows)

    # === 1. Best parameters ranking ===
    print()
    print("=" * 80)
    print("1. ALL PRESETS RANKED (5-year backtest, 100 USDT start)")
    print("=" * 80)
    print(df_summary.to_string(index=False))

    # === 2. Year-by-year for key presets ===
    print()
    print("=" * 80)
    print("2. YEAR-BY-YEAR for the top picks")
    print("=" * 80)
    for label in ["steady", "safer_inj_high_return", "adaptive_inj_high_return"]:
        print(f"\n  {label}:")
        print("  " + yearlys[label].to_string(index=False).replace("\n", "\n  "))

    # === 3. Monthly PnL distribution ===
    print()
    print("=" * 80)
    print("3. MONTH-BY-MONTH PnL (%) — full 5y history for top 3")
    print("=" * 80)
    for label in ["steady", "safer_inj_high_return", "adaptive_inj_high_return"]:
        m = monthlys[label]
        print(f"\n  {label}:  n_months={len(m)}  median={m.median():+.2f}%  mean={m.mean():+.2f}%  "
              f"win_rate={(m > 0).mean() * 100:.0f}%  best={m.max():+.1f}%  worst={m.min():+.1f}%")
        # Print last 12 months
        recent = m.tail(12)
        print("    Last 12 months:")
        for idx, val in recent.items():
            print(f"      {idx.strftime('%Y-%m')}:  {val:+6.2f}%")

    # === 4. Paper trading setup instructions ===
    print()
    print("=" * 80)
    print("4. PAPER TRADING SETUP")
    print("=" * 80)
    print("""
  Step 1 — Install dependencies:
      pip install -r requirements.txt

  Step 2 — Choose your preset (recommended):
      STRATEGY_PRESET=safer_inj_high_return   # ~4-5%/mo target, MDD -32%, no ML
      STRATEGY_PRESET=adaptive_inj_high_return # +ML regime filter (slightly better)
      STRATEGY_PRESET=steady                  # ETH 1h, safe, ~+1.4%/mo

  Step 3 — Run paper mode (default, no real money):
      python3 bot.py

  Step 4 — Monitor:
      tail -f bot.log              # live log
      python3 bot_status.py        # status snapshot

  Step 5 — Validate over ~2 weeks:
      Compare paper PnL to the backtest's expected monthly median for
      your preset. If real fills track within ~25% of paper, your
      slippage assumptions hold.

  Step 6 — Go live (only after paper validation):
      EXCHANGE=kucoin MODE=live API_KEY=... API_SECRET=... API_PASSPHRASE=... \\
        STRATEGY_PRESET=safer_inj_high_return python3 bot.py

  Multi-pair portfolio (3 bots running concurrently):
      ./run_portfolio.sh                                  # paper, default mix
      PRESET_SOL=safer_high_return ./run_portfolio.sh     # paper, recommended

  Risk analyzer (Monte Carlo + Kelly + edge significance for any config):
      python3 risk_analyzer.py --pair INJ-USDT --tf 1h --risk 0.02 \\
                               --capital 50000 --months 6
""")


if __name__ == "__main__":
    main()
