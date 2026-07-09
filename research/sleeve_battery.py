"""SLEEVE BATTERY (crypto data) — candidates to stack Sharpe onto the trend
book. Seat price (pre-registered, the sleeve law): standalone Sh(mo) >= 0.5
full-history AND pre-2023-08 >= 0.0 AND |corr(monthly, trend book)| <= 0.5.
Survivors go to an assembly study; nothing here touches capital without the
full gate battery afterwards.

CANDIDATES (pre-registered cells, no grids):
  S2 ETHBTC-MR   4h log-ratio ETH/BTC z-score vs rolling 30d (180 bars,
                 min 120): enter -sign(z) when |z|>2, exit |z|<0.5 or 90
                 bars; costs 22bp per unit position change (2 taker legs).
                 Market-neutral by construction.
  S3 XSMOM-14/21 QUAL23 1d: residual-vs-BTC momentum over {14,21}d, weekly
                 Monday rank; long top / short bottom quintile, inverse-vol
                 sizing (sleeve base_w), 8bp/turnover + REAL funding.
  S4 CORE-MA200  BTC+ETH 1d: the classic 200d-MA filter, {long-flat,
                 long-short} cells, inverse-vol sizing, 8bp/turnover +
                 real funding. Different clock (monthly-scale turnover)
                 from the 4h book.
  S6 CALENDAR    BTC 1d: {turn-of-month long (last day -> +3), weekend
                 long (Sat+Sun)}; 22bp per round trip. Low prior; cheap.

Corr is measured against the ACTUAL deployed book (BLEND50_CONF daily
returns, rebuilt here). Multiple testing: 6 cells; expect ~0-1 false
positives at this bar; survivors must hold up in the assembly study.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/sleeve_battery.py
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
from core.strategies import pullback_in_trend, triple_confirm_bidir
from research.cost_engine import (apply_funding_real, regime_mask, fng_persist,
                                  build, FEE_TAKER, FEE_MAKER)
from research.regime_upgrades import wf_labels_conf
from research.sleeve_diagnosis import port_returns, base_w
from research.tsmom_sleeve import QUAL23, eq_from_rets
from research.vol_target import vt_mult

MAJORS8 = [f"{b}-USDT" for b in
           ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX"]]
SPLIT = pd.Timestamp("2023-08-17", tz="UTC")
COST_PAIR = 0.0022      # 2 taker legs per unit position change
COST_RT = 0.0022        # event round trip


def sh(mo: pd.Series) -> float:
    return float(mo.mean() / mo.std() * np.sqrt(12)) \
        if len(mo) > 3 and mo.std() > 0 else float("nan")


def verdict(r_daily: pd.Series, label: str, book_mo: pd.Series):
    eq = (1 + r_daily.fillna(0.0)).cumprod()
    mo = eq.resample("ME").last().pct_change().dropna()
    pre, post = mo[mo.index < SPLIT], mo[mo.index >= SPLIT]
    corr = float(mo.corr(book_mo.reindex(mo.index))) \
        if len(mo.index.intersection(book_mo.index)) > 6 else float("nan")
    full_s, pre_s, post_s = sh(mo), sh(pre), sh(post)
    ok = full_s >= 0.5 and (pre_s >= 0.0 or np.isnan(pre_s)) and \
        (np.isnan(corr) or abs(corr) <= 0.5)
    yl = "  ".join(f"{y} {sh(g):+.1f}" for y, g in mo.groupby(mo.index.year))
    print(f"  {label:14s} full {full_s:+5.2f}  pre {pre_s:+5.2f}  "
          f"post {post_s:+5.2f}  corr(book) {corr:+5.2f}"
          f"{'   << SEAT EARNED' if ok else ''}")
    print(f"    yearly: {yl}")
    return ok


def build_book_daily() -> pd.Series:
    """The deployed BLEND50_CONF book's daily returns (for honest corr)."""
    fng = fetch_fear_greed()
    eq_t, eq_p = {}, {}
    for p in MAJORS8:
        df = fetch_ohlcv_bybit(p, "4h", days=2000)
        regs, conf = wf_labels_conf(df, 6)
        fa = align_to_bars(fng, df.index)
        fund = fetch_funding(p, days=2000, source="auto")
        a = regs.reindex(df.index, method="ffill").fillna("CHOP")
        mult = np.where(a == "CHOP", 0.5, 1.0) * \
            vt_mult(df).reindex(df.index).fillna(1.0).to_numpy() * \
            (0.5 + 0.5 * conf.reindex(df.index).fillna(1.0).to_numpy())
        cut = df.index[0] + pd.Timedelta(days=365)
        dfe = df[df.index >= cut]
        for tag, raw in [("t", triple_confirm_bidir(df, tp_mult=6.0)),
                         ("p", pullback_in_trend(df))]:
            sig = fng_persist(regime_mask(raw, regs), fa)
            sig["risk_mult"] = mult
            cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                                   max_leverage=5.0,
                                   eq_decay_tiers=DEFAULT_DECAY_TIERS,
                                   cooldown_bars=3, fee_rate=FEE_TAKER,
                                   fee_maker=FEE_MAKER,
                                   entry_style="maker_close", tp_as_limit=True)
            eq, tr = run_backtest_enhanced(dfe, sig[sig.index >= cut], cfg)
            (eq_t if tag == "t" else eq_p)[p] = apply_funding_real(eq, tr, fund)
    idx = None
    for p in MAJORS8:
        e = eq_t[p][eq_t[p].index >= SPLIT]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    w = {p: 1 / 8 for p in MAJORS8}
    pt, pp = build(eq_t, w, idx), build(eq_p, w, idx)
    blend = 0.5 * pt / pt.iloc[0] + 0.5 * pp / pp.iloc[0]
    return blend.resample("1D").last().pct_change().dropna()


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print("SLEEVE BATTERY — seat price: full>=0.5, pre>=0.0, |corr|<=0.5",
          flush=True)
    t0 = time.time()
    book_d = build_book_daily()
    book_mo = (1 + book_d).cumprod().resample("ME").last().pct_change().dropna()
    print(f"  [book rebuilt for corr: {time.time()-t0:.0f}s]", flush=True)

    results = {}

    # S2 --------------------------------------------------------- ETHBTC-MR
    btc = fetch_ohlcv_bybit("BTC-USDT", "4h", days=2000)["close"]
    eth = fetch_ohlcv_bybit("ETH-USDT", "4h", days=2000)["close"]
    both = pd.concat([btc.rename("b"), eth.rename("e")], axis=1).dropna()
    ratio = np.log(both["e"] / both["b"])
    z = (ratio - ratio.rolling(180, min_periods=120).mean()) \
        / ratio.rolling(180, min_periods=120).std()
    pos = np.zeros(len(both))
    cur, age = 0.0, 0
    zv = z.to_numpy()
    for i in range(len(both)):
        if cur != 0:
            age += 1
            if abs(zv[i]) < 0.5 or age > 90:
                cur, age = 0.0, 0
        if cur == 0 and abs(zv[i]) > 2 and zv[i] == zv[i]:
            cur, age = -np.sign(zv[i]), 0
        pos[i] = cur
    pos = pd.Series(pos, index=both.index)
    spread_ret = both["e"].pct_change() - both["b"].pct_change()
    r = pos.shift(1) * spread_ret - pos.diff().abs() * COST_PAIR
    results["S2 ETHBTC-MR"] = verdict(
        (1 + r.fillna(0)).resample("1D").prod() - 1, "S2 ETHBTC-MR", book_mo)

    # S3 ---------------------------------------------------------- XSMOM k
    closes, fund_d = {}, {}
    for p in QUAL23:
        closes[p] = fetch_ohlcv_bybit(p, "1d", days=2000)["close"]
        f = fetch_funding(p, days=2000, source="auto")
        fund_d[p] = f["funding_rate"].resample("1D").sum() \
            if f is not None and len(f) else pd.Series(dtype=float)
    C = pd.DataFrame(closes)
    F = pd.DataFrame(fund_d)
    bw = base_w(C)
    for k in (14, 21):
        mom = C.pct_change(k).sub(C["BTC-USDT"].pct_change(k), axis=0) \
            .shift(1).drop(columns=["BTC-USDT"])
        rank = mom.rank(axis=1, pct=True)
        raw = pd.DataFrame(0.0, index=C.index, columns=mom.columns)
        raw[rank >= 0.8] = 1.0
        raw[rank <= 0.2] = -1.0
        is_reb = pd.Series(C.index.dayofweek == 0, index=C.index)
        sigw = raw.where(is_reb).ffill().fillna(0.0)
        pos = (sigw * bw.drop(columns=["BTC-USDT"])).fillna(0.0)
        pos = pos.reindex(columns=C.columns).fillna(0.0)   # BTC col = 0
        r = port_returns(C, F, pos)
        results[f"S3 XSMOM-{k}"] = verdict(r, f"S3 XSMOM-{k}", book_mo)

    # S4 ------------------------------------------------------- CORE-MA200
    core = C[["BTC-USDT", "ETH-USDT"]]
    ma = core.rolling(200).mean()
    bw2 = base_w(core)
    for name, sig in (("S4 CORE-LF", (core > ma).astype(float).shift(1)),
                      ("S4 CORE-LS", np.sign(core - ma).shift(1))):
        pos = (sig * bw2).fillna(0.0)
        r = port_returns(core, F[["BTC-USDT", "ETH-USDT"]], pos)
        results[name] = verdict(r, name, book_mo)

    # S6 ---------------------------------------------------------- calendar
    b1d = C["BTC-USDT"].dropna()
    rb = b1d.pct_change()
    dom = b1d.index.day
    month_len = b1d.index.days_in_month
    tom = ((dom >= month_len) | (dom <= 3)).astype(float)
    wkd = b1d.index.dayofweek.isin([5, 6]).astype(float)
    for name, posv in (("S6 TOM", tom), ("S6 WEEKEND", wkd)):
        pos = pd.Series(posv, index=b1d.index)
        r = pos.shift(1) * rb - pos.diff().abs() * (COST_RT / 2)
        results[name] = verdict(r, name, book_mo)

    print("\n" + "=" * 64)
    print("SEATS")
    print("=" * 64)
    for k, v in results.items():
        print(f"  {'SEAT EARNED' if v else 'dead      '}  {k}")
    print("\nSurvivors: assembly study next (exact weights + book impact), "
          "then the full gate battery before any capital.")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
