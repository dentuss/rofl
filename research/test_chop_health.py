"""Test chop_filter and health_filter across the 7-pair validation set.

Baseline = production signal (triple_bidir + dir-regime + F&G) + three-tier
decay + flat funding. The bar for promotion: improve the WEAK pairs (LTC,
BTC) meaningfully without damaging the winners (INJ/SOL/ADA/ETH/LINK).

Run from project root:
    python3 research/test_chop_health.py
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
from core.filters import chop_filter, health_filter
from core.regime_strategy import walk_forward_regimes
from core.risk import DEFAULT_DECAY_TIERS
from core.sentiment import align_to_bars, fetch_fear_greed
from core.strategies import triple_confirm_bidir

PAIRS = ["INJ-USDT", "SOL-USDT", "ADA-USDT", "ETH-USDT", "LINK-USDT",
         "BTC-USDT", "LTC-USDT"]
WINNERS = {"INJ-USDT", "SOL-USDT", "ADA-USDT", "ETH-USDT", "LINK-USDT"}


def apply_funding(eq, trades, bps=1.0):
    if not trades: return eq
    pb = (bps / 1e4) / 8; off = pd.Series(0.0, index=eq.index)
    for t in trades:
        off.loc[eq.index >= t.exit_time] -= pb * t.notional * t.bars_held * t.side
    return eq + off


def base_signal(df, regimes, fa):
    sig = triple_confirm_bidir(df)
    a = regimes.reindex(sig.index, method="ffill").fillna("CHOP")
    b = ((sig["signal"] == 1) & (~a.isin(["BULL","CHOP"]))) | \
        ((sig["signal"] == -1) & (~a.isin(["BEAR","CHOP"])))
    sig.loc[b, "signal"] = 0; sig.loc[b, ["sl","tp"]] = np.nan
    if fa is not None:
        bf = ((sig["signal"] == 1) & (fa >= 80)) | ((sig["signal"] == -1) & (fa <= 20))
        sig.loc[bf, "signal"] = 0; sig.loc[bf, ["sl","tp"]] = np.nan
    return sig


def stat(df, sig):
    cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                           max_leverage=5.0, eq_decay_tiers=DEFAULT_DECAY_TIERS)
    eq, tr = run_backtest_enhanced(df, sig, cfg, long_only=False)
    eq = apply_funding(eq, tr)
    final = float(eq.iloc[-1]); mdd = float((eq/eq.cummax()-1).min())
    br = eq.pct_change().fillna(0)
    sharpe = float(br.mean()/br.std()*np.sqrt(24*365)) if br.std()>0 else 0
    return dict(final=final, ret=final/100-1, mdd=mdd, sharpe=sharpe, n=len(tr))


# Filter variants to test (name -> function(sig, df))
VARIANTS = {
    "baseline":             lambda s, d: s,
    "chop ER<.30|CI>61.8":  lambda s, d: chop_filter(s, d, min_efficiency=0.30, max_choppiness=61.8),
    "chop ER<.40|CI>55":    lambda s, d: chop_filter(s, d, min_efficiency=0.40, max_choppiness=55),
    "health ER90<.15":      lambda s, d: health_filter(s, d, min_efficiency=0.15),
    "health ER90<.18":      lambda s, d: health_filter(s, d, min_efficiency=0.18),
    "health ER90<.22":      lambda s, d: health_filter(s, d, min_efficiency=0.22),
    "chop+health(.30/.18)": lambda s, d: health_filter(chop_filter(s, d, min_efficiency=0.30), d, min_efficiency=0.18),
}


def main():
    days = int(_os.environ.get("DAYS", 4000))
    try:
        fng = fetch_fear_greed()
    except Exception:
        fng = None

    results = {}  # variant -> {pair -> stat}
    for v in VARIANTS:
        results[v] = {}

    for p in PAIRS:
        print(f"\n--- {p} ---", flush=True)
        df = fetch_ohlcv(p, "1h", days=days)
        t0 = time.time()
        regs = walk_forward_regimes(df, bars_per_day=24, train_days=365, step_days=30)
        fa = align_to_bars(fng, df.index) if fng is not None else None
        base = base_signal(df, regs, fa)
        print(f"  regimes {time.time()-t0:.0f}s, {len(df)} bars", flush=True)
        for vname, vfn in VARIANTS.items():
            sig = vfn(base, df)
            results[vname][p] = stat(df, sig)
        b = results["baseline"][p]
        print(f"  baseline: final ${b['final']:.0f}  mdd {b['mdd']*100:.1f}%  "
              f"sharpe {b['sharpe']:.2f}", flush=True)

    # Per-variant table: show sharpe & mdd for each pair
    for metric in ["sharpe", "mdd", "final"]:
        print("\n" + "=" * 130)
        print(f"{metric.upper()} by pair and variant")
        print("=" * 130)
        rows = []
        for vname in VARIANTS:
            row = {"variant": vname}
            for p in PAIRS:
                val = results[vname][p][metric]
                if metric == "mdd":
                    row[p.split("-")[0]] = f"{val*100:.0f}"
                elif metric == "final":
                    row[p.split("-")[0]] = f"{val:.0f}"
                else:
                    row[p.split("-")[0]] = f"{val:.2f}"
            rows.append(row)
        pd.set_option("display.width", 200); pd.set_option("display.max_columns", 20)
        print(pd.DataFrame(rows).to_string(index=False))

    # Verdict: winners avg sharpe vs weak (BTC/LTC) avg sharpe & mdd
    print("\n" + "=" * 130)
    print("VERDICT — does the filter help weak pairs without hurting winners?")
    print("=" * 130)
    print(f"{'variant':24s}{'winners_sharpe':>16s}{'winners_mdd':>14s}"
          f"{'BTC_sharpe':>12s}{'BTC_mdd':>10s}{'LTC_sharpe':>12s}{'LTC_mdd':>10s}")
    for vname in VARIANTS:
        ws = np.mean([results[vname][p]["sharpe"] for p in WINNERS])
        wm = np.mean([results[vname][p]["mdd"] for p in WINNERS]) * 100
        bs = results[vname]["BTC-USDT"]["sharpe"]; bm = results[vname]["BTC-USDT"]["mdd"]*100
        ls = results[vname]["LTC-USDT"]["sharpe"]; lm = results[vname]["LTC-USDT"]["mdd"]*100
        print(f"{vname:24s}{ws:>16.2f}{wm:>13.1f}%{bs:>12.2f}{bm:>9.1f}%{ls:>12.2f}{lm:>9.1f}%")


if __name__ == "__main__":
    main()
