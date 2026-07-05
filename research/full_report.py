"""FULL PORTFOLIO REPORT — the classic stats table for the complete honest
program: per-sleeve stats, the assembled portfolio at deployable vol targets,
and the month-by-month grid. $2300 starting equity, all-in costs (maker
entries, real funding, entry-bar stops, 8bp/turnover on sleeve rebalances).

Sleeves: trend = MAJORS8/RSCD3+VT (4h), TSMOM-90 (1d), carry (weekly funding
quintiles). Combination: agnostic inverse-vol monthly weights. Vol-targeted
rows scale the combined monthly stream by a constant k = target / realized
full-window vol (presentation-level scaling; the deployed version would use
trailing vol — same first-order economics on perps, funding scales with k).

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/full_report.py
"""
from __future__ import annotations

import sys as _sys, os as _os
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

import numpy as np
import pandas as pd

import core.regime as _regime
from core.backtest_enhanced import EnhancedBTConfig, run_backtest_enhanced
from core.data import fetch_ohlcv_bybit
from core.funding import fetch_funding
from core.regime_strategy import walk_forward_regimes
from core.risk import DEFAULT_DECAY_TIERS
from core.sentiment import align_to_bars, fetch_fear_greed
from core.strategies import triple_confirm_bidir
from research.cost_engine import (apply_funding_real, regime_mask, fng_persist,
                                  build, FEE_TAKER, FEE_MAKER)
from research.vol_target import vt_mult
from research.tsmom_sleeve import sleeve_returns, eq_from_rets, QUAL23
from research.carry_sleeve import carry_returns

MAJORS8 = [f"{b}-USDT" for b in
           ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX"]]
TOTAL = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))
COMMON_START = pd.Timestamp("2023-08-17", tz="UTC")
WARMUP_D = 365
BPD = 6
VOL_TARGETS = (0.15, 0.25, 0.35)


def row_stats(mo: pd.Series):
    eq = TOTAL * (1 + mo).cumprod()
    yrs = len(mo) / 12
    final = float(eq.iloc[-1])
    cagr = (final / TOTAL) ** (1 / yrs) - 1
    sh = float(mo.mean() / mo.std() * np.sqrt(12)) if mo.std() > 0 else 0.0
    mdd = float((eq / eq.cummax() - 1).min())
    n = len(mo)
    i, o = mo.iloc[:int(n * 0.6)], mo.iloc[int(n * 0.6):]
    shi = float(i.mean() / i.std() * np.sqrt(12)) if i.std() > 0 else 0.0
    sho = float(o.mean() / o.std() * np.sqrt(12)) if o.std() > 0 else 0.0
    return dict(final=final, cagr=cagr, sh=sh, mdd=mdd,
                worst=float(mo.min()) * 100, med=float(mo.median()) * 100,
                pos=float((mo > 0).mean()) * 100, shi=shi, sho=sho)


def print_row(label, s):
    print(f"{label:24s}{s['final']:>9.0f}{s['cagr']*100:>8.1f}{s['sh']:>7.2f}"
          f"{s['mdd']*100:>7.1f}{s['worst']:>8.1f}{s['med']:>7.2f}"
          f"{s['pos']:>6.0f}{s['shi']:>7.2f}{s['sho']:>8.2f}")


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print("FULL REPORT — building sleeves ...", flush=True)
    fng = fetch_fear_greed()

    t_eq = {}
    for p in MAJORS8:
        df = fetch_ohlcv_bybit(p, "4h", days=DAYS)
        regs = walk_forward_regimes(df, bars_per_day=BPD, train_days=365, step_days=30)
        fa = align_to_bars(fng, df.index)
        fund = fetch_funding(p, days=DAYS, source="auto")
        sig = fng_persist(regime_mask(triple_confirm_bidir(df, tp_mult=6.0), regs), fa)
        a = regs.reindex(sig.index, method="ffill").fillna("CHOP")
        sig["risk_mult"] = np.where(a == "CHOP", 0.5, 1.0) * \
            vt_mult(df).reindex(sig.index).fillna(1.0).to_numpy()
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                               max_leverage=5.0, eq_decay_tiers=DEFAULT_DECAY_TIERS,
                               cooldown_bars=3, fee_rate=FEE_TAKER,
                               fee_maker=FEE_MAKER, entry_style="maker_close")
        eq, tr = run_backtest_enhanced(df[df.index >= cut], sig[sig.index >= cut], cfg)
        t_eq[p] = apply_funding_real(eq, tr, fund)
        print(f"  {p:10s} done", flush=True)
    tidx = None
    for p in MAJORS8:
        e = t_eq[p][t_eq[p].index >= COMMON_START]
        tidx = e.index if tidx is None else tidx.intersection(e.index)
    trend_mo = build(t_eq, {p: 1 / 8 for p in MAJORS8}, tidx.sort_values()) \
        .resample("ME").last().pct_change().dropna()

    closes, fund_d = {}, {}
    for p in QUAL23:
        closes[p] = fetch_ohlcv_bybit(p, "1d", days=DAYS)["close"]
        f = fetch_funding(p, days=DAYS, source="auto")
        fund_d[p] = f["funding_rate"].resample("1D").sum() if f is not None and len(f) \
            else pd.Series(dtype=float)
    closes = pd.DataFrame(closes)
    fund_daily = pd.DataFrame(fund_d)
    win = closes.index[closes.index >= COMMON_START]
    tsmom_mo = eq_from_rets(sleeve_returns(closes, fund_daily, 90).reindex(win)
                            .fillna(0.0)).resample("ME").last().pct_change().dropna()
    carry_mo = eq_from_rets(carry_returns(closes, fund_daily).reindex(win)
                            .fillna(0.0)).resample("ME").last().pct_change().dropna()

    dfm = pd.concat([trend_mo.rename("TREND (MAJORS8 4h)"),
                     tsmom_mo.rename("TSMOM-90 (1d)"),
                     carry_mo.rename("CARRY (funding L/S)")],
                    axis=1, join="inner")
    wts = 1.0 / dfm.std()
    combo = (dfm * wts).sum(axis=1) / wts.sum()
    realized = float(combo.std() * np.sqrt(12))

    hdr = (f"{'':24s}{'final$':>9s}{'CAGR%':>8s}{'Sh(mo)':>7s}{'MDD%':>7s}"
           f"{'worst%':>8s}{'med%':>7s}{'pos%':>6s}{'IS Sh':>7s}{'OOS Sh':>8s}")
    print("\n" + "=" * 92)
    print(f"FULL PORTFOLIO REPORT  {dfm.index[0].strftime('%Y-%m')}..".ljust(50)
          + f"start ${TOTAL:.0f}, all-in costs, {len(dfm)} months")
    print("=" * 92)
    print(hdr)
    print("-" * 92)
    for c in dfm.columns:
        print_row(c, row_stats(dfm[c]))
    print("-" * 92)
    print_row("ASSEMBLED (unit vol)", row_stats(combo))
    for vt in VOL_TARGETS:
        k = vt / realized
        print_row(f"ASSEMBLED @ {vt:.0%} vol (x{k:.1f})", row_stats(combo * k))
    print(f"\n  sleeve weights (inverse-vol): " +
          "  ".join(f"{c.split()[0]} {float(wts[c]/wts.sum()):.2f}" for c in dfm.columns))
    print(f"  realized combo vol {realized:.1%} ann; correlations "
          f"trend/tsmom {dfm.iloc[:,0].corr(dfm.iloc[:,1]):+.2f}, "
          f"trend/carry {dfm.iloc[:,0].corr(dfm.iloc[:,2]):+.2f}, "
          f"tsmom/carry {dfm.iloc[:,1].corr(dfm.iloc[:,2]):+.2f}")

    k25 = 0.25 / realized
    scaled = combo * k25
    print("\n" + "=" * 92)
    print(f"MONTH-BY-MONTH  (ASSEMBLED @ 25% vol, %)")
    print("=" * 92)
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    print(f"{'year':6s}" + "".join(f"{m:>7s}" for m in months) + f"{'YEAR':>9s}")
    for y, grp in scaled.groupby(scaled.index.year):
        cells = {ts.month: v for ts, v in grp.items()}
        line = f"{y:<6d}"
        for m in range(1, 13):
            line += f"{cells[m]*100:>7.1f}" if m in cells else f"{'':>7s}"
        yr = float((1 + grp).prod() - 1) * 100
        line += f"{yr:>9.1f}"
        print(line)


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
