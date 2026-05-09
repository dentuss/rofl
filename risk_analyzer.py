"""Risk analyzer: Monte Carlo + edge significance + Kelly sizing for any preset.

Usage:
    python3 risk_analyzer.py                                   # default = INJ 1h r=2%
    python3 risk_analyzer.py --pair SOL-USDT --tf 30m --risk 0.02
    python3 risk_analyzer.py --capital 50000 --months 6
"""
from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

from core.backtest import BTConfig, run_backtest
from core.data import fetch_ohlcv
from core.quant_models import (
    edge_significance,
    kelly_fraction,
    mc_summary,
    monte_carlo_paths,
)
from core.strategies import triple_confirm_long

BASE = dict(ema_fast=9, ema_slow=26, ema_trend=50,
            rsi_min=55.0, adx_min=22.0,
            atr_n=14, sl_mult=1.8, tp_mult=3.0)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pair", default="INJ-USDT")
    p.add_argument("--tf", default="1h")
    p.add_argument("--risk", type=float, default=0.02)
    p.add_argument("--leverage", type=float, default=5.0)
    p.add_argument("--capital", type=float, default=50000.0)
    p.add_argument("--months", type=int, default=6)
    p.add_argument("--days", type=int, default=5 * 365, help="backtest history")
    p.add_argument("--paths", type=int, default=2000, help="MC simulation count")
    args = p.parse_args()

    bpd = {"15m": 96, "30m": 48, "1h": 24, "4h": 6, "1d": 1}.get(args.tf, 24)
    bpyear = bpd * 365

    print(f"Loading {args.pair} {args.tf} for last {args.days} days...")
    df = fetch_ohlcv(args.pair, args.tf, days=args.days)
    sig = triple_confirm_long(df, **BASE)
    cfg = BTConfig(risk_per_trade=args.risk, max_leverage=args.leverage)
    res = run_backtest(df, sig, cfg, long_only=True)
    stats = res.stats()
    days = (df.index[-1] - df.index[0]).total_seconds() / 86400
    cagr = (stats["final_equity"] / 100) ** (365 / days) - 1

    print()
    print("=" * 80)
    print(f"{'Preset:':<25s} triple_long {args.pair} {args.tf} r={args.risk*100:.1f}%")
    print(f"{'Backtest range:':<25s} {df.index[0].date()} -> {df.index[-1].date()} ({days:.0f} days)")
    print(f"{'Trades:':<25s} {stats['trades']}")
    print(f"{'Win rate:':<25s} {stats['win_rate']*100:.1f}%")
    print(f"{'CAGR:':<25s} {cagr*100:+.1f}%")
    print(f"{'Max drawdown:':<25s} {stats['max_drawdown']*100:+.1f}%")
    print(f"{'Sharpe:':<25s} {stats['sharpe']:.2f}")
    print(f"{'Profit factor:':<25s} {stats['profit_factor']:.2f}")

    # Kelly criterion
    print()
    print("=" * 80)
    print("KELLY CRITERION — optimal risk per trade given the strategy's stats")
    print("=" * 80)
    k = kelly_fraction(stats["win_rate"], stats["avg_win"], stats["avg_loss"])
    print(f"  win_rate:           {k['win_rate']}")
    print(f"  avg_win/avg_loss:   {k['win_loss_ratio']}")
    print(f"  edge per trade:     {k['edge_per_trade']:+.4f}")
    print(f"  Kelly fraction:     {k['kelly_full_pct']:.2f}%  (theoretical optimum)")
    print(f"  Half-Kelly:         {k['kelly_half_pct']:.2f}%  (most pros stop here)")
    print(f"  Quarter-Kelly:      {k['kelly_quarter_pct']:.2f}%  (conservative)")
    print(f"  Current risk:       {args.risk*100:.2f}%")
    if args.risk * 100 > k['kelly_half_pct']:
        delta = (args.risk * 100) / k['kelly_half_pct'] - 1
        print(f"  -> Current is {delta*100:.0f}% ABOVE half-Kelly. You're betting "
              "more aggressively than the math suggests is optimal.")
    elif args.risk * 100 < k['kelly_quarter_pct']:
        print(f"  -> Current is BELOW quarter-Kelly. Very conservative. "
              "Could safely increase risk.")
    else:
        print("  -> Current risk sits between quarter- and half-Kelly. Reasonable.")

    # Edge significance
    print()
    print("=" * 80)
    print(f"EDGE SIGNIFICANCE — Sharpe vs 1500 random-entry strategies")
    print("=" * 80)
    avg_hold = int(np.mean([t.bars_held for t in res.trades])) if res.trades else 24
    sig_test = edge_significance(
        res.equity_curve, df["close"],
        strategy_n_trades=len(res.trades),
        avg_hold_bars=avg_hold,
        n_simulations=1500,
        bars_per_year=bpyear,
    )
    print(f"  observed Sharpe:           {sig_test['observed_sharpe']:.2f}")
    print(f"  median random-strat Sharpe:{sig_test['random_strats_median_sharpe']:.2f}")
    print(f"  95th-pct random Sharpe:    {sig_test['random_strats_p95_sharpe']:.2f}")
    print(f"  99th-pct random Sharpe:    {sig_test['random_strats_p99_sharpe']:.2f}")
    print(f"  p-value:                   {sig_test['p_value']:.4f}")
    if sig_test["p_value"] < 0.01:
        print("  -> STRONG REAL EDGE — strategy clearly beats random timing")
    elif sig_test["p_value"] < 0.05:
        print("  -> Real edge at 5% confidence")
    elif sig_test["p_value"] < 0.20:
        print("  -> Weak edge — could be partly luck")
    else:
        print("  -> NO statistical edge over random — be very cautious")

    # Monte Carlo
    print()
    print("=" * 80)
    print(f"MONTE CARLO — {args.paths} bootstrap forward paths over {args.months} months")
    print("=" * 80)
    horizon_bars = bpd * 30 * args.months
    block = bpd  # daily blocks for reasonable autocorrelation preservation
    paths = monte_carlo_paths(res.equity_curve,
                              n_paths=args.paths,
                              horizon_bars=horizon_bars,
                              block_size=block)
    s = mc_summary(paths)
    print(f"  Forward {args.months}-month return distribution:")
    for q in (5, 25, 50, 75, 95):
        ret = s["final_pct"][f"p{q}"]
        cap = args.capital * (1 + ret / 100)
        print(f"    {q:>2d}th pct: {ret:>+7.1f}%  ${cap:>10,.0f}")
    print(f"  Expected max drawdown distribution:")
    for q in (5, 25, 50, 75, 95):
        dd = s["mdd_pct"][f"p{q}"]
        print(f"    {q:>2d}th pct: {dd:>+7.1f}%")
    print()
    print(f"  P(losing money over {args.months}mo):  {s['p_loss']*100:.1f}%")
    print(f"  P(doubling capital):                   {s['p_double']*100:.1f}%")
    print(f"  P(losing half capital):                {s['p_halve']*100:.1f}%")

    print()
    print("=" * 80)
    print("CAVEATS")
    print("=" * 80)
    print("  - Monte Carlo bootstraps from HISTORICAL bar returns. If the future")
    print("    regime differs, real distribution will differ. The 5y window")
    print("    includes the 2022 bear market, so it's not all bull.")
    print("  - Edge significance test assumes random-entry on the SAME ASSET.")
    print("    If the asset itself trends down forward, the strategy's absolute")
    print("    return will suffer regardless of edge.")
    print("  - Kelly assumes win-rate / payoff stay stable. They don't,")
    print("    especially in regime shifts. Half-Kelly is a robust compromise.")


if __name__ == "__main__":
    main()
