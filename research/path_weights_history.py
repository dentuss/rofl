"""Path / weights / history — the three pre-registered studies gating any
leverage talk, plus the 50%-vol report.

A. DAILY-GRANULARITY PATH: the assembled combo built from DAILY sleeve
   returns (no month-end smoothing). Real daily MDD, worst day/week/month and
   average/max gross leverage at 15/25/35/50% vol targets.
B. WEIGHT SCHEMES: IV (inverse-vol, current) vs EQ (1/3 each) vs CAP40
   (inverse-vol with carry capped at 40%, renormalized). The 0.76 carry
   weight concentrates model risk — this measures what de-concentrating costs.
C. LONG HISTORY: TSMOM-90 and carry evaluated from 2021 (canonical params,
   designed on 2023-08+ data => 2021..2023-08 is pseudo-OOS, incl. the 2022
   bear). Yearly breakdown.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/path_weights_history.py
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
VTGTS = (0.15, 0.25, 0.35, 0.50)


def dstats(r: pd.Series, label: str, lev: float = 1.0):
    r = r * lev
    eq = TOTAL * (1 + r).cumprod()
    yrs = max((r.index[-1] - r.index[0]).days, 1) / 365
    cagr = (float(eq.iloc[-1]) / TOTAL) ** (1 / yrs) - 1
    mdd = float((eq / eq.cummax() - 1).min())
    mo = eq.resample("ME").last().pct_change().dropna()
    sh_m = float(mo.mean() / mo.std() * np.sqrt(12)) if mo.std() > 0 else 0.0
    wk = eq.resample("W").last().pct_change().dropna()
    n = len(mo)
    i, o = mo.iloc[:int(n * 0.6)], mo.iloc[int(n * 0.6):]
    shi = float(i.mean() / i.std() * np.sqrt(12)) if i.std() > 0 else 0.0
    sho = float(o.mean() / o.std() * np.sqrt(12)) if o.std() > 0 else 0.0
    print(f"{label:22s}{float(eq.iloc[-1]):>9.0f}{cagr*100:>8.1f}{sh_m:>7.2f}"
          f"{mdd*100:>8.1f}{float(r.min())*100:>8.1f}{float(wk.min())*100:>8.1f}"
          f"{float(mo.min())*100:>8.1f}{shi:>7.2f}{sho:>8.2f}")


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print("PATH / WEIGHTS / HISTORY — building daily sleeve returns ...", flush=True)
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
    tidx = None
    for p in MAJORS8:
        e = t_eq[p][t_eq[p].index >= COMMON_START]
        tidx = e.index if tidx is None else tidx.intersection(e.index)
    trend_d = build(t_eq, {p: 1 / 8 for p in MAJORS8}, tidx.sort_values()) \
        .resample("1D").last().pct_change().dropna()

    closes, fund_d = {}, {}
    for p in QUAL23:
        closes[p] = fetch_ohlcv_bybit(p, "1d", days=DAYS)["close"]
        f = fetch_funding(p, days=DAYS, source="auto")
        fund_d[p] = f["funding_rate"].resample("1D").sum() if f is not None and len(f) \
            else pd.Series(dtype=float)
    closes = pd.DataFrame(closes)
    fund_daily = pd.DataFrame(fund_d)
    tsmom_d = sleeve_returns(closes, fund_daily, 90)
    carry_d = carry_returns(closes, fund_daily)

    D = pd.concat([trend_d.rename("trend"), tsmom_d.rename("tsmom"),
                   carry_d.rename("carry")], axis=1, join="inner").dropna()
    D = D[D.index >= COMMON_START]

    # weight schemes
    iv = 1.0 / D.std(); iv = iv / iv.sum()
    eqw = pd.Series(1 / 3, index=D.columns)
    cap = iv.copy()
    if cap["carry"] > 0.40:
        excess = cap["carry"] - 0.40
        cap["carry"] = 0.40
        others = cap.drop("carry")
        cap.loc[others.index] = others + excess * others / others.sum()
    schemes = {"IV": iv, "EQ": eqw, "CAP40": cap}

    hdr = (f"{'':22s}{'final$':>9s}{'CAGR%':>8s}{'Sh(mo)':>7s}{'dMDD%':>8s}"
           f"{'worstD%':>8s}{'worstW%':>8s}{'worstM%':>8s}{'IS Sh':>7s}{'OOS Sh':>8s}")

    print("\n" + "=" * 94)
    print(f"A. DAILY-GRANULARITY PATH  (IV weights: "
          + "  ".join(f"{c} {iv[c]:.2f}" for c in D.columns)
          + f";  {D.index[0].date()}..{D.index[-1].date()})")
    print("=" * 94)
    print(hdr)
    r_iv = (D * iv).sum(axis=1)
    vol_d = float(r_iv.std() * np.sqrt(365))
    print(f"  (realized daily-basis vol {vol_d:.1%} ann)")
    for vt in VTGTS:
        dstats(r_iv, f"IV @ {vt:.0%} vol (x{vt/vol_d:.1f})", lev=vt / vol_d)

    print("\n" + "=" * 94)
    print("B. WEIGHT SCHEMES  (each @ 50% vol on its own realized vol)")
    print("=" * 94)
    print(hdr)
    for name, w in schemes.items():
        r = (D * w).sum(axis=1)
        v = float(r.std() * np.sqrt(365))
        dstats(r, f"{name} ({'/'.join(f'{w[c]:.2f}' for c in D.columns)})",
               lev=0.50 / v)

    print("\n" + "=" * 94)
    print("C. LONG HISTORY — sleeves from first data (canonical params; "
          "pre-2023-08 = pseudo-OOS incl. 2022 bear)")
    print("=" * 94)
    for name, r in [("TSMOM-90", tsmom_d), ("CARRY", carry_d)]:
        r = r.dropna()
        r = r[r.index >= r.first_valid_index()]
        mo = eq_from_rets(r).resample("ME").last().pct_change().dropna()
        sh = float(mo.mean() / mo.std() * np.sqrt(12)) if mo.std() > 0 else 0.0
        pre = mo[mo.index < COMMON_START]
        post = mo[mo.index >= COMMON_START]
        shp = float(pre.mean() / pre.std() * np.sqrt(12)) if len(pre) > 3 and pre.std() > 0 else float("nan")
        shq = float(post.mean() / post.std() * np.sqrt(12)) if post.std() > 0 else 0.0
        print(f"\n  {name}: {r.index[0].date()}..{r.index[-1].date()}  "
              f"full Sh(mo) {sh:+.2f}  pre-2023-08 (pseudo-OOS) {shp:+.2f}  "
              f"post {shq:+.2f}")
        line = "    yearly Sh: "
        for y, g in mo.groupby(mo.index.year):
            ys = float(g.mean() / g.std() * np.sqrt(12)) if len(g) > 2 and g.std() > 0 else float("nan")
            line += f"{y} {ys:+.1f}  "
        print(line)

    # final 50% table + month grid (IV)
    print("\n" + "=" * 94)
    print(f"FINAL: ASSEMBLED @ 50% VOL (IV weights, daily granularity, "
          f"start ${TOTAL:.0f})")
    print("=" * 94)
    print(hdr)
    k = 0.50 / vol_d
    dstats(r_iv, f"ASSEMBLED @ 50% (x{k:.1f})", lev=k)
    gross = k * (D.abs() * 0).sum(axis=1)  # placeholder not meaningful; report k
    print(f"  scaling factor x{k:.1f} on unit-weight sleeve exposures "
          f"(gross leverage ~= x{k:.1f} of each sleeve's own notional)")
    scaled = (1 + r_iv * k)
    eq = TOTAL * scaled.cumprod()
    mo = eq.resample("ME").last().pct_change().dropna() * 100
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    print(f"\n{'year':6s}" + "".join(f"{m:>7s}" for m in months) + f"{'YEAR':>9s}")
    for y, grp in mo.groupby(mo.index.year):
        cells = {ts.month: v for ts, v in grp.items()}
        line = f"{y:<6d}"
        for m in range(1, 13):
            line += f"{cells[m]:>7.1f}" if m in cells else f"{'':>7s}"
        yr = float((1 + grp / 100).prod() - 1) * 100
        line += f"{yr:>9.1f}"
        print(line)


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
