"""TRADFI TREND SLEEVE — the promoted crypto signal pair applied to the
commodities Bybit now lists as USDT perps (XAUUSDT, XAGUSDT, CLUSDT,
BZUSDT). Long history via Yahoo continuous futures (GC=F, SI=F from 2000;
CL=F 2000; BZ=F 2007) — a ~23-year pseudo-OOS window for a stack designed
entirely on 2023+ crypto. Trend on gold/oil is the most documented edge in
CTA history; the question is whether OUR exact implementation carries.

PRE-REGISTERED (no tuning, canonical params):
- 1d bars; legs = triple_confirm_bidir(tp_mult=6.0) + pullback_in_trend()
  at 50/50; walk-forward GMM regime mask (bars_per_day=1) + CHOP half-size
  + vol targeting + CONF sizing. No F&G (crypto-only), no funding (proxy
  data; Bybit perp funding becomes an L2 measurement if deployed).
- Costs: TAKER 6bp + 5bp slip both sides (no maker assumptions on a venue
  we haven't measured), cooldown 3 bars, decay tiers.
- GATE (stricter than the crypto sleeve law because the pre-window is ~22y,
  not 1.5y): EW book full Sh(mo) >= 0.5 AND pre-2023-08 Sh >= 0.3 AND >=3/4
  names profitable over their full windows.
- Corr to BTC (monthly, 2021+ overlap) reported; exact corr to the deployed
  book measured in the assembly study for survivors.

Deployment reality if PASS: Bybit's contracts are ~4 months old, so a pass
here buys a PAPER/min-size forward stage on the real venue (fills, funding,
weekend gaps), never a direct allocation.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/tradfi_sleeve.py
"""
from __future__ import annotations

import sys as _sys, os as _os, json, time, urllib.request
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

import numpy as np
import pandas as pd

import core.regime as _regime
from core.backtest_enhanced import EnhancedBTConfig, run_backtest_enhanced
from core.data import CACHE_DIR, fetch_ohlcv_bybit
from core.risk import DEFAULT_DECAY_TIERS
from core.strategies import pullback_in_trend, triple_confirm_bidir
from research.cost_engine import regime_mask, FEE_TAKER
from research.regime_upgrades import wf_labels_conf
from research.vol_target import vt_mult

NAMES = {"GC=F": "GOLD", "SI=F": "SILVER", "CL=F": "WTI", "BZ=F": "BRENT"}
SPLIT = pd.Timestamp("2023-08-17", tz="UTC")
WARMUP_D = 365


def fetch_yahoo_daily(sym: str) -> pd.DataFrame:
    safe = sym.replace("=", "_")
    cache = CACHE_DIR / f"yahoo_{safe}_1d.parquet"
    if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
        return pd.read_parquet(cache)
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
           f"?range=25y&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    res = data["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({
        "open": q["open"], "high": q["high"], "low": q["low"],
        "close": q["close"], "volume": q["volume"],
    }, index=pd.to_datetime(res["timestamp"], unit="s", utc=True).normalize())
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df["volume"] = df["volume"].fillna(0.0)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache)
    return df


def sh(mo: pd.Series) -> float:
    return float(mo.mean() / mo.std() * np.sqrt(12)) \
        if len(mo) > 3 and mo.std() > 0 else float("nan")


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print("TRADFI TREND SLEEVE — canonical stack on GC/SI/CL/BZ, 1d, "
          "taker costs", flush=True)

    eqs, prof = {}, {}
    for sym, tag in NAMES.items():
        t0 = time.time()
        df = fetch_yahoo_daily(sym)
        regs, conf = wf_labels_conf(df, 1)
        a = regs.reindex(df.index, method="ffill").fillna("CHOP")
        mult = np.where(a == "CHOP", 0.5, 1.0) * \
            vt_mult(df).reindex(df.index).fillna(1.0).to_numpy() * \
            (0.5 + 0.5 * conf.reindex(df.index).fillna(1.0).to_numpy())
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]
        legs = {}
        for leg, raw in [("t", triple_confirm_bidir(df, tp_mult=6.0)),
                         ("p", pullback_in_trend(df))]:
            sig = regime_mask(raw, regs)
            sig["risk_mult"] = mult
            cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                                   max_leverage=5.0,
                                   eq_decay_tiers=DEFAULT_DECAY_TIERS,
                                   cooldown_bars=3, fee_rate=FEE_TAKER,
                                   slip_bps=5.0, entry_style="taker")
            eq, tr = run_backtest_enhanced(dfe, sig[sig.index >= cut], cfg)
            legs[leg] = eq
        blend = 0.5 * legs["t"] / legs["t"].iloc[0] \
            + 0.5 * legs["p"] / legs["p"].iloc[0]
        eqs[tag] = blend
        prof[tag] = float(blend.iloc[-1]) > 1.0
        mo = blend.resample("ME").last().pct_change().dropna()
        pre, post = mo[mo.index < SPLIT], mo[mo.index >= SPLIT]
        print(f"  {tag:7s} {blend.index[0].date()}..{blend.index[-1].date()}"
              f"  final x{float(blend.iloc[-1]):.2f}  full {sh(mo):+5.2f}"
              f"  pre {sh(pre):+5.2f}  post {sh(post):+5.2f}"
              f"  ({time.time()-t0:.0f}s)", flush=True)

    # EW book on the union calendar (names enter as their data starts)
    union = None
    for e in eqs.values():
        union = e.index if union is None else union.union(e.index)
    union = union.sort_values()
    rets = pd.DataFrame({t: eqs[t].reindex(union).pct_change()
                         for t in eqs})
    n_live = rets.notna().sum(axis=1)
    book = rets.mean(axis=1)[n_live >= 2]
    eqb = (1 + book.fillna(0)).cumprod()
    mo = eqb.resample("ME").last().pct_change().dropna()
    pre, post = mo[mo.index < SPLIT], mo[mo.index >= SPLIT]
    full_s, pre_s, post_s = sh(mo), sh(pre), sh(post)
    mdd = float((eqb / eqb.cummax() - 1).min())

    print("\n" + "=" * 78)
    print(f"EW TRADFI BOOK  {book.index[0].date()}..{book.index[-1].date()}")
    print("=" * 78)
    print(f"  full Sh(mo) {full_s:+.2f}   pre-2023-08 {pre_s:+.2f}   "
          f"post {post_s:+.2f}   MDD {mdd*100:.1f}%   names+ "
          f"{sum(prof.values())}/4")
    for dec, g in mo.groupby((mo.index.year // 5) * 5):
        print(f"    {dec}-{dec+4}: Sh {sh(g):+5.2f}  ({len(g)} months)")
    yl = "  ".join(f"{y} {sh(g):+.1f}"
                   for y, g in mo[mo.index.year >= 2021].groupby(
                       mo[mo.index.year >= 2021].index.year))
    print(f"    recent yearly: {yl}")

    btc = fetch_ohlcv_bybit("BTC-USDT", "1d", days=2000)["close"]
    btc_mo = btc.resample("ME").last().pct_change().dropna()
    corr = float(mo.corr(btc_mo.reindex(mo.index)))
    print(f"  corr(monthly, BTC 2021+): {corr:+.2f}")

    ok = full_s >= 0.5 and pre_s >= 0.3 and sum(prof.values()) >= 3
    print(f"\nGATE VERDICT: {'PASS — assembly study + paper/min-size stage '
          'on the real Bybit contracts next' if ok else 'FAIL'}"
          f"  (need full>=0.5, pre>=0.3, names+>=3/4)")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
