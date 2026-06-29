"""Detailed view of the multi-pair bidir backtest (Exp 4).

Runs adaptive_inj_bidir-style strategy on ETH/BTC/SOL/INJ independently
(each with $25 of a $100 starting pot), then aggregates and prints:
  - per-pair year-by-year ret / mdd
  - per-pair monthly stats (median, win rate, best, worst)
  - portfolio combined stats year-by-year
  - last 12 months month-by-month
  - saves per-pair equity curves to /tmp/multipair_eq_<PAIR>.csv

Run from project root:
    python3 research/view_multipair.py
"""
from __future__ import annotations

import sys as _sys, os as _os
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

import numpy as np
import pandas as pd

from core.backtest import Trade
from core.backtest_enhanced import EnhancedBTConfig, run_backtest_enhanced
from core.data import fetch_ohlcv
from core.regime_strategy import walk_forward_regimes
from core.strategies import triple_confirm_bidir


PAIRS = ["ETH-USDT", "BTC-USDT", "SOL-USDT", "INJ-USDT"]


def make_bidir_dir_regime(df, regimes):
    sig = triple_confirm_bidir(df)
    a = regimes.reindex(sig.index, method="ffill").fillna("CHOP")
    block = ((sig["signal"] == 1) & (~a.isin(["BULL","CHOP"]))) | \
            ((sig["signal"] == -1) & (~a.isin(["BEAR","CHOP"])))
    sig.loc[block, "signal"] = 0
    sig.loc[block, ["sl", "tp"]] = np.nan
    return sig


def apply_funding(eq, trades, bps=1.0):
    if not trades: return eq, trades
    per_bar = (bps / 1e4) / 8
    new = []; deltas = []
    for t in trades:
        c = per_bar * t.notional * t.bars_held * t.side
        nt = Trade(side=t.side, entry_time=t.entry_time, exit_time=t.exit_time,
                   entry_px=t.entry_px, exit_px=t.exit_px, qty=t.qty,
                   notional=t.notional, sl=t.sl, tp=t.tp,
                   pnl=t.pnl - c, fees=t.fees + max(c,0), reason=t.reason,
                   bars_held=t.bars_held)
        new.append(nt); deltas.append((nt.exit_time, nt.pnl - t.pnl))
    eq2 = eq.copy(); off = pd.Series(0.0, index=eq2.index)
    for ts, d in deltas: off.loc[eq2.index >= ts] += d
    return eq2 + off, new


def yearly(eq):
    rows = []
    for y in sorted({d.year for d in eq.index}):
        s = eq[eq.index.year == y]
        if len(s) < 100: continue
        ret = s.iloc[-1] / s.iloc[0] - 1
        mdd = (s / s.cummax() - 1).min()
        rows.append({"year": y, "ret%": round(ret*100,1), "mdd%": round(float(mdd)*100,1)})
    return pd.DataFrame(rows)


def monthly(eq):
    m_eq = eq.resample("ME").last()
    return m_eq.pct_change().dropna() * 100


def main():
    days = int(_os.environ.get("DAYS", 5 * 365))
    cfg = EnhancedBTConfig(starting_equity=25.0, risk_per_trade=0.020,
                           max_leverage=5.0, eq_risk_decay=0.5,
                           drawdown_for_decay=0.20)

    per_pair_eqs = {}
    per_pair_trades = {}

    for p in PAIRS:
        print(f"\n=== {p} ===")
        d = fetch_ohlcv(p, "1h", days=days)
        print(f"  bars: {len(d)}  range: {d.index[0]} → {d.index[-1]}")
        regs = walk_forward_regimes(d, bars_per_day=24, train_days=365, step_days=30)
        print(f"  regime dist: {regs.value_counts().to_dict()}")
        sig = make_bidir_dir_regime(d, regs)
        eq, trades = run_backtest_enhanced(d, sig, cfg, long_only=False)
        eq, trades = apply_funding(eq, trades)
        per_pair_eqs[p] = eq; per_pair_trades[p] = trades
        n_long = sum(1 for t in trades if t.side == 1)
        n_short = sum(1 for t in trades if t.side == -1)
        long_pnl = sum(t.pnl for t in trades if t.side == 1)
        short_pnl = sum(t.pnl for t in trades if t.side == -1)
        print(f"  final ${eq.iloc[-1]:.2f}  ret +{(eq.iloc[-1]/eq.iloc[0]-1)*100:.0f}%  "
              f"mdd {(eq/eq.cummax()-1).min()*100:.1f}%")
        print(f"  trades n={len(trades)}  long={n_long} ({long_pnl:+.1f})  "
              f"short={n_short} ({short_pnl:+.1f})")
        print(f"  year-by-year:")
        print("    " + yearly(eq).to_string(index=False).replace("\n", "\n    "))
        m = monthly(eq)
        if len(m) > 0:
            print(f"  monthly:  n={len(m)}  median {m.median():+.2f}%  "
                  f"mean {m.mean():+.2f}%  win_rate {(m>0).mean()*100:.0f}%  "
                  f"best {m.max():+.1f}%  worst {m.min():+.1f}%")
        # Dump eq curve
        eq.to_csv(f"/tmp/multipair_eq_{p.replace('-','_')}.csv", header=["equity"])

    # Combined portfolio
    common_idx = per_pair_eqs[PAIRS[0]].index
    for p in PAIRS[1:]:
        common_idx = common_idx.intersection(per_pair_eqs[p].index)
    portfolio = sum(per_pair_eqs[p].reindex(common_idx).ffill() for p in PAIRS)
    portfolio.to_csv("/tmp/multipair_eq_PORTFOLIO.csv", header=["equity"])

    print("\n" + "=" * 80)
    print("COMBINED PORTFOLIO  (ETH+BTC+SOL+INJ, equal weight, $25 each)")
    print("=" * 80)
    print(f"  start: ${portfolio.iloc[0]:.2f}   final: ${portfolio.iloc[-1]:.2f}   "
          f"ret +{(portfolio.iloc[-1]/portfolio.iloc[0]-1)*100:.0f}%   "
          f"mdd {(portfolio/portfolio.cummax()-1).min()*100:.1f}%")
    print(f"\n  Year-by-year:")
    print("    " + yearly(portfolio).to_string(index=False).replace("\n","\n    "))
    pm = monthly(portfolio)
    print(f"\n  Monthly:  n={len(pm)}  median {pm.median():+.2f}%  "
          f"mean {pm.mean():+.2f}%  win_rate {(pm>0).mean()*100:.0f}%  "
          f"best {pm.max():+.1f}%  worst {pm.min():+.1f}%")
    print(f"\n  Last 12 months of portfolio equity:")
    for ts, val in pm.tail(12).items():
        print(f"    {ts.strftime('%Y-%m')}:  {val:+6.2f}%")

    # Single-pair INJ comparison
    inj_eq = per_pair_eqs["INJ-USDT"]
    # Scale INJ-only result to $100 starting equity for fair comparison
    inj_100 = inj_eq * (100 / inj_eq.iloc[0])
    print(f"\n  Comparison vs INJ-only (scaled to $100 start):")
    print(f"    portfolio:  final ${portfolio.iloc[-1]:.0f}   "
          f"mdd {(portfolio/portfolio.cummax()-1).min()*100:.1f}%")
    print(f"    INJ-only:   final ${inj_100.iloc[-1]:.0f}   "
          f"mdd {(inj_100/inj_100.cummax()-1).min()*100:.1f}%")

    print("\nEquity curves saved to:")
    for p in PAIRS:
        print(f"  /tmp/multipair_eq_{p.replace('-','_')}.csv")
    print("  /tmp/multipair_eq_PORTFOLIO.csv")
    print("\nLoad in Python:  pd.read_csv('/tmp/multipair_eq_PORTFOLIO.csv', "
          "index_col=0, parse_dates=True)")


if __name__ == "__main__":
    main()
