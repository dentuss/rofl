"""Structural universe + VT universe-gate (G4).

Two questions, one run over the 23 structural qualifiers:
1. Does an EX-ANTE structural basket (top-8 by median daily dollar volume —
   a strategy-independent liquidity criterion, computed not chosen) beat the
   EW5/EW10 books? (The breadth laws forbid performance-picking; this is the
   legal version of "trade the majors".)
2. G4 for vol targeting: does RSCD3+VT hold up on every book, not just EW5?

PRE-REGISTERED cells: books {EW5, EW10, MAJORS8, EW23} x stacks {RSCD3,
RSCD3+VT}. All reported.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/structural_universe.py
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
from research.vol_target import vt_mult

QUAL23 = [f"{b}-USDT" for b in
          ["BTC", "ETH", "SOL", "ADA", "LINK", "AVAX", "NEAR", "AAVE", "GRT",
           "RUNE", "DOGE", "DOT", "ATOM", "LTC", "XRP", "BNB", "FIL", "OP",
           "UNI", "ETC", "BCH", "TRX", "SAND"]]
EW5_NAMES = ["INJ-USDT", "SOL-USDT", "ADA-USDT", "ETH-USDT", "LINK-USDT"]
TEN = [f"{b}-USDT" for b in ["SOL", "ADA", "ETH", "LINK", "AVAX", "NEAR",
                             "AAVE", "GRT", "RUNE", "DOGE"]]
TOTAL = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))
WARMUP_D = 365
BPD = 6
COMMON_START = pd.Timestamp("2023-08-17", tz="UTC")


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    pairs = sorted(set(QUAL23) | set(EW5_NAMES))
    print(f"STRUCTURAL UNIVERSE + VT G4  pairs={len(pairs)}", flush=True)
    fng = fetch_fear_greed()

    eqs = {"RSCD3": {}, "RSCD3+VT": {}}
    dollar_vol = {}
    for p in pairs:
        t0 = time.time()
        df = fetch_ohlcv_bybit(p, "4h", days=DAYS)
        dollar_vol[p] = float((df["close"] * df["volume"])
                              .loc[df.index >= COMMON_START]
                              .resample("1D").sum().median())
        regs = walk_forward_regimes(df, bars_per_day=BPD, train_days=365,
                                    step_days=30)
        fa = align_to_bars(fng, df.index)
        fund = fetch_funding(p, days=DAYS, source="auto")
        sig = fng_persist(regime_mask(triple_confirm_bidir(df, tp_mult=6.0),
                                      regs), fa)
        a = regs.reindex(sig.index, method="ffill").fillna("CHOP")
        chop = np.where(a == "CHOP", 0.5, 1.0)
        vt = vt_mult(df).reindex(sig.index).fillna(1.0).to_numpy()
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]
        for name, mult in [("RSCD3", chop), ("RSCD3+VT", chop * vt)]:
            s = sig.copy()
            s["risk_mult"] = mult
            cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                                   max_leverage=5.0,
                                   eq_decay_tiers=DEFAULT_DECAY_TIERS,
                                   cooldown_bars=3, fee_rate=FEE_TAKER,
                                   fee_maker=FEE_MAKER, entry_style="maker_close")
            eq, tr = run_backtest_enhanced(dfe, s[s.index >= cut], cfg)
            eqs[name][p] = apply_funding_real(eq, tr, fund)
        print(f"  {p:10s} {time.time()-t0:4.0f}s  medDV ${dollar_vol[p]/1e6:.1f}M",
              flush=True)

    majors8 = sorted(QUAL23, key=lambda p: -dollar_vol[p])[:8]
    print(f"\nMAJORS8 by median daily $vol (ex-ante, strategy-independent): "
          f"{', '.join(p.split('-')[0] for p in majors8)}")

    books = {
        "EW5": {p: 1 / 5 for p in EW5_NAMES},
        "EW10": {p: 1 / len(TEN) for p in TEN},
        "MAJORS8": {p: 1 / 8 for p in majors8},
        "EW23": {p: 1 / len(QUAL23) for p in QUAL23},
    }

    print("\n" + "=" * 88)
    print(f"BOOKS x STACKS  (common window from {COMMON_START.date()}) @ ${TOTAL:.0f}")
    print("=" * 88)
    print(f"{'book/stack':20s}{'final$':>8s}{'CAGR%':>8s}{'Sh(mo)':>8s}{'MDD%':>7s}"
          f"{'worst':>7s}{'IS Sh':>7s}{'OOS Sh':>8s}{'thirds':>22s}")
    for bname, w in books.items():
        idx = None
        for p in w:
            e = eqs["RSCD3"][p][eqs["RSCD3"][p].index >= COMMON_START]
            idx = e.index if idx is None else idx.intersection(e.index)
        idx = idx.sort_values()
        split = idx[int(len(idx) * 0.6)]
        i_idx, o_idx = idx[idx < split], idx[idx >= split]
        b3 = [idx[0] + (idx[-1] - idx[0]) * k / 3 for k in range(4)]
        for sname in ("RSCD3", "RSCD3+VT"):
            s = stats(build(eqs[sname], w, idx))
            i = stats(build(eqs[sname], w, i_idx))
            o = stats(build(eqs[sname], w, o_idx))
            th = "  ".join(
                f"{sharpe_m(build(eqs[sname], w, idx[(idx >= b3[k]) & (idx < b3[k+1])])):+.2f}"
                for k in range(3))
            print(f"{bname + '/' + sname:20s}{s['final']:8.0f}{s['cagr']*100:8.1f}"
                  f"{s['sh_m']:8.2f}{s['mdd']*100:7.1f}{s['worst_mo']:7.1f}"
                  f"{i['sh_m']:7.2f}{o['sh_m']:8.2f}{th:>22s}")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
