"""Portfolio construction study:

  PART A — diversification depth: does adding pairs beyond 5 help? Build
           best-N portfolios (equal-weight) for N = 3,4,5,6,7,8 and compare.
  PART B — weighting schemes on a fixed set:
             equal            1/N
             inj_heavy        current production (40/20/15/15/10)
             sharpe_tilt      proportional to backtest Sharpe
             inv_vol          risk parity (proportional to 1/monthly-vol)
             max_sharpe       in-sample mean-variance optimum (overfit ceiling)
             lean_sol_ada     the user's "winners get more" idea
  PART C — dynamic rebalancing: monthly reallocate toward trailing-3-month
           Sharpe winners vs the static equal-weight. Tests the "give more
           equity to pairs that are winning" idea at the portfolio level.

All on the current production strategy. Honest about in-sample overfit:
max_sharpe / lean_sol_ada are fit to the same data they're scored on.

Run from project root (set PAIRS to the viable universe):
    PAIRS="INJ-USDT,SOL-USDT,ADA-USDT,ETH-USDT,LINK-USDT" python3 research/portfolio_construction.py
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

DEFAULT_PAIRS = ["INJ-USDT", "SOL-USDT", "ADA-USDT", "ETH-USDT", "LINK-USDT"]


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
    if fng is not None:
        daily = fng["fng"].copy()
        daily.index = pd.DatetimeIndex(daily.index, tz="UTC")
        g = ((daily >= 80).rolling(3).sum() == 3).reindex(s.index, method="ffill").fillna(False)
        f = ((daily <= 20).rolling(3).sum() == 3).reindex(s.index, method="ffill").fillna(False)
        bf = ((s["signal"] == 1) & g) | ((s["signal"] == -1) & f)
        s.loc[bf, "signal"] = 0
        s.loc[bf, ["sl", "tp"]] = np.nan
    return s


def pair_equity(sym, fng, days):
    df = fetch_ohlcv(sym, "1h", days=days)
    regs = walk_forward_regimes(df, bars_per_day=24, train_days=365, step_days=30)
    sig = production_signal(df, regs, fng)
    cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                           max_leverage=5.0, eq_decay_tiers=DEFAULT_DECAY_TIERS)
    eq, tr = run_backtest_enhanced(df, sig, cfg, long_only=False)
    return apply_funding(eq, tr)


def stats(eq):
    final = float(eq.iloc[-1]); ret = float(final / eq.iloc[0] - 1)
    mdd = float((eq / eq.cummax() - 1).min())
    br = eq.pct_change().fillna(0)
    sharpe = float(br.mean() / br.std() * np.sqrt(24 * 365)) if br.std() > 0 else 0
    d = (eq.index[-1] - eq.index[0]).days
    cagr = (final / eq.iloc[0]) ** (365 / max(d, 1)) - 1
    months = eq.resample("ME").last().pct_change().dropna() * 100
    return dict(final=final, ret=ret, cagr=cagr, mdd=mdd, sharpe=sharpe,
                worst_mo=float(months.min()) if len(months) else 0,
                win_mo=float((months > 0).mean() * 100) if len(months) else 0)


def combine(norm, weights, common):
    return sum(norm[p].reindex(common).ffill() * weights[p] for p in weights) * 100


def main():
    days = int(_os.environ.get("DAYS", 4000))
    pairs = _os.environ.get("PAIRS", ",".join(DEFAULT_PAIRS)).split(",")
    fng = fetch_fear_greed()

    print(f"Computing per-pair equity curves for {len(pairs)} pairs ...", flush=True)
    eqs = {}
    for p in pairs:
        t0 = time.time()
        eqs[p] = pair_equity(p, fng, days)
        print(f"  {p:12s} done ({time.time()-t0:.0f}s)", flush=True)

    # common window + normalized curves
    common = eqs[pairs[0]].index
    for p in pairs[1:]:
        common = common.intersection(eqs[p].index)
    norm = {p: (eqs[p].reindex(common).ffill() / eqs[p].reindex(common).ffill().iloc[0])
            for p in pairs}
    print(f"\nCommon window: {common[0].date()} → {common[-1].date()} "
          f"({(common[-1]-common[0]).days/365:.1f}y)")

    # per-pair stats on the common window (for ranking + weighting)
    pstats = {p: stats(norm[p] * 100) for p in pairs}
    monthly = {p: (norm[p].resample("ME").last().pct_change().dropna()) for p in pairs}
    ranked = sorted(pairs, key=lambda p: pstats[p]["sharpe"], reverse=True)
    print("\nPer-pair (common window), ranked by Sharpe:")
    for p in ranked:
        s = pstats[p]
        print(f"  {p:12s} sharpe {s['sharpe']:.2f}  cagr {s['cagr']*100:+.0f}%  "
              f"mdd {s['mdd']*100:.0f}%")

    # ---------- PART A: diversification depth (equal-weight best-N) ----------
    print("\n" + "=" * 92)
    print("PART A — DIVERSIFICATION DEPTH (best-N by Sharpe, equal-weight)")
    print("=" * 92)
    print(f"{'N pairs':10s}{'pairs':42s}{'CAGR':>8s}{'MDD':>8s}{'Sharpe':>8s}{'worst_mo':>10s}")
    for n in range(3, len(pairs) + 1):
        sel = ranked[:n]
        w = {p: 1.0 / n for p in sel}
        port = combine(norm, w, common)
        s = stats(port)
        names = ",".join(p.split("-")[0] for p in sel)
        print(f"{n:<10d}{names:42s}{s['cagr']*100:>+7.0f}%{s['mdd']*100:>+7.0f}%"
              f"{s['sharpe']:>8.2f}{s['worst_mo']:>+9.1f}%")

    # ---------- PART B: weighting schemes on the current 5 (or given set) ----
    base5 = [p for p in DEFAULT_PAIRS if p in pairs]
    if len(base5) >= 3:
        print("\n" + "=" * 92)
        print(f"PART B — WEIGHTING SCHEMES on {len(base5)} pairs: {[p.split('-')[0] for p in base5]}")
        print("=" * 92)
        n = len(base5)
        # equal
        schemes = {"equal": {p: 1.0 / n for p in base5}}
        # inj_heavy (only if exactly the production 5)
        ih = {"INJ-USDT": .40, "SOL-USDT": .20, "ADA-USDT": .15, "ETH-USDT": .15, "LINK-USDT": .10}
        if set(base5) == set(ih):
            schemes["inj_heavy(current)"] = ih
        # sharpe-tilt (proportional to max(sharpe,0))
        sh = {p: max(pstats[p]["sharpe"], 0.01) for p in base5}
        tot = sum(sh.values()); schemes["sharpe_tilt"] = {p: sh[p] / tot for p in base5}
        # inverse-vol (risk parity on monthly vol)
        iv = {p: 1.0 / (monthly[p].std() or 1) for p in base5}
        tot = sum(iv.values()); schemes["inv_vol(risk_parity)"] = {p: iv[p] / tot for p in base5}
        # lean_sol_ada (user idea)
        if set(["SOL-USDT", "ADA-USDT"]).issubset(base5):
            la = {p: (0.30 if p in ("SOL-USDT", "ADA-USDT") else 0.40 / (n - 2)) for p in base5}
            tot = sum(la.values()); schemes["lean_sol_ada"] = {p: la[p] / tot for p in base5}
        # max-sharpe MVO (in-sample; overfit ceiling) via simple long-only grid on monthly
        M = pd.DataFrame({p: monthly[p] for p in base5}).dropna()
        mu = M.mean().values; cov = M.cov().values
        best = None
        rng = np.random.default_rng(42)
        for _ in range(20000):
            wv = rng.random(n); wv /= wv.sum()
            r = wv @ mu; v = np.sqrt(wv @ cov @ wv)
            sr = r / v if v > 0 else 0
            if best is None or sr > best[0]:
                best = (sr, wv)
        schemes["max_sharpe(overfit)"] = {p: float(best[1][i]) for i, p in enumerate(base5)}

        print(f"{'scheme':22s}{'weights':38s}{'CAGR':>8s}{'MDD':>8s}{'Sharpe':>8s}{'worst_mo':>10s}")
        for name, w in schemes.items():
            port = combine(norm, w, common)
            s = stats(port)
            wstr = " ".join(f"{p.split('-')[0]}:{w[p]*100:.0f}" for p in base5)
            print(f"{name:22s}{wstr:38s}{s['cagr']*100:>+7.0f}%{s['mdd']*100:>+7.0f}%"
                  f"{s['sharpe']:>8.2f}{s['worst_mo']:>+9.1f}%")

        # ---------- PART C: dynamic rebalancing toward trailing winners -------
        print("\n" + "=" * 92)
        print("PART C — DYNAMIC REBALANCE (monthly, weight by trailing 3mo Sharpe) vs static equal")
        print("=" * 92)
        # Build monthly return matrix, walk forward: each month, weight next
        # month by prior-3mo Sharpe of each pair (no look-ahead).
        Mall = pd.DataFrame({p: monthly[p] for p in base5}).dropna()
        eqw = {p: 1.0 / n for p in base5}
        static_port = combine(norm, eqw, common)
        # dynamic: reconstruct monthly portfolio return
        dyn_ret = []
        idx = Mall.index
        for i in range(len(idx)):
            if i < 3:
                w = np.array([1.0 / n] * n)
            else:
                window = Mall.iloc[i-3:i]
                sr = window.mean() / window.std().replace(0, np.nan)
                sr = sr.clip(lower=0).fillna(0).values
                w = sr / sr.sum() if sr.sum() > 0 else np.array([1.0 / n] * n)
            dyn_ret.append(float(w @ Mall.iloc[i].values))
        dyn = pd.Series(dyn_ret, index=idx)
        static_m = static_port.resample("ME").last().pct_change().dropna() * 100
        def m_stats(m):
            comp = float((1 + m / 100).prod() - 1)
            sr = float(m.mean() / m.std() * np.sqrt(12)) if m.std() > 0 else 0
            # approx MDD from monthly compounding
            curve = (1 + m / 100).cumprod()
            mdd = float((curve / curve.cummax() - 1).min())
            return comp, sr, mdd
        sc, ss, sm = m_stats(static_m)
        dc, ds, dm = m_stats(dyn)
        print(f"  {'scheme':22s}{'total_ret':>12s}{'mo_sharpe':>11s}{'mo_mdd':>9s}")
        print(f"  {'static equal':22s}{sc*100:>+11.0f}%{ss:>11.2f}{sm*100:>8.0f}%")
        print(f"  {'dynamic 3mo-sharpe':22s}{dc*100:>+11.0f}%{ds:>11.2f}{dm*100:>8.0f}%")
        print("\n  (monthly-return approximation; dynamic uses prior-3mo Sharpe, no look-ahead)")


if __name__ == "__main__":
    main()
