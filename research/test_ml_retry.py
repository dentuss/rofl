"""Retry of Exp 7 (ML entry filter) with:
  - regime label as a categorical feature
  - probability-threshold sweep instead of fixed p>=0.50
  - separate models for long-side and short-side signals
  - report ROC-AUC + per-threshold backtest stats

Compares each variant to the realistic baseline (bidir + dir-regime + funding).

Run from project root:
    python3 research/test_ml_retry.py
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
from core.ml_filter import build_features as ml_features
from core.ml_filter import label_signals, walk_forward_predict
from core.regime_strategy import walk_forward_regimes
from core.sentiment import fetch_fear_greed
from core.strategies import triple_confirm_bidir


def stats(eq, trades, bpd=24):
    n = len(trades); final = float(eq.iloc[-1])
    ret = float(final / eq.iloc[0] - 1)
    mdd = float((eq / eq.cummax() - 1).min())
    if n:
        pnls = np.array([t.pnl for t in trades])
        wins = pnls[pnls > 0]; losses = pnls[pnls <= 0]
        pf = float(wins.sum() / -losses.sum()) if losses.sum() < 0 else 99.0
        wr = float(len(wins) / n)
    else:
        pf = 0; wr = 0
    bar_ret = eq.pct_change().fillna(0)
    sharpe = float(bar_ret.mean() / bar_ret.std() * np.sqrt(bpd * 365)) \
             if bar_ret.std() > 0 else 0
    longs = [t for t in trades if t.side == 1]
    shorts = [t for t in trades if t.side == -1]
    return dict(final=final, ret=ret, mdd=mdd, sharpe=sharpe, pf=pf, wr=wr,
                n=n, n_long=len(longs), n_short=len(shorts))


def apply_funding(eq, trades, bps=1.0):
    if not trades: return eq, trades
    per_bar = (bps / 1e4) / 8
    new_trades = []; deltas = []
    for t in trades:
        cost = per_bar * t.notional * t.bars_held * t.side
        nt = Trade(side=t.side, entry_time=t.entry_time, exit_time=t.exit_time,
                   entry_px=t.entry_px, exit_px=t.exit_px, qty=t.qty,
                   notional=t.notional, sl=t.sl, tp=t.tp,
                   pnl=t.pnl - cost, fees=t.fees + max(cost,0), reason=t.reason,
                   bars_held=t.bars_held)
        new_trades.append(nt); deltas.append((nt.exit_time, nt.pnl - t.pnl))
    eq2 = eq.copy(); off = pd.Series(0.0, index=eq2.index)
    for ts, d in deltas: off.loc[eq2.index >= ts] += d
    return eq2 + off, new_trades


def make_bidir_dir_regime(df, regimes):
    sig = triple_confirm_bidir(df)
    aligned = regimes.reindex(sig.index, method="ffill").fillna("CHOP")
    block = ((sig["signal"] == 1) & (~aligned.isin(["BULL", "CHOP"]))) | \
            ((sig["signal"] == -1) & (~aligned.isin(["BEAR", "CHOP"])))
    sig.loc[block, "signal"] = 0
    sig.loc[block, ["sl", "tp"]] = np.nan
    return sig


def build_features_with_regime(df, regimes, fng):
    """ml_features + regime one-hot columns."""
    feats = ml_features(df, fng=fng)
    aligned = regimes.reindex(feats.index, method="ffill").fillna("CHOP")
    feats["regime_bull"] = (aligned == "BULL").astype(float)
    feats["regime_bear"] = (aligned == "BEAR").astype(float)
    # regime persistence: how many consecutive bars in current regime
    same_as_prev = (aligned != aligned.shift()).cumsum()
    feats["regime_age"] = aligned.groupby(same_as_prev).cumcount().astype(float)
    return feats


def per_side_walk_forward(features, labels, signals,
                          train_bars=24*365, step_bars=24*90, min_proba=0.50):
    """Train SEPARATE models for long-side (sig==1) and short-side (sig==-1) entries.

    Returns a Series of per-bar predicted P(win), with side-specific models.
    """
    from sklearn.ensemble import GradientBoostingClassifier
    factory = lambda: GradientBoostingClassifier(
        n_estimators=120, max_depth=3, learning_rate=0.05,
        subsample=0.85, random_state=42)
    preds = pd.Series(np.nan, index=features.index, dtype="float64")
    feat = features.replace([np.inf, -np.inf], np.nan).fillna(0)
    n = len(feat)
    start = train_bars
    while start < n:
        end = min(start + step_bars, n)
        train_idx = feat.index[max(0, start - train_bars):start]
        test_idx = feat.index[start:end]
        for side in (1, -1):
            # Train on rows that are labeled AND were signals of THIS side
            train_mask = labels.loc[train_idx].notna() & (signals.loc[train_idx, "signal"] == side)
            test_mask = signals.loc[test_idx, "signal"] == side
            if train_mask.sum() < 30 or not test_mask.any():
                continue
            Xtr = feat.loc[train_idx][train_mask].values
            ytr = labels.loc[train_idx][train_mask].astype(int).values
            if len(set(ytr)) < 2:
                continue
            m = factory(); m.fit(Xtr, ytr)
            Xte = feat.loc[test_idx][test_mask].values
            proba = m.predict_proba(Xte)[:, 1]
            preds.loc[feat.loc[test_idx][test_mask].index] = proba
        start = end
    return preds


def main():
    days = int(_os.environ.get("DAYS", 5 * 365))
    print(f"\n[1] Fetch INJ-USDT 1h, {days}d ...")
    df = fetch_ohlcv("INJ-USDT", "1h", days=days)
    print(f"    {len(df)} bars")

    print("[2] Walk-forward regimes ...")
    t0 = time.time()
    regimes = walk_forward_regimes(df, bars_per_day=24, train_days=365, step_days=30)
    print(f"    {time.time()-t0:.0f}s")

    print("[3] F&G fetch ...")
    try:
        fng = fetch_fear_greed()
    except Exception as e:
        print(f"    fail: {e}"); fng = None

    sig = make_bidir_dir_regime(df, regimes)

    cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                           max_leverage=5.0, eq_risk_decay=0.5,
                           drawdown_for_decay=0.20)

    # Realistic baseline
    eq_b, tr_b = run_backtest_enhanced(df, sig, cfg, long_only=False)
    eq_b, tr_b = apply_funding(eq_b, tr_b)
    base = stats(eq_b, tr_b)
    print(f"\n[4] Baseline:  final ${base['final']:.0f}  ret +{base['ret']*100:.0f}%  "
          f"mdd {base['mdd']*100:.1f}%  sharpe {base['sharpe']:.2f}  trades {base['n']}")

    # Build features WITH regime
    print("\n[5] Build features (incl regime label) ...")
    feats = build_features_with_regime(df, regimes, fng)
    labels = label_signals(df, sig, max_horizon_bars=96)
    n_lab = int(labels.notna().sum())
    wr = float(labels.dropna().mean())
    print(f"    n_labeled={n_lab}  base_win_rate={wr:.2%}  feature_count={feats.shape[1]}")

    # === Variant A: single model, threshold sweep ===
    print("\n[6] Variant A — single model (long+short combined), threshold sweep")
    t0 = time.time()
    proba_single = walk_forward_predict(feats, labels,
                                        train_bars=24 * 365, step_bars=24 * 90)
    print(f"    walk-forward predict: {time.time()-t0:.0f}s")
    n_scored = int(proba_single.notna().sum())
    proba_with_sig = proba_single[sig["signal"] != 0]
    print(f"    n_scored_signals={int(proba_with_sig.notna().sum())}  "
          f"proba_median={proba_with_sig.median():.3f}  "
          f"proba_q25={proba_with_sig.quantile(.25):.3f}  "
          f"proba_q75={proba_with_sig.quantile(.75):.3f}")

    results = [("baseline (no ML)", base)]
    for thresh in [0.35, 0.40, 0.45, 0.50, 0.55]:
        sf = sig.copy()
        skip = (sf["signal"] != 0) & (proba_single.fillna(0) < thresh)
        sf.loc[skip, "signal"] = 0
        sf.loc[skip, ["sl", "tp"]] = np.nan
        eq, tr = run_backtest_enhanced(df, sf, cfg, long_only=False)
        eq, tr = apply_funding(eq, tr)
        s = stats(eq, tr)
        results.append((f"A single model p>={thresh:.2f}", s))
        print(f"    p>={thresh:.2f}:  final ${s['final']:.0f}  ret {s['ret']*100:+.0f}%  "
              f"mdd {s['mdd']*100:.1f}%  sharpe {s['sharpe']:.2f}  trades {s['n']}  "
              f"({s['n_long']} long / {s['n_short']} short)")

    # === Variant B: per-side models ===
    print("\n[7] Variant B — per-side models (separate long & short classifiers)")
    t0 = time.time()
    proba_side = per_side_walk_forward(feats, labels, sig,
                                       train_bars=24 * 365, step_bars=24 * 90)
    print(f"    walk-forward predict: {time.time()-t0:.0f}s")
    n_long_scored = int((proba_side.notna() & (sig["signal"] == 1)).sum())
    n_short_scored = int((proba_side.notna() & (sig["signal"] == -1)).sum())
    proba_l = proba_side[sig["signal"] == 1]
    proba_s = proba_side[sig["signal"] == -1]
    print(f"    long scored: {n_long_scored}  median_proba {proba_l.median():.3f}")
    print(f"    short scored: {n_short_scored}  median_proba {proba_s.median():.3f}")

    for thresh in [0.35, 0.40, 0.45, 0.50, 0.55]:
        sf = sig.copy()
        skip = (sf["signal"] != 0) & (proba_side.fillna(0) < thresh)
        sf.loc[skip, "signal"] = 0
        sf.loc[skip, ["sl", "tp"]] = np.nan
        eq, tr = run_backtest_enhanced(df, sf, cfg, long_only=False)
        eq, tr = apply_funding(eq, tr)
        s = stats(eq, tr)
        results.append((f"B per-side p>={thresh:.2f}", s))
        print(f"    p>={thresh:.2f}:  final ${s['final']:.0f}  ret {s['ret']*100:+.0f}%  "
              f"mdd {s['mdd']*100:.1f}%  sharpe {s['sharpe']:.2f}  trades {s['n']}  "
              f"({s['n_long']} long / {s['n_short']} short)")

    # === Variant C: per-side, side-asymmetric thresholds ===
    # Hypothesis: long-side and short-side signals may have very different
    # optimal cutoffs. Try a few asymmetric combos.
    print("\n[8] Variant C — per-side, asymmetric thresholds")
    for tl, ts in [(0.40, 0.50), (0.40, 0.55), (0.45, 0.55), (0.35, 0.50)]:
        sf = sig.copy()
        skip = ((sf["signal"] ==  1) & (proba_side.fillna(0) < tl)) | \
               ((sf["signal"] == -1) & (proba_side.fillna(0) < ts))
        sf.loc[skip, "signal"] = 0
        sf.loc[skip, ["sl", "tp"]] = np.nan
        eq, tr = run_backtest_enhanced(df, sf, cfg, long_only=False)
        eq, tr = apply_funding(eq, tr)
        s = stats(eq, tr)
        results.append((f"C asym L>={tl:.2f}/S>={ts:.2f}", s))
        print(f"    L>={tl:.2f}/S>={ts:.2f}:  final ${s['final']:.0f}  "
              f"ret {s['ret']*100:+.0f}%  mdd {s['mdd']*100:.1f}%  "
              f"sharpe {s['sharpe']:.2f}  trades {s['n']}  "
              f"({s['n_long']} long / {s['n_short']} short)")

    # === Summary ===
    print("\n" + "=" * 95)
    print("ML FILTER RETRY — SUMMARY  (vs realistic baseline)")
    print("=" * 95)
    rows = []
    for name, s in results:
        d_sharpe = s["sharpe"] - base["sharpe"]
        d_mdd = s["mdd"] - base["mdd"]
        d_ret = (s["ret"] - base["ret"]) * 100
        v = "FAILED"
        if name.startswith("baseline"):
            v = "(reference)"
        elif d_sharpe >= 0.10 and s["ret"] >= 0.8 * base["ret"]:
            v = "WORKED (+sharpe)"
        elif d_mdd >= 0.03 and s["ret"] >= 0.8 * base["ret"]:
            v = "WORKED (-mdd)"
        elif d_ret >= 100 and d_sharpe >= 0:
            v = "WORKED (+ret)"
        elif abs(d_sharpe) < 0.05 and abs(d_mdd) < 0.02:
            v = "NEUTRAL"
        rows.append({
            "variant": name,
            "final$": f"{s['final']:>7.0f}",
            "ret%": f"{s['ret']*100:>+5.0f}",
            "mdd%": f"{s['mdd']*100:>+5.1f}",
            "sharpe": f"{s['sharpe']:>4.2f}",
            "trades": s["n"],
            "n_long": s["n_long"],
            "n_short": s["n_short"],
            "Δsharpe": f"{d_sharpe:+.2f}",
            "Δmdd": f"{d_mdd*100:+.1f}",
            "verdict": v,
        })
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 30)
    print(pd.DataFrame(rows).to_string(index=False))
    print()


if __name__ == "__main__":
    main()
