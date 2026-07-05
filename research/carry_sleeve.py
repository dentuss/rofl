"""Funding-carry sleeve — cross-sectional carry on perp funding rates.

We measured 40x dispersion in mean funding across pairs (0.04-1.69 bp/8h) and
found funding useless as a FAST signal (IC~0 at 8-72h). The documented crypto
premium is the SLOW cross-sectional version: be long the names whose funding
is cheap/negative (you get paid to hold) and short the names whose funding is
expensive (crowded longs pay you), harvesting carry + the crowding unwind.

PRE-REGISTERED design (vectorized daily prototype, single parameterization):
- Universe: the 23 structural qualifiers.
- Signal: trailing 7d sum of funding events per name, ranked daily.
- Portfolio: long the cheapest quintile, short the most expensive quintile,
  rebalanced WEEKLY; inverse-vol sizing to 20% ann per name, cap 2x.
- P&L: price return + funding received/paid, minus 8 bps per unit turnover.
- Judged: Sh(mo) full/IS/OOS, thirds, corr to trend book, corr to TSMOM-90,
  and 3-sleeve combo Sharpe (trend + TSMOM-90 + carry, inverse-vol weights).

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/carry_sleeve.py
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
from research.tsmom_sleeve import sleeve_returns, eq_from_rets, QUAL23, SOFT5

DAYS = int(_os.environ.get("DAYS", 2000))
COMMON_START = pd.Timestamp("2023-08-17", tz="UTC")
COST_TURN = 0.0008
VOL_TARGET_D = 0.20 / np.sqrt(365)
POS_CAP = 2.0
WARMUP_D = 365
BPD = 6


def carry_returns(closes: pd.DataFrame, fund_daily: pd.DataFrame) -> pd.Series:
    rets = closes.pct_change()
    f7 = fund_daily.reindex(closes.index).fillna(0.0).rolling(7).sum().shift(1)
    rank = f7.rank(axis=1, pct=True)
    raw = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    raw[rank <= 0.2] = 1.0          # cheap funding -> long
    raw[rank >= 0.8] = -1.0         # expensive funding -> short
    # weekly rebalance: hold the Monday snapshot all week
    is_reb = pd.Series(closes.index.dayofweek == 0, index=closes.index)
    sigw = raw.where(is_reb).ffill().fillna(0.0)
    vol = rets.shift(1).rolling(60, min_periods=40).std()
    w = (VOL_TARGET_D / vol).clip(upper=POS_CAP)
    pos = (sigw * w).fillna(0.0)
    n = closes.notna().sum(axis=1).clip(lower=1)
    gross = (pos * rets).sum(axis=1) / n
    turn = pos.diff().abs().sum(axis=1) / n
    fund_pnl = -(pos * fund_daily.reindex(closes.index).fillna(0.0)).sum(axis=1) / n
    return gross - turn * COST_TURN + fund_pnl


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print(f"CARRY SLEEVE  universe={len(QUAL23)}  7d lookback, weekly quintiles",
          flush=True)
    closes, fund_d = {}, {}
    for p in QUAL23:
        closes[p] = fetch_ohlcv_bybit(p, "1d", days=DAYS)["close"]
        f = fetch_funding(p, days=DAYS, source="auto")
        fund_d[p] = f["funding_rate"].resample("1D").sum() if f is not None and len(f) \
            else pd.Series(dtype=float)
    closes = pd.DataFrame(closes)
    fund_daily = pd.DataFrame(fund_d)

    # trend book (EW5/RSCD3) monthly returns for correlations
    print("  building EW5/RSCD3 trend book ...", flush=True)
    fng = fetch_fear_greed()
    t_eq = {}
    for p in SOFT5:
        df = fetch_ohlcv_bybit(p, "4h", days=DAYS)
        regs = walk_forward_regimes(df, bars_per_day=BPD, train_days=365, step_days=30)
        fa = align_to_bars(fng, df.index)
        fund = fetch_funding(p, days=DAYS, source="auto")
        sig = fng_persist(regime_mask(triple_confirm_bidir(df, tp_mult=6.0), regs), fa)
        a = regs.reindex(sig.index, method="ffill").fillna("CHOP")
        sig["risk_mult"] = np.where(a == "CHOP", 0.5, 1.0)
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                               max_leverage=5.0, eq_decay_tiers=DEFAULT_DECAY_TIERS,
                               cooldown_bars=3, fee_rate=FEE_TAKER,
                               fee_maker=FEE_MAKER, entry_style="maker_close")
        eq, tr = run_backtest_enhanced(df[df.index >= cut], sig[sig.index >= cut], cfg)
        t_eq[p] = apply_funding_real(eq, tr, fund)
    tidx = None
    for p in SOFT5:
        tidx = t_eq[p].index if tidx is None else tidx.intersection(t_eq[p].index)
    trend_mo = build(t_eq, {p: 0.2 for p in SOFT5}, tidx.sort_values()) \
        .resample("ME").last().pct_change().dropna()

    win = closes.index[closes.index >= COMMON_START]
    split = win[int(len(win) * 0.6)]
    r_carry = carry_returns(closes, fund_daily).reindex(win).fillna(0.0)
    r_tsmom = sleeve_returns(closes, fund_daily, 90).reindex(win).fillna(0.0)

    eq = eq_from_rets(r_carry)
    s = stats(eq)
    i = stats(eq_from_rets(r_carry[r_carry.index < split]))
    o = stats(eq_from_rets(r_carry[r_carry.index >= split]))
    mo_c = eq.resample("ME").last().pct_change().dropna()
    mo_t = eq_from_rets(r_tsmom).resample("ME").last().pct_change().dropna()
    b3 = [win[0] + (win[-1] - win[0]) * j / 3 for j in range(4)]
    th = "  ".join(
        f"{sharpe_m(eq_from_rets(r_carry[(r_carry.index >= b3[j]) & (r_carry.index < b3[j+1])])):+.2f}"
        for j in range(3))

    print("\n" + "=" * 88)
    print(f"CARRY SLEEVE  {win[0].date()}..{win[-1].date()}")
    print("=" * 88)
    print(f"  CAGR {s['cagr']*100:+.1f}%  Sh(mo) {s['sh_m']:+.2f}  "
          f"MDD {s['mdd']*100:.1f}%  worst {s['worst_mo']:+.1f}%  "
          f"IS {i['sh_m']:+.2f}  OOS {o['sh_m']:+.2f}  thirds {th}")
    a1, a2 = mo_c.align(trend_mo, join="inner")
    a3, a4 = mo_c.align(mo_t, join="inner")
    print(f"  corr vs trend {float(a1.corr(a2)):+.2f}   "
          f"corr vs TSMOM-90 {float(a3.corr(a4)):+.2f}")

    # 3-sleeve combo (inverse-vol monthly weights)
    dfm = pd.concat([trend_mo.rename("trend"), mo_t.rename("tsmom"),
                     mo_c.rename("carry")], axis=1, join="inner")
    wts = 1.0 / dfm.std()
    combo = (dfm * wts).sum(axis=1) / wts.sum()
    print(f"  3-sleeve combo Sh(mo): "
          f"{float(combo.mean() / combo.std() * np.sqrt(12)):+.2f}  "
          f"(trend-only reference {float(a2.mean() / a2.std() * np.sqrt(12)):+.2f})")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
