"""Final assembly — the full honest portfolio, all adopted layers:

  Sleeve 1 (trend):  MAJORS8 book (ex-ante liquidity top-8: BTC ETH SOL XRP
                     DOGE ADA LINK AVAX), 4h triple_bidir + regime + F&G +
                     decay + CHOP half-size + K=3 cooldown + vol targeting,
                     maker entries, real funding.
  Sleeve 2 (TSMOM):  sign of 90d return, 23-name universe, daily.
  Sleeve 3 (carry):  weekly funding-quintile long/short, 23 names.

Combination: agnostic inverse-vol weights on monthly returns (no
optimization). Reports each sleeve, pairwise correlations, and the assembled
portfolio's Sh(mo) full/IS/OOS + thirds — THE scoreboard number.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/assemble_portfolio.py
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
                                  sharpe_m, stats, build, FEE_TAKER, FEE_MAKER)
from research.vol_target import vt_mult
from research.tsmom_sleeve import sleeve_returns, eq_from_rets, QUAL23
from research.carry_sleeve import carry_returns

MAJORS8 = [f"{b}-USDT" for b in
           ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX"]]
DAYS = int(_os.environ.get("DAYS", 2000))
COMMON_START = pd.Timestamp("2023-08-17", tz="UTC")
WARMUP_D = 365
BPD = 6


def mo_stats(mo: pd.Series, label: str):
    sh = float(mo.mean() / mo.std() * np.sqrt(12)) if mo.std() > 0 else 0.0
    n = len(mo)
    split = int(n * 0.6)
    i, o = mo.iloc[:split], mo.iloc[split:]
    shi = float(i.mean() / i.std() * np.sqrt(12)) if i.std() > 0 else 0.0
    sho = float(o.mean() / o.std() * np.sqrt(12)) if o.std() > 0 else 0.0
    th = []
    for k in range(3):
        w = mo.iloc[k * n // 3:(k + 1) * n // 3]
        th.append(float(w.mean() / w.std() * np.sqrt(12)) if w.std() > 0 else 0.0)
    eq = (1 + mo).cumprod()
    mdd = float((eq / eq.cummax() - 1).min())
    print(f"  {label:22s} Sh {sh:+.2f}  IS {shi:+.2f}  OOS {sho:+.2f}  "
          f"mo-MDD {mdd*100:5.1f}%  worst-mo {mo.min()*100:+.1f}%  "
          f"thirds {'  '.join(f'{t:+.2f}' for t in th)}")
    return sh


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print("FINAL ASSEMBLY  trend(MAJORS8/RSCD3+VT) + TSMOM90 + carry", flush=True)
    fng = fetch_fear_greed()

    t_eq = {}
    for p in MAJORS8:
        df = fetch_ohlcv_bybit(p, "4h", days=DAYS)
        regs = walk_forward_regimes(df, bars_per_day=BPD, train_days=365, step_days=30)
        fa = align_to_bars(fng, df.index)
        fund = fetch_funding(p, days=DAYS, source="auto")
        sig = fng_persist(regime_mask(triple_confirm_bidir(df, tp_mult=6.0), regs), fa)
        a = regs.reindex(sig.index, method="ffill").fillna("CHOP")
        chop = np.where(a == "CHOP", 0.5, 1.0)
        vt = vt_mult(df).reindex(sig.index).fillna(1.0).to_numpy()
        sig["risk_mult"] = chop * vt
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

    dfm = pd.concat([trend_mo.rename("trend"), tsmom_mo.rename("tsmom"),
                     carry_mo.rename("carry")], axis=1, join="inner")
    print("\n" + "=" * 88)
    print(f"SLEEVES ({dfm.index[0].date()}..{dfm.index[-1].date()}, monthly)")
    print("=" * 88)
    for c in dfm.columns:
        mo_stats(dfm[c], c)
    print(f"\n  correlations: trend/tsmom {dfm['trend'].corr(dfm['tsmom']):+.2f}  "
          f"trend/carry {dfm['trend'].corr(dfm['carry']):+.2f}  "
          f"tsmom/carry {dfm['tsmom'].corr(dfm['carry']):+.2f}")

    wts = 1.0 / dfm.std()
    combo = (dfm * wts).sum(axis=1) / wts.sum()
    print(f"\n  inverse-vol weights: " +
          "  ".join(f"{c} {float(wts[c]/wts.sum()):.2f}" for c in dfm.columns))
    print("\n" + "=" * 88)
    print("ASSEMBLED PORTFOLIO (inverse-vol monthly weights, no optimization)")
    print("=" * 88)
    mo_stats(combo, "3-SLEEVE PORTFOLIO")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
