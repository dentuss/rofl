"""Two-tier drawdown decay + real-funding test.

Part 1 — DECAY LADDER: compare drawdown-decay schemes on INJ 5y and the
  inj_heavy portfolio. Variants:
    none            no decay
    single          halve risk at -20% (current production)
    two_tier        -20%->0.5, -35%->0.25
    three_tier      -20%->0.5, -35%->0.25, -50%->stop  (DEFAULT_DECAY_TIERS)
  We want lower MDD / higher Sharpe without giving up too much return.

Part 2 — REAL FUNDING: on the window where real funding is fetchable
  (OKX ~90d here, Bybit full-history on EC2), compare the flat 1bp/8h
  constant vs real per-8h funding to quantify the modeling error.

Run from project root:
    python3 research/test_decay_funding.py
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
from core.funding import fetch_funding, per_bar_funding
from core.regime_strategy import walk_forward_regimes
from core.risk import DEFAULT_DECAY_TIERS
from core.sentiment import align_to_bars, fetch_fear_greed
from core.strategies import triple_confirm_bidir


DECAY_SCHEMES = {
    "none":       {"eq_risk_decay": 0.0, "eq_decay_tiers": ()},
    "single":     {"eq_risk_decay": 0.5, "drawdown_for_decay": 0.20, "eq_decay_tiers": ()},
    "two_tier":   {"eq_decay_tiers": ((0.20, 0.5), (0.35, 0.25))},
    "three_tier": {"eq_decay_tiers": DEFAULT_DECAY_TIERS},
}

WINNERS = ["INJ-USDT", "SOL-USDT", "ADA-USDT", "ETH-USDT", "LINK-USDT"]
WEIGHTS = {"INJ-USDT": .40, "SOL-USDT": .20, "ADA-USDT": .15, "ETH-USDT": .15, "LINK-USDT": .10}


def apply_funding(eq, trades, funding_series=None, fallback_bps=1.0,
                  bar_index=None):
    """Charge funding. If funding_series given (per-bar rate), accrue real
    funding over each trade's holding window; else flat fallback per bar."""
    if not trades:
        return eq, trades, 0.0
    new = []; deltas = []; total = 0.0
    if funding_series is not None:
        cum = funding_series.cumsum()
        for t in trades:
            # funding paid = (cum at exit - cum at entry) * notional * side
            try:
                c_entry = cum.asof(t.entry_time)
                c_exit = cum.asof(t.exit_time)
                rate_sum = float(c_exit - c_entry)
            except Exception:
                rate_sum = 0.0
            cost = rate_sum * t.notional * t.side
            total += cost
            new.append(_rebook(t, cost)); deltas.append((t.exit_time, -cost))
    else:
        per_bar = (fallback_bps / 1e4) / 8
        for t in trades:
            cost = per_bar * t.notional * t.bars_held * t.side
            total += cost
            new.append(_rebook(t, cost)); deltas.append((t.exit_time, -cost))
    eq2 = eq.copy(); off = pd.Series(0.0, index=eq2.index)
    for ts, d in deltas:
        off.loc[eq2.index >= ts] += d
    return eq2 + off, new, total


def _rebook(t, cost):
    return Trade(side=t.side, entry_time=t.entry_time, exit_time=t.exit_time,
                 entry_px=t.entry_px, exit_px=t.exit_px, qty=t.qty,
                 notional=t.notional, sl=t.sl, tp=t.tp,
                 pnl=t.pnl - cost, fees=t.fees + max(cost, 0), reason=t.reason,
                 bars_held=t.bars_held)


def make_signal(df, regimes, fng_aligned):
    sig = triple_confirm_bidir(df)
    a = regimes.reindex(sig.index, method="ffill").fillna("CHOP")
    block = ((sig["signal"] ==  1) & (~a.isin(["BULL","CHOP"]))) | \
            ((sig["signal"] == -1) & (~a.isin(["BEAR","CHOP"])))
    sig.loc[block, "signal"] = 0
    sig.loc[block, ["sl","tp"]] = np.nan
    if fng_aligned is not None:
        bf = ((sig["signal"] ==  1) & (fng_aligned >= 80)) | \
             ((sig["signal"] == -1) & (fng_aligned <= 20))
        sig.loc[bf, "signal"] = 0
        sig.loc[bf, ["sl","tp"]] = np.nan
    return sig


def cstats(eq, trades=None):
    final = float(eq.iloc[-1]); ret = float(final/eq.iloc[0]-1)
    mdd = float((eq/eq.cummax()-1).min())
    br = eq.pct_change().fillna(0)
    sharpe = float(br.mean()/br.std()*np.sqrt(24*365)) if br.std()>0 else 0
    months = eq.resample("ME").last().pct_change().dropna()*100
    d = dict(final=final, ret=ret, mdd=mdd, sharpe=sharpe,
             worst_mo=float(months.min()) if len(months) else 0,
             med_mo=float(months.median()) if len(months) else 0)
    if trades is not None:
        d["trades"] = len(trades)
    return d


def base_cfg(**over):
    c = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                         max_leverage=5.0)
    for k, v in over.items():
        setattr(c, k, v)
    return c


def main():
    days = int(_os.environ.get("DAYS", 5 * 365))
    print("F&G fetch ...", flush=True)
    try:
        fng = fetch_fear_greed()
    except Exception:
        fng = None

    # Precompute signals + regimes for each pair (once).
    print("Fetching pairs + regimes ...", flush=True)
    pair_data = {}
    for p in WINNERS:
        df = fetch_ohlcv(p, "1h", days=days)
        t0 = time.time()
        regs = walk_forward_regimes(df, bars_per_day=24, train_days=365, step_days=30)
        fa = align_to_bars(fng, df.index) if fng is not None else None
        sig = make_signal(df, regs, fa)
        pair_data[p] = (df, sig)
        print(f"  {p}: {len(df)} bars, regimes {time.time()-t0:.0f}s", flush=True)

    # ---- PART 1: decay ladder on INJ + portfolio (flat funding, held constant) ----
    print("\n" + "=" * 100)
    print("PART 1 — DRAWDOWN DECAY LADDER (flat 1bp/8h funding held constant)")
    print("=" * 100)

    # INJ single-pair
    print("\nINJ single-pair:")
    inj_df, inj_sig = pair_data["INJ-USDT"]
    rows = []
    for scheme, over in DECAY_SCHEMES.items():
        cfg = base_cfg(**over)
        eq, tr = run_backtest_enhanced(inj_df, inj_sig, cfg, long_only=False)
        eq, tr, _ = apply_funding(eq, tr, fallback_bps=1.0)
        s = cstats(eq, tr)
        rows.append({"scheme": scheme, "final$": f"{s['final']:.0f}",
                     "ret%": f"{s['ret']*100:+.0f}", "mdd%": f"{s['mdd']*100:+.1f}",
                     "sharpe": f"{s['sharpe']:.2f}", "worst_mo%": f"{s['worst_mo']:+.1f}",
                     "trades": s["trades"]})
    print(pd.DataFrame(rows).to_string(index=False))

    # inj_heavy portfolio
    print("\ninj_heavy portfolio (5 pairs):")
    rows = []
    for scheme, over in DECAY_SCHEMES.items():
        norm = {}
        for p in WINNERS:
            df, sig = pair_data[p]
            cfg = base_cfg(**over)
            eq, tr = run_backtest_enhanced(df, sig, cfg, long_only=False)
            eq, tr, _ = apply_funding(eq, tr, fallback_bps=1.0)
            norm[p] = eq / eq.iloc[0]
        common = norm[WINNERS[0]].index
        for p in WINNERS[1:]:
            common = common.intersection(norm[p].index)
        port = sum(norm[p].reindex(common).ffill() * WEIGHTS[p] for p in WINNERS) * 100
        s = cstats(port)
        rows.append({"scheme": scheme, "final$": f"{s['final']:.0f}",
                     "ret%": f"{s['ret']*100:+.0f}", "mdd%": f"{s['mdd']*100:+.1f}",
                     "sharpe": f"{s['sharpe']:.2f}", "worst_mo%": f"{s['worst_mo']:+.1f}"})
    print(pd.DataFrame(rows).to_string(index=False))

    # ---- PART 2: real funding vs constant (on whatever window is fetchable) ----
    print("\n" + "=" * 100)
    print("PART 2 — REAL FUNDING vs FLAT CONSTANT (INJ)")
    print("=" * 100)
    fund = fetch_funding("INJ-USDT", days=days, source="auto", use_cache=True)
    if fund.empty:
        print("  No real funding reachable from here (Bybit geo-blocked, OKX failed).")
        print("  On the EC2 box (Singapore) Bybit funding works — code is ready.")
        return
    cov_start = fund.index[0]
    print(f"  real funding covers {fund.index[0].date()} → {fund.index[-1].date()} "
          f"({len(fund)} events)")
    print(f"  mean {fund['funding_rate'].mean()*100:.4f}%/8h  "
          f"min {fund['funding_rate'].min()*100:.3f}%  "
          f"max {fund['funding_rate'].max()*100:.3f}%")

    # Restrict INJ backtest to the covered window for an apples-to-apples compare
    inj_window = inj_df[inj_df.index >= cov_start]
    sig_window = inj_sig[inj_sig.index >= cov_start]
    cfg = base_cfg(eq_decay_tiers=DEFAULT_DECAY_TIERS)
    eq0, tr0 = run_backtest_enhanced(inj_window, sig_window, cfg, long_only=False)

    # constant
    eq_c, tr_c, tot_c = apply_funding(eq0.copy(), list(tr0), fallback_bps=1.0)
    # real
    fseries = per_bar_funding(fund, inj_window.index, fallback_bps_per_8h=1.0)
    eq_r, tr_r, tot_r = apply_funding(eq0.copy(), list(tr0), funding_series=fseries,
                                      bar_index=inj_window.index)
    sc = cstats(eq_c); sr = cstats(eq_r)
    print(f"\n  window: {inj_window.index[0].date()} → {inj_window.index[-1].date()} "
          f"({len(tr0)} trades)")
    print(f"  flat 1bp/8h : final ${sc['final']:.2f}  funding_total ${tot_c:+.2f}  "
          f"mdd {sc['mdd']*100:.1f}%")
    print(f"  REAL funding: final ${sr['final']:.2f}  funding_total ${tot_r:+.2f}  "
          f"mdd {sr['mdd']*100:.1f}%")
    diff = sr['final'] - sc['final']
    print(f"  difference  : ${diff:+.2f} on ${eq0.iloc[0]:.0f} start "
          f"({diff/eq0.iloc[0]*100:+.2f}% of starting equity over window)")
    print("\n  Interpretation: bidir is ~long/short balanced so funding largely")
    print("  cancels; the flat constant is a fair central estimate. Real funding")
    print("  matters mainly via spikes during big moves (captured live on Bybit).")


if __name__ == "__main__":
    main()
