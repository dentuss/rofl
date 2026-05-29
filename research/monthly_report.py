"""5y simulation of adaptive_inj_bidir + walk-forward retune (production setup),
plus a 12-month-by-month return table for each of the live presets.

Compares:
  A. adaptive_inj_high_return (long-only, ML BEAR filter)
  B. adaptive_inj_bidir       (bidir + dir-regime + F&G)
  C. adaptive_inj_bidir_wf    (same as B + annual param retune)

Run from project root:
    python3 research/monthly_report.py
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
from core.strategies import triple_confirm_bidir, triple_confirm_long


GRID_EF = [7, 9, 12]
GRID_ES = [21, 26, 34]
GRID_RSI = [50, 55, 60]


def apply_funding(eq, trades, bps=1.0):
    if not trades:
        return eq, trades
    per_bar = (bps / 1e4) / 8
    new = []; deltas = []
    for t in trades:
        c = per_bar * t.notional * t.bars_held * t.side
        nt = Trade(side=t.side, entry_time=t.entry_time, exit_time=t.exit_time,
                   entry_px=t.entry_px, exit_px=t.exit_px, qty=t.qty,
                   notional=t.notional, sl=t.sl, tp=t.tp,
                   pnl=t.pnl - c, fees=t.fees + max(c, 0), reason=t.reason,
                   bars_held=t.bars_held)
        new.append(nt); deltas.append((nt.exit_time, nt.pnl - t.pnl))
    eq2 = eq.copy(); off = pd.Series(0.0, index=eq2.index)
    for ts, d in deltas:
        off.loc[eq2.index >= ts] += d
    return eq2 + off, new


def make_signal_bidir(df, regimes, fng_aligned,
                      ema_fast=9, ema_slow=26, rsi_min=55.0,
                      use_fng=True, fng_greed_max=80, fng_fear_min=20):
    sig = triple_confirm_bidir(df, ema_fast=ema_fast, ema_slow=ema_slow,
                                rsi_min=rsi_min)
    a = regimes.reindex(sig.index, method="ffill").fillna("CHOP")
    block_reg = ((sig["signal"] ==  1) & (~a.isin(["BULL","CHOP"]))) | \
                ((sig["signal"] == -1) & (~a.isin(["BEAR","CHOP"])))
    sig.loc[block_reg, "signal"] = 0
    sig.loc[block_reg, ["sl","tp"]] = np.nan
    if use_fng and fng_aligned is not None:
        block_fng = ((sig["signal"] ==  1) & (fng_aligned >= fng_greed_max)) | \
                    ((sig["signal"] == -1) & (fng_aligned <= fng_fear_min))
        sig.loc[block_fng, "signal"] = 0
        sig.loc[block_fng, ["sl","tp"]] = np.nan
    return sig


def make_signal_long(df, regimes):
    sig = triple_confirm_long(df)
    a = regimes.reindex(sig.index, method="ffill").fillna("CHOP")
    block = (sig["signal"] == 1) & (~a.isin(["BULL","CHOP"]))
    sig.loc[block, "signal"] = 0
    sig.loc[block, ["sl","tp"]] = np.nan
    return sig


def grid_best(df, regimes, fng_aligned, cfg):
    """Return (ef, es, rm) with best Sharpe on `df`."""
    best = None
    for ef in GRID_EF:
        for es in GRID_ES:
            if ef >= es: continue
            for rm in GRID_RSI:
                sig = make_signal_bidir(df, regimes, fng_aligned, ef, es, rm)
                if (sig["signal"] != 0).sum() < 5: continue
                eq, tr = run_backtest_enhanced(df, sig, cfg, long_only=False)
                if len(tr) < 5: continue
                bar_ret = eq.pct_change().fillna(0)
                if bar_ret.std() == 0: continue
                sharpe = float(bar_ret.mean() / bar_ret.std() * np.sqrt(24 * 365))
                if best is None or sharpe > best[0]:
                    best = (sharpe, ef, es, rm)
    return best[1:] if best else (9, 26, 55)


def run_variant_C_wf_retune(df, regimes, fng_aligned, cfg):
    """Walk-forward: each year, retune on prior year, apply to this year."""
    sig_combined = pd.DataFrame(index=df.index,
                                columns=["signal","sl","tp"], dtype="float64")
    sig_combined["signal"] = 0
    sig_combined[["sl","tp"]] = np.nan
    years = sorted({d.year for d in df.index})
    history = []
    for yi, y in enumerate(years):
        if yi == 0:
            continue  # need a prior year to fit on
        prev_idx = df.index[df.index.year == years[yi-1]]
        test_idx = df.index[df.index.year == y]
        if len(prev_idx) == 0 or len(test_idx) == 0:
            continue
        train_df = df.loc[prev_idx[0]:prev_idx[-1]]
        train_regs = regimes.loc[train_df.index]
        train_fng = fng_aligned.loc[train_df.index] if fng_aligned is not None else None
        ef, es, rm = grid_best(train_df, train_regs, train_fng, cfg)
        history.append((y, ef, es, rm))
        # Generate signal on FULL df (for warmup), then take only the test year.
        s_full = make_signal_bidir(df, regimes, fng_aligned, ef, es, rm)
        sig_combined.loc[test_idx] = s_full.loc[test_idx].values
    print(f"      annual params: {history}")
    eq, trades = run_backtest_enhanced(df, sig_combined, cfg, long_only=False)
    eq, trades = apply_funding(eq, trades)
    return eq, trades


def monthly_returns(eq):
    m_eq = eq.resample("ME").last()
    return m_eq.pct_change().dropna() * 100


def yearly(eq):
    rows = []
    for y in sorted({d.year for d in eq.index}):
        s = eq[eq.index.year == y]
        if len(s) < 100: continue
        ret = s.iloc[-1] / s.iloc[0] - 1
        mdd = (s / s.cummax() - 1).min()
        rows.append({"year": y, "ret%": round(ret*100, 1),
                     "mdd%": round(float(mdd)*100, 1)})
    return pd.DataFrame(rows)


def main():
    days = int(_os.environ.get("DAYS", 5 * 365))
    print(f"\nFetching INJ-USDT 1h, {days}d ...")
    df = fetch_ohlcv("INJ-USDT", "1h", days=days)
    print(f"  {len(df)} bars  ({df.index[0]} → {df.index[-1]})")

    print("Walk-forward regimes ...")
    t0 = time.time()
    regimes = walk_forward_regimes(df, bars_per_day=24, train_days=365, step_days=30)
    print(f"  {time.time()-t0:.0f}s, dist={regimes.value_counts().to_dict()}")

    print("F&G fetch ...")
    try:
        fng = fetch_fear_greed()
        fng_aligned = align_to_bars(fng, df.index)
        print(f"  OK ({len(fng)} rows)")
    except Exception as e:
        print(f"  fail: {e}")
        fng_aligned = None

    cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                           max_leverage=5.0, eq_risk_decay=0.5,
                           drawdown_for_decay=0.20)

    variants = {}

    print("\n[A] adaptive_inj_high_return (long-only, BEAR skip) ...")
    sig_A = make_signal_long(df, regimes)
    eq_A, tr_A = run_backtest_enhanced(df, sig_A, cfg, long_only=True)
    eq_A, tr_A = apply_funding(eq_A, tr_A)
    variants["A. adaptive_inj_high_return"] = (eq_A, tr_A)

    print("\n[B] adaptive_inj_bidir (bidir + dir-regime + F&G, fixed params) ...")
    sig_B = make_signal_bidir(df, regimes, fng_aligned)
    eq_B, tr_B = run_backtest_enhanced(df, sig_B, cfg, long_only=False)
    eq_B, tr_B = apply_funding(eq_B, tr_B)
    variants["B. adaptive_inj_bidir"] = (eq_B, tr_B)

    print("\n[C] adaptive_inj_bidir_wf (B + annual walk-forward retune) ...")
    t0 = time.time()
    eq_C, tr_C = run_variant_C_wf_retune(df, regimes, fng_aligned, cfg)
    print(f"  retune backtest done in {time.time()-t0:.0f}s")
    variants["C. adaptive_inj_bidir_wf"] = (eq_C, tr_C)

    # ===== summary =====
    print("\n" + "=" * 100)
    print("HEADLINE NUMBERS  (5y INJ 1h, $100 start, funding modeled)")
    print("=" * 100)
    rows = []
    for name, (eq, tr) in variants.items():
        m = monthly_returns(eq)
        n_long = sum(1 for t in tr if t.side == 1)
        n_short = sum(1 for t in tr if t.side == -1)
        rows.append({
            "variant": name,
            "final$": f"{eq.iloc[-1]:.0f}",
            "5y_ret%": f"{(eq.iloc[-1]/eq.iloc[0]-1)*100:+.0f}",
            "mdd%": f"{(eq/eq.cummax()-1).min()*100:+.1f}",
            "sharpe": f"{eq.pct_change().fillna(0).mean()/eq.pct_change().fillna(0).std()*np.sqrt(24*365):.2f}",
            "trades": len(tr),
            "n_long": n_long,
            "n_short": n_short,
            "monthly_med%": f"{m.median():+.2f}",
            "monthly_mean%": f"{m.mean():+.2f}",
            "monthly_win%": f"{(m > 0).mean()*100:.0f}",
            "best_mo%": f"{m.max():+.1f}",
            "worst_mo%": f"{m.min():+.1f}",
        })
    summary = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    print(summary.to_string(index=False))

    print("\n" + "=" * 100)
    print("YEAR-BY-YEAR")
    print("=" * 100)
    for name, (eq, _) in variants.items():
        print(f"\n  {name}:")
        print("  " + yearly(eq).to_string(index=False).replace("\n","\n  "))

    print("\n" + "=" * 100)
    print("LAST 12 MONTHS — MONTH-BY-MONTH RETURN (%)")
    print("=" * 100)
    months_A = monthly_returns(variants["A. adaptive_inj_high_return"][0]).tail(12)
    months_B = monthly_returns(variants["B. adaptive_inj_bidir"][0]).tail(12)
    months_C = monthly_returns(variants["C. adaptive_inj_bidir_wf"][0]).tail(12)
    tbl = pd.DataFrame({"month": months_B.index.strftime("%Y-%m"),
                        "A_long_only%": months_A.values,
                        "B_bidir%": months_B.values,
                        "C_bidir_wf%": months_C.values})
    tbl[["A_long_only%","B_bidir%","C_bidir_wf%"]] = tbl[["A_long_only%","B_bidir%","C_bidir_wf%"]].round(2)
    print(tbl.to_string(index=False))

    # Compounded over those 12 months
    print()
    for nm, ms in [("A_long_only", months_A), ("B_bidir", months_B), ("C_bidir_wf", months_C)]:
        compound = float((1 + ms/100).prod() - 1) * 100
        avg_mo = float(ms.mean())
        med_mo = float(ms.median())
        print(f"  {nm}:  12mo compound {compound:+.1f}%   "
              f"mean {avg_mo:+.2f}%/mo   median {med_mo:+.2f}%/mo")

    # Full monthly distribution for the C variant (production target)
    print("\n" + "=" * 100)
    print("MONTHLY DISTRIBUTION — adaptive_inj_bidir_wf (production-target preset)")
    print("=" * 100)
    m_C = monthly_returns(variants["C. adaptive_inj_bidir_wf"][0])
    pct = lambda p: float(np.percentile(m_C.values, p))
    print(f"  n_months = {len(m_C)}")
    print(f"  p10/p25/median/p75/p90 = "
          f"{pct(10):+.2f}% / {pct(25):+.2f}% / {pct(50):+.2f}% / "
          f"{pct(75):+.2f}% / {pct(90):+.2f}%")
    print(f"  mean = {m_C.mean():+.2f}%/mo   std = {m_C.std():.2f}pp")
    print(f"  win_rate = {(m_C > 0).mean()*100:.0f}%   "
          f"best = {m_C.max():+.1f}%   worst = {m_C.min():+.1f}%")
    # losing-streak stats
    is_loss = (m_C <= 0).astype(int).values
    streaks = []; cur = 0
    for x in is_loss:
        if x: cur += 1
        else:
            if cur > 0: streaks.append(cur)
            cur = 0
    if cur > 0: streaks.append(cur)
    if streaks:
        print(f"  worst losing streak: {max(streaks)} consecutive months "
              f"(streaks: {streaks})")
    print()


if __name__ == "__main__":
    main()
