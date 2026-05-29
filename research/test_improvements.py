"""Master experiment script: test the 10 improvement ideas against
the current best preset (adaptive_inj_bidir) on 5y INJ-USDT 1h data.

Run from project root:
    python3 research/test_improvements.py            # full run, ~15-20 min
    SKIP="5,7" python3 research/test_improvements.py # skip slow ones
    DAYS=400 python3 research/test_improvements.py   # quick smoke
"""
from __future__ import annotations

import sys as _sys, os as _os
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

import time
from copy import deepcopy

import numpy as np
import pandas as pd

from core.backtest import Trade
from core.backtest_enhanced import EnhancedBTConfig, run_backtest_enhanced
from core.data import fetch_ohlcv
from core.regime import build_features, feature_matrix, fit_gmm, predict_regimes
from core.regime_strategy import walk_forward_regimes
from core.sentiment import align_to_bars, fetch_fear_greed
from core.strategies import triple_confirm_bidir


# ----- utilities -----------------------------------------------------------

def stats(eq, trades, bpd=24):
    n = len(trades)
    final = float(eq.iloc[-1])
    ret = float(final / eq.iloc[0] - 1)
    mdd = float((eq / eq.cummax() - 1).min())
    if n:
        pnls = np.array([t.pnl for t in trades])
        wins = pnls[pnls > 0]
        losses = pnls[pnls <= 0]
        pf = float(wins.sum() / -losses.sum()) if losses.sum() < 0 else 99.0
        wr = float(len(wins) / n)
    else:
        pf = 0.0; wr = 0.0
    bar_ret = eq.pct_change().fillna(0)
    sharpe = float(bar_ret.mean() / bar_ret.std() * np.sqrt(bpd * 365)) \
             if bar_ret.std() > 0 else 0.0
    longs = [t for t in trades if t.side == 1]
    shorts = [t for t in trades if t.side == -1]
    return dict(final=final, ret=ret, mdd=mdd, sharpe=sharpe, pf=pf, wr=wr,
                n=n, n_long=len(longs), n_short=len(shorts),
                long_pnl=float(sum(t.pnl for t in longs)),
                short_pnl=float(sum(t.pnl for t in shorts)))


def verdict(b, e):
    d_sharpe = e["sharpe"] - b["sharpe"]
    d_mdd = e["mdd"] - b["mdd"]
    d_ret_pct = (e["ret"] - b["ret"]) * 100
    keep_ret = e["ret"] >= 0.8 * b["ret"]
    if d_sharpe >= 0.10 and keep_ret:
        return "WORKED (+sharpe)"
    if d_mdd >= 0.03 and keep_ret:
        return "WORKED (-mdd)"
    if d_ret_pct >= 50 and d_sharpe >= 0:
        return "WORKED (+ret)"
    if abs(d_sharpe) < 0.05 and abs(d_mdd) < 0.02:
        return "NEUTRAL"
    return "FAILED"


def make_bidir_dir_regime(df, regimes):
    sig = triple_confirm_bidir(df)
    aligned = regimes.reindex(sig.index, method="ffill").fillna("CHOP")
    block = ((sig["signal"] ==  1) & (~aligned.isin(["BULL", "CHOP"]))) | \
            ((sig["signal"] == -1) & (~aligned.isin(["BEAR", "CHOP"])))
    sig.loc[block, "signal"] = 0
    sig.loc[block, ["sl", "tp"]] = np.nan
    return sig


def apply_funding(eq, trades, funding_bps_per_8h=1.0, bar_minutes=60):
    """Deduct/credit per-bar funding cost from trade pnls and equity curve.

    Bybit USDT-perp funding: long pays ~+1bp per 8h on average; short receives.
    """
    if funding_bps_per_8h == 0 or not trades:
        return eq, trades, 0.0
    per_bar = (funding_bps_per_8h / 1e4) * (bar_minutes / (8 * 60))
    new_trades = []
    deltas = []
    funding_total = 0.0
    for t in trades:
        cost = per_bar * t.notional * t.bars_held * t.side
        funding_total += cost
        nt = Trade(side=t.side, entry_time=t.entry_time, exit_time=t.exit_time,
                   entry_px=t.entry_px, exit_px=t.exit_px, qty=t.qty,
                   notional=t.notional, sl=t.sl, tp=t.tp,
                   pnl=t.pnl - cost, fees=t.fees + max(cost, 0),
                   reason=t.reason, bars_held=t.bars_held)
        new_trades.append(nt)
        deltas.append((nt.exit_time, nt.pnl - t.pnl))
    eq2 = eq.copy()
    offsets = pd.Series(0.0, index=eq2.index)
    for ts, d in deltas:
        offsets.loc[eq2.index >= ts] += d
    return eq2 + offsets, new_trades, funding_total


# ----- experiments ---------------------------------------------------------

def exp_baseline(df, regimes, cfg, funding_bps=0.0):
    sig = make_bidir_dir_regime(df, regimes)
    eq, trades = run_backtest_enhanced(df, sig, cfg, long_only=False)
    eq, trades, ft = apply_funding(eq, trades, funding_bps_per_8h=funding_bps)
    s = stats(eq, trades)
    s["funding_total"] = ft
    return s


def exp1_per_regime_risk(df, regimes, cfg, chop_mult=0.5):
    """Single equity pool. Risk-mult on entry: 1.0 in (long+BULL) or (short+BEAR),
    chop_mult in CHOP (default 0.5), 0.0 in opposing-regime (effectively blocked).
    """
    sig = triple_confirm_bidir(df)
    aligned = regimes.reindex(sig.index, method="ffill").fillna("CHOP")
    # risk_mult per bar where signal != 0
    rmult = pd.Series(0.0, index=sig.index)
    rmult[((sig["signal"] ==  1) & (aligned == "BULL")) |
          ((sig["signal"] == -1) & (aligned == "BEAR"))] = 1.0
    rmult[((sig["signal"] != 0) & (aligned == "CHOP"))] = chop_mult
    sig2 = sig.copy()
    sig2["risk_mult"] = rmult
    # The backtester's allow_short stays True; rmult=0 disables opposing-regime
    eq, trades = run_backtest_enhanced(df, sig2, cfg, long_only=False)
    eq, trades, _ = apply_funding(eq, trades, 1.0)
    return stats(eq, trades)


def exp3_partial_tp_breakeven(df, regimes, cfg):
    sig = make_bidir_dir_regime(df, regimes)
    cfg2 = deepcopy(cfg); cfg2.partial_tp_atr = 1.5; cfg2.partial_to_breakeven = True
    eq, trades = run_backtest_enhanced(df, sig, cfg2, long_only=False)
    eq, trades, _ = apply_funding(eq, trades, 1.0)
    return stats(eq, trades)


def exp4_multipair_portfolio(_df, _regimes, cfg, days=5 * 365):
    pairs = ["ETH-USDT", "BTC-USDT", "SOL-USDT", "INJ-USDT"]
    eqs = []; total_trades = []; per_pair = {}
    for p in pairs:
        print(f"      fetching {p} …", flush=True)
        d = fetch_ohlcv(p, "1h", days=days)
        regs = walk_forward_regimes(d, bars_per_day=24, train_days=365, step_days=30)
        sig = make_bidir_dir_regime(d, regs)
        cfg_p = deepcopy(cfg); cfg_p.starting_equity = cfg.starting_equity / len(pairs)
        eq, trades = run_backtest_enhanced(d, sig, cfg_p, long_only=False)
        eq, trades, _ = apply_funding(eq, trades, 1.0)
        per_pair[p] = stats(eq, trades)
        eqs.append(eq); total_trades.extend(trades)
    for p, s in per_pair.items():
        print(f"        {p}: ${s['final']:.0f}  ret {s['ret']*100:+.0f}%  "
              f"mdd {s['mdd']*100:+.1f}%  trades {s['n']}")
    common_idx = eqs[0].index
    for e in eqs[1:]:
        common_idx = common_idx.intersection(e.index)
    portfolio_eq = sum(e.reindex(common_idx).ffill() for e in eqs)
    return stats(portfolio_eq, total_trades)


def exp5_walkforward_params(df, regimes, cfg):
    grid_ef = [7, 9, 12]
    grid_es = [21, 26, 34]
    grid_rsi = [50, 55, 60]
    sig_combined = pd.DataFrame(index=df.index, columns=["signal", "sl", "tp"],
                                dtype="float64")
    sig_combined["signal"] = 0
    sig_combined[["sl", "tp"]] = np.nan
    years = sorted({d.year for d in df.index})
    chosen = []
    for yi, y in enumerate(years):
        if yi == 0: continue
        prev_year_idx = df.index[df.index.year == years[yi - 1]]
        if len(prev_year_idx) == 0: continue
        train_df = df.loc[prev_year_idx[0]:prev_year_idx[-1]]
        best = None
        for ef in grid_ef:
            for es in grid_es:
                if ef >= es: continue
                for rm in grid_rsi:
                    s = triple_confirm_bidir(train_df, ema_fast=ef, ema_slow=es, rsi_min=rm)
                    eq_t, tr_t = run_backtest_enhanced(train_df, s, cfg, long_only=False)
                    st = stats(eq_t, tr_t)
                    if best is None or st["sharpe"] > best[0]:
                        best = (st["sharpe"], ef, es, rm)
        _, ef, es, rm = best
        chosen.append((y, ef, es, rm))
        s_full = triple_confirm_bidir(df, ema_fast=ef, ema_slow=es, rsi_min=rm)
        aligned = regimes.reindex(s_full.index, method="ffill").fillna("CHOP")
        block = ((s_full["signal"] ==  1) & (~aligned.isin(["BULL", "CHOP"]))) | \
                ((s_full["signal"] == -1) & (~aligned.isin(["BEAR", "CHOP"])))
        s_full.loc[block, "signal"] = 0
        s_full.loc[block, ["sl", "tp"]] = np.nan
        test_idx = df.index[df.index.year == y]
        sig_combined.loc[test_idx] = s_full.loc[test_idx].values
    print(f"      annual params chosen: {chosen}")
    eq, trades = run_backtest_enhanced(df, sig_combined, cfg, long_only=False)
    eq, trades, _ = apply_funding(eq, trades, 1.0)
    return stats(eq, trades)


def exp6_fresh_crossover(df, regimes, cfg, lookback=5):
    sig = triple_confirm_bidir(df)
    prev_zero = sig["signal"].shift(lookback).fillna(0) == 0
    fresh = (sig["signal"] != 0) & prev_zero
    sig = sig.copy()
    stale = (sig["signal"] != 0) & (~fresh)
    sig.loc[stale, "signal"] = 0
    sig.loc[stale, ["sl", "tp"]] = np.nan
    aligned = regimes.reindex(sig.index, method="ffill").fillna("CHOP")
    block = ((sig["signal"] ==  1) & (~aligned.isin(["BULL", "CHOP"]))) | \
            ((sig["signal"] == -1) & (~aligned.isin(["BEAR", "CHOP"])))
    sig.loc[block, "signal"] = 0
    sig.loc[block, ["sl", "tp"]] = np.nan
    eq, trades = run_backtest_enhanced(df, sig, cfg, long_only=False)
    eq, trades, _ = apply_funding(eq, trades, 1.0)
    return stats(eq, trades)


def exp7_ml_filter(df, regimes, cfg, min_proba=0.50):
    from core.ml_filter import build_features as ml_features
    from core.ml_filter import label_signals, walk_forward_predict
    try:
        fng = fetch_fear_greed()
    except Exception:
        fng = None
    sig = make_bidir_dir_regime(df, regimes)
    feats = ml_features(df, fng=fng)
    labels = label_signals(df, sig, max_horizon_bars=96)
    n_lab = int(labels.notna().sum())
    print(f"      n_labeled={n_lab}  win_rate={labels.dropna().mean():.2%}", flush=True)
    proba = walk_forward_predict(feats, labels, train_bars=24 * 365, step_bars=24 * 90)
    n_scored = int(proba.notna().sum())
    print(f"      n_scored={n_scored}  proba_median={proba.median():.3f}")
    sig_f = sig.copy()
    skip = (sig_f["signal"] != 0) & (proba.fillna(0) < min_proba)
    sig_f.loc[skip, "signal"] = 0
    sig_f.loc[skip, ["sl", "tp"]] = np.nan
    eq, trades = run_backtest_enhanced(df, sig_f, cfg, long_only=False)
    eq, trades, _ = apply_funding(eq, trades, 1.0)
    return stats(eq, trades)


def exp8_finer_regimes(df, cfg, n_components=4):
    bpd = 24
    feats = build_features(df, bars_per_day=bpd)
    fm = feature_matrix(feats)
    train_bars = 365 * bpd
    step_bars = 30 * bpd
    regs = pd.Series("CHOP", index=df.index)
    cursor = train_bars
    while cursor < len(df):
        train_slice = fm.iloc[max(0, cursor - train_bars):cursor]
        if len(train_slice.dropna()) < 200:
            cursor += step_bars; continue
        try:
            gmm, mapping = fit_gmm(train_slice, n_components=n_components)
        except Exception:
            cursor += step_bars; continue
        end = min(cursor + step_bars, len(df))
        test_slice = fm.iloc[cursor:end]
        preds = predict_regimes(gmm, mapping, test_slice)
        regs.loc[preds.index] = preds.values
        cursor = end
    print(f"      n={n_components} regime dist: {regs.value_counts().to_dict()}")
    sig = make_bidir_dir_regime(df, regs)
    eq, trades = run_backtest_enhanced(df, sig, cfg, long_only=False)
    eq, trades, _ = apply_funding(eq, trades, 1.0)
    return stats(eq, trades)


def exp10_sentiment(df, regimes, cfg, fear_max=20, greed_min=80):
    try:
        fng = fetch_fear_greed()
    except Exception as e:
        print(f"      F&G fetch failed: {e}")
        return None
    sig = make_bidir_dir_regime(df, regimes)
    fng_aligned = align_to_bars(fng, df.index)
    n_extr_long = int((sig["signal"] == 1) & (fng_aligned >= greed_min)).sum() if False else int(((sig["signal"] == 1) & (fng_aligned >= greed_min)).sum())
    n_extr_short = int(((sig["signal"] == -1) & (fng_aligned <= fear_max)).sum())
    print(f"      blocking {n_extr_long} extreme-greed longs and {n_extr_short} extreme-fear shorts")
    sig = sig.copy()
    skip = ((sig["signal"] ==  1) & (fng_aligned >= greed_min)) | \
           ((sig["signal"] == -1) & (fng_aligned <= fear_max))
    sig.loc[skip, "signal"] = 0
    sig.loc[skip, ["sl", "tp"]] = np.nan
    eq, trades = run_backtest_enhanced(df, sig, cfg, long_only=False)
    eq, trades, _ = apply_funding(eq, trades, 1.0)
    return stats(eq, trades)


# ----- main ----------------------------------------------------------------

def main():
    skip = set(_os.environ.get("SKIP", "").split(","))
    days = int(_os.environ.get("DAYS", 5 * 365))

    print(f"\n[1] Fetching INJ-USDT 1h, {days}d ...", flush=True)
    df = fetch_ohlcv("INJ-USDT", "1h", days=days)
    print(f"    {len(df)} bars, {df.index[0]} → {df.index[-1]}")

    print(f"[2] Walk-forward regime detection (3-component GMM) ...", flush=True)
    t0 = time.time()
    regimes = walk_forward_regimes(df, bars_per_day=24, train_days=365, step_days=30)
    print(f"    done in {time.time()-t0:.1f}s, dist={regimes.value_counts().to_dict()}")

    cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                           max_leverage=5.0, eq_risk_decay=0.5, drawdown_for_decay=0.20)

    results = []

    print("\n[3] BASELINE — bidir + dir-regime, NO funding model")
    t0 = time.time()
    base_no_funding = exp_baseline(df, regimes, cfg, funding_bps=0.0)
    print(f"    {time.time()-t0:.1f}s  final ${base_no_funding['final']:.0f}  "
          f"sharpe {base_no_funding['sharpe']:.2f}")
    results.append(("baseline (no funding)", base_no_funding, "(reference)"))

    print("\n[4] REALISTIC BASELINE — same + funding @ 1bp / 8h (Bybit median)")
    t0 = time.time()
    base = exp_baseline(df, regimes, cfg, funding_bps=1.0)
    print(f"    {time.time()-t0:.1f}s  final ${base['final']:.0f}  "
          f"sharpe {base['sharpe']:.2f}  funding_total ${base.get('funding_total',0):+.1f}")
    results.append(("REALISTIC baseline (funding modeled)", base, "(comparison reference)"))

    def maybe_run(idx, name, fn, *args, **kwargs):
        key = str(idx)
        if key in skip:
            print(f"\n[exp {idx}] {name}: SKIPPED")
            return
        print(f"\n[exp {idx}] {name}")
        t0 = time.time()
        try:
            s = fn(*args, **kwargs)
            if s is None:
                results.append((name, None, "n/a"))
                return
            v = verdict(base, s)
            print(f"    {time.time()-t0:.1f}s  final ${s['final']:.0f}  "
                  f"ret {s['ret']*100:+.0f}%  mdd {s['mdd']*100:+.1f}%  "
                  f"sharpe {s['sharpe']:.2f}  verdict={v}")
            results.append((name, s, v))
        except Exception as e:
            import traceback; traceback.print_exc()
            results.append((name, None, f"ERROR: {type(e).__name__}"))

    maybe_run(1,    "Per-regime risk sizing",     exp1_per_regime_risk,      df, regimes, cfg)
    maybe_run(3,    "Partial TP + breakeven",     exp3_partial_tp_breakeven, df, regimes, cfg)
    maybe_run(4,    "Multi-pair portfolio (ETH/BTC/SOL/INJ)", exp4_multipair_portfolio, df, regimes, cfg, days=days)
    maybe_run(5,    "Walk-forward params (annual retune)",    exp5_walkforward_params,  df, regimes, cfg)
    maybe_run(6,    "Fresh-crossover gating (lookback=5)",    exp6_fresh_crossover,     df, regimes, cfg)
    maybe_run(7,    "ML entry filter (p>=0.50)",  exp7_ml_filter,            df, regimes, cfg)
    maybe_run("8a", "GMM 4 regimes",              exp8_finer_regimes,        df, cfg, n_components=4)
    maybe_run("8b", "GMM 5 regimes",              exp8_finer_regimes,        df, cfg, n_components=5)
    maybe_run(10,   "F&G sentiment filter (20/80)", exp10_sentiment,         df, regimes, cfg)

    # ----- summary ----------------------------------------------------------
    print("\n" + "=" * 120)
    print("COMPREHENSIVE RESULTS  (5y INJ 1h, r=2%, decay=0.5 @ -20% DD, max_lev=5, funding 1bp/8h)")
    print("=" * 120)
    rows = []
    for name, s, v in results:
        if s is None:
            rows.append({"experiment": name, "final$": "—", "ret%": "—", "mdd%": "—",
                         "sharpe": "—", "trades": "—", "verdict": v})
            continue
        rows.append({
            "experiment": name[:42],
            "final$":     f"{s['final']:>8.0f}",
            "ret%":       f"{s['ret']*100:>+6.0f}",
            "mdd%":       f"{s['mdd']*100:>+5.1f}",
            "sharpe":     f"{s['sharpe']:>4.2f}",
            "PF":         f"{s['pf']:>4.2f}",
            "trades":     s["n"],
            "n_long":     s["n_long"],
            "n_short":    s["n_short"],
            "long$":      f"{s['long_pnl']:>+6.0f}",
            "short$":     f"{s['short_pnl']:>+6.0f}",
            "verdict":    v,
        })
    out = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    print(out.to_string(index=False))


if __name__ == "__main__":
    main()
