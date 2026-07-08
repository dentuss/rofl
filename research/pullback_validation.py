"""Pullback-family validation — the entry_families bake-off surfaced PULL_T6
(EMA50 side + RSI14 recross of 40/60, sl 1.8 / tp 6.0 ATR) as the one
ensemble candidate: standalone Sh(mo) 1.36, MDD -2.1%, corr 0.17 to the
triple baseline, 50/50 blend Sh 1.50. Only 178 trades -> this round decides
adoption with the full gate battery.

PRE-REGISTERED:
- G3 random-entry null (the r3 design): 60 draws; each draw replaces the
  pullback entries with the SAME NUMBER of long/short entries drawn
  uniformly from the bars where the regime+F&G stack would have allowed
  that side, with the SAME SL/TP geometry (1.8/6.0 ATR at the drawn bar),
  same risk_mult, costs, cooldown. Real must rank >= 95th pct on portfolio
  Sharpe. rng seed 42.
- G1: IS/OOS 60-40 + sub-window thirds, standalone and blends.
- Blends with the TRIPLE_T6 baseline at 50/50 (the bake-off's pre-registered
  weight) and 75/25, with CAGR shown (capital efficiency is the objection).
- Per-name table + time-in-market.

ADOPTION RULE (pre-registered): adopt the 50/50 blend as the new trend-book
configuration iff (a) G3 >= 95th pct, (b) blend thirds all positive,
(c) blend OOS Sh >= baseline OOS Sh. If G3 fails, the pullback's edge is the
overlay stack, not the entry — reject regardless of headline numbers.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/pullback_validation.py
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
from core.indicators import atr
from core.risk import DEFAULT_DECAY_TIERS
from core.sentiment import align_to_bars, fetch_fear_greed
from core.strategies import triple_confirm_bidir
from research.cost_engine import (apply_funding_real, regime_mask, fng_persist,
                                  sharpe_m, stats, build, FEE_TAKER, FEE_MAKER,
                                  FNG_BARS)
from research.entry_families import pullback_in_trend
from research.regime_cache import wf_regimes_cached
from research.vol_target import vt_mult

MAJORS8 = [f"{b}-USDT" for b in
           ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX"]]
TOTAL = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))
COMMON_START = pd.Timestamp("2023-08-17", tz="UTC")
WARMUP_D = 365
BPD = 6
N_NULL = int(_os.environ.get("N_NULL", 60))
SL_M, TP_M = 1.8, 6.0


def cfg_dep():
    return EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                            max_leverage=5.0, eq_decay_tiers=DEFAULT_DECAY_TIERS,
                            cooldown_bars=3, fee_rate=FEE_TAKER,
                            fee_maker=FEE_MAKER, entry_style="maker_close")


def allowed_sides(df, regs, fa):
    """Bars where the regime+F&G stack would allow a long / short entry."""
    a = regs.reindex(df.index, method="ffill").fillna("CHOP")
    above = (fa >= 80).rolling(FNG_BARS, min_periods=FNG_BARS).sum() == FNG_BARS
    below = (fa <= 20).rolling(FNG_BARS, min_periods=FNG_BARS).sum() == FNG_BARS
    above = above.reindex(df.index).fillna(False)
    below = below.reindex(df.index).fillna(False)
    ok_l = a.isin(["BULL", "CHOP"]) & ~above
    ok_s = a.isin(["BEAR", "CHOP"]) & ~below
    return ok_l, ok_s


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print(f"PULLBACK VALIDATION  MAJORS8  N_NULL={N_NULL}", flush=True)
    fng = fetch_fear_greed()
    rng = np.random.default_rng(42)

    pair_data = {}
    eq_pull, eq_base = {}, {}
    tim = {}
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
        pair_data[p] = (df, regs, fa, fund, mult, cut)

        for tag, raw in [("pull", pullback_in_trend(df)),
                         ("base", triple_confirm_bidir(df, tp_mult=6.0))]:
            sig = fng_persist(regime_mask(raw, regs), fa)
            sig["risk_mult"] = mult
            eq, tr = run_backtest_enhanced(df[df.index >= cut],
                                           sig[sig.index >= cut], cfg_dep())
            eqf = apply_funding_real(eq, tr, fund)
            if tag == "pull":
                eq_pull[p] = eqf
                held = sum((t.exit_time - t.entry_time).total_seconds()
                           for t in tr) / 3600 / 4
                tim[p] = (len(tr), held / max(len(df[df.index >= cut]), 1))
            else:
                eq_base[p] = eqf
        print(f"  {p:10s} {time.time()-t0:5.0f}s  pull trades={tim[p][0]} "
              f"TIM={tim[p][1]*100:.0f}%", flush=True)

    idx = None
    for p in MAJORS8:
        e = eq_base[p][eq_base[p].index >= COMMON_START]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    split = idx[int(len(idx) * 0.6)]
    i_idx, o_idx = idx[idx < split], idx[idx >= split]
    b3 = [idx[0] + (idx[-1] - idx[0]) * k / 3 for k in range(4)]
    w = {p: 1 / 8 for p in MAJORS8}

    port_pull = build(eq_pull, w, idx)
    port_base = build(eq_base, w, idx)
    real_sh = sharpe_m(port_pull)

    # ---- G3 random-entry null ------------------------------------------------
    print(f"\n[G3] {N_NULL} random-entry draws (matched counts, same gates/"
          f"geometry/costs) ...", flush=True)
    null_sh = []
    for d in range(N_NULL):
        eq_d = {}
        for p in MAJORS8:
            df, regs, fa, fund, mult, cut = pair_data[p]
            ok_l, ok_s = allowed_sides(df, regs, fa)
            real_sig = fng_persist(regime_mask(pullback_in_trend(df), regs), fa)
            n_l = int((real_sig["signal"] == 1).sum())
            n_s = int((real_sig["signal"] == -1).sum())
            a14 = atr(df["high"], df["low"], df["close"], 14)
            cand_l = np.flatnonzero(ok_l.to_numpy())
            cand_s = np.flatnonzero(ok_s.to_numpy())
            pick_l = rng.choice(cand_l, size=min(n_l, len(cand_l)),
                                replace=False)
            pick_s = rng.choice(cand_s, size=min(n_s, len(cand_s)),
                                replace=False)
            c = np.asarray(df["close"], dtype=float)
            av = np.asarray(a14, dtype=float)
            n = len(df)
            sg = np.zeros(n, dtype=int)
            sl = np.full(n, np.nan)
            tp = np.full(n, np.nan)
            sg[pick_l] = 1
            sl[pick_l] = c[pick_l] - SL_M * av[pick_l]
            tp[pick_l] = c[pick_l] + TP_M * av[pick_l]
            sg[pick_s] = -1
            sl[pick_s] = c[pick_s] + SL_M * av[pick_s]
            tp[pick_s] = c[pick_s] - TP_M * av[pick_s]
            sig = pd.DataFrame({"signal": sg, "sl": sl, "tp": tp},
                               index=df.index)
            sig["risk_mult"] = mult
            eq, tr = run_backtest_enhanced(df[df.index >= cut],
                                           sig[sig.index >= cut], cfg_dep())
            eq_d[p] = apply_funding_real(eq, tr, fund)
        null_sh.append(sharpe_m(build(eq_d, w, idx)))
        if (d + 1) % 10 == 0:
            print(f"    {d+1}/{N_NULL}  null Sh median so far "
                  f"{np.median(null_sh):+.2f}", flush=True)

    null_sh = np.array(null_sh)
    pct = float((real_sh > null_sh).mean() * 100)
    print(f"\n  real PULL Sh {real_sh:+.2f}  vs null median "
          f"{np.median(null_sh):+.2f}  p5..p95 [{np.percentile(null_sh,5):+.2f},"
          f" {np.percentile(null_sh,95):+.2f}]  -> percentile {pct:.0f}")

    # ---- tables ----------------------------------------------------------------
    def line(name, port):
        s = stats(port)
        i = stats(port.reindex(i_idx).dropna())
        o = stats(port.reindex(o_idx).dropna())
        th = "  ".join(
            f"{sharpe_m(port.reindex(idx[(idx >= b3[k]) & (idx < b3[k+1])]).dropna()):+.2f}"
            for k in range(3))
        print(f"{name:10s}{s['final']:8.0f}{s['cagr']*100:8.1f}{s['sh_m']:8.2f}"
              f"{s['mdd']*100:7.1f}{s['worst_mo']:7.1f}{i['sh_m']:7.2f}"
              f"{o['sh_m']:8.2f}{th:>22s}")
        return s, o

    print("\n" + "=" * 96)
    print(f"STANDALONE + BLENDS  {idx[0].date()}..{idx[-1].date()}  "
          f"MAJORS8 EW @ ${TOTAL:.0f}")
    print("=" * 96)
    print(f"{'book':10s}{'final$':>8s}{'CAGR%':>8s}{'Sh(mo)':>8s}{'MDD%':>7s}"
          f"{'worst':>7s}{'IS Sh':>7s}{'OOS Sh':>8s}{'thirds':>22s}")
    _, o_base = line("TRIPLE", port_base)
    line("PULL", port_pull)
    nb = port_base / port_base.iloc[0]
    npl = port_pull / port_pull.iloc[0]
    s50, o50 = line("BLEND50", (0.5 * nb + 0.5 * npl) * TOTAL)
    line("BLEND75", (0.75 * nb + 0.25 * npl) * TOTAL)

    print("\nper-name PULL (common window):")
    for p in MAJORS8:
        e = eq_pull[p].reindex(idx).ffill()
        print(f"  {p.split('-')[0]:5s} final {float(e.iloc[-1]):7.1f}  "
              f"Sh {sharpe_m(e):+5.2f}  trades {tim[p][0]:3d}  "
              f"TIM {tim[p][1]*100:4.0f}%")

    ok_g3 = pct >= 95
    print(f"\nVERDICT inputs: G3 {'PASS' if ok_g3 else 'FAIL'} ({pct:.0f}th pct); "
          f"blend50 OOS {o50['sh_m']:+.2f} vs baseline OOS {o_base['sh_m']:+.2f}")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
