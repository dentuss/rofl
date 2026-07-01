"""Side-by-side portfolio backtest: 5-pair inj_heavy vs 8-pair equal-weight,
FULL production stack + post-SL re-entry cooldown, $2300 start.

Production stack per pair (matches the cooldown VALIDATION harness exactly):
  triple_confirm_bidir -> walk-forward GMM directional regime mask
  -> F&G 3-day persistence mask -> enhanced engine w/ decay tiers
  -> funding -> post-SL same-side re-entry cooldown (K bars).
NOTE on regime fidelity: this uses the no-look-ahead walk_forward_regimes (the
correct backtest choice); the LIVE bot uses fit_predict_full (full-history in-
sample fit). Both portfolios here use the same walk-forward method, so the 5-vs-8
RELATIVE comparison is method-consistent, but the absolute numbers are validated
against a regime path that differs slightly from the one production applies.
Prices are KuCoin spot (repo-wide backtest convention); live executes on Bybit
perps — SL/TP fills and per-pair listing windows differ somewhat.

Reuses the ALREADY-VALIDATED engine from research/test_reentry_cooldown_prod.py
(run_enhanced_with_cooldown, regime_filtered, fng_persist_3d, apply_funding),
whose K=0 path is asserted identical to core.backtest_enhanced.run_backtest_enhanced.

Each leg starts at weight * TOTAL_EQUITY and compounds independently inside the
window (exactly how the live per-bot containers behave). Portfolio = sum of legs.

Reports, for each portfolio at K=0 (baseline) and K=3 (production):
  final $, total ret, CAGR, Sharpe, MaxDD, worst/best/median/mean month,
  % positive months, trade count.
Two windows:
  (A) each portfolio over its OWN maximum common window (max years each);
  (B) both over the SHARED overlap window (fair apples-to-apples).
Plus month-by-month table (5 vs 8 at K=3) and avg pairwise monthly correlation.

Run from project root (uses the local sklearn venv):
    PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/portfolio_compare.py
    DAYS=2000 TOTAL_EQUITY=2300 ... research/portfolio_compare.py
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

# ---- portfolio definitions -------------------------------------------------
FIVE = {"INJ-USDT": .40, "SOL-USDT": .20, "ADA-USDT": .15,
        "ETH-USDT": .15, "LINK-USDT": .10}                       # inj_heavy
EIGHT = {p: 0.125 for p in ["INJ-USDT", "AVAX-USDT", "NEAR-USDT", "AAVE-USDT",
                            "GRT-USDT", "RUNE-USDT", "DOGE-USDT", "ADA-USDT"]}  # equal
ALL_PAIRS = sorted(set(FIVE) | set(EIGHT))

TOTAL_EQUITY = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))       # request max; each pair caps at listing
WARMUP_D = 365                                    # regime warmup, dropped per pair
KS = [0, 3]                                        # baseline vs production cooldown
BPY = 24 * 365


def curve_stats(eq):
    final = float(eq.iloc[-1]); ret = final / float(eq.iloc[0]) - 1
    mdd = float((eq / eq.cummax() - 1).min())
    br = eq.pct_change().fillna(0)
    sharpe = float(br.mean() / br.std() * np.sqrt(BPY)) if br.std() > 0 else 0
    days = max((eq.index[-1] - eq.index[0]).days, 1)
    cagr = (final / float(eq.iloc[0])) ** (365 / days) - 1
    mo = eq.resample("ME").last().pct_change().dropna() * 100
    return dict(final=final, ret=ret, cagr=cagr, mdd=mdd, sharpe=sharpe, years=days / 365,
                worst_mo=float(mo.min()) if len(mo) else 0,
                best_mo=float(mo.max()) if len(mo) else 0,
                med_mo=float(mo.median()) if len(mo) else 0,
                mean_mo=float(mo.mean()) if len(mo) else 0,
                win_mo=float((mo > 0).mean() * 100) if len(mo) else 0)


def build_portfolio(pair_eq, weights, K, idx, total):
    """Weighted portfolio: each leg normalized to 1.0 at idx start, scaled by
    its weight, summed, scaled to `total` starting capital."""
    port = None
    for p, w in weights.items():
        e = pair_eq[K][p].reindex(idx).ffill()
        e = e / e.iloc[0] * w
        port = e if port is None else port + e
    return port * total


def portfolio_trades(pair_tr, weights, K, idx):
    n = 0
    for p in weights:
        n += sum(1 for t in pair_tr[K][p] if idx[0] <= t.exit_time <= idx[-1])
    return n


def avg_pairwise_corr(pair_eq, weights, K, idx):
    cols = {}
    for p in weights:
        e = pair_eq[K][p].reindex(idx).ffill()
        cols[p] = e.resample("ME").last().pct_change().dropna()
    m = pd.DataFrame(cols).dropna()
    if m.shape[1] < 2 or len(m) < 3:
        return float("nan")
    c = m.corr().values
    iu = np.triu_indices_from(c, k=1)
    return float(np.nanmean(c[iu]))


def print_block(title, stats5, stats8, tr5, tr8, corr5, corr8):
    print("\n" + "=" * 92)
    print(title)
    print("=" * 92)
    hdr = f"{'metric':18s}{'5p K=0':>12s}{'5p K=3':>12s}{'8p K=0':>12s}{'8p K=3':>12s}"
    print(hdr)
    print("-" * 92)

    def row(label, key, scale=1.0, fmt="{:>12.1f}"):
        vals = [stats5[0][key] * scale, stats5[3][key] * scale,
                stats8[0][key] * scale, stats8[3][key] * scale]
        print(f"{label:18s}" + "".join(fmt.format(v) for v in vals))

    print(f"{'window years':18s}{stats5[0]['years']:>12.2f}{stats5[3]['years']:>12.2f}"
          f"{stats8[0]['years']:>12.2f}{stats8[3]['years']:>12.2f}")
    row("final $", "final", 1.0, "{:>12.0f}")
    row("total ret %", "ret", 100, "{:>12.0f}")
    row("CAGR %", "cagr", 100, "{:>12.1f}")
    row("Sharpe", "sharpe", 1.0, "{:>12.2f}")
    row("MaxDD %", "mdd", 100, "{:>12.1f}")
    row("worst month %", "worst_mo", 1.0, "{:>12.1f}")
    row("best month %", "best_mo", 1.0, "{:>12.1f}")
    row("median month %", "med_mo", 1.0, "{:>12.2f}")
    row("mean month %", "mean_mo", 1.0, "{:>12.2f}")
    row("% pos months", "win_mo", 1.0, "{:>12.0f}")
    print(f"{'# trades':18s}{tr5[0]:>12d}{tr5[3]:>12d}{tr8[0]:>12d}{tr8[3]:>12d}")
    print(f"{'avg pair corr (mo)':18s}{'':>12s}{corr5:>12.2f}{'':>12s}{corr8:>12.2f}")


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing — regime would be an all-CHOP no-op")
    cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                           max_leverage=5.0, eq_decay_tiers=DEFAULT_DECAY_TIERS)
    print(f"TOTAL_EQUITY={TOTAL_EQUITY:.0f}  DAYS={DAYS}  warmup_drop={WARMUP_D}d  KS={KS}")
    print(f"pairs: {ALL_PAIRS}\n", flush=True)
    fng = fetch_fear_greed()

    pair_eq = {K: {} for K in KS}
    pair_tr = {K: {} for K in KS}
    windows = {}
    for p in ALL_PAIRS:
        t0 = time.time()
        df = fetch_ohlcv(p, "1h", days=DAYS)
        regs = walk_forward_regimes(df, bars_per_day=24, train_days=365, step_days=30)
        rc = regs.value_counts().to_dict()
        assert (rc.get("BULL", 0) + rc.get("BEAR", 0)) > 0, \
            f"{p}: regime all-CHOP {rc} — sklearn broken"
        fa = align_to_bars(fng, df.index)
        sig = fng_persist_3d(regime_filtered(df, regs), fa)
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]; sige = sig[sig.index >= cut]
        for K in KS:
            eq, tr = run_enhanced_with_cooldown(dfe, sige, cfg, K)
            eq = apply_funding(eq, tr)
            pair_eq[K][p] = eq; pair_tr[K][p] = tr
        windows[p] = (dfe.index[0], dfe.index[-1])
        yrs = (dfe.index[-1] - dfe.index[0]).days / 365
        print(f"  {p:11s} {time.time()-t0:5.0f}s  {yrs:4.2f}y  "
              f"{dfe.index[0].date()}..{dfe.index[-1].date()}  "
              f"({len(dfe)} bars, BULL={rc.get('BULL',0)} BEAR={rc.get('BEAR',0)} "
              f"CHOP={rc.get('CHOP',0)})", flush=True)

    def common_of(weights, extra=None):
        idx = None
        legs = list(weights) + (list(extra) if extra else [])
        for p in legs:
            e = pair_eq[0][p]
            idx = e.index if idx is None else idx.intersection(e.index)
        return idx

    idx5 = common_of(FIVE)
    idx8 = common_of(EIGHT)
    idx_shared = common_of(FIVE, EIGHT)   # overlap of ALL pairs used in either

    # ---- (A) each on its own max window ----
    s5 = {K: curve_stats(build_portfolio(pair_eq, FIVE, K, idx5, TOTAL_EQUITY)) for K in KS}
    s8 = {K: curve_stats(build_portfolio(pair_eq, EIGHT, K, idx8, TOTAL_EQUITY)) for K in KS}
    tr5 = {K: portfolio_trades(pair_tr, FIVE, K, idx5) for K in KS}
    tr8 = {K: portfolio_trades(pair_tr, EIGHT, K, idx8) for K in KS}
    corr5 = avg_pairwise_corr(pair_eq, FIVE, 3, idx5)
    corr8 = avg_pairwise_corr(pair_eq, EIGHT, 3, idx8)
    print_block(
        f"(A) OWN MAX WINDOW   5p: {idx5[0].date()}..{idx5[-1].date()} "
        f"({(idx5[-1]-idx5[0]).days/365:.1f}y)   "
        f"8p: {idx8[0].date()}..{idx8[-1].date()} ({(idx8[-1]-idx8[0]).days/365:.1f}y)",
        s5, s8, tr5, tr8, corr5, corr8)

    # ---- (B) shared overlap window (fair) ----
    s5b = {K: curve_stats(build_portfolio(pair_eq, FIVE, K, idx_shared, TOTAL_EQUITY)) for K in KS}
    s8b = {K: curve_stats(build_portfolio(pair_eq, EIGHT, K, idx_shared, TOTAL_EQUITY)) for K in KS}
    tr5b = {K: portfolio_trades(pair_tr, FIVE, K, idx_shared) for K in KS}
    tr8b = {K: portfolio_trades(pair_tr, EIGHT, K, idx_shared) for K in KS}
    corr5b = avg_pairwise_corr(pair_eq, FIVE, 3, idx_shared)
    corr8b = avg_pairwise_corr(pair_eq, EIGHT, 3, idx_shared)
    print_block(
        f"(B) SHARED WINDOW (fair)   {idx_shared[0].date()}..{idx_shared[-1].date()} "
        f"({(idx_shared[-1]-idx_shared[0]).days/365:.1f}y)  — both portfolios, same bars",
        s5b, s8b, tr5b, tr8b, corr5b, corr8b)

    # ---- month-by-month, production K=3, shared window ----
    p5 = build_portfolio(pair_eq, FIVE, 3, idx_shared, TOTAL_EQUITY)
    p8 = build_portfolio(pair_eq, EIGHT, 3, idx_shared, TOTAL_EQUITY)
    m5 = p5.resample("ME").last().pct_change().dropna() * 100
    m8 = p8.resample("ME").last().pct_change().dropna() * 100
    mt = pd.DataFrame({"5p": m5, "8p": m8}).dropna(); mt["d"] = mt["8p"] - mt["5p"]
    print("\n" + "=" * 92)
    print(f"MONTH-BY-MONTH RETURN %  (production K=3, shared window)   5p | 8p | 8p-5p")
    print("=" * 92)
    for ts, r in mt.iterrows():
        flag = "  <-- 8p worse" if r["d"] < 0 else ""
        print(f"  {ts:%Y-%m}   {r['5p']:>+8.1f}   {r['8p']:>+8.1f}   {r['d']:>+8.1f}{flag}")
    print(f"\n  {len(mt)} months;  8p beat 5p in {int((mt['d']>0).sum())}/{len(mt)};  "
          f"8p worst {mt['8p'].min():+.1f}% vs 5p worst {mt['5p'].min():+.1f}%;  "
          f"8p median {mt['8p'].median():+.2f}% vs 5p median {mt['5p'].median():+.2f}%")

    # ---- final $ at 2300 start, production K=3 ----
    print("\n" + "=" * 92)
    print(f"BOTTOM LINE at ${TOTAL_EQUITY:.0f} start, production cooldown K=3")
    print("=" * 92)
    print(f"  5-pair (own {s5[3]['years']:.1f}y): ${s5[3]['final']:.0f}  "
          f"CAGR {s5[3]['cagr']*100:+.0f}%  Sharpe {s5[3]['sharpe']:.2f}  "
          f"MDD {s5[3]['mdd']*100:.0f}%  worst-mo {s5[3]['worst_mo']:+.1f}%")
    print(f"  8-pair (own {s8[3]['years']:.1f}y): ${s8[3]['final']:.0f}  "
          f"CAGR {s8[3]['cagr']*100:+.0f}%  Sharpe {s8[3]['sharpe']:.2f}  "
          f"MDD {s8[3]['mdd']*100:.0f}%  worst-mo {s8[3]['worst_mo']:+.1f}%")
    print(f"  [fair {s5b[3]['years']:.1f}y] 5p ${s5b[3]['final']:.0f} (Sh {s5b[3]['sharpe']:.2f}) "
          f"vs 8p ${s8b[3]['final']:.0f} (Sh {s8b[3]['sharpe']:.2f})")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
