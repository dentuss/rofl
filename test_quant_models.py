"""Test the quantitative model add-ons.

For each: does it actually help, or is it just sophisticated-sounding?
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd

from backtest import BTConfig, run_backtest
from data import fetch_ohlcv
from indicators import atr
from quant_models import (
    edge_significance,
    garch_forecast,
    hurst_exponent,
    monte_carlo_paths,
    mc_summary,
    rolling_hurst,
)
from strategies import triple_confirm_long

BASE = dict(ema_fast=9, ema_slow=26, ema_trend=50,
            rsi_min=55.0, adx_min=22.0,
            atr_n=14, sl_mult=1.8, tp_mult=3.0)


def main():
    pd.set_option("display.width", 200)

    # Use INJ-USDT 1h — our newest best
    df = fetch_ohlcv("INJ-USDT", "1h", days=5 * 365)
    sig = triple_confirm_long(df, **BASE)

    print("=" * 80)
    print("PART 1: Edge significance test (INJ 1h triple_long, r=2%, 5y)")
    print("=" * 80)
    res = run_backtest(df, sig, BTConfig(risk_per_trade=0.02, max_leverage=5),
                       long_only=True)
    eq = res.equity_curve
    bar_ret = eq.pct_change().dropna()
    sig_test = edge_significance(bar_ret, n_bootstrap=2000)
    print(sig_test)
    if sig_test["p_value"] < 0.01:
        print("  -> EDGE IS REAL: shuffled returns rarely produce this Sharpe.")
    elif sig_test["p_value"] < 0.05:
        print("  -> Edge is statistically significant at 5% level.")
    else:
        print("  -> Edge could be random — be cautious.")

    print()
    print("=" * 80)
    print("PART 2: Monte Carlo forward simulation (INJ 1h, 6 months)")
    print("=" * 80)
    print("Bootstrapping 2000 forward 6-month paths from historical bar returns...")
    t0 = time.time()
    paths = monte_carlo_paths(eq, n_paths=2000, horizon_bars=24 * 30 * 6,
                              block_size=24)
    summary = mc_summary(paths)
    print(f"  done in {time.time()-t0:.1f}s")
    print(summary)
    print()
    print("Translation for $50,000 starting capital, 6 months:")
    p5, p50, p95 = summary['final_pct']['p5'], summary['final_pct']['p50'], summary['final_pct']['p95']
    print(f"  5th  pct (bad case):    ${50000 * (1 + p5/100):,.0f}  ({p5:+.1f}%)")
    print(f"  25th pct:               ${50000 * (1 + summary['final_pct']['p25']/100):,.0f}  ({summary['final_pct']['p25']:+.1f}%)")
    print(f"  50th pct (median):      ${50000 * (1 + p50/100):,.0f}  ({p50:+.1f}%)")
    print(f"  75th pct:               ${50000 * (1 + summary['final_pct']['p75']/100):,.0f}  ({summary['final_pct']['p75']:+.1f}%)")
    print(f"  95th pct (good case):   ${50000 * (1 + p95/100):,.0f}  ({p95:+.1f}%)")
    print(f"  P(losing money):  {summary['p_loss']*100:.1f}%")
    print(f"  P(doubling):      {summary['p_double']*100:.1f}%")
    print(f"  P(halving):       {summary['p_halve']*100:.1f}%")

    print()
    print("=" * 80)
    print("PART 3: Hurst exponent regime detection (INJ 1h, full 5y)")
    print("=" * 80)
    H = hurst_exponent(df["close"].iloc[-2000:])
    print(f"  Recent 2000-bar Hurst: {H:.3f}")
    print("    > 0.5 = trending  | = 0.5 = random walk | < 0.5 = mean-reverting")
    if H > 0.55:
        print("  -> Recent regime is TRENDING — favorable for triple_long")
    elif H < 0.45:
        print("  -> Recent regime is MEAN-REVERTING — triple_long likely struggling")
    else:
        print("  -> Recent regime is roughly RANDOM-WALK — neutral for triple_long")

    print()
    print("=" * 80)
    print("PART 4: GARCH vs ATR — does forecasted vol help sizing?")
    print("=" * 80)
    print("Computing GARCH forecast (this takes ~10s on 5y of 1h data)...")
    t0 = time.time()
    rets = df["close"].pct_change()
    g_fc = garch_forecast(rets)
    print(f"  done in {time.time()-t0:.1f}s")

    a = atr(df["high"], df["low"], df["close"], 14) / df["close"]

    # Test correlation
    common = pd.concat([g_fc.rename("garch"), a.rename("atr_pct")], axis=1).dropna()
    realized = rets.shift(-1).abs().rename("realized")  # next-bar realized abs return
    common = common.join(realized).dropna()
    print(f"\n  Correlation with NEXT-BAR realized abs return:")
    print(f"    GARCH forecast:  corr = {common['garch'].corr(common['realized']):.4f}")
    print(f"    ATR/price:       corr = {common['atr_pct'].corr(common['realized']):.4f}")
    print(f"  Interpretation: higher = better at predicting future vol")
    print(f"  (Both are reasonable; GARCH typically slight edge if forecast horizon is short)")


if __name__ == "__main__":
    main()
