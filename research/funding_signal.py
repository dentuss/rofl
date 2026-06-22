"""Research: does the funding rate carry PREDICTIVE signal, not just cost?

The handoff flags funding-as-signal as the one strategy avenue not yet tested.
Economic hypothesis (analogous to the F&G extreme filter): a large positive
funding rate means crowded, over-leveraged longs paying to hold — a setup that
can mean-revert. Symmetrically, very negative funding = crowded shorts.

This script tests that rigorously and reports the verdict. It is RESEARCH ONLY
— nothing here is imported by bot.py or the live path.

Three tests per pair (1h bars, real funding from Bybit→OKX):
  1. Information Coefficient — Spearman corr between the funding z-score at bar
     t and the FORWARD return over the next H bars. |IC| < ~0.03 = no edge.
  2. Forward-return by funding quantile — monotonic = exploitable signal.
  3. Strategy overlay — add a "fade extreme funding" filter to the production
     bidir signal and backtest vs baseline (Sharpe / return / MDD).

Run:  python research/funding_signal.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:  # keep unicode (→, Δ) from crashing a cp1252 Windows console / pipe
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core.backtest import BTConfig, run_backtest  # noqa: E402
from core.data import fetch_ohlcv  # noqa: E402
from core.funding import fetch_funding  # noqa: E402
from core.strategies import triple_confirm_bidir  # noqa: E402

BASE = dict(ema_fast=9, ema_slow=26, ema_trend=50, rsi_min=55.0, adx_min=22.0,
            atr_n=14, sl_mult=1.8, tp_mult=3.0)
PAIRS = ["INJ-USDT", "SOL-USDT", "ADA-USDT", "ETH-USDT", "LINK-USDT"]
DAYS = 400
Z_WIN = 720          # 30d rolling window for the funding z-score
HORIZONS = (8, 24, 72)
FADE_Z = 1.5         # block trades that fight funding beyond this many sigma


def load(pair: str):
    df = fetch_ohlcv(pair, "1h", days=DAYS)
    fund = fetch_funding(pair, days=DAYS + 40, source="auto")
    if fund is None or fund.empty:
        return df, None
    level = fund["funding_rate"].reindex(df.index, method="ffill")
    return df, level


def fund_zscore(level: pd.Series) -> pd.Series:
    mu = level.rolling(Z_WIN).mean()
    sd = level.rolling(Z_WIN).std()
    return (level - mu) / sd.replace(0, np.nan)


def _spearman(a: pd.Series, b: pd.Series) -> float:
    """Spearman = Pearson on ranks (avoids the scipy dependency)."""
    d = pd.DataFrame({"a": a, "b": b}).replace([np.inf, -np.inf], np.nan).dropna()
    if len(d) < 50:
        return float("nan")
    return float(d["a"].rank().corr(d["b"].rank()))


def ic(df: pd.DataFrame, z: pd.Series) -> dict:
    out = {}
    for H in HORIZONS:
        fwd = df["close"].shift(-H) / df["close"] - 1
        out[H] = _spearman(z, fwd)
    return out


def quantiles(df: pd.DataFrame, z: pd.Series, H: int = 24, q: int = 5):
    fwd = df["close"].shift(-H) / df["close"] - 1
    d = pd.DataFrame({"z": z, "fwd": fwd}).dropna()
    if len(d) < 100:
        return None
    d["bucket"] = pd.qcut(d["z"], q, labels=False, duplicates="drop")
    g = d.groupby("bucket")["fwd"].mean() * 100
    return g


def overlay(df: pd.DataFrame, z: pd.Series):
    sig = triple_confirm_bidir(df, **BASE)
    cfg = BTConfig(starting_equity=100.0, risk_per_trade=0.02, max_leverage=5.0,
                   max_bars_in_trade=96, allow_short=True)
    base = run_backtest(df, sig, cfg, long_only=False).stats()
    # Fade: drop longs into very high funding (crowded longs), shorts into very
    # low funding (crowded shorts).
    block = ((sig["signal"] == 1) & (z > FADE_Z)) | \
            ((sig["signal"] == -1) & (z < -FADE_Z))
    sig2 = sig.copy()
    sig2.loc[block, "signal"] = 0
    sig2.loc[block, ["sl", "tp"]] = np.nan
    fade = run_backtest(df, sig2, cfg, long_only=False).stats()
    return base, fade, int(block.sum())


def main():
    pooled_ic = {H: [] for H in HORIZONS}
    print(f"Funding-signal research — {DAYS}d 1h, z-window {Z_WIN} bars, "
          f"fade |z|>{FADE_Z}\n")
    deltas = []
    for pair in PAIRS:
        try:
            df, level = load(pair)
        except Exception as e:
            print(f"{pair}: load failed: {e}\n"); continue
        if level is None or level.notna().sum() < Z_WIN + 200:
            print(f"{pair}: insufficient funding data — skipped\n"); continue
        z = fund_zscore(level)
        ics = ic(df, z)
        for H in HORIZONS:
            if pd.notna(ics[H]):
                pooled_ic[H].append(ics[H])
        qs = quantiles(df, z, H=24)
        base, fade, n_blocked = overlay(df, z)
        d_ret = fade["total_return"] - base["total_return"]
        d_sharpe = fade["sharpe"] - base["sharpe"]
        deltas.append((d_ret, d_sharpe))

        print(f"{pair}:")
        print(f"  funding/8h: mean {level.mean()*100:+.4f}%  "
              f"std {level.std()*100:.4f}pp  "
              f"max {level.max()*100:+.3f}%  min {level.min()*100:+.3f}%")
        print(f"  IC (Spearman, funding-z vs fwd ret): " +
              "  ".join(f"{H}h={ics[H]:+.3f}" for H in HORIZONS))
        if qs is not None:
            print("  fwd-24h ret by funding quintile (low→high z): " +
                  " ".join(f"{v:+.2f}%" for v in qs.values))
        print(f"  overlay 'fade |z|>{FADE_Z}' blocked {n_blocked} signals: "
              f"return {base['total_return']*100:+.0f}%→{fade['total_return']*100:+.0f}%  "
              f"Sharpe {base['sharpe']:.2f}→{fade['sharpe']:.2f}  "
              f"MDD {base['max_drawdown']*100:.0f}%→{fade['max_drawdown']*100:.0f}%\n")

    print("=" * 64)
    for H in HORIZONS:
        vals = pooled_ic[H]
        if vals:
            print(f"  mean IC @ {H}h across pairs: {np.mean(vals):+.3f} "
                  f"(n={len(vals)})")
    if deltas:
        mr = np.mean([d[0] for d in deltas]) * 100
        ms = np.mean([d[1] for d in deltas])
        print(f"  overlay mean Δreturn {mr:+.1f}pp, mean ΔSharpe {ms:+.2f}")
        verdict = ("NO usable edge — |IC| tiny and overlay doesn't help"
                   if abs(np.mean([np.mean(pooled_ic[H]) for H in HORIZONS])) < 0.03
                   and ms <= 0.05
                   else "SIGNAL worth a closer look — investigate before trusting")
        print(f"  VERDICT: {verdict}")


if __name__ == "__main__":
    main()
