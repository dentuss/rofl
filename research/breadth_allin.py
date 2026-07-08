"""Breadth study — the ALL_IN trustworthy stack across the widest qualifying
Bybit-perp universe. Breadth is the cheapest Sharpe: pairwise monthly
correlation of the strategy's pair books has historically been ~0.16, so
portfolio Sharpe should scale toward sqrt(N_effective).

PRE-REGISTERED design (no performance selection anywhere):
- Candidates: 30 liquid Bybit USDT-perps (list below, fixed before running).
- Qualification gate (non-performance): 4h history starting early enough for
  >= 2y of POST-WARMUP evaluation on the common scoreboard window
  (post-warmup start <= 2023-08-17), AND a real funding series. Younger names
  are listed as deferred, not judged.
- Per pair: the exact ALL_IN config (4h, tp_mult=6.0, entry-bar check, maker
  entries, cooldown_bars=1, decay tiers, REAL funding).
- Portfolios (all reported, none cherry-picked):
    EW-ALL     equal weight across every qualifying name
    IV-ALL     inverse-vol weights estimated on the IS window ONLY (no leak),
               applied unchanged to full + OOS
    EW11 / SOFT5 reference books for continuity with the scoreboard
- Gates reported: IS/OOS 60-40, sub-window thirds, per-pair table in full.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/breadth_allin.py
"""
from __future__ import annotations

import sys as _sys, os as _os, time
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

import numpy as np
import pandas as pd

import core.regime as _regime
from core.backtest_enhanced import EnhancedBTConfig, run_backtest_enhanced
from core.data import fetch_ohlcv_bybit
from core.funding import fetch_funding
from core.regime_strategy import walk_forward_regimes
from core.risk import DEFAULT_DECAY_TIERS
from core.sentiment import align_to_bars, fetch_fear_greed
from core.strategies import triple_confirm_bidir
from research.cost_engine import (apply_funding_real, regime_mask, fng_persist,
                                  sharpe_m, stats, build, FEE_TAKER, FEE_MAKER)

CANDIDATES = [f"{b}-USDT" for b in
              ["BTC", "ETH", "SOL", "ADA", "LINK", "INJ", "AVAX", "NEAR",
               "AAVE", "GRT", "RUNE", "DOGE", "DOT", "ATOM", "LTC", "XRP",
               "BNB", "FIL", "APT", "ARB", "OP", "SUI", "UNI", "ETC",
               "BCH", "TRX", "SAND", "EOS", "TON", "WLD"]]
SOFT5 = {"INJ-USDT": .25, "SOL-USDT": .1875, "ADA-USDT": .1875,
         "ETH-USDT": .1875, "LINK-USDT": .1875}
EW11_NAMES = list(SOFT5) + ["AVAX-USDT", "NEAR-USDT", "AAVE-USDT",
                            "GRT-USDT", "RUNE-USDT", "DOGE-USDT"]
TOTAL = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))
WARMUP_D = 365
BPD = 6
COMMON_START = pd.Timestamp("2023-08-17", tz="UTC")   # scoreboard window


def allin_cfg():
    return EnhancedBTConfig(
        starting_equity=100.0, risk_per_trade=0.020, max_leverage=5.0,
        eq_decay_tiers=DEFAULT_DECAY_TIERS, cooldown_bars=1,
        fee_rate=FEE_TAKER, fee_maker=FEE_MAKER, entry_style="maker_close")


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print(f"BREADTH  ALL_IN 4h tp6  candidates={len(CANDIDATES)}", flush=True)
    fng = fetch_fear_greed()

    pair_eq, deferred, failed = {}, [], []
    rows = []
    for p in CANDIDATES:
        t0 = time.time()
        try:
            df = fetch_ohlcv_bybit(p, "4h", days=DAYS)
            cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
            if cut > COMMON_START:
                deferred.append((p, str(df.index[0].date())))
                print(f"  {p:11s} DEFERRED (data from {df.index[0].date()})", flush=True)
                continue
            fund = fetch_funding(p, days=DAYS, source="auto")
            if fund is None or not len(fund):
                deferred.append((p, "no funding series"))
                continue
            regs = walk_forward_regimes(df, bars_per_day=BPD, train_days=365,
                                        step_days=30)
            rc = regs.value_counts().to_dict()
            assert (rc.get("BULL", 0) + rc.get("BEAR", 0)) > 0, "all-CHOP"
            fa = align_to_bars(fng, df.index)
            sig = fng_persist(regime_mask(
                triple_confirm_bidir(df, tp_mult=6.0), regs), fa)
            dfe = df[df.index >= cut]
            eq, tr = run_backtest_enhanced(dfe, sig[sig.index >= cut], allin_cfg())
            pair_eq[p] = apply_funding_real(eq, tr, fund)
            s = stats(pair_eq[p][pair_eq[p].index >= COMMON_START])
            rows.append((p, s, len(tr)))
            print(f"  {p:11s} {time.time()-t0:4.0f}s  final {s['final']:7.1f}  "
                  f"Sh {s['sh_m']:+5.2f}  MDD {s['mdd']*100:5.1f}%  n={len(tr)}",
                  flush=True)
        except Exception as e:
            failed.append((p, str(e)[:80]))
            print(f"  {p:11s} FAILED: {str(e)[:80]}", flush=True)

    qual = list(pair_eq)
    print(f"\nqualifying {len(qual)}/{len(CANDIDATES)}  "
          f"deferred {len(deferred)}  failed {len(failed)}")
    for p, why in deferred:
        print(f"  deferred: {p} ({why})")
    for p, why in failed:
        print(f"  failed:   {p} ({why})")

    idx = None
    for p in qual:
        e = pair_eq[p][pair_eq[p].index >= COMMON_START]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    split = idx[int(len(idx) * 0.6)]
    is_idx, oos_idx = idx[idx < split], idx[idx >= split]
    yrs = (idx[-1] - idx[0]).days / 365

    # inverse-vol weights from the IS window ONLY (no leak into OOS)
    iv = {}
    for p in qual:
        r = pair_eq[p].reindex(is_idx).ffill().pct_change().dropna()
        v = float(r.std())
        iv[p] = 1.0 / v if v > 1e-9 else 0.0
    tot = sum(iv.values())
    iv = {p: w / tot for p, w in iv.items()}

    books = {
        f"EW-{len(qual)}": {p: 1 / len(qual) for p in qual},
        f"IV-{len(qual)}": iv,
        "EW11": {p: 1 / len(EW11_NAMES) for p in EW11_NAMES if p in pair_eq},
        "SOFT5": {p: w for p, w in SOFT5.items() if p in pair_eq},
    }

    print("\n" + "=" * 88)
    print(f"PORTFOLIOS  {idx[0].date()}..{idx[-1].date()} ({yrs:.2f}y) @ ${TOTAL:.0f}"
          f"   (IV weights estimated on IS only)")
    print("=" * 88)
    print(f"{'book':10s}{'final$':>8s}{'CAGR%':>8s}{'Sh(mo)':>8s}{'MDD%':>7s}"
          f"{'worst':>7s}{'pos%':>6s}{'IS Sh':>7s}{'OOS Sh':>8s}{'thirds':>22s}")
    for name, w in books.items():
        s = stats(build(pair_eq, w, idx))
        i = stats(build(pair_eq, w, is_idx))
        o = stats(build(pair_eq, w, oos_idx))
        b = [idx[0] + (idx[-1] - idx[0]) * k / 3 for k in range(4)]
        th = "  ".join(f"{sharpe_m(build(pair_eq, w, idx[(idx >= b[k]) & (idx < b[k+1])])):+.2f}"
                       for k in range(3))
        print(f"{name:10s}{s['final']:8.0f}{s['cagr']*100:8.1f}{s['sh_m']:8.2f}"
              f"{s['mdd']*100:7.1f}{s['worst_mo']:7.1f}{s['win_mo']:6.0f}"
              f"{i['sh_m']:7.2f}{o['sh_m']:8.2f}{th:>22s}")

    pos = sum(1 for _, s, _ in rows if s["final"] > 100)
    print(f"\n  per-pair: {pos}/{len(rows)} profitable on the common window")
    sh = sorted(rows, key=lambda r: -r[1]["sh_m"])
    print(f"  best 5 by Sh(mo): " + ", ".join(f"{p} {s['sh_m']:+.2f}" for p, s, _ in sh[:5]))
    print(f"  worst 5 by Sh(mo): " + ", ".join(f"{p} {s['sh_m']:+.2f}" for p, s, _ in sh[-5:]))


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
