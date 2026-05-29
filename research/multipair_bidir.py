"""Multi-pair bidir production portfolio backtest + correlation analysis.

Runs the production preset (triple_bidir + dir-regime + F&G + decay + funding)
on each of the 5 validated winner pairs, then combines them under several
weighting schemes. The key question: does diversification actually reduce
drawdown, or does crypto correlation kill the benefit?

Outputs:
  - per-pair stats over the COMMON date window (apples-to-apples)
  - monthly-return correlation matrix between pairs
  - combined portfolio under equal / sharpe-tilted / inj-heavy weights
  - diversification verdict (portfolio MDD vs best single asset)

Run from project root:
    python3 research/multipair_bidir.py
"""
from __future__ import annotations

import sys as _sys, os as _os
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

import time
import numpy as np
import pandas as pd

from core.backtest import Trade
from core.backtest_enhanced import EnhancedBTConfig, run_backtest_enhanced
from core.data import fetch_ohlcv
from core.regime_strategy import walk_forward_regimes
from core.sentiment import align_to_bars, fetch_fear_greed
from core.strategies import triple_confirm_bidir


# 5 validated winners (Sharpe > 1.1 in the cross-asset sweep)
WINNERS = ["INJ-USDT", "SOL-USDT", "ADA-USDT", "ETH-USDT", "LINK-USDT"]

WEIGHT_SCHEMES = {
    "equal":        {"INJ-USDT": .20, "SOL-USDT": .20, "ADA-USDT": .20, "ETH-USDT": .20, "LINK-USDT": .20},
    "sharpe_tilt":  {"INJ-USDT": .30, "SOL-USDT": .22, "ADA-USDT": .20, "ETH-USDT": .16, "LINK-USDT": .12},
    "inj_heavy":    {"INJ-USDT": .40, "SOL-USDT": .20, "ADA-USDT": .15, "ETH-USDT": .15, "LINK-USDT": .10},
    "inj_sol_ada":  {"INJ-USDT": .40, "SOL-USDT": .30, "ADA-USDT": .30, "ETH-USDT": .00, "LINK-USDT": .00},
}


def apply_funding(eq, trades, bps=1.0):
    if not trades: return eq, trades
    per_bar = (bps / 1e4) / 8
    new = []; deltas = []
    for t in trades:
        c = per_bar * t.notional * t.bars_held * t.side
        nt = Trade(side=t.side, entry_time=t.entry_time, exit_time=t.exit_time,
                   entry_px=t.entry_px, exit_px=t.exit_px, qty=t.qty,
                   notional=t.notional, sl=t.sl, tp=t.tp,
                   pnl=t.pnl - c, fees=t.fees + max(c,0), reason=t.reason,
                   bars_held=t.bars_held)
        new.append(nt); deltas.append((nt.exit_time, nt.pnl - t.pnl))
    eq2 = eq.copy(); off = pd.Series(0.0, index=eq2.index)
    for ts, d in deltas: off.loc[eq2.index >= ts] += d
    return eq2 + off, new


def make_signal(df, regimes, fng_aligned):
    sig = triple_confirm_bidir(df)
    a = regimes.reindex(sig.index, method="ffill").fillna("CHOP")
    block_reg = ((sig["signal"] ==  1) & (~a.isin(["BULL","CHOP"]))) | \
                ((sig["signal"] == -1) & (~a.isin(["BEAR","CHOP"])))
    sig.loc[block_reg, "signal"] = 0
    sig.loc[block_reg, ["sl","tp"]] = np.nan
    if fng_aligned is not None:
        block_fng = ((sig["signal"] ==  1) & (fng_aligned >= 80)) | \
                    ((sig["signal"] == -1) & (fng_aligned <= 20))
        sig.loc[block_fng, "signal"] = 0
        sig.loc[block_fng, ["sl","tp"]] = np.nan
    return sig


def curve_stats(eq, name=""):
    final = float(eq.iloc[-1]); ret = float(final / eq.iloc[0] - 1)
    mdd = float((eq / eq.cummax() - 1).min())
    bar_ret = eq.pct_change().fillna(0)
    sharpe = float(bar_ret.mean() / bar_ret.std() * np.sqrt(24*365)) if bar_ret.std() > 0 else 0
    days = (eq.index[-1] - eq.index[0]).days
    cagr = (final / eq.iloc[0]) ** (365/max(days,1)) - 1
    months = eq.resample("ME").last().pct_change().dropna() * 100
    return dict(name=name, final=final, ret=ret, cagr=cagr, mdd=mdd, sharpe=sharpe,
                med_mo=float(months.median()), win_mo=float((months>0).mean()*100),
                worst_mo=float(months.min()), n_months=len(months))


def main():
    days = int(_os.environ.get("DAYS", 4000))
    print("F&G fetch ...", flush=True)
    try:
        fng = fetch_fear_greed()
        print(f"  OK ({len(fng)} rows)")
    except Exception as e:
        print(f"  fail: {e}"); fng = None

    cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                           max_leverage=5.0, eq_risk_decay=0.5,
                           drawdown_for_decay=0.20)

    # Run each pair at $100 (we rescale by weight later via normalized curves)
    pair_eq = {}
    for p in WINNERS:
        print(f"\n--- {p} ---", flush=True)
        df = fetch_ohlcv(p, "1h", days=days)
        t0 = time.time()
        regs = walk_forward_regimes(df, bars_per_day=24, train_days=365, step_days=30)
        fa = align_to_bars(fng, df.index) if fng is not None else None
        sig = make_signal(df, regs, fa)
        eq, tr = run_backtest_enhanced(df, sig, cfg, long_only=False)
        eq, tr = apply_funding(eq, tr)
        pair_eq[p] = eq
        s = curve_stats(eq, p)
        print(f"  {time.time()-t0:.0f}s  final ${s['final']:.0f}  cagr {s['cagr']*100:+.1f}%  "
              f"mdd {s['mdd']*100:.1f}%  sharpe {s['sharpe']:.2f}  ({len(eq)} bars from {eq.index[0].date()})")

    # Common window (intersection) — needed to combine
    common = pair_eq[WINNERS[0]].index
    for p in WINNERS[1:]:
        common = common.intersection(pair_eq[p].index)
    print(f"\nCommon window: {common[0].date()} → {common[-1].date()} "
          f"({(common[-1]-common[0]).days/365:.1f}y, {len(common)} bars)")

    # Normalize each curve to start at 1.0 on the common window
    norm = {}
    for p in WINNERS:
        e = pair_eq[p].reindex(common).ffill()
        norm[p] = e / e.iloc[0]

    # ---- Monthly-return correlation matrix ----
    monthly = {}
    for p in WINNERS:
        monthly[p] = norm[p].resample("ME").last().pct_change().dropna()
    mdf = pd.DataFrame(monthly)
    corr = mdf.corr()
    print("\n" + "=" * 80)
    print("MONTHLY-RETURN CORRELATION MATRIX (lower = better diversification)")
    print("=" * 80)
    print(corr.round(2).to_string())
    avg_corr = (corr.values[np.triu_indices(len(WINNERS), 1)]).mean()
    print(f"\n  average pairwise correlation: {avg_corr:.2f}")

    # Per-pair stats on COMMON window (apples to apples)
    print("\n" + "=" * 110)
    print(f"PER-PAIR STATS on common {(common[-1]-common[0]).days/365:.1f}y window")
    print("=" * 110)
    rows = []
    for p in WINNERS:
        e = norm[p] * 100
        s = curve_stats(e, p)
        rows.append({"pair": p, "final$": f"{s['final']:.0f}", "cagr%": f"{s['cagr']*100:+.1f}",
                     "mdd%": f"{s['mdd']*100:+.1f}", "sharpe": f"{s['sharpe']:.2f}",
                     "med_mo%": f"{s['med_mo']:+.2f}", "win_mo%": f"{s['win_mo']:.0f}",
                     "worst_mo%": f"{s['worst_mo']:+.1f}"})
    print(pd.DataFrame(rows).to_string(index=False))

    # ---- Combined portfolios under each weight scheme ----
    print("\n" + "=" * 110)
    print("COMBINED PORTFOLIO under different weight schemes ($100 total)")
    print("=" * 110)
    best_single = max(WINNERS, key=lambda p: curve_stats(norm[p]*100)["sharpe"])
    best_single_stats = curve_stats(norm[best_single] * 100, best_single)

    rows = []
    for scheme, weights in WEIGHT_SCHEMES.items():
        port = sum(norm[p] * weights[p] for p in WINNERS) * 100
        s = curve_stats(port, scheme)
        rows.append({"scheme": scheme, "final$": f"{s['final']:.0f}",
                     "cagr%": f"{s['cagr']*100:+.1f}", "mdd%": f"{s['mdd']*100:+.1f}",
                     "sharpe": f"{s['sharpe']:.2f}", "med_mo%": f"{s['med_mo']:+.2f}",
                     "win_mo%": f"{s['win_mo']:.0f}", "worst_mo%": f"{s['worst_mo']:+.1f}"})
    rows.append({"scheme": f"[best single: {best_single}]",
                 "final$": f"{best_single_stats['final']:.0f}",
                 "cagr%": f"{best_single_stats['cagr']*100:+.1f}",
                 "mdd%": f"{best_single_stats['mdd']*100:+.1f}",
                 "sharpe": f"{best_single_stats['sharpe']:.2f}",
                 "med_mo%": f"{best_single_stats['med_mo']:+.2f}",
                 "win_mo%": f"{best_single_stats['win_mo']:.0f}",
                 "worst_mo%": f"{best_single_stats['worst_mo']:+.1f}"})
    print(pd.DataFrame(rows).to_string(index=False))

    # ---- Verdict ----
    print("\n" + "=" * 110)
    print("DIVERSIFICATION VERDICT")
    print("=" * 110)
    for scheme, weights in WEIGHT_SCHEMES.items():
        port = sum(norm[p] * weights[p] for p in WINNERS) * 100
        s = curve_stats(port)
        d_sharpe = s["sharpe"] - best_single_stats["sharpe"]
        d_mdd = s["mdd"] - best_single_stats["mdd"]
        print(f"  {scheme:14s}: sharpe {s['sharpe']:.2f} ({d_sharpe:+.2f} vs best single)  "
              f"mdd {s['mdd']*100:.1f}% ({d_mdd*100:+.1f}pp)  cagr {s['cagr']*100:+.1f}%")


if __name__ == "__main__":
    main()
