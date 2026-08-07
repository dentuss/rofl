"""PULL demotion trigger — how much evidence does the current rule actually see?

A REPORT, not an experiment. No cells, no strategy claim. It measures the
SAMPLE SIZE behind an existing pre-registered kill criterion so the criterion
can be specified on evidence instead of on a round number.

The rule today (SESSIONHANDOFF §1, ROADMAP Phase 6):
    trailing-3-month forward Sharpe < 0  ->  drop to BLEND75 or triple-only

FINDINGS 2026-08-03 flagged that it reads -3.93 on the backtest analogue while
three of the last six PULL months contain no trades at all. A Sharpe computed
from ~2 non-zero monthly observations cannot distinguish the pre-2023 -2.57
disease from an ordinary quiet stretch, in EITHER direction — it will fire on
noise and it will also miss real decay.

This script answers the only question that lets us fix it:
  * how many PULL trades actually close per month, and per rolling 3 months?
  * how often would a 3-month window contain too few to mean anything?
  * what trade count N makes a cumulative-R rule meaningfully powered?

Config is exactly the deployed pull leg (research/deploy_report.py): MAJORS8,
pullback_in_trend tp6, walk-forward GMM mask, F&G persistence, decay tiers,
CHOP half-size, VT, CONF sizing, maker entries, TP-as-limit, real funding.

Run:  PYTHONIOENCODING=utf-8 ./.venv/bin/python research/pull_trigger_power.py
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
from core.data import fetch_ohlcv_bybit
from core.funding import fetch_funding
from core.risk import DEFAULT_DECAY_TIERS
from core.sentiment import align_to_bars, fetch_fear_greed
from core.strategies import pullback_in_trend
from research.cost_engine import regime_mask, fng_persist, FEE_TAKER, FEE_MAKER
from research.regime_upgrades import wf_labels_conf
from research.vol_target import vt_mult

MAJORS8 = [f"{b}-USDT" for b in
           ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX"]]
DAYS = int(_os.environ.get("DAYS", 2000))
WARMUP_D, BPD = 365, 6
COMMON_START = pd.Timestamp("2023-08-17", tz="UTC")


def main() -> None:
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print("PULL TRIGGER POWER — trade counts behind the demotion rule", flush=True)
    fng = fetch_fear_greed()

    trades = []
    for p in MAJORS8:
        t0 = time.time()
        df = fetch_ohlcv_bybit(p, "4h", days=DAYS)
        regs, conf = wf_labels_conf(df, BPD)
        fa = align_to_bars(fng, df.index)
        fetch_funding(p, days=DAYS, source="auto")     # cache warm; R is pre-funding
        a = regs.reindex(df.index, method="ffill").fillna("CHOP")
        mult = np.where(a == "CHOP", 0.5, 1.0) * \
            vt_mult(df).reindex(df.index).fillna(1.0).to_numpy() * \
            (0.5 + 0.5 * conf.reindex(df.index).fillna(1.0).to_numpy())
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]
        sig = fng_persist(regime_mask(pullback_in_trend(df), regs), fa)
        sig["risk_mult"] = mult
        cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                               max_leverage=5.0, eq_decay_tiers=DEFAULT_DECAY_TIERS,
                               cooldown_bars=3, fee_rate=FEE_TAKER,
                               fee_maker=FEE_MAKER, entry_style="maker_close",
                               tp_as_limit=True)
        _, tr = run_backtest_enhanced(dfe, sig[sig.index >= cut], cfg)
        for t in tr:
            stop = abs(t.entry_px - t.sl) / t.entry_px
            trades.append(dict(pair=p, exit_time=t.exit_time, reason=t.reason,
                               r=(t.pnl / (t.notional * stop)) if stop > 0 else 0.0))
        print(f"  {p:10s} {len(tr):3d} trades  {time.time()-t0:4.0f}s", flush=True)

    td = pd.DataFrame(trades)
    td = td[td.exit_time >= COMMON_START].sort_values("exit_time")
    td["month"] = td.exit_time.dt.to_period("M")
    n_mo = td.groupby("month").size()
    full = pd.period_range(td.month.min(), td.month.max(), freq="M")
    n_mo = n_mo.reindex(full, fill_value=0)

    print("\n" + "=" * 84)
    print(f"1) PULL TRADE ARRIVALS — {len(td)} closes over {len(full)} months, "
          f"book-wide (all 8 names)")
    print("=" * 84)
    print(f"  per month: mean {n_mo.mean():.1f}  median {n_mo.median():.0f}  "
          f"min {n_mo.min()}  max {n_mo.max()}")
    print(f"  months with ZERO trades: {(n_mo == 0).sum()} of {len(n_mo)} "
          f"({100*(n_mo==0).mean():.0f}%)")
    print("  monthly counts: " + " ".join(str(int(v)) for v in n_mo.values))

    roll3 = n_mo.rolling(3).sum().dropna()
    print(f"\n  trailing-3-MONTH trade count: mean {roll3.mean():.1f}  "
          f"median {roll3.median():.0f}  min {roll3.min():.0f}  max {roll3.max():.0f}")
    for thr in (5, 10, 20):
        print(f"    windows with < {thr:2d} trades: {(roll3 < thr).sum():2d} of "
              f"{len(roll3)} ({100*(roll3 < thr).mean():3.0f}%)")

    print("\n" + "=" * 84)
    print("2) HOW LONG TO ACCUMULATE N TRADES (at the measured arrival rate)")
    print("=" * 84)
    rate = n_mo.mean()
    for n in (10, 20, 30, 50):
        print(f"  N={n:3d} trades -> {n/rate:5.1f} months of live record")

    print("\n" + "=" * 84)
    print("3) NOISE FLOOR — what does cumulative R do by chance?")
    print("=" * 84)
    r = td.r.to_numpy()
    print(f"  per-trade R: mean {r.mean():+.3f}  sd {r.std():.3f}  n={len(r)}")
    print(f"  win rate {100*(r > 0).mean():.0f}%  "
          f"(tp {100*(td.reason=='tp').mean():.0f}% / sl {100*(td.reason=='sl').mean():.0f}%)")
    rng = np.random.default_rng(42)
    print(f"\n  {'N':>5s}{'P(cumR<0 | true mean)':>24s}{'5th pct cumR':>15s}"
          f"{'95th pct cumR':>15s}")
    for n in (5, 10, 20, 30, 50):
        draws = rng.choice(r, size=(20000, n), replace=True).sum(axis=1)
        print(f"  {n:>5d}{100*(draws < 0).mean():>23.0f}%"
              f"{np.percentile(draws, 5):>15.1f}{np.percentile(draws, 95):>15.1f}")
    print("\n  Read the first column as the FALSE-DEMOTION rate: the chance a")
    print("  leg with this leg's own historical edge still shows cumulative")
    print("  R < 0 over N trades purely by chance. That is the number the")
    print("  trigger's N has to buy down.")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
