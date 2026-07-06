"""Sleeve diagnosis + conditioned redesign — why did TSMOM-90 and carry bleed
before 2023-08, and do CONDITIONED versions pass the full-history gate?

DIAGNOSIS (measurements, not cells):
- universe thinness by year (# names with data)
- carry: cross-sectional funding dispersion by year + corr(sleeve month,
  lagged dispersion) — is carry only paid when dispersion is fat?
- tsmom: whipsaw rate (sign flips/name/year) + corr(sleeve month, |BTC month|)
  — does it die in crash-chop?

PRE-REGISTERED conditioned variants (gates are rolling quantiles, lagged,
no fitted constants; early history defaults to TRADING so the gate cannot
hide the bleed by construction):
  CARRY_GATED   position x0 when yesterday's funding dispersion (cross-sec
                std of the 7d signal) <= its trailing 1y median
  TSMOM_STRONG  positions only in names whose |90d return| is in the top
                half cross-sectionally (trade strong trends, skip drift)
  TSMOM_CALM    flat when BTC 30d vol > its trailing 1y 80th pct (crash-chop)

PASS BAR (full-history gate): full Sh(mo) >= 0.5 AND pre-2023-08 >= 0.0
without destroying the post period. Variants that pass get re-assembled with
the trend book on the common window.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/sleeve_diagnosis.py
"""
from __future__ import annotations

import sys as _sys, os as _os
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

import numpy as np
import pandas as pd

from core.data import fetch_ohlcv_bybit
from core.funding import fetch_funding
from research.tsmom_sleeve import QUAL23, VOL_TARGET_D, POS_CAP, eq_from_rets

DAYS = int(_os.environ.get("DAYS", 2000))
COMMON_START = pd.Timestamp("2023-08-17", tz="UTC")
COST_TURN = 0.0008


def load():
    closes, fund_d = {}, {}
    for p in QUAL23:
        closes[p] = fetch_ohlcv_bybit(p, "1d", days=DAYS)["close"]
        f = fetch_funding(p, days=DAYS, source="auto")
        fund_d[p] = f["funding_rate"].resample("1D").sum() if f is not None and len(f) \
            else pd.Series(dtype=float)
    return pd.DataFrame(closes), pd.DataFrame(fund_d)


def port_returns(closes, fund_daily, pos):
    """Daily portfolio return from a position matrix (already lagged)."""
    rets = closes.pct_change()
    n = closes.notna().sum(axis=1).clip(lower=1)
    gross = (pos * rets).sum(axis=1) / n
    turn = pos.diff().abs().sum(axis=1) / n
    fpnl = -(pos * fund_daily.reindex(closes.index).fillna(0.0)).sum(axis=1) / n
    return gross - turn * COST_TURN + fpnl


def base_w(closes):
    vol = closes.pct_change().shift(1).rolling(60, min_periods=40).std()
    return (VOL_TARGET_D / vol).clip(upper=POS_CAP)


def tsmom_pos(closes, mask=None):
    sig = np.sign(closes.shift(1) / closes.shift(91) - 1)
    if mask is not None:
        sig = sig * mask
    return (sig * base_w(closes)).fillna(0.0)


def carry_pos(closes, fund_daily, scale=None):
    f7 = fund_daily.reindex(closes.index).fillna(0.0).rolling(7).sum().shift(1)
    rank = f7.rank(axis=1, pct=True)
    raw = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    raw[rank <= 0.2] = 1.0
    raw[rank >= 0.8] = -1.0
    is_reb = pd.Series(closes.index.dayofweek == 0, index=closes.index)
    sigw = raw.where(is_reb).ffill().fillna(0.0)
    pos = (sigw * base_w(closes)).fillna(0.0)
    if scale is not None:
        pos = pos.mul(scale, axis=0)
    return pos


def line_stats(r, label):
    mo = eq_from_rets(r.fillna(0.0)).resample("ME").last().pct_change().dropna()
    def sh(x):
        return float(x.mean() / x.std() * np.sqrt(12)) if len(x) > 3 and x.std() > 0 else float("nan")
    pre, post = mo[mo.index < COMMON_START], mo[mo.index >= COMMON_START]
    yl = "  ".join(f"{y} {sh(g):+.1f}" for y, g in mo.groupby(mo.index.year))
    print(f"  {label:14s} full {sh(mo):+5.2f}   pre {sh(pre):+5.2f}   "
          f"post {sh(post):+5.2f}   | {yl}")
    return sh(mo), sh(pre), sh(post), mo


def main():
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("SLEEVE DIAGNOSIS + CONDITIONED REDESIGN", flush=True)
    closes, fund_daily = load()

    # --- diagnosis ------------------------------------------------------------
    print("\n" + "=" * 92)
    print("DIAGNOSIS")
    print("=" * 92)
    nn = closes.notna().sum(axis=1)
    print("  names with data:  " + "  ".join(
        f"{y} {int(g.median())}" for y, g in nn.groupby(nn.index.year)))

    f7 = fund_daily.reindex(closes.index).fillna(0.0).rolling(7).sum().shift(1)
    disp = f7.std(axis=1)
    print("  funding dispersion (median cross-sec std of 7d funding, bp):  "
          + "  ".join(f"{y} {float(g.median())*1e4:.1f}"
                      for y, g in disp.groupby(disp.index.year)))

    sig = np.sign(closes.shift(1) / closes.shift(91) - 1)
    flips = (sig != sig.shift(1)).sum(axis=1)
    print("  tsmom sign flips/name/year:  " + "  ".join(
        f"{y} {float(g.sum()) / max(int(nn.loc[g.index].median()), 1):.1f}"
        for y, g in flips.groupby(flips.index.year)))

    r_c0 = port_returns(closes, fund_daily, carry_pos(closes, fund_daily))
    mo_c = eq_from_rets(r_c0).resample("ME").last().pct_change().dropna()
    disp_mo = disp.resample("ME").median().shift(1).reindex(mo_c.index)
    print(f"  corr(carry month, LAGGED dispersion): "
          f"{float(mo_c.corr(disp_mo)):+.2f}")
    btc_mo = closes["BTC-USDT"].resample("ME").last().pct_change().abs() \
        .reindex(mo_c.index)
    r_t0 = port_returns(closes, fund_daily, tsmom_pos(closes))
    mo_t = eq_from_rets(r_t0).resample("ME").last().pct_change().dropna()
    print(f"  corr(tsmom month, |BTC month|):       "
          f"{float(mo_t.reindex(btc_mo.index).corr(btc_mo)):+.2f}")

    # --- conditioned variants ---------------------------------------------------
    print("\n" + "=" * 92)
    print("VARIANTS  (full-history gate: full >= 0.5 AND pre >= 0.0)")
    print("=" * 92)

    gate_c = (disp > disp.rolling(365, min_periods=90).median()).shift(1) \
        .fillna(True).astype(float)
    mom = closes.shift(1) / closes.shift(91) - 1
    strong = (mom.abs().rank(axis=1, pct=True) >= 0.5).astype(float)
    btc_vol = closes["BTC-USDT"].pct_change().rolling(30, min_periods=20).std()
    calm = (btc_vol <= btc_vol.rolling(365, min_periods=90)
            .quantile(0.8)).shift(1).fillna(True)
    calm_m = pd.DataFrame({c: calm for c in closes.columns}, index=closes.index)

    results = {}
    _, _, _, mo_t0 = line_stats(r_t0, "TSMOM90 (ref)")
    results["TSMOM_STRONG"] = line_stats(
        port_returns(closes, fund_daily, tsmom_pos(closes, mask=strong)),
        "TSMOM_STRONG")
    results["TSMOM_CALM"] = line_stats(
        port_returns(closes, fund_daily, tsmom_pos(closes, mask=calm_m)),
        "TSMOM_CALM")
    print()
    _, _, _, mo_c0 = line_stats(r_c0, "CARRY (ref)")
    results["CARRY_GATED"] = line_stats(
        port_returns(closes, fund_daily,
                     carry_pos(closes, fund_daily, scale=gate_c)),
        "CARRY_GATED")

    print("\nGATE VERDICTS:")
    for name, (full, pre, post, _mo) in results.items():
        ok = (full >= 0.5) and (pre >= 0.0)
        print(f"  {name:14s} {'PASS' if ok else 'FAIL'}  "
              f"(full {full:+.2f}, pre {pre:+.2f}, post {post:+.2f})")


if __name__ == "__main__":
    main()
