"""INJ-softened 5-pair variant test. The robustness study showed the original
inj_heavy 5-pair's backtested edge leans on INJ at 40% — a concentration the
backtest rewards but that is a forward risk. This caps INJ at 25% and equal-
weights the other four (the least-overfit redistribution), then compares:

  ORIG5  INJ .40  SOL .20  ADA .15  ETH .15  LINK .10   (current live)
  SOFT5  INJ .25  SOL .1875 ADA .1875 ETH .1875 LINK .1875  (capped + equal rest)
  EIGHT  equal .125 across INJ AVAX NEAR AAVE GRT RUNE DOGE ADA

Bybit perp data (execution venue), full production stack + K=3 cooldown, honest
MONTHLY Sharpe, full window + OOS holdout. Question: does softening INJ keep the
5-pair's robustness (better tails, OOS stability) while removing the single-name
concentration — and does it still hold up vs the 8-pair?

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/portfolio_softened.py
"""
from __future__ import annotations

import sys as _sys, os as _os, time
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

import numpy as np
import pandas as pd

import core.regime as _regime
from core.backtest_enhanced import EnhancedBTConfig
from core.risk import DEFAULT_DECAY_TIERS
from core.data import fetch_ohlcv_bybit
from core.regime_strategy import walk_forward_regimes
from core.sentiment import align_to_bars, fetch_fear_greed
from research.test_reentry_cooldown_prod import (
    run_enhanced_with_cooldown, regime_filtered, fng_persist_3d, apply_funding)

ORIG5 = {"INJ-USDT": .40, "SOL-USDT": .20, "ADA-USDT": .15, "ETH-USDT": .15, "LINK-USDT": .10}
SOFT5 = {"INJ-USDT": .25, "SOL-USDT": .1875, "ADA-USDT": .1875, "ETH-USDT": .1875, "LINK-USDT": .1875}
EIGHT = {p: 0.125 for p in ["INJ-USDT", "AVAX-USDT", "NEAR-USDT", "AAVE-USDT",
                            "GRT-USDT", "RUNE-USDT", "DOGE-USDT", "ADA-USDT"]}
ALL = sorted(set(ORIG5) | set(EIGHT))

TOTAL = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))
WARMUP_D = 365
K = int(_os.environ.get("K", 3))
BPY = 24 * 365


def sharpe_m(eq):
    m = eq.resample("ME").last().pct_change().dropna()
    return float(m.mean() / m.std() * np.sqrt(12)) if len(m) > 2 and m.std() > 0 else 0.0


def stats(eq):
    final = float(eq.iloc[-1]); ret = final / float(eq.iloc[0]) - 1
    mdd = float((eq / eq.cummax() - 1).min())
    days = max((eq.index[-1] - eq.index[0]).days, 1)
    cagr = (final / float(eq.iloc[0])) ** (365 / days) - 1
    br = eq.pct_change().fillna(0)
    sh_h = float(br.mean() / br.std() * np.sqrt(BPY)) if br.std() > 0 else 0
    mo = eq.resample("ME").last().pct_change().dropna() * 100
    return dict(final=final, ret=ret, cagr=cagr, mdd=mdd, sh_h=sh_h, sh_m=sharpe_m(eq),
                worst_mo=float(mo.min()) if len(mo) else 0,
                med_mo=float(mo.median()) if len(mo) else 0,
                win_mo=float((mo > 0).mean() * 100) if len(mo) else 0)


def build(pair_eq, w, idx):
    port = None
    for p, wt in w.items():
        e = pair_eq[p].reindex(idx).ffill()
        e = e / e.iloc[0] * wt
        port = e if port is None else port + e
    return port * TOTAL


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                           max_leverage=5.0, eq_decay_tiers=DEFAULT_DECAY_TIERS)
    print(f"BYBIT PERP  TOTAL={TOTAL:.0f}  K={K}  warmup={WARMUP_D}d")
    fng = fetch_fear_greed()
    pair_eq = {}
    for p in ALL:
        t0 = time.time()
        df = fetch_ohlcv_bybit(p, "1h", days=DAYS)
        regs = walk_forward_regimes(df, bars_per_day=24, train_days=365, step_days=30)
        fa = align_to_bars(fng, df.index)
        sig = fng_persist_3d(regime_filtered(df, regs), fa)
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]; sige = sig[sig.index >= cut]
        eq, tr = run_enhanced_with_cooldown(dfe, sige, cfg, K)
        pair_eq[p] = apply_funding(eq, tr)
        print(f"  {p:11s} {time.time()-t0:4.0f}s  {dfe.index[0].date()}..{dfe.index[-1].date()}", flush=True)

    idx = None
    for p in set(ORIG5) | set(EIGHT):
        idx = pair_eq[p].index if idx is None else idx.intersection(pair_eq[p].index)
    idx = idx.sort_values()
    split = idx[int(len(idx) * 0.6)]
    is_idx, oos_idx = idx[idx < split], idx[idx >= split]
    yrs = (idx[-1] - idx[0]).days / 365

    books = {"ORIG5": ORIG5, "SOFT5": SOFT5, "8p": EIGHT}
    full = {k: stats(build(pair_eq, w, idx)) for k, w in books.items()}

    print("\n" + "=" * 82)
    print(f"FULL WINDOW  {idx[0].date()}..{idx[-1].date()} ({yrs:.2f}y), Bybit, K={K}")
    print("=" * 82)
    print(f"{'metric':18s}{'ORIG5 (INJ40)':>16s}{'SOFT5 (INJ25)':>16s}{'8-pair':>16s}")
    rows = [("final $", "final", 1, "{:>16.0f}"), ("CAGR %", "cagr", 100, "{:>16.1f}"),
            ("Sharpe (monthly)", "sh_m", 1, "{:>16.2f}"), ("Sharpe (hourly)", "sh_h", 1, "{:>16.2f}"),
            ("MaxDD %", "mdd", 100, "{:>16.1f}"), ("worst month %", "worst_mo", 1, "{:>16.1f}"),
            ("median month %", "med_mo", 1, "{:>16.2f}"), ("% pos months", "win_mo", 1, "{:>16.0f}")]
    for label, key, sc, fmt in rows:
        print(f"{label:18s}" + "".join(fmt.format(full[k][key] * sc) for k in ["ORIG5", "SOFT5", "8p"]))

    print("\n" + "=" * 82)
    print(f"OOS HOLDOUT   IS {is_idx[0].date()}..{is_idx[-1].date()} | OOS {oos_idx[0].date()}..{oos_idx[-1].date()}")
    print("=" * 82)
    print(f"{'':18s}{'ORIG5 IS':>10s}{'ORIG5 OOS':>11s}{'SOFT5 IS':>10s}{'SOFT5 OOS':>11s}{'8p IS':>9s}{'8p OOS':>9s}")
    cells = {}
    for k, w in books.items():
        cells[k + " IS"] = stats(build(pair_eq, w, is_idx))
        cells[k + " OOS"] = stats(build(pair_eq, w, oos_idx))
    order = ["ORIG5 IS", "ORIG5 OOS", "SOFT5 IS", "SOFT5 OOS", "8p IS", "8p OOS"]
    fmts = [">10", ">11", ">10", ">11", ">9", ">9"]
    for label, key, sc in [("Sharpe (mo)", "sh_m", 1), ("CAGR %", "cagr", 100),
                           ("MaxDD %", "mdd", 100), ("worst mo %", "worst_mo", 1)]:
        line = f"{label:18s}"
        for c, f in zip(order, fmts):
            line += ("{:" + f + ".2f}").format(cells[c][key] * sc) if key == "sh_m" \
                else ("{:" + f + ".1f}").format(cells[c][key] * sc)
        print(line)

    print("\n" + "=" * 82)
    print("VERDICT")
    print("=" * 82)
    o, s, e = full["ORIG5"], full["SOFT5"], full["8p"]
    print(f"  softening INJ 40->25:  Sharpe(mo) {o['sh_m']:.2f}->{s['sh_m']:.2f}  "
          f"worst-mo {o['worst_mo']:+.1f}%->{s['worst_mo']:+.1f}%  "
          f"CAGR {o['cagr']*100:.0f}%->{s['cagr']*100:.0f}%  final ${o['final']:.0f}->${s['final']:.0f}")
    print(f"  SOFT5 OOS Sharpe(mo) {cells['SOFT5 OOS']['sh_m']:.2f} vs ORIG5 {cells['ORIG5 OOS']['sh_m']:.2f} "
          f"vs 8p {cells['8p OOS']['sh_m']:.2f}")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
