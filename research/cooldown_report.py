"""Full backtest comparison: PRODUCTION (K=0) vs RE-ENTRY COOLDOWN, on the live
5-pair inj_heavy portfolio. The decision input for deploying the cooldown live.

Full production stack on every pair: triple_confirm_bidir -> walk-forward GMM
regime mask -> F&G 3-day persistence -> enhanced engine w/ decay tiers -> funding.

Produces:
  - per-pair K=0 / K=3 / K=6 metrics
  - inj_heavy PORTFOLIO comparison (return, CAGR, Sharpe, MDD, worst/best/median
    month, % positive months)
  - MONTH-BY-MONTH portfolio returns (PROD K=0 | COOL K=3 | diff)
  - OOS robustness: pick best K on the first 60% of the window, report held-out 40%

Run from project root:
    python research/cooldown_report.py
    DAYS=1825 python research/cooldown_report.py
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
from core.data import fetch_ohlcv
from core.regime_strategy import walk_forward_regimes
from core.sentiment import align_to_bars, fetch_fear_greed
from research.test_reentry_cooldown_prod import (
    run_enhanced_with_cooldown, regime_filtered, fng_persist_3d, apply_funding)

PAIRS = ["INJ-USDT", "SOL-USDT", "ADA-USDT", "ETH-USDT", "LINK-USDT"]
WEIGHTS = {"INJ-USDT": .40, "SOL-USDT": .20, "ADA-USDT": .15,
           "ETH-USDT": .15, "LINK-USDT": .10}
DAYS = int(_os.environ.get("DAYS", 1825))
WARMUP_D = 365
KS = [0, 3, 6, 12]


def curve_stats(eq):
    final = float(eq.iloc[-1]); ret = final / float(eq.iloc[0]) - 1
    mdd = float((eq / eq.cummax() - 1).min())
    br = eq.pct_change().fillna(0)
    sharpe = float(br.mean() / br.std() * np.sqrt(24 * 365)) if br.std() > 0 else 0
    days = (eq.index[-1] - eq.index[0]).days
    cagr = (final / float(eq.iloc[0])) ** (365 / max(days, 1)) - 1
    mo = eq.resample("ME").last().pct_change().dropna() * 100
    return dict(final=final, ret=ret, cagr=cagr, mdd=mdd, sharpe=sharpe,
                worst_mo=float(mo.min()) if len(mo) else 0,
                best_mo=float(mo.max()) if len(mo) else 0,
                med_mo=float(mo.median()) if len(mo) else 0,
                win_mo=float((mo > 0).mean() * 100) if len(mo) else 0)


def portfolio_on(pair_eq, K, idx):
    """inj_heavy weighted portfolio (each leg normalized to 1.0 at idx start)."""
    port = None
    for p in PAIRS:
        e = pair_eq[K][p].reindex(idx).ffill()
        e = e / e.iloc[0] * WEIGHTS[p]
        port = e if port is None else port + e
    return port * 100


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing — regime would be an all-CHOP no-op")
    cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                           max_leverage=5.0, eq_decay_tiers=DEFAULT_DECAY_TIERS)
    print(f"DAYS={DAYS}  warmup_drop={WARMUP_D}d   PROD=K0  COOLDOWN=K3 (K6/K12 shown)\n", flush=True)
    fng = fetch_fear_greed()

    pair_eq = {K: {} for K in KS}
    pair_tr = {K: {} for K in KS}
    per_pair = []
    for p in PAIRS:
        t0 = time.time()
        df = fetch_ohlcv(p, "1h", days=DAYS)
        regs = walk_forward_regimes(df, bars_per_day=24, train_days=365, step_days=30)
        fa = align_to_bars(fng, df.index)
        sig = fng_persist_3d(regime_filtered(df, regs), fa)
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]; sige = sig[sig.index >= cut]
        row = {"pair": p}
        for K in KS:
            eq, tr = run_enhanced_with_cooldown(dfe, sige, cfg, K)
            eq = apply_funding(eq, tr)
            pair_eq[K][p] = eq; pair_tr[K][p] = tr
            s = curve_stats(eq)
            row[K] = (s["ret"] * 100, s["sharpe"], s["mdd"] * 100, len(tr))
        per_pair.append(row)
        print(f"  {p}: {time.time()-t0:.0f}s  window {dfe.index[0].date()}..{dfe.index[-1].date()} "
              f"({len(dfe)} bars)", flush=True)

    common = pair_eq[0][PAIRS[0]].index
    for p in PAIRS[1:]:
        common = common.intersection(pair_eq[0][p].index)
    yrs = (common[-1] - common[0]).days / 365
    ports = {K: portfolio_on(pair_eq, K, common) for K in KS}

    # ---- per-pair ----
    print("\n" + "=" * 86)
    print("PER-PAIR (each $100, post-warmup, full production stack)")
    print("=" * 86)
    print(f"{'pair':10s}" + "".join(f"{'K'+str(K)+' ret%':>11s}{'Sh':>6s}{'MDD%':>7s}{'n':>5s}" for K in [0, 3, 6]))
    for r in per_pair:
        ln = f"{r['pair']:10s}"
        for K in [0, 3, 6]:
            ret, sh, mdd, n = r[K]
            ln += f"{ret:>+11.0f}{sh:>6.2f}{mdd:>+7.0f}{n:>5d}"
        print(ln)

    # ---- portfolio ----
    s = {K: curve_stats(ports[K]) for K in KS}
    print("\n" + "=" * 86)
    print(f"INJ_HEAVY PORTFOLIO  ($100 start, {yrs:.1f}y common window "
          f"{common[0].date()}..{common[-1].date()})")
    print("=" * 86)
    print(f"{'metric':16s}{'PROD K=0':>13s}{'COOL K=3':>13s}{'COOL K=6':>13s}{'Δ K3-K0':>12s}")

    def line(label, key, scale=1.0, fmt="{:>13.1f}", dfmt="{:>+12.1f}"):
        a, b, c = s[0][key] * scale, s[3][key] * scale, s[6][key] * scale
        print(f"{label:16s}" + fmt.format(a) + fmt.format(b) + fmt.format(c) + dfmt.format(b - a))

    print(f"{'final $':16s}{s[0]['final']:>13.0f}{s[3]['final']:>13.0f}{s[6]['final']:>13.0f}{s[3]['final']-s[0]['final']:>+12.0f}")
    line("total ret %", "ret", 100, "{:>13.0f}", "{:>+12.0f}")
    line("CAGR %", "cagr", 100)
    line("Sharpe", "sharpe", 1.0, "{:>13.2f}", "{:>+12.2f}")
    line("MaxDD %", "mdd", 100)
    line("worst month %", "worst_mo")
    line("best month %", "best_mo")
    line("median month %", "med_mo", 1.0, "{:>13.2f}", "{:>+12.2f}")
    line("% pos months", "win_mo", 1.0, "{:>13.0f}", "{:>+12.0f}")

    # ---- month by month ----
    print("\n" + "=" * 86)
    print("MONTH-BY-MONTH PORTFOLIO RETURN %   (PROD K=0 | COOL K=3 | diff)")
    print("=" * 86)
    m0 = ports[0].resample("ME").last().pct_change().dropna() * 100
    m3 = ports[3].resample("ME").last().pct_change().dropna() * 100
    mt = pd.DataFrame({"K0": m0, "K3": m3}); mt["d"] = mt["K3"] - mt["K0"]
    for ts, r in mt.iterrows():
        flag = "  <" if r["d"] < 0 else ""
        print(f"  {ts:%Y-%m}    {r['K0']:>+7.1f}   {r['K3']:>+7.1f}   {r['d']:>+7.1f}{flag}")
    print(f"\n  {len(mt)} months;  K=3 beat K=0 in {int((mt['d']>0).sum())}/{len(mt)} months;  "
          f"K=3 worst {mt['K3'].min():+.1f}% vs K=0 worst {mt['K0'].min():+.1f}%")

    # ---- OOS robustness ----
    print("\n" + "=" * 86)
    print("OOS CHECK — pick best K>0 on first 60% of window, report on held-out last 40%")
    print("=" * 86)
    split = common[int(len(common) * 0.6)]
    is_idx = common[common < split]; oos_idx = common[common >= split]
    is_sh = {K: curve_stats(portfolio_on(pair_eq, K, is_idx))["sharpe"] for K in [3, 6, 12]}
    best_K = max(is_sh, key=is_sh.get)
    oos0 = curve_stats(portfolio_on(pair_eq, 0, oos_idx))["sharpe"]
    oosb = curve_stats(portfolio_on(pair_eq, best_K, oos_idx))["sharpe"]
    print(f"  IS  ({is_idx[0].date()}..{is_idx[-1].date()}): best K = {best_K}  "
          f"(Sharpe by K: " + ", ".join(f"K{k} {v:.2f}" for k, v in is_sh.items()) + ")")
    print(f"  OOS ({oos_idx[0].date()}..{oos_idx[-1].date()}): Sharpe  K=0 {oos0:.2f}  "
          f"vs  K={best_K} {oosb:.2f}   (Δ {oosb-oos0:+.2f})")
    print(f"\n  VERDICT: in-sample-selected K generalizes OOS "
          f"{'YES' if oosb > oos0 else 'NO'} (OOS ΔSharpe {oosb-oos0:+.2f}).")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
