"""CHOP MEAN-REVERSION SLEEVE — trade the regime the trend book only
suffers through. The GMM's CHOP label is our strongest verified layer; the
trend book half-sizes there. This sleeve does the opposite: Bollinger fades
allowed ONLY in CHOP (the bake-off's BB_MR control died under the
DIRECTIONAL mask — this is the mask it was built for).

PRE-REGISTERED (library defaults, no tuning):
- MAJORS8 4h, full Bybit history (per-pair start + 365d warmup).
- bb_meanrev(20, 2.0) defaults (RSI 30/70 confirm, sl 1.5 / tp 2.0 ATR);
  signals BLOCKED unless walk-forward regime == CHOP; risk_mult = vol
  targeting only (no chop-halving — the sleeve IS the chop trade; no F&G).
- Engine: TAKER both sides + 2bp slip default, cooldown 3, decay tiers,
  no maker/TP-limit assumptions (fades exit at touch-sensitive targets).
- Real per-pair funding applied.
- GATE (sleeve law): expanding EW book full Sh(mo) >= 0.5 AND pre-2023-08
  >= 0.0; corr to BTC reported, exact corr to the book in assembly.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/chop_mr_sleeve.py
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
from core.strategies import bb_meanrev
from research.cost_engine import apply_funding_real, FEE_TAKER
from research.regime_cache import wf_regimes_cached
from research.vol_target import vt_mult

MAJORS8 = [f"{b}-USDT" for b in
           ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX"]]
SPLIT = pd.Timestamp("2023-08-17", tz="UTC")
WARMUP_D = 365


def chop_only_mask(sig: pd.DataFrame, regs: pd.Series) -> pd.DataFrame:
    a = regs.reindex(sig.index, method="ffill").fillna("CHOP")
    block = (sig["signal"] != 0) & (a != "CHOP")
    s = sig.copy()
    s.loc[block, "signal"] = 0
    s.loc[block, ["sl", "tp"]] = np.nan
    return s


def sh(mo: pd.Series) -> float:
    return float(mo.mean() / mo.std() * np.sqrt(12)) \
        if len(mo) > 3 and mo.std() > 0 else float("nan")


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print("CHOP-MR SLEEVE — BB fades only in CHOP, MAJORS8 4h, taker",
          flush=True)
    eqs, trades_n = {}, 0
    for p in MAJORS8:
        t0 = time.time()
        df = fetch_ohlcv_bybit(p, "4h", days=2000)
        regs = wf_regimes_cached(df, p, "4h", 6)
        fund = fetch_funding(p, days=2000, source="auto")
        sig = chop_only_mask(bb_meanrev(df), regs)
        sig["risk_mult"] = vt_mult(df).reindex(sig.index).fillna(1.0) \
            .to_numpy()
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                               max_leverage=5.0,
                               eq_decay_tiers=DEFAULT_DECAY_TIERS,
                               cooldown_bars=3, fee_rate=FEE_TAKER,
                               entry_style="taker")
        eq, tr = run_backtest_enhanced(df[df.index >= cut],
                                       sig[sig.index >= cut], cfg)
        eqs[p] = apply_funding_real(eq, tr, fund)
        trades_n += len(tr)
        print(f"  {p:10s} {time.time()-t0:4.0f}s  trades={len(tr)}  "
              f"final {float(eqs[p].iloc[-1]):.1f}", flush=True)

    union = None
    for e in eqs.values():
        union = e.index if union is None else union.union(e.index)
    union = union.sort_values()
    rets = pd.DataFrame({p: eqs[p].reindex(union).pct_change()
                         for p in MAJORS8})
    n_live = rets.notna().sum(axis=1)
    book = rets.mean(axis=1)[n_live >= 3]
    eqb = (1 + book.fillna(0)).cumprod()
    mo = eqb.resample("ME").last().pct_change().dropna()
    pre, post = mo[mo.index < SPLIT], mo[mo.index >= SPLIT]
    full_s, pre_s, post_s = sh(mo), sh(pre), sh(post)
    mdd = float((eqb / eqb.cummax() - 1).min())

    btc = fetch_ohlcv_bybit("BTC-USDT", "1d", days=2000)["close"]
    btc_mo = btc.resample("ME").last().pct_change().dropna()
    corr = float(mo.corr(btc_mo.reindex(mo.index)))

    print("\n" + "=" * 74)
    print(f"CHOP-MR EW BOOK  {book.index[0].date()}..{book.index[-1].date()}"
          f"  trades={trades_n}")
    print("=" * 74)
    print(f"  full Sh(mo) {full_s:+.2f}   pre-2023-08 {pre_s:+.2f}   "
          f"post {post_s:+.2f}   MDD {mdd*100:.1f}%   corr(BTC) {corr:+.2f}")
    yl = "  ".join(f"{y} {sh(g):+.1f}" for y, g in mo.groupby(mo.index.year))
    print(f"  yearly: {yl}")
    ok = full_s >= 0.5 and pre_s >= 0.0
    print(f"\nGATE VERDICT: {'PASS — assembly study next' if ok else 'FAIL'}"
          f"  (sleeve law: full>=0.5, pre>=0.0)")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
