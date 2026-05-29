"""Backtest: adaptive_inj_high_return with ALLOW_SHORT.

Compares 4 variants on 5y INJ 1h data:
  A. Baseline long-only (current adaptive_inj_high_return)
  B. Naive ALLOW_SHORT=1 on the existing triple_long (long-only strategy →
     no shorts ever generated; provides a sanity-check that allow_short
     alone is a no-op).
  C. Bidirectional triple_confirm (long + mirror-image short), no regime
     filter (allow shorts anywhere).
  D. Regime-aware bidirectional: long only in BULL/CHOP, short only in
     BEAR/CHOP (this is the actually-useful adaptive version).

Then prints a comparison table + per-year breakdown + short-trade-only stats.

Run from project root:
    python3 research/test_short_adapt.py
"""
from __future__ import annotations

import sys as _sys, os as _os
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

import numpy as np
import pandas as pd

from core.backtest_enhanced import EnhancedBTConfig, run_backtest_enhanced
from core.data import fetch_ohlcv
from core.indicators import adx, atr, ema, rsi
from core.regime_strategy import filter_by_regime, walk_forward_regimes
from core.strategies import triple_confirm_long


# ---- Strategy variants ----------------------------------------------------

def triple_confirm_bidir(df: pd.DataFrame,
                         ema_fast: int = 9, ema_slow: int = 26, ema_trend: int = 50,
                         rsi_n: int = 14,
                         rsi_long_min: float = 55.0, rsi_short_max: float = 45.0,
                         adx_n: int = 14, adx_min: float = 22.0,
                         atr_n: int = 14,
                         sl_mult: float = 1.8, tp_mult: float = 3.0) -> pd.DataFrame:
    """Long + mirror-image short version of triple_confirm_long.

    LONG:  EMA stack up, close > EMA_fast, RSI > rsi_long_min,    ADX > adx_min
    SHORT: EMA stack dn, close < EMA_fast, RSI < rsi_short_max,   ADX > adx_min
    Stops/TPs are symmetric ATR multiples.
    """
    e_f = ema(df["close"], ema_fast)
    e_s = ema(df["close"], ema_slow)
    e_t = ema(df["close"], ema_trend)
    r = rsi(df["close"], rsi_n)
    adx_v = adx(df["high"], df["low"], df["close"], adx_n)
    a = atr(df["high"], df["low"], df["close"], atr_n)

    long_cond = (e_f > e_s) & (e_s > e_t) & (df["close"] > e_f) \
              & (r > rsi_long_min) & (adx_v > adx_min)
    short_cond = (e_f < e_s) & (e_s < e_t) & (df["close"] < e_f) \
               & (r < rsi_short_max) & (adx_v > adx_min)

    sig = np.where(long_cond, 1, np.where(short_cond, -1, 0))
    out = pd.DataFrame(index=df.index)
    out["signal"] = sig
    out["sl"] = np.where(sig ==  1, df["close"] - sl_mult * a,
                np.where(sig == -1, df["close"] + sl_mult * a, np.nan))
    out["tp"] = np.where(sig ==  1, df["close"] + tp_mult * a,
                np.where(sig == -1, df["close"] - tp_mult * a, np.nan))
    return out


def regime_filter_directional(sig: pd.DataFrame, regimes: pd.Series) -> pd.DataFrame:
    """Allow LONG only in BULL/CHOP, SHORT only in BEAR/CHOP."""
    out = sig.copy()
    aligned = regimes.reindex(out.index, method="ffill").fillna("CHOP")
    long_block  = (out["signal"] ==  1) & (~aligned.isin(["BULL", "CHOP"]))
    short_block = (out["signal"] == -1) & (~aligned.isin(["BEAR", "CHOP"]))
    block = long_block | short_block
    out.loc[block, "signal"] = 0
    out.loc[block, ["sl", "tp"]] = np.nan
    return out


# ---- Metrics --------------------------------------------------------------

def stats_from_eq_trades(eq: pd.Series, trades, bars_per_year: int = 24 * 365) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "win_rate": 0.0, "final_equity": float(eq.iloc[-1]),
                "total_return": 0.0, "mdd": 0.0, "sharpe": 0.0, "profit_factor": 0.0}
    pnls = np.array([t.pnl for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    pf = (wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf")
    bar_ret = eq.pct_change().fillna(0)
    sharpe = (bar_ret.mean() / bar_ret.std() * np.sqrt(bars_per_year)) \
             if bar_ret.std() > 0 else 0.0
    return {
        "trades": n,
        "win_rate": float(len(wins) / n),
        "final_equity": float(eq.iloc[-1]),
        "total_return": float(eq.iloc[-1] / eq.iloc[0] - 1),
        "mdd": float((eq / eq.cummax() - 1).min()),
        "sharpe": float(sharpe),
        "profit_factor": float(min(pf, 99.0)),
    }


def split_stats(trades):
    longs  = [t for t in trades if t.side ==  1]
    shorts = [t for t in trades if t.side == -1]
    def _s(ts):
        if not ts: return {"n": 0, "win_rate": 0, "total_pnl": 0, "avg_pnl": 0}
        pnls = np.array([t.pnl for t in ts])
        return {
            "n": len(ts),
            "win_rate": float((pnls > 0).mean()),
            "total_pnl": float(pnls.sum()),
            "avg_pnl": float(pnls.mean()),
        }
    return _s(longs), _s(shorts)


def yearly_breakdown(eq: pd.Series) -> pd.DataFrame:
    rows = []
    for y in sorted({d.year for d in eq.index}):
        s = eq[eq.index.year == y]
        if len(s) < 100: continue
        ret = s.iloc[-1] / s.iloc[0] - 1
        mdd = (s / s.cummax() - 1).min()
        rows.append({"year": y, "ret%": round(ret * 100, 1),
                     "mdd%": round(float(mdd) * 100, 1)})
    return pd.DataFrame(rows)


# ---- Main -----------------------------------------------------------------

def main():
    DAYS = int(_os.environ.get("DAYS", 5 * 365))
    print(f"\nFetching INJ-USDT 1h, ~{DAYS} days …", flush=True)
    df = fetch_ohlcv("INJ-USDT", "1h", days=DAYS)
    print(f"  got {len(df)} bars,  {df.index[0]}  →  {df.index[-1]}")

    BPD = 24
    cfg = EnhancedBTConfig(
        starting_equity=100.0,
        risk_per_trade=0.020,
        max_leverage=5.0,
        eq_risk_decay=0.5,
        drawdown_for_decay=0.20,
    )

    # Walk-forward regimes (computed once, reused).
    print("Walk-forward regime detection (re-fit GMM every 30d on prior 365d) …",
          flush=True)
    regimes = walk_forward_regimes(df, bars_per_day=BPD,
                                   train_days=365, step_days=30)
    print(f"  regime dist: {regimes.value_counts().to_dict()}")

    sig_long_only = triple_confirm_long(df)
    sig_long_only_regime = filter_by_regime(sig_long_only, regimes,
                                            allow=("BULL", "CHOP"))
    sig_bidir = triple_confirm_bidir(df)
    sig_bidir_regime = regime_filter_directional(sig_bidir, regimes)

    print(f"  signal counts:")
    for name, s in [("baseline (long-only + BEAR-skip)", sig_long_only_regime),
                    ("bidir, no regime",                  sig_bidir),
                    ("bidir + directional regime filter", sig_bidir_regime)]:
        nL = int((s["signal"] ==  1).sum())
        nS = int((s["signal"] == -1).sum())
        print(f"    {name:<40s}  long={nL:>5}  short={nS:>5}")

    # Run all variants.
    variants = []

    print("\nA. Baseline long-only + adaptive (current adaptive_inj_high_return)")
    eq, tr = run_backtest_enhanced(df, sig_long_only_regime, cfg, long_only=True)
    variants.append(("A. baseline (long-only adaptive)", eq, tr))

    print("B. Naive ALLOW_SHORT on triple_long (sanity: should match A)")
    eq, tr = run_backtest_enhanced(df, sig_long_only_regime, cfg, long_only=False)
    variants.append(("B. allow_short on long-only sig", eq, tr))

    print("C. Bidirectional, no regime filter")
    eq, tr = run_backtest_enhanced(df, sig_bidir, cfg, long_only=False)
    variants.append(("C. bidir, no regime", eq, tr))

    print("D. Bidirectional + directional regime filter (long BULL/CHOP, short BEAR/CHOP)")
    eq, tr = run_backtest_enhanced(df, sig_bidir_regime, cfg, long_only=False)
    variants.append(("D. bidir + directional regime", eq, tr))

    # Summary table.
    print("\n" + "=" * 100)
    print("SUMMARY  (5y INJ 1h, 100 USDT start, r=2%, decay=0.5 @ -20% DD, max_lev=5)")
    print("=" * 100)
    rows = []
    for name, eq, tr in variants:
        s = stats_from_eq_trades(eq, tr)
        L, S = split_stats(tr)
        rows.append({
            "variant":     name,
            "final$":      f"{s['final_equity']:>8.2f}",
            "ret%":        f"{s['total_return']*100:>+7.1f}",
            "mdd%":        f"{s['mdd']*100:>+6.1f}",
            "sharpe":      f"{s['sharpe']:>5.2f}",
            "PF":          f"{s['profit_factor']:>4.2f}",
            "trades":      s["trades"],
            "wr%":         f"{s['win_rate']*100:>3.0f}",
            "n_long":      L["n"],
            "long_wr":     f"{L['win_rate']*100:>3.0f}%",
            "long_pnl":    f"{L['total_pnl']:>+7.1f}",
            "n_short":     S["n"],
            "short_wr":    f"{S['win_rate']*100:>3.0f}%" if S["n"] else " — ",
            "short_pnl":   f"{S['total_pnl']:>+7.1f}" if S["n"] else "  —  ",
        })
    summary = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 30)
    print(summary.to_string(index=False))

    # Year-by-year for the most interesting variant (D vs A).
    print("\n" + "=" * 100)
    print("YEAR-BY-YEAR — A (baseline) vs D (bidir + regime)")
    print("=" * 100)
    for name, eq, tr in variants:
        if name.startswith(("A.", "D.")):
            print(f"\n  {name}:")
            print("  " + yearly_breakdown(eq).to_string(index=False).replace("\n", "\n  "))

    # Verdict.
    print("\n" + "=" * 100)
    print("VERDICT")
    print("=" * 100)
    a_stats = stats_from_eq_trades(variants[0][1], variants[0][2])
    d_stats = stats_from_eq_trades(variants[3][1], variants[3][2])
    d_L, d_S = split_stats(variants[3][2])
    ret_lift = (d_stats["total_return"] - a_stats["total_return"]) * 100
    mdd_lift = (d_stats["mdd"] - a_stats["mdd"]) * 100  # negative is worse
    sharpe_lift = d_stats["sharpe"] - a_stats["sharpe"]
    print(f"  D vs A: ret {ret_lift:+.1f} pp, mdd {mdd_lift:+.1f} pp, sharpe {sharpe_lift:+.2f}")
    print(f"  Short trades contributed: n={d_S['n']}  win_rate={d_S['win_rate']*100:.0f}%  "
          f"total_pnl={d_S['total_pnl']:+.2f} USDT")
    print()


if __name__ == "__main__":
    main()
