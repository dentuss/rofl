"""Expand the pair universe: run the CURRENT production strategy across a wide
candidate set, rank by risk-adjusted return, and identify pairs worth adding.

Production strategy = triple_bidir + directional regime filter + F&G 3-day
persistence + three-tier decay + funding. Bar for "viable": Sharpe >= 1.1
(the level the current 5 winners cleared).

Run from project root:
    python3 research/expand_universe.py
    CANDS="AVAX-USDT,DOT-USDT" python3 research/expand_universe.py
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

CURRENT = ["INJ-USDT", "SOL-USDT", "ADA-USDT", "ETH-USDT", "LINK-USDT"]
CANDIDATES = [
    "AVAX-USDT", "DOT-USDT", "ATOM-USDT", "NEAR-USDT", "RUNE-USDT",
    "AAVE-USDT", "UNI-USDT", "FIL-USDT", "ICP-USDT", "ALGO-USDT",
    "XLM-USDT", "APT-USDT", "ARB-USDT", "OP-USDT", "DOGE-USDT", "DYDX-USDT",
    "GRT-USDT", "SAND-USDT", "XRP-USDT", "BNB-USDT", "ETC-USDT", "EGLD-USDT",
    "MANA-USDT", "AXS-USDT", "THETA-USDT", "HBAR-USDT", "VET-USDT", "BCH-USDT",
]
MIN_DAYS = 900  # ~2.5y minimum so the regime detector + decay have room


def apply_funding(eq, trades, bps=1.0):
    if not trades:
        return eq
    pb = (bps / 1e4) / 8
    off = pd.Series(0.0, index=eq.index)
    for t in trades:
        off.loc[eq.index >= t.exit_time] -= pb * t.notional * t.bars_held * t.side
    return eq + off


def production_signal(df, regs, fng):
    s = triple_confirm_bidir(df)
    a = regs.reindex(s.index, method="ffill").fillna("CHOP")
    b = ((s["signal"] == 1) & (~a.isin(["BULL", "CHOP"]))) | \
        ((s["signal"] == -1) & (~a.isin(["BEAR", "CHOP"])))
    s.loc[b, "signal"] = 0
    s.loc[b, ["sl", "tp"]] = np.nan
    # F&G 3-day persistence (matches the live bot)
    if fng is not None:
        daily = fng["fng"].copy()
        daily.index = pd.DatetimeIndex(daily.index, tz="UTC")
        greed3 = (daily >= 80).rolling(3).sum() == 3
        fear3 = (daily <= 20).rolling(3).sum() == 3
        g = greed3.reindex(s.index, method="ffill").fillna(False)
        f = fear3.reindex(s.index, method="ffill").fillna(False)
        bf = ((s["signal"] == 1) & g) | ((s["signal"] == -1) & f)
        s.loc[bf, "signal"] = 0
        s.loc[bf, ["sl", "tp"]] = np.nan
    return s


def run_pair(sym, fng, days):
    df = fetch_ohlcv(sym, "1h", days=days)
    yrs = (df.index[-1] - df.index[0]).days / 365
    if (df.index[-1] - df.index[0]).days < MIN_DAYS:
        return None
    regs = walk_forward_regimes(df, bars_per_day=24, train_days=365, step_days=30)
    sig = production_signal(df, regs, fng)
    cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                           max_leverage=5.0, eq_decay_tiers=DEFAULT_DECAY_TIERS)
    eq, tr = run_backtest_enhanced(df, sig, cfg, long_only=False)
    eq = apply_funding(eq, tr)
    final = float(eq.iloc[-1]); ret = final / 100 - 1
    mdd = float((eq / eq.cummax() - 1).min())
    br = eq.pct_change().fillna(0)
    sharpe = float(br.mean() / br.std() * np.sqrt(24 * 365)) if br.std() > 0 else 0
    d = (eq.index[-1] - eq.index[0]).days
    cagr = (final / 100) ** (365 / max(d, 1)) - 1
    months = eq.resample("ME").last().pct_change().dropna() * 100
    n = len(tr)
    short_pnl = sum(t.pnl for t in tr if t.side == -1)
    return dict(pair=sym, years=round(yrs, 1), final=final, cagr=cagr, mdd=mdd,
                sharpe=sharpe, n=n, short_pnl=short_pnl,
                med_mo=float(months.median()) if len(months) else 0,
                win_mo=float((months > 0).mean() * 100) if len(months) else 0)


def main():
    days = int(_os.environ.get("DAYS", 4000))
    cands = _os.environ.get("CANDS")
    pairs = cands.split(",") if cands else (CURRENT + CANDIDATES)
    fng = fetch_fear_greed()

    rows = []
    for p in pairs:
        try:
            t0 = time.time()
            r = run_pair(p, fng, days)
            if r is None:
                print(f"  {p:12s} skipped (<{MIN_DAYS}d history)", flush=True)
                continue
            r["is_current"] = p in CURRENT
            rows.append(r)
            print(f"  {p:12s} {r['years']:.1f}y  final ${r['final']:.0f}  "
                  f"cagr {r['cagr']*100:+.0f}%  mdd {r['mdd']*100:.0f}%  "
                  f"sharpe {r['sharpe']:.2f}  ({time.time()-t0:.0f}s)", flush=True)
        except Exception as e:
            print(f"  {p:12s} ERROR {str(e)[:60]}", flush=True)

    df = pd.DataFrame(rows).sort_values("sharpe", ascending=False)
    print("\n" + "=" * 110)
    print("UNIVERSE RANKED BY SHARPE (production strategy, funding modeled)")
    print("=" * 110)
    print(f"{'pair':12s}{'cur?':>5s}{'years':>7s}{'final$':>9s}{'cagr':>8s}"
          f"{'mdd':>8s}{'sharpe':>8s}{'trades':>8s}{'short$':>9s}{'med_mo':>8s}{'win_mo':>8s}")
    for _, r in df.iterrows():
        cur = "***" if r["is_current"] else ""
        bar = " <-- viable (Sharpe>=1.1)" if (r["sharpe"] >= 1.1 and not r["is_current"]) else ""
        print(f"{r['pair']:12s}{cur:>5s}{r['years']:>7.1f}{r['final']:>9.0f}"
              f"{r['cagr']*100:>+7.0f}%{r['mdd']*100:>+7.0f}%{r['sharpe']:>8.2f}"
              f"{r['n']:>8.0f}{r['short_pnl']:>+9.0f}{r['med_mo']:>+7.2f}%"
              f"{r['win_mo']:>7.0f}%{bar}")

    # New viable candidates
    new_viable = df[(~df["is_current"]) & (df["sharpe"] >= 1.1)]
    print(f"\nNew viable pairs (Sharpe >= 1.1, not currently used): "
          f"{list(new_viable['pair']) if len(new_viable) else 'NONE'}")
    print(f"Current 5 Sharpe range: "
          f"{df[df['is_current']]['sharpe'].min():.2f} - {df[df['is_current']]['sharpe'].max():.2f}")


if __name__ == "__main__":
    main()
