"""Path-dependent strategy-health gate sweep across the 7-pair set.

Pauses new entries when the bot's OWN trailing equity return over a lookback
window is below a floor; resumes when it recovers. This directly measures
"is my strategy currently working on this pair" (unlike the price-based
health_filter, which couldn't separate dead from healthy).

Baseline = production signal + three-tier decay + flat funding.
Bar for promotion: help BTC/LTC meaningfully without hurting the winners.

Run from project root:
    python3 research/test_health_gate.py
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

PAIRS = ["INJ-USDT", "SOL-USDT", "ADA-USDT", "ETH-USDT", "LINK-USDT",
         "BTC-USDT", "LTC-USDT"]
WINNERS = {"INJ-USDT", "SOL-USDT", "ADA-USDT", "ETH-USDT", "LINK-USDT"}

# (name, lookback_bars, min_return, resume_return)
GATES = [
    ("baseline",          0,         0.0,   0.0),
    ("30d/-15%",          24 * 30,  -0.15, -0.05),
    ("60d/-15%",          24 * 60,  -0.15, -0.05),
    ("60d/-20%",          24 * 60,  -0.20, -0.08),
    ("90d/-20%",          24 * 90,  -0.20, -0.08),
    ("90d/-25%",          24 * 90,  -0.25, -0.10),
]


def apply_funding(eq, trades, bps=1.0):
    if not trades: return eq
    pb = (bps / 1e4) / 8; off = pd.Series(0.0, index=eq.index)
    for t in trades:
        off.loc[eq.index >= t.exit_time] -= pb * t.notional * t.bars_held * t.side
    return eq + off


def base_signal(df, regs, fa):
    s = triple_confirm_bidir(df)
    a = regs.reindex(s.index, method="ffill").fillna("CHOP")
    b = ((s["signal"] == 1) & (~a.isin(["BULL","CHOP"]))) | \
        ((s["signal"] == -1) & (~a.isin(["BEAR","CHOP"])))
    s.loc[b, "signal"] = 0; s.loc[b, ["sl","tp"]] = np.nan
    if fa is not None:
        bf = ((s["signal"] == 1) & (fa >= 80)) | ((s["signal"] == -1) & (fa <= 20))
        s.loc[bf, "signal"] = 0; s.loc[bf, ["sl","tp"]] = np.nan
    return s


def run(df, sig, lb, mn, rs):
    cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                           max_leverage=5.0, eq_decay_tiers=DEFAULT_DECAY_TIERS,
                           health_lookback_bars=lb, health_min_return=mn,
                           health_resume_return=rs)
    eq, tr = run_backtest_enhanced(df, sig, cfg, long_only=False)
    eq = apply_funding(eq, tr)
    final = float(eq.iloc[-1]); mdd = float((eq/eq.cummax()-1).min())
    br = eq.pct_change().fillna(0)
    sh = float(br.mean()/br.std()*np.sqrt(24*365)) if br.std()>0 else 0
    return dict(final=final, mdd=mdd, sharpe=sh, n=len(tr))


def main():
    days = int(_os.environ.get("DAYS", 4000))
    try:
        fng = fetch_fear_greed()
    except Exception:
        fng = None

    res = {g[0]: {} for g in GATES}
    for p in PAIRS:
        print(f"\n--- {p} ---", flush=True)
        df = fetch_ohlcv(p, "1h", days=days)
        t0 = time.time()
        regs = walk_forward_regimes(df, bars_per_day=24, train_days=365, step_days=30)
        fa = align_to_bars(fng, df.index) if fng is not None else None
        sig = base_signal(df, regs, fa)
        print(f"  regimes {time.time()-t0:.0f}s", flush=True)
        for name, lb, mn, rs in GATES:
            res[name][p] = run(df, sig, lb, mn, rs)
        b = res["baseline"][p]
        print(f"  baseline final ${b['final']:.0f} mdd {b['mdd']*100:.0f}% "
              f"sharpe {b['sharpe']:.2f}", flush=True)

    for metric in ["sharpe", "mdd", "final"]:
        print("\n" + "=" * 120)
        print(f"{metric.upper()} by pair and gate")
        print("=" * 120)
        rows = []
        for name, *_ in GATES:
            row = {"gate": name}
            for p in PAIRS:
                v = res[name][p][metric]
                row[p.split("-")[0]] = (f"{v*100:.0f}" if metric=="mdd"
                                        else f"{v:.0f}" if metric=="final"
                                        else f"{v:.2f}")
            rows.append(row)
        pd.set_option("display.width", 200)
        print(pd.DataFrame(rows).to_string(index=False))

    print("\n" + "=" * 120)
    print("VERDICT — winners (avg) vs BTC vs LTC")
    print("=" * 120)
    print(f"{'gate':14s}{'win_sharpe':>12s}{'win_mdd':>10s}{'win_final':>11s}"
          f"{'BTC_sh':>9s}{'BTC_mdd':>9s}{'LTC_sh':>9s}{'LTC_mdd':>9s}{'LTC_fin':>9s}")
    for name, *_ in GATES:
        ws = np.mean([res[name][p]["sharpe"] for p in WINNERS])
        wm = np.mean([res[name][p]["mdd"] for p in WINNERS]) * 100
        wf = np.mean([res[name][p]["final"] for p in WINNERS])
        bs = res[name]["BTC-USDT"]; ls = res[name]["LTC-USDT"]
        print(f"{name:14s}{ws:>12.2f}{wm:>9.1f}%{wf:>11.0f}"
              f"{bs['sharpe']:>9.2f}{bs['mdd']*100:>8.1f}%"
              f"{ls['sharpe']:>9.2f}{ls['mdd']*100:>8.1f}%{ls['final']:>9.0f}")


if __name__ == "__main__":
    main()
