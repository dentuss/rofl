"""TSMOM 1d sleeve — time-series momentum (Moskowitz/Ooi/Pedersen 2012 style)
as a SECOND, decorrelated return stream next to the 4h trend book.

PRE-REGISTERED design (vectorized daily prototype; if it clears the gates it
gets engineered properly):
- Universe: the 23 structural qualifiers from breadth_allin (history+funding
  gate, no performance picks).
- Signal: sign of the trailing k-day close-to-close return, k in {30, 90},
  plus ENS = average of the two signs (0 allowed = half size). Signal from
  day t-1 close -> position held on day t (strict shift, no look-ahead).
- Sizing: per-name inverse-vol to a 20% annualized target (60d trailing vol,
  computed through t-1), position capped at 2x. Equal capital across names.
- Costs: 8 bps per unit turnover (taker 6 + slip 2; daily rebal can't assume
  maker), REAL funding events applied with position sign.
- Judged: Sharpe(mo) full/IS/OOS (60/40), MDD, thirds, and the number that
  decides everything: monthly-return correlation to the EW5/RSCD3 trend book
  and the Sharpe of a 50/50 vol-weighted combination.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/tsmom_sleeve.py
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
from core.regime_strategy import walk_forward_regimes
from core.risk import DEFAULT_DECAY_TIERS
from core.sentiment import align_to_bars, fetch_fear_greed
from core.strategies import triple_confirm_bidir
from research.cost_engine import (apply_funding_real, regime_mask, fng_persist,
                                  sharpe_m, stats, build, FEE_TAKER, FEE_MAKER)

QUAL23 = [f"{b}-USDT" for b in
          ["BTC", "ETH", "SOL", "ADA", "LINK", "AVAX", "NEAR", "AAVE", "GRT",
           "RUNE", "DOGE", "DOT", "ATOM", "LTC", "XRP", "BNB", "FIL", "OP",
           "UNI", "ETC", "BCH", "TRX", "SAND"]]
SOFT5 = {"INJ-USDT": .25, "SOL-USDT": .1875, "ADA-USDT": .1875,
         "ETH-USDT": .1875, "LINK-USDT": .1875}
DAYS = int(_os.environ.get("DAYS", 2000))
COMMON_START = pd.Timestamp("2023-08-17", tz="UTC")
COST_TURN = 0.0008          # 8 bps per unit turnover
VOL_TARGET_D = 0.20 / np.sqrt(365)
POS_CAP = 2.0
WARMUP_D = 365
BPD = 6


def sleeve_returns(closes: pd.DataFrame, fund_daily: pd.DataFrame, k) -> pd.Series:
    """Daily portfolio returns of the TSMOM sleeve. k = int or 'ENS'."""
    rets = closes.pct_change()
    if k == "ENS":
        sig = (np.sign(closes.shift(1) / closes.shift(31) - 1)
               + np.sign(closes.shift(1) / closes.shift(91) - 1)) / 2.0
    else:
        sig = np.sign(closes.shift(1) / closes.shift(k + 1) - 1)
    vol = rets.shift(1).rolling(60, min_periods=40).std()
    w = (VOL_TARGET_D / vol).clip(upper=POS_CAP)
    pos = (sig * w).fillna(0.0)
    n = closes.notna().sum(axis=1).clip(lower=1)
    gross = (pos * rets).sum(axis=1) / n
    turn = pos.diff().abs().sum(axis=1) / n
    fund_pnl = -(pos * fund_daily.reindex(closes.index).fillna(0.0)).sum(axis=1) / n
    return gross - turn * COST_TURN + fund_pnl


def eq_from_rets(r: pd.Series) -> pd.Series:
    return 100.0 * (1.0 + r.fillna(0.0)).cumprod()


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print(f"TSMOM 1d SLEEVE  universe={len(QUAL23)}  k=30/90/ENS", flush=True)

    closes, fund_d = {}, {}
    for p in QUAL23:
        df = fetch_ohlcv_bybit(p, "1d", days=DAYS)
        closes[p] = df["close"]
        f = fetch_funding(p, days=DAYS, source="auto")
        fund_d[p] = f["funding_rate"].resample("1D").sum() if f is not None and len(f) \
            else pd.Series(dtype=float)
    closes = pd.DataFrame(closes)
    fund_daily = pd.DataFrame(fund_d)
    print(f"  data: {closes.index[0].date()}..{closes.index[-1].date()}", flush=True)

    # Trend book (EW5/RSCD3) for the correlation/combination test
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
    trend_eq = build(t_eq, {p: 0.2 for p in SOFT5}, tidx.sort_values())
    trend_mo = trend_eq.resample("ME").last().pct_change().dropna()

    win = closes.index[closes.index >= COMMON_START]
    split = win[int(len(win) * 0.6)]

    print("\n" + "=" * 88)
    print(f"TSMOM SLEEVE  {win[0].date()}..{win[-1].date()}  "
          f"(costs 8bp/turnover + real funding)")
    print("=" * 88)
    print(f"{'k':6s}{'CAGR%':>8s}{'Sh(mo)':>8s}{'MDD%':>7s}{'worst':>7s}"
          f"{'IS Sh':>7s}{'OOS Sh':>8s}{'corr':>7s}{'combo Sh':>10s}{'thirds':>22s}")
    for k in (30, 90, "ENS"):
        r = sleeve_returns(closes, fund_daily, k).reindex(win).fillna(0.0)
        eq = eq_from_rets(r)
        s = stats(eq)
        i = stats(eq_from_rets(r[r.index < split]))
        o = stats(eq_from_rets(r[r.index >= split]))
        mo = eq.resample("ME").last().pct_change().dropna()
        al, tr_ = mo.align(trend_mo, join="inner")
        corr = float(al.corr(tr_)) if len(al) > 3 else float("nan")
        # 50/50 monthly-vol-weighted combination
        wt_s = 1.0 / mo.std() if mo.std() > 0 else 0
        wt_t = 1.0 / tr_.std() if tr_.std() > 0 else 0
        combo = (al * wt_s + tr_ * wt_t) / (wt_s + wt_t)
        combo_sh = float(combo.mean() / combo.std() * np.sqrt(12)) if combo.std() > 0 else 0
        b3 = [win[0] + (win[-1] - win[0]) * j / 3 for j in range(4)]
        th = "  ".join(
            f"{sharpe_m(eq_from_rets(r[(r.index >= b3[j]) & (r.index < b3[j+1])])):+.2f}"
            for j in range(3))
        print(f"{str(k):6s}{s['cagr']*100:8.1f}{s['sh_m']:8.2f}{s['mdd']*100:7.1f}"
              f"{s['worst_mo']:7.1f}{i['sh_m']:7.2f}{o['sh_m']:8.2f}{corr:7.2f}"
              f"{combo_sh:10.2f}{th:>22s}")
    tsh = sharpe_m(trend_eq[trend_eq.index >= COMMON_START])
    print(f"\n  reference: EW5/RSCD3 trend book Sh(mo) {tsh:.2f} on the same window")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
