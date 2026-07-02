"""Robustness / anti-overfit study for the 5-pair vs 8-pair decision, addressing
the audit's HIGH finding (in-sample selection + survivorship) and the KuCoin-vs-
Bybit and hourly-Sharpe caveats. Three things the headline compare didn't do:

  1. BYBIT PERP DATA  — the actual execution venue (not KuCoin spot). Kills the
     price-source / listing-window caveat.
  2. OUT-OF-SAMPLE SPLIT — first 60% (IS) vs held-out last 40% (OOS). If the 8p
     edge only exists IS, it's overfit.
  3. RANDOM-BASKET BASELINE — draw many random equal-weight 8-baskets from the
     SAME candidate pool the live 8 were selected from, and report where the
     chosen 8 ranks. A ~50th-pct rank means "a broad basket works" (robust); a
     ~95th-pct rank means the specific selection is doing the lifting (overfit).
  4. RANDOM-5 NULL — the same test at basket size 5, so the 5-pair SELECTION is
     judged against a like-for-like null (the panel flagged that ranking a
     5-basket inside an 8-basket distribution is not a fair selection test).
     Scores the chosen 5 names EQUAL-WEIGHT (pure selection effect) plus the
     actual weighted ORIG5 (inj_heavy) and SOFT5 (INJ-capped) books.

Also reports MONTHLY-resampled Sharpe alongside the hourly one (the audit flagged
sqrt(24*365) on autocorrelated hourly marks as inflated) — monthly is the honest
absolute; hourly is kept only for cross-reference / ranking.

Full production stack per leg (same validated engine as the compare/validation):
  triple_confirm_bidir -> walk-forward GMM regime -> F&G 3d persistence
  -> enhanced engine + decay tiers -> funding -> post-SL cooldown K.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/portfolio_robustness.py
      DAYS=2000 K=3 R=500 SEED=42 ... research/portfolio_robustness.py
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

FIVE = {"INJ-USDT": .40, "SOL-USDT": .20, "ADA-USDT": .15,
        "ETH-USDT": .15, "LINK-USDT": .10}
SOFT5 = {"INJ-USDT": .25, "SOL-USDT": .1875, "ADA-USDT": .1875,
         "ETH-USDT": .1875, "LINK-USDT": .1875}
EIGHT_PAIRS = ["INJ-USDT", "AVAX-USDT", "NEAR-USDT", "AAVE-USDT",
               "GRT-USDT", "RUNE-USDT", "DOGE-USDT", "ADA-USDT"]
# The candidate POOL the 8 were selected from (expand_universe universe, the
# liquid long-history subset available on Bybit perps). Random baskets are drawn
# from whichever of these cover the analysis window.
POOL = sorted(set(FIVE) | set(EIGHT_PAIRS) | {
    "DOT-USDT", "ATOM-USDT", "UNI-USDT", "XRP-USDT",
    "BCH-USDT", "ETC-USDT", "LTC-USDT"})

TOTAL_EQUITY = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))
WARMUP_D = 365
K = int(_os.environ.get("K", 3))
R = int(_os.environ.get("R", 500))
SEED = int(_os.environ.get("SEED", 42))
BPY = 24 * 365


def _sharpe_hourly(eq):
    br = eq.pct_change().fillna(0)
    return float(br.mean() / br.std() * np.sqrt(BPY)) if br.std() > 0 else 0.0


def _sharpe_monthly(eq):
    m = eq.resample("ME").last().pct_change().dropna()
    return float(m.mean() / m.std() * np.sqrt(12)) if len(m) > 2 and m.std() > 0 else 0.0


def curve_stats(eq):
    final = float(eq.iloc[-1]); ret = final / float(eq.iloc[0]) - 1
    mdd = float((eq / eq.cummax() - 1).min())
    days = max((eq.index[-1] - eq.index[0]).days, 1)
    cagr = (final / float(eq.iloc[0])) ** (365 / days) - 1
    mo = eq.resample("ME").last().pct_change().dropna() * 100
    return dict(final=final, ret=ret, cagr=cagr, mdd=mdd, years=days / 365,
                sh_h=_sharpe_hourly(eq), sh_m=_sharpe_monthly(eq),
                worst_mo=float(mo.min()) if len(mo) else 0,
                med_mo=float(mo.median()) if len(mo) else 0,
                win_mo=float((mo > 0).mean() * 100) if len(mo) else 0)


def build(pair_eq, weights, idx, total):
    port = None
    for p, w in weights.items():
        e = pair_eq[p].reindex(idx).ffill()
        e = e / e.iloc[0] * w
        port = e if port is None else port + e
    return port * total


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing — regime would be an all-CHOP no-op")
    cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                           max_leverage=5.0, eq_decay_tiers=DEFAULT_DECAY_TIERS)
    print(f"SOURCE=BYBIT PERP  TOTAL_EQUITY={TOTAL_EQUITY:.0f}  DAYS={DAYS}  "
          f"warmup={WARMUP_D}d  K={K}  R={R} random baskets  SEED={SEED}")
    print(f"pool ({len(POOL)}): {POOL}\n", flush=True)
    fng = fetch_fear_greed()

    pair_eq = {}            # normalized-to-1.0 K-cooldown curve per pair
    windows = {}
    for p in POOL:
        t0 = time.time()
        try:
            df = fetch_ohlcv_bybit(p, "1h", days=DAYS)
        except Exception as e:
            print(f"  {p:11s} FETCH FAILED: {str(e)[:70]}", flush=True)
            continue
        if (df.index[-1] - df.index[0]).days < WARMUP_D + 200:
            print(f"  {p:11s} too short ({(df.index[-1]-df.index[0]).days}d) — skip", flush=True)
            continue
        regs = walk_forward_regimes(df, bars_per_day=24, train_days=365, step_days=30)
        rc = regs.value_counts().to_dict()
        if (rc.get("BULL", 0) + rc.get("BEAR", 0)) == 0:
            print(f"  {p:11s} regime all-CHOP — skip", flush=True); continue
        fa = align_to_bars(fng, df.index)
        sig = fng_persist_3d(regime_filtered(df, regs), fa)
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]; sige = sig[sig.index >= cut]
        eq, tr = run_enhanced_with_cooldown(dfe, sige, cfg, K)
        eq = apply_funding(eq, tr)
        pair_eq[p] = eq / eq.iloc[0]
        windows[p] = (dfe.index[0], dfe.index[-1])
        print(f"  {p:11s} {time.time()-t0:5.0f}s  {(dfe.index[-1]-dfe.index[0]).days/365:4.2f}y  "
              f"{dfe.index[0].date()}..{dfe.index[-1].date()}", flush=True)

    missing5 = [p for p in FIVE if p not in pair_eq]
    missing8 = [p for p in EIGHT_PAIRS if p not in pair_eq]
    if missing5 or missing8:
        raise SystemExit(f"missing core pairs — 5p:{missing5} 8p:{missing8}")

    # analysis window = intersection of the CHOSEN portfolios' pairs
    idx = None
    for p in set(FIVE) | set(EIGHT_PAIRS):
        idx = pair_eq[p].index if idx is None else idx.intersection(pair_eq[p].index)
    idx = idx.sort_values()
    yrs = (idx[-1] - idx[0]).days / 365
    # eligible pool for random baskets = pairs covering the analysis window
    eligible = [p for p in pair_eq if pair_eq[p].reindex(idx).notna().all()]
    split = idx[int(len(idx) * 0.6)]
    is_idx = idx[idx < split]; oos_idx = idx[idx >= split]

    W8 = {p: 1 / 8 for p in EIGHT_PAIRS}
    print("\n" + "=" * 88)
    print(f"BYBIT PERP — 5p vs 8p   window {idx[0].date()}..{idx[-1].date()} ({yrs:.2f}y), K={K}")
    print("=" * 88)
    s5 = curve_stats(build(pair_eq, FIVE, idx, TOTAL_EQUITY))
    s8 = curve_stats(build(pair_eq, W8, idx, TOTAL_EQUITY))
    hdr = f"{'metric':16s}{'5-pair':>14s}{'8-pair':>14s}"
    print(hdr); print("-" * 88)
    rows = [("final $", "final", 1, "{:>14.0f}"), ("total ret %", "ret", 100, "{:>14.0f}"),
            ("CAGR %", "cagr", 100, "{:>14.1f}"), ("Sharpe (monthly)", "sh_m", 1, "{:>14.2f}"),
            ("Sharpe (hourly)", "sh_h", 1, "{:>14.2f}"), ("MaxDD %", "mdd", 100, "{:>14.1f}"),
            ("worst month %", "worst_mo", 1, "{:>14.1f}"), ("median month %", "med_mo", 1, "{:>14.2f}"),
            ("% pos months", "win_mo", 1, "{:>14.0f}")]
    for label, key, sc, fmt in rows:
        print(f"{label:16s}" + fmt.format(s5[key] * sc) + fmt.format(s8[key] * sc))

    # ---- OOS split ----
    print("\n" + "=" * 88)
    print(f"OUT-OF-SAMPLE SPLIT   IS {is_idx[0].date()}..{is_idx[-1].date()} "
          f"({(is_idx[-1]-is_idx[0]).days/365:.2f}y)  |  "
          f"OOS {oos_idx[0].date()}..{oos_idx[-1].date()} ({(oos_idx[-1]-oos_idx[0]).days/365:.2f}y)")
    print("=" * 88)
    print(f"{'':16s}{'5p IS':>11s}{'5p OOS':>11s}{'8p IS':>11s}{'8p OOS':>11s}")
    parts = {"5p IS": (FIVE, is_idx), "5p OOS": (FIVE, oos_idx),
             "8p IS": (W8, is_idx), "8p OOS": (W8, oos_idx)}
    st = {k: curve_stats(build(pair_eq, w, ix, TOTAL_EQUITY)) for k, (w, ix) in parts.items()}
    for label, key, sc, fmt in [("ret %", "ret", 100, "{:>11.0f}"), ("CAGR %", "cagr", 100, "{:>11.0f}"),
                                ("Sharpe (mo)", "sh_m", 1, "{:>11.2f}"), ("MaxDD %", "mdd", 100, "{:>11.1f}"),
                                ("worst mo %", "worst_mo", 1, "{:>11.1f}")]:
        print(f"{label:16s}" + "".join(fmt.format(st[k][key] * sc) for k in
              ["5p IS", "5p OOS", "8p IS", "8p OOS"]))

    # ---- random-basket baseline ----
    print("\n" + "=" * 88)
    print(f"RANDOM 8-BASKET BASELINE   {R} draws (equal-weight) from {len(eligible)} eligible pool pairs")
    print("=" * 88)
    print(f"  eligible pool: {eligible}")
    rng = np.random.default_rng(SEED)
    full_shm, full_cagr, oos_shm = [], [], []
    for _ in range(R):
        pick = list(rng.choice(eligible, size=8, replace=False))
        w = {p: 1 / 8 for p in pick}
        full_shm.append(curve_stats(build(pair_eq, w, idx, TOTAL_EQUITY))["sh_m"])
        full_cagr.append(curve_stats(build(pair_eq, w, idx, TOTAL_EQUITY))["cagr"] * 100)
        oos_shm.append(curve_stats(build(pair_eq, w, oos_idx, TOTAL_EQUITY))["sh_m"])
    full_shm = np.array(full_shm); oos_shm = np.array(oos_shm); full_cagr = np.array(full_cagr)

    def pct_rank(arr, v):
        return float((arr <= v).mean() * 100)

    chosen_full = s8["sh_m"]; chosen_oos = st["8p OOS"]["sh_m"]
    five_full = s5["sh_m"]; five_oos = st["5p OOS"]["sh_m"]
    print(f"\n  random 8-basket monthly-Sharpe (full window):  "
          f"p5 {np.percentile(full_shm,5):.2f}  p50 {np.percentile(full_shm,50):.2f}  "
          f"p95 {np.percentile(full_shm,95):.2f}  mean {full_shm.mean():.2f}")
    print(f"  random 8-basket CAGR% (full window):           "
          f"p5 {np.percentile(full_cagr,5):.0f}  p50 {np.percentile(full_cagr,50):.0f}  "
          f"p95 {np.percentile(full_cagr,95):.0f}")
    print(f"  random 8-basket monthly-Sharpe (OOS):          "
          f"p5 {np.percentile(oos_shm,5):.2f}  p50 {np.percentile(oos_shm,50):.2f}  "
          f"p95 {np.percentile(oos_shm,95):.2f}  mean {oos_shm.mean():.2f}")
    print(f"\n  CHOSEN 8-pair  full monthly-Sharpe {chosen_full:.2f}  -> percentile {pct_rank(full_shm, chosen_full):.0f}")
    print(f"  CHOSEN 8-pair  OOS  monthly-Sharpe {chosen_oos:.2f}  -> percentile {pct_rank(oos_shm, chosen_oos):.0f}")
    print(f"  5-pair (ref)   full monthly-Sharpe {five_full:.2f}  -> percentile {pct_rank(full_shm, five_full):.0f}")
    print(f"  5-pair (ref)   OOS  monthly-Sharpe {five_oos:.2f}  -> percentile {pct_rank(oos_shm, five_oos):.0f}")

    # ---- random-5 null (like-for-like selection test for the 5-pair) ----
    print("\n" + "=" * 88)
    print(f"RANDOM 5-BASKET NULL   {R} draws (equal-weight, size 5) from the same pool")
    print("=" * 88)
    full5, oos5 = [], []
    for _ in range(R):
        pick = list(rng.choice(eligible, size=5, replace=False))
        w = {p: 1 / 5 for p in pick}
        full5.append(curve_stats(build(pair_eq, w, idx, TOTAL_EQUITY))["sh_m"])
        oos5.append(curve_stats(build(pair_eq, w, oos_idx, TOTAL_EQUITY))["sh_m"])
    full5 = np.array(full5); oos5 = np.array(oos5)
    print(f"\n  random 5-basket monthly-Sharpe (full window):  "
          f"p5 {np.percentile(full5,5):.2f}  p50 {np.percentile(full5,50):.2f}  "
          f"p95 {np.percentile(full5,95):.2f}  mean {full5.mean():.2f}")
    print(f"  random 5-basket monthly-Sharpe (OOS):          "
          f"p5 {np.percentile(oos5,5):.2f}  p50 {np.percentile(oos5,50):.2f}  "
          f"p95 {np.percentile(oos5,95):.2f}  mean {oos5.mean():.2f}")
    # Pure selection effect: the chosen 5 NAMES, equal-weight (same form as the null).
    eq5 = {p: 1 / 5 for p in FIVE}
    books = {"chosen-5 eq-wt": eq5, "ORIG5 (inj_heavy)": FIVE, "SOFT5 (INJ25)": SOFT5}
    print()
    for name, w in books.items():
        f_sh = curve_stats(build(pair_eq, w, idx, TOTAL_EQUITY))["sh_m"]
        o_sh = curve_stats(build(pair_eq, w, oos_idx, TOTAL_EQUITY))["sh_m"]
        print(f"  {name:18s} full {f_sh:.2f} -> pct {pct_rank(full5, f_sh):3.0f}   "
              f"OOS {o_sh:.2f} -> pct {pct_rank(oos5, o_sh):3.0f}")
    print("\n  (chosen-5 eq-wt is the LIKE-FOR-LIKE selection test: same size, same "
          "weighting form as the null.\n   High pct on the eq-wt row = the NAME "
          "selection itself carries signal; weighted rows show the full books.)")

    print("\n" + "=" * 88)
    print("VERDICT")
    print("=" * 88)
    print(f"  - Bybit (execution venue): 8p Sharpe(mo) {s8['sh_m']:.2f} vs 5p {s5['sh_m']:.2f}; "
          f"8p worst-mo {s8['worst_mo']:+.1f}% vs 5p {s5['worst_mo']:+.1f}%.")
    print(f"  - OOS holdout: 8p Sharpe(mo) {st['8p OOS']['sh_m']:.2f} vs IS {st['8p IS']['sh_m']:.2f}; "
          f"5p OOS {st['5p OOS']['sh_m']:.2f} vs IS {st['5p IS']['sh_m']:.2f}.")
    print(f"  - Selection test: chosen-8 sits at the {pct_rank(oos_shm, chosen_oos):.0f}th pct of random "
          f"8-baskets OOS. Near ~50 => broad-basket effect (robust); high => selection-driven (overfit).")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
