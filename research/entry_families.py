"""Entry-family bake-off — is triple_bidir actually the best signal generator,
or just the one we happen to have? Every family runs through the IDENTICAL
adopted stack (walk-forward regime mask, F&G 3d persistence, CHOP half-size,
vol targeting, SL cooldown K=3, maker entries, real funding) on MAJORS8 4h.
Only the entry/exit signal generator changes.

PRE-REGISTERED CELLS (no post-hoc grid; library defaults + the one honest
lesson of this era — wide TP lets winners run — applied as a tp_mult=6.0
variant per family):
  TRIPLE_T6   triple_confirm_bidir tp6            — baseline, must reproduce
                                                    MAJORS8/RSCD3+VT ~20%/1.42
  DONCH20_T4  donchian_breakout defaults (20-bar entry, ADX>20, sl2/tp4)
  DONCH20_T6  donchian_breakout tp_mult=6.0
  DONCH55_T6  donchian_breakout entry 55 / exit 20, tp_mult=6.0 (slow turtle)
  EMA_T3      ema_trend defaults (21/55/200 + RSI band, sl2/tp3)
  EMA_T6      ema_trend tp_mult=6.0
  ST_T25      supertrend_rsi defaults (10/3.0 + RSI50, sl1.5/tp2.5)
  ST_T6       supertrend_rsi tp_mult=6.0
  MACD_T3     macd_trend defaults (12/26/9 + EMA200, sl1.5/tp3)
  MACD_T6     macd_trend tp_mult=6.0
  BB_MR       bb_meanrev defaults — CONTROL: mean-reversion inside a
              directional regime mask is expected to conflict; included to
              measure that, not to win
  PULL_T6     pullback-in-trend (new, defined here): EMA50 side filter +
              RSI14 recross of 40 (long) / 60 (short), sl 1.8 / tp 6.0 ATR —
              the "buy the dip inside the trend" counterpart to breakout

DECISION RULES (pre-registered):
- A family REPLACES triple only if full Sh(mo) > baseline AND OOS does not
  decay materially (no OOS<<IS) AND >= 5/8 names profitable.
- A family is an ENSEMBLE CANDIDATE if standalone Sh >= 0.6, monthly corr to
  baseline < 0.7, and the 50/50 blend improves Sh over baseline. Blends are
  computed for ALL families (no cherry-pick).

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/entry_families.py
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
from core.strategies import (bb_meanrev, donchian_breakout, ema_trend,
                             macd_trend, pullback_in_trend, supertrend_rsi,
                             triple_confirm_bidir)
from research.cost_engine import (apply_funding_real, regime_mask, fng_persist,
                                  sharpe_m, stats, build, FEE_TAKER, FEE_MAKER)
from research.regime_cache import wf_regimes_cached
from research.vol_target import vt_mult

MAJORS8 = [f"{b}-USDT" for b in
           ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX"]]
TOTAL = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))
COMMON_START = pd.Timestamp("2023-08-17", tz="UTC")
WARMUP_D = 365
BPD = 6


# pullback_in_trend was promoted 2026-07-06 and moved to core.strategies
# (imported above; re-exported here for pullback_validation.py and friends).

FAMS = {
    "TRIPLE_T6":  lambda df: triple_confirm_bidir(df, tp_mult=6.0),
    "DONCH20_T4": lambda df: donchian_breakout(df),
    "DONCH20_T6": lambda df: donchian_breakout(df, tp_mult=6.0),
    "DONCH55_T6": lambda df: donchian_breakout(df, entry_n=55, exit_n=20,
                                               tp_mult=6.0),
    "EMA_T3":     lambda df: ema_trend(df),
    "EMA_T6":     lambda df: ema_trend(df, tp_mult=6.0),
    "ST_T25":     lambda df: supertrend_rsi(df),
    "ST_T6":      lambda df: supertrend_rsi(df, tp_mult=6.0),
    "MACD_T3":    lambda df: macd_trend(df),
    "MACD_T6":    lambda df: macd_trend(df, tp_mult=6.0),
    "BB_MR":      lambda df: bb_meanrev(df),
    "PULL_T6":    lambda df: pullback_in_trend(df),
}
BASELINE = "TRIPLE_T6"


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print(f"ENTRY-FAMILY BAKE-OFF  MAJORS8 4h  cells={list(FAMS)}", flush=True)
    fng = fetch_fear_greed()

    eqs = {f: {} for f in FAMS}
    counts = {f: 0 for f in FAMS}
    for p in MAJORS8:
        t0 = time.time()
        df = fetch_ohlcv_bybit(p, "4h", days=DAYS)
        regs = wf_regimes_cached(df, p, "4h", BPD)
        fa = align_to_bars(fng, df.index)
        fund = fetch_funding(p, days=DAYS, source="auto")
        a = regs.reindex(df.index, method="ffill").fillna("CHOP")
        mult = np.where(a == "CHOP", 0.5, 1.0) * \
            vt_mult(df).reindex(df.index).fillna(1.0).to_numpy()
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]
        for fam, fn in FAMS.items():
            sig = fng_persist(regime_mask(fn(df), regs), fa)
            sig["risk_mult"] = mult
            cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                                   max_leverage=5.0,
                                   eq_decay_tiers=DEFAULT_DECAY_TIERS,
                                   cooldown_bars=3, fee_rate=FEE_TAKER,
                                   fee_maker=FEE_MAKER, entry_style="maker_close")
            eq, tr = run_backtest_enhanced(dfe, sig[sig.index >= cut], cfg)
            eqs[fam][p] = apply_funding_real(eq, tr, fund)
            counts[fam] += len(tr)
        print(f"  {p:10s} {time.time()-t0:5.0f}s", flush=True)

    idx = None
    for p in MAJORS8:
        e = eqs[BASELINE][p][eqs[BASELINE][p].index >= COMMON_START]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    split = idx[int(len(idx) * 0.6)]
    i_idx, o_idx = idx[idx < split], idx[idx >= split]
    w = {p: 1 / 8 for p in MAJORS8}

    base_port = build(eqs[BASELINE], w, idx)
    base_mo = base_port.resample("ME").last().pct_change().dropna()

    print("\n" + "=" * 116)
    print(f"FAMILIES x ADOPTED STACK  {idx[0].date()}..{idx[-1].date()}  "
          f"MAJORS8 EW @ ${TOTAL:.0f}")
    print("=" * 116)
    print(f"{'cell':12s}{'final$':>8s}{'CAGR%':>8s}{'Sh(mo)':>8s}{'MDD%':>7s}"
          f"{'worst':>7s}{'IS Sh':>7s}{'OOS Sh':>8s}{'prof':>6s}{'trades':>8s}"
          f"{'corrM':>7s}{'blendSh':>9s}{'blendOOS':>9s}")
    for fam in FAMS:
        port = build(eqs[fam], w, idx)
        s = stats(port)
        i = stats(build(eqs[fam], w, i_idx))
        o = stats(build(eqs[fam], w, o_idx))
        prof = sum(1 for p in MAJORS8
                   if float(eqs[fam][p].reindex(idx).ffill().iloc[-1])
                   > float(eqs[fam][p].reindex(idx).ffill().iloc[0]))
        mo = port.resample("ME").last().pct_change().dropna()
        corr = float(mo.corr(base_mo)) if fam != BASELINE else 1.0
        # 50/50 blend with baseline (normalized equities)
        bl = 0.5 * base_port / base_port.iloc[0] + 0.5 * port / port.iloc[0]
        bl = bl * TOTAL
        bs = stats(bl)
        blo = stats(bl.reindex(o_idx).dropna())
        print(f"{fam:12s}{s['final']:8.0f}{s['cagr']*100:8.1f}{s['sh_m']:8.2f}"
              f"{s['mdd']*100:7.1f}{s['worst_mo']:7.1f}{i['sh_m']:7.2f}"
              f"{o['sh_m']:8.2f}{prof:>4d}/8{counts[fam]:8d}{corr:7.2f}"
              f"{bs['sh_m']:9.2f}{blo['sh_m']:9.2f}")

    print("\nDecision rules: REPLACE iff Sh > baseline AND no OOS collapse AND "
          ">=5/8 names profitable.")
    print("ENSEMBLE CANDIDATE iff standalone Sh >= 0.6 AND corrM < 0.7 AND "
          "blend Sh > baseline Sh.")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
