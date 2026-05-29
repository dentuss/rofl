"""Year-to-date monthly PnL breakdown for the production setups.

Reports month-by-month return for 2026 YTD (and trailing 12 months) for:
  - single-pair production preset (adaptive_inj_bidir on INJ)
  - 5-pair inj_heavy portfolio

Production config: triple_bidir + dir-regime + F&G + three-tier decay,
funding modeled.

Run from project root:
    python3 research/ytd_pnl.py
"""
from __future__ import annotations

import sys as _sys, os as _os
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

import time
import numpy as np
import pandas as pd

from core.backtest_enhanced import EnhancedBTConfig, run_backtest_enhanced
from core.data import fetch_ohlcv
from core.regime_strategy import walk_forward_regimes
from core.risk import DEFAULT_DECAY_TIERS
from core.sentiment import align_to_bars, fetch_fear_greed
from core.strategies import triple_confirm_bidir

WINNERS = ["INJ-USDT", "SOL-USDT", "ADA-USDT", "ETH-USDT", "LINK-USDT"]
WEIGHTS = {"INJ-USDT": .40, "SOL-USDT": .20, "ADA-USDT": .15, "ETH-USDT": .15, "LINK-USDT": .10}


def apply_funding(eq, trades, bps=1.0):
    if not trades: return eq
    pb = (bps / 1e4) / 8; off = pd.Series(0.0, index=eq.index)
    for t in trades:
        off.loc[eq.index >= t.exit_time] -= pb * t.notional * t.bars_held * t.side
    return eq + off


def prod_signal(df, regs, fa):
    s = triple_confirm_bidir(df)
    a = regs.reindex(s.index, method="ffill").fillna("CHOP")
    b = ((s["signal"] == 1) & (~a.isin(["BULL","CHOP"]))) | \
        ((s["signal"] == -1) & (~a.isin(["BEAR","CHOP"])))
    s.loc[b, "signal"] = 0; s.loc[b, ["sl","tp"]] = np.nan
    if fa is not None:
        bf = ((s["signal"] == 1) & (fa >= 80)) | ((s["signal"] == -1) & (fa <= 20))
        s.loc[bf, "signal"] = 0; s.loc[bf, ["sl","tp"]] = np.nan
    return s


def equity_for(pair, fng):
    df = fetch_ohlcv(pair, "1h", days=4000)
    regs = walk_forward_regimes(df, bars_per_day=24, train_days=365, step_days=30)
    fa = align_to_bars(fng, df.index) if fng is not None else None
    sig = prod_signal(df, regs, fa)
    cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                           max_leverage=5.0, eq_decay_tiers=DEFAULT_DECAY_TIERS)
    eq, tr = run_backtest_enhanced(df, sig, cfg, long_only=False)
    return apply_funding(eq, tr)


def monthly(eq):
    return eq.resample("ME").last().pct_change().dropna() * 100


def print_year_table(name, eq, year=2026):
    m = monthly(eq)
    ytd = m[m.index.year == year]
    print(f"\n  {name} — {year} YTD month-by-month:")
    if len(ytd) == 0:
        print("    (no months)"); return
    for ts, v in ytd.items():
        bar = "#" * int(abs(v) / 2)
        sign = "+" if v >= 0 else "-"
        print(f"    {ts.strftime('%Y-%m')}:  {v:+6.2f}%   {sign}{bar}")
    comp = float((1 + ytd / 100).prod() - 1) * 100
    print(f"    {'':7s}  ------")
    print(f"    YTD compounded:  {comp:+.2f}%   "
          f"(mean {ytd.mean():+.2f}%/mo, {(ytd>0).sum()}/{len(ytd)} positive)")


def main():
    try:
        fng = fetch_fear_greed()
    except Exception:
        fng = None

    print("Computing equity curves (5 pairs + regimes) ...", flush=True)
    eqs = {}
    for p in WINNERS:
        t0 = time.time()
        eqs[p] = equity_for(p, fng)
        print(f"  {p} done ({time.time()-t0:.0f}s)", flush=True)

    # Portfolio (normalized, inj_heavy weights)
    common = eqs[WINNERS[0]].index
    for p in WINNERS[1:]:
        common = common.intersection(eqs[p].index)
    port = sum((eqs[p].reindex(common).ffill() / eqs[p].reindex(common).ffill().iloc[0])
               * WEIGHTS[p] for p in WINNERS) * 100

    print("\n" + "=" * 78)
    print("YEAR-TO-DATE MONTHLY PnL BREAKDOWN (2026)")
    print("production config: triple_bidir + dir-regime + F&G + 3-tier decay, funding modeled")
    print("=" * 78)

    print_year_table("INJ single-pair (adaptive_inj_bidir)", eqs["INJ-USDT"])
    print_year_table("5-pair portfolio (inj_heavy)", port)

    # Side-by-side YTD table
    inj_m = monthly(eqs["INJ-USDT"]); port_m = monthly(port)
    inj_ytd = inj_m[inj_m.index.year == 2026]; port_ytd = port_m[port_m.index.year == 2026]
    print("\n" + "=" * 78)
    print("SIDE BY SIDE (2026 YTD, %/month)")
    print("=" * 78)
    print(f"  {'month':9s}{'INJ single':>14s}{'portfolio':>14s}")
    for ts in inj_ytd.index:
        i = inj_ytd.get(ts, float('nan'))
        p = port_ytd.get(ts, float('nan'))
        print(f"  {ts.strftime('%Y-%m'):9s}{i:>+13.2f}%{p:>+13.2f}%")
    print(f"  {'':9s}{'':>14s}{'':>14s}")
    ic = float((1 + inj_ytd/100).prod()-1)*100
    pc = float((1 + port_ytd/100).prod()-1)*100
    print(f"  {'YTD':9s}{ic:>+13.2f}%{pc:>+13.2f}%")

    # Trailing 12 months for context
    print("\n" + "=" * 78)
    print("TRAILING 12 MONTHS (context)")
    print("=" * 78)
    print(f"  {'month':9s}{'INJ single':>14s}{'portfolio':>14s}")
    for ts in inj_m.tail(12).index:
        i = inj_m.get(ts, float('nan')); p = port_m.reindex([ts]).iloc[0] if ts in port_m.index else float('nan')
        print(f"  {ts.strftime('%Y-%m'):9s}{i:>+13.2f}%{p:>+13.2f}%")
    i12 = float((1 + inj_m.tail(12)/100).prod()-1)*100
    p12 = float((1 + port_m.tail(12)/100).prod()-1)*100
    print(f"  {'':9s}{'':>14s}{'':>14s}")
    print(f"  {'12mo':9s}{i12:>+13.2f}%{p12:>+13.2f}%")


if __name__ == "__main__":
    main()
