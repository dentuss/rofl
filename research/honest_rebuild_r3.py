"""Honest rebuild, round 3 — make-or-break validation of the 4h port.

Round 2 found every 4h cell OOS-positive (H4_BASE: untouched production params
on 4h bars, OOS Sh(mo) 1.47 > IS 0.41; H4_TP60 CAGR 15.1%, Sh 0.98, MDD −9.6%)
with fees cut 3-5x vs 1h. Before believing it, three tests — all pre-registered,
all reported in full (no cherry-picking):

  A. UNIVERSE GENERALIZATION — H4_BASE and H4_TP60 on ALL 11 Bybit-perp names
     ever considered (SOFT5 + AVAX NEAR AAVE GRT RUNE DOGE), per-pair + EW11
     portfolio. A real mechanism (less churn -> costs stop eating the edge)
     should lift the cross-section, not just the 5 chosen names.
  B. SUB-WINDOW STABILITY — thirds of the common window, portfolio Sharpe per
     third (from the same equity curves; path-dependent but indicative).
  C. RANDOM-ENTRY NULL — 60 draws: relocate each pair's long/short signal bars
     uniformly among regime+F&G-ALLOWED bars for that direction, same counts,
     same ATR-based SL/TP scheme (1.8/6.0), same engine/costs/cooldown. This
     preserves everything EXCEPT entry timing. If the real H4_TP60 doesn't
     clearly beat this distribution, the "edge" is just directional
     regime-following, not the entry signal.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/honest_rebuild_r3.py
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
from core.indicators import atr as atr_fn
from core.risk import DEFAULT_DECAY_TIERS
from core.data import fetch_ohlcv_bybit
from core.regime_strategy import walk_forward_regimes
from core.sentiment import align_to_bars, fetch_fear_greed
from core.strategies import triple_confirm_bidir
from research.test_reentry_cooldown_prod import apply_funding

SOFT5 = {"INJ-USDT": .25, "SOL-USDT": .1875, "ADA-USDT": .1875,
         "ETH-USDT": .1875, "LINK-USDT": .1875}
UNIVERSE = list(SOFT5) + ["AVAX-USDT", "NEAR-USDT", "AAVE-USDT",
                          "GRT-USDT", "RUNE-USDT", "DOGE-USDT"]
TOTAL = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))
WARMUP_D = 365
BPD = 6                       # 4h bars
BPY = BPD * 365
N_NULL = int(_os.environ.get("N_NULL", 60))
FNG_BARS = 3 * BPD
RNG = np.random.default_rng(42)

CFG = dict(starting_equity=100.0, risk_per_trade=0.020, max_leverage=5.0,
           eq_decay_tiers=DEFAULT_DECAY_TIERS, cooldown_bars=1)


def regime_mask(sig, regs):
    a = regs.reindex(sig.index, method="ffill").fillna("CHOP")
    b = ((sig["signal"] == 1) & (~a.isin(["BULL", "CHOP"]))) | \
        ((sig["signal"] == -1) & (~a.isin(["BEAR", "CHOP"])))
    s = sig.copy()
    s.loc[b, "signal"] = 0
    s.loc[b, ["sl", "tp"]] = np.nan
    return s


def fng_blocks(fa):
    above = (fa >= 80).rolling(FNG_BARS, min_periods=FNG_BARS).sum() == FNG_BARS
    below = (fa <= 20).rolling(FNG_BARS, min_periods=FNG_BARS).sum() == FNG_BARS
    return above.fillna(False), below.fillna(False)


def fng_persist(sig, fa):
    above, below = fng_blocks(fa)
    block = ((sig["signal"] == 1) & above) | ((sig["signal"] == -1) & below)
    s = sig.copy()
    s.loc[block, "signal"] = 0
    s.loc[block, ["sl", "tp"]] = np.nan
    return s


def sharpe_m(eq):
    m = eq.resample("ME").last().pct_change().dropna()
    return float(m.mean() / m.std() * np.sqrt(12)) if len(m) > 2 and m.std() > 0 else 0.0


def stats(eq):
    final = float(eq.iloc[-1])
    mdd = float((eq / eq.cummax() - 1).min())
    days = max((eq.index[-1] - eq.index[0]).days, 1)
    cagr = (final / float(eq.iloc[0])) ** (365 / days) - 1
    mo = eq.resample("ME").last().pct_change().dropna() * 100
    return dict(final=final, cagr=cagr, mdd=mdd, sh_m=sharpe_m(eq),
                worst_mo=float(mo.min()) if len(mo) else 0,
                win_mo=float((mo > 0).mean() * 100) if len(mo) else 0)


def build(pair_eq, w, idx):
    port = None
    for p, wt in w.items():
        e = pair_eq[p].reindex(idx).ffill()
        e = e / e.iloc[0] * wt
        port = e if port is None else port + e
    return port * TOTAL


def null_sig(df, regs, fa, n_long, n_short, sl_mult=1.8, tp_mult=6.0):
    """Random-entry null: same number of long/short SIGNAL bars, relocated
    uniformly among bars the regime+F&G stack ALLOWS for that direction; same
    ATR SL/TP geometry. Destroys entry timing, preserves everything else."""
    a = regs.reindex(df.index, method="ffill").fillna("CHOP")
    above, below = fng_blocks(fa)
    # fa spans the pre-warmup history too — align strictly to df.index or the
    # boolean AND below silently produces union-index positions
    above = above.reindex(df.index).fillna(False)
    below = below.reindex(df.index).fillna(False)
    atr14 = atr_fn(df["high"], df["low"], df["close"], 14)
    ok = atr14.notna()
    allow_l = np.flatnonzero((a.isin(["BULL", "CHOP"]) & ~above & ok).to_numpy())
    allow_s = np.flatnonzero((a.isin(["BEAR", "CHOP"]) & ~below & ok).to_numpy())
    s = pd.DataFrame({"signal": 0, "sl": np.nan, "tp": np.nan}, index=df.index)
    cl = df["close"].to_numpy(); at = atr14.to_numpy()
    for side, pool, n in [(1, allow_l, n_long), (-1, allow_s, n_short)]:
        if len(pool) == 0 or n == 0:
            continue
        pick = RNG.choice(pool, size=min(n, len(pool)), replace=False)
        s.iloc[pick, 0] = side
        s.iloc[pick, 1] = cl[pick] - side * sl_mult * at[pick]
        s.iloc[pick, 2] = cl[pick] + side * tp_mult * at[pick]
    return s


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print(f"HONEST REBUILD r3  4h  universe={len(UNIVERSE)} pairs  N_NULL={N_NULL}",
          flush=True)
    fng = fetch_fear_greed()

    pair_eq = {"BASE": {}, "TP60": {}}
    pair_stats_rows = []
    null_inputs = {}          # pair -> (dfe, regs, fa, n_long, n_short, cut)

    for p in UNIVERSE:
        t0 = time.time()
        df = fetch_ohlcv_bybit(p, "4h", days=DAYS)
        regs = walk_forward_regimes(df, bars_per_day=BPD, train_days=365, step_days=30)
        rc = regs.value_counts().to_dict()
        assert (rc.get("BULL", 0) + rc.get("BEAR", 0)) > 0, f"{p}: all-CHOP"
        fa = align_to_bars(fng, df.index)
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]
        for name, tpm in [("BASE", None), ("TP60", 6.0)]:
            kw = {} if tpm is None else {"tp_mult": tpm}
            sig = fng_persist(regime_mask(triple_confirm_bidir(df, **kw), regs), fa)
            if name == "TP60" and p in SOFT5:
                se = sig[sig.index >= cut]
                null_inputs[p] = (dfe, regs, fa,
                                  int((se["signal"] == 1).sum()),
                                  int((se["signal"] == -1).sum()))
            eq, tr = run_backtest_enhanced(dfe, sig[sig.index >= cut],
                                           EnhancedBTConfig(**CFG))
            eqf = apply_funding(eq, tr, bps=4.0)
            pair_eq[name][p] = eqf
            s = stats(eqf)
            pair_stats_rows.append((p, name, s, len(tr)))
        print(f"  {p:11s} {time.time()-t0:5.0f}s", flush=True)

    # --- A. universe table ----------------------------------------------------
    print("\n" + "=" * 86)
    print("A. UNIVERSE GENERALIZATION (per-pair, per-100 units, full post-warmup window)")
    print("=" * 86)
    print(f"{'pair':11s}{'BASE final':>11s}{'BASE Sh':>9s}{'TP60 final':>12s}"
          f"{'TP60 Sh':>9s}{'TP60 MDD%':>10s}{'trades':>8s}")
    rows = {p: {} for p in UNIVERSE}
    for p, name, s, n in pair_stats_rows:
        rows[p][name] = (s, n)
    pos = 0
    for p in UNIVERSE:
        sb, _ = rows[p]["BASE"]; st, n = rows[p]["TP60"]
        pos += st["final"] > 100
        print(f"{p:11s}{sb['final']:11.1f}{sb['sh_m']:9.2f}{st['final']:12.1f}"
              f"{st['sh_m']:9.2f}{st['mdd']*100:10.1f}{n:8d}")
    print(f"\n  TP60 profitable on {pos}/{len(UNIVERSE)} pairs")

    for w_name, w in [("SOFT5", SOFT5),
                      ("EW11", {p: 1 / len(UNIVERSE) for p in UNIVERSE})]:
        idx = None
        for p in w:
            e = pair_eq["TP60"][p]
            idx = e.index if idx is None else idx.intersection(e.index)
        idx = idx.sort_values()
        yrs = (idx[-1] - idx[0]).days / 365
        print(f"\n  {w_name} portfolios ({yrs:.2f}y common window):")
        for name in ("BASE", "TP60"):
            s = stats(build(pair_eq[name], w, idx))
            print(f"    {name:5s} final ${s['final']:.0f}  CAGR {s['cagr']*100:.1f}%  "
                  f"Sh(mo) {s['sh_m']:.2f}  MDD {s['mdd']*100:.1f}%  "
                  f"worst-mo {s['worst_mo']:.1f}%  pos-mo {s['win_mo']:.0f}%")
        # --- B. sub-window thirds (portfolio) ---------------------------------
        b = [idx[0] + (idx[-1] - idx[0]) * k / 3 for k in range(4)]
        line = f"    TP60 Sh(mo) by thirds: "
        for k in range(3):
            sl_idx = idx[(idx >= b[k]) & (idx < b[k + 1])]
            eq = build(pair_eq["TP60"], w, sl_idx)
            line += f"W{k+1} {sharpe_m(eq):+.2f}  "
        print(line)

    # --- C. random-entry null (SOFT5, TP60 scheme) -----------------------------
    print("\n" + "=" * 86)
    print(f"C. RANDOM-ENTRY NULL  ({N_NULL} draws, SOFT5, same regime/F&G gates, "
          f"same SL/TP geometry, same engine/costs)")
    print("=" * 86)
    idx = None
    for p in SOFT5:
        e = pair_eq["TP60"][p]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    real = stats(build(pair_eq["TP60"], SOFT5, idx))
    finals, sharpes = [], []
    t0 = time.time()
    for d in range(N_NULL):
        draw_eq = {}
        for p in SOFT5:
            dfe, regs, fa, nl, ns = null_inputs[p]
            ns_sig = null_sig(dfe, regs, fa, nl, ns)
            eq, tr = run_backtest_enhanced(dfe, ns_sig, EnhancedBTConfig(**CFG))
            draw_eq[p] = apply_funding(eq, tr, bps=4.0)
        s = stats(build(draw_eq, SOFT5, idx))
        finals.append(s["final"]); sharpes.append(s["sh_m"])
        if (d + 1) % 10 == 0:
            print(f"  {d+1}/{N_NULL} draws ({time.time()-t0:.0f}s)", flush=True)
    finals = np.array(finals); sharpes = np.array(sharpes)
    print(f"\n  real TP60:   final ${real['final']:.0f}  Sh(mo) {real['sh_m']:.2f}")
    print(f"  null final:  p5 ${np.percentile(finals,5):.0f}  median "
          f"${np.median(finals):.0f}  p95 ${np.percentile(finals,95):.0f}")
    print(f"  null Sh(mo): p5 {np.percentile(sharpes,5):.2f}  median "
          f"{np.median(sharpes):.2f}  p95 {np.percentile(sharpes,95):.2f}")
    print(f"  real pct-rank vs null: final {100*(finals < real['final']).mean():.0f}th"
          f"  Sh(mo) {100*(sharpes < real['sh_m']).mean():.0f}th")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
