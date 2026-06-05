"""Experiment: soften the F&G extreme filter.

The current production filter blocks longs at F&G >= 80 and shorts at F&G <= 20.
The user observation: during prolonged bear stretches, blocking shorts at
extreme fear may cost real short-side profit if the move continues.

Tests across the 5 winner pairs, with the production stack
(triple_bidir + dir-regime + decay + funding):

  baseline       block long>=80, short<=20  (current)
  no_fng         filter OFF entirely (research-only ceiling)
  tight 85/15    looser long block, looser short block
  tight 90/10    block only the MOST extreme zones
  asym_long_only block long tops, ALLOW shorts at extreme fear
  asym_short_only block short bottoms, ALLOW longs at extreme greed
  persist_3d     only block when F&G has been extreme >=3 consecutive days

For each variant we report:
  - portfolio Sharpe / CAGR / MDD / worst month
  - how many trades were "rescued" (allowed back in vs baseline)
  - those rescued trades' OWN win rate and total $ PnL — this directly answers
    "would the trades the filter blocked have been profitable?"

Run from project root:
    python3 research/test_fng_soften.py
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
WEIGHTS = {"INJ-USDT": .40, "SOL-USDT": .20, "ADA-USDT": .15,
           "ETH-USDT": .15, "LINK-USDT": .10}


def apply_funding(eq, trades, bps=1.0):
    if not trades:
        return eq
    pb = (bps / 1e4) / 8
    off = pd.Series(0.0, index=eq.index)
    for t in trades:
        off.loc[eq.index >= t.exit_time] -= pb * t.notional * t.bars_held * t.side
    return eq + off


def regime_filtered(df, regs):
    s = triple_confirm_bidir(df)
    a = regs.reindex(s.index, method="ffill").fillna("CHOP")
    b = ((s["signal"] == 1) & (~a.isin(["BULL", "CHOP"]))) | \
        ((s["signal"] == -1) & (~a.isin(["BEAR", "CHOP"])))
    s.loc[b, "signal"] = 0
    s.loc[b, ["sl", "tp"]] = np.nan
    return s


def fng_persistence(fa: pd.Series, hi: float, lo: float, days: int = 3):
    """Per-bar: True if F&G has been outside (lo, hi) for >=days consecutive."""
    bars = days * 24  # 1h bars
    above = (fa >= hi).rolling(bars, min_periods=bars).sum() == bars
    below = (fa <= lo).rolling(bars, min_periods=bars).sum() == bars
    return above, below


def fng_variant(sig_in, fa, name):
    """Apply a particular F&G filter variant to a regime-filtered signal."""
    s = sig_in.copy()
    if fa is None or name == "no_fng":
        return s
    if name == "baseline":
        hi, lo = 80, 20
        bl = (s["signal"] == 1) & (fa >= hi)
        bs = (s["signal"] == -1) & (fa <= lo)
    elif name == "tight_85_15":
        bl = (s["signal"] == 1) & (fa >= 85)
        bs = (s["signal"] == -1) & (fa <= 15)
    elif name == "tight_90_10":
        bl = (s["signal"] == 1) & (fa >= 90)
        bs = (s["signal"] == -1) & (fa <= 10)
    elif name == "asym_long_only":
        bl = (s["signal"] == 1) & (fa >= 80)
        bs = pd.Series(False, index=s.index)
    elif name == "asym_short_only":
        bl = pd.Series(False, index=s.index)
        bs = (s["signal"] == -1) & (fa <= 20)
    elif name == "persist_3d":
        above, below = fng_persistence(fa, 80, 20, days=3)
        bl = (s["signal"] == 1) & above.fillna(False)
        bs = (s["signal"] == -1) & below.fillna(False)
    else:
        return s
    block = bl | bs
    s.loc[block, "signal"] = 0
    s.loc[block, ["sl", "tp"]] = np.nan
    return s


VARIANTS = ["baseline", "no_fng", "tight_85_15", "tight_90_10",
            "asym_long_only", "asym_short_only", "persist_3d"]


def cstats(eq, trades, bpd=24):
    final = float(eq.iloc[-1])
    ret = float(final / eq.iloc[0] - 1)
    mdd = float((eq / eq.cummax() - 1).min())
    br = eq.pct_change().fillna(0)
    sharpe = float(br.mean() / br.std() * np.sqrt(bpd * 365)) if br.std() > 0 else 0
    days = (eq.index[-1] - eq.index[0]).days
    cagr = (final / eq.iloc[0]) ** (365 / max(days, 1)) - 1
    months = eq.resample("ME").last().pct_change().dropna() * 100
    return dict(final=final, ret=ret, cagr=cagr, mdd=mdd, sharpe=sharpe,
                worst_mo=float(months.min()) if len(months) else 0,
                med_mo=float(months.median()) if len(months) else 0,
                win_mo=float((months > 0).mean() * 100) if len(months) else 0,
                n=len(trades),
                n_long=sum(1 for t in trades if t.side == 1),
                n_short=sum(1 for t in trades if t.side == -1))


def trades_set(trades):
    """Identify a trade by (entry_time, side) for comparison across variants."""
    return {(t.entry_time, t.side): t for t in trades}


def main():
    days = int(_os.environ.get("DAYS", 5 * 365))
    fng = fetch_fear_greed()

    # Show recent F&G context — what's the bot seeing right now?
    print("Recent F&G readings (last 14 days):")
    for ts, val in fng["fng"].tail(14).items():
        tag = ""
        if val >= 80: tag = "  <- BLOCKED LONG (current rule)"
        elif val <= 20: tag = "  <- BLOCKED SHORT (current rule)"
        print(f"  {ts.strftime('%Y-%m-%d')}: F&G={int(val):3d}{tag}")
    print()

    cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                           max_leverage=5.0, eq_decay_tiers=DEFAULT_DECAY_TIERS)

    # Per-pair: precompute regime-filtered signal + F&G aligned series
    pair_inputs = {}
    pair_trades = {}  # variant -> pair -> trades
    pair_eqs = {}     # variant -> pair -> eq
    for v in VARIANTS:
        pair_trades[v] = {}
        pair_eqs[v] = {}

    for p in WINNERS:
        print(f"--- {p} ---", flush=True)
        df = fetch_ohlcv(p, "1h", days=days)
        t0 = time.time()
        regs = walk_forward_regimes(df, bars_per_day=24, train_days=365, step_days=30)
        fa = align_to_bars(fng, df.index)
        sig_rg = regime_filtered(df, regs)
        pair_inputs[p] = (df, sig_rg, fa)
        print(f"  regimes {time.time()-t0:.0f}s", flush=True)
        for v in VARIANTS:
            s = fng_variant(sig_rg, fa, v)
            eq, tr = run_backtest_enhanced(df, s, cfg, long_only=False)
            eq = apply_funding(eq, tr)
            pair_trades[v][p] = tr
            pair_eqs[v][p] = eq

    # Per-pair table (INJ only for brevity)
    print("\n" + "=" * 100)
    print("INJ — by F&G variant (5y, $100 start, production stack)")
    print("=" * 100)
    print(f"{'variant':18s}{'final$':>9s}{'CAGR':>8s}{'MDD':>8s}{'Sharpe':>9s}"
          f"{'trades':>8s}{'long':>7s}{'short':>7s}{'worst_mo':>10s}")
    base_inj = cstats(pair_eqs["baseline"]["INJ-USDT"], pair_trades["baseline"]["INJ-USDT"])
    for v in VARIANTS:
        s = cstats(pair_eqs[v]["INJ-USDT"], pair_trades[v]["INJ-USDT"])
        d_n = s['n'] - base_inj['n']
        tag = f"  ({d_n:+d} trades vs baseline)" if v != "baseline" else "  (reference)"
        print(f"{v:18s}{s['final']:>9.0f}{s['cagr']*100:>+7.0f}%{s['mdd']*100:>+7.1f}%"
              f"{s['sharpe']:>9.2f}{s['n']:>8d}{s['n_long']:>7d}{s['n_short']:>7d}"
              f"{s['worst_mo']:>+9.1f}%{tag}")

    # Portfolio (inj_heavy normalized weighted sum)
    print("\n" + "=" * 100)
    print("5-PAIR PORTFOLIO (inj_heavy) — by F&G variant")
    print("=" * 100)
    print(f"{'variant':18s}{'final$':>9s}{'CAGR':>8s}{'MDD':>8s}{'Sharpe':>9s}"
          f"{'worst_mo':>10s}{'win_mo%':>9s}")

    def portfolio_eq(variant):
        common = pair_eqs[variant][WINNERS[0]].index
        for p in WINNERS[1:]:
            common = common.intersection(pair_eqs[variant][p].index)
        return sum((pair_eqs[variant][p].reindex(common).ffill()
                    / pair_eqs[variant][p].reindex(common).ffill().iloc[0]) * WEIGHTS[p]
                   for p in WINNERS) * 100

    base_port = cstats(portfolio_eq("baseline"), [])
    rows = []
    for v in VARIANTS:
        s = cstats(portfolio_eq(v), [])
        d_sharpe = s["sharpe"] - base_port["sharpe"]
        d_mdd = (s["mdd"] - base_port["mdd"]) * 100
        d_ret = (s["ret"] - base_port["ret"]) * 100
        verdict = "(reference)" if v == "baseline" else \
                  "WORKED" if (d_sharpe >= 0.05 and s["ret"] >= 0.95 * base_port["ret"]) \
                  else "neutral" if (abs(d_sharpe) < 0.05 and abs(d_mdd) < 2) \
                  else "WORSE"
        print(f"{v:18s}{s['final']:>9.0f}{s['cagr']*100:>+7.0f}%{s['mdd']*100:>+7.1f}%"
              f"{s['sharpe']:>9.2f}{s['worst_mo']:>+9.1f}%{s['win_mo']:>8.0f}%"
              f"  {verdict}  Δret {d_ret:+.0f}pp  Δsharpe {d_sharpe:+.2f}")

    # CRITICAL: were the "rescued" trades profitable? Diff vs baseline on INJ
    print("\n" + "=" * 100)
    print("RESCUED-TRADES P/L (trades a variant allows that baseline blocks)")
    print("INJ 5y — directly answers: would the blocked trades have made money?")
    print("=" * 100)
    base_keys = trades_set(pair_trades["baseline"]["INJ-USDT"])
    for v in VARIANTS:
        if v == "baseline":
            continue
        vtrades = pair_trades[v]["INJ-USDT"]
        rescued = [t for t in vtrades if (t.entry_time, t.side) not in base_keys]
        if not rescued:
            print(f"  {v:18s}  no rescued trades")
            continue
        long_rescued = [t for t in rescued if t.side == 1]
        short_rescued = [t for t in rescued if t.side == -1]
        total_pnl = sum(t.pnl for t in rescued)
        wr = sum(1 for t in rescued if t.pnl > 0) / len(rescued)
        l_pnl = sum(t.pnl for t in long_rescued)
        s_pnl = sum(t.pnl for t in short_rescued)
        tag = "(WORTH RESCUING)" if total_pnl > 0 else "(trap — would have lost)"
        print(f"  {v:18s}  n={len(rescued)}  wr={wr*100:.0f}%  "
              f"total ${total_pnl:+.1f}  (long ${l_pnl:+.1f} from {len(long_rescued)}, "
              f"short ${s_pnl:+.1f} from {len(short_rescued)})  {tag}")


if __name__ == "__main__":
    main()
