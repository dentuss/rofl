"""TP-as-limit — the last unpriced execution lever (ROADMAP Phase 2 leftover).

Exiting winners with a resting limit at the target instead of a market order
saves fee_taker - fee_maker (4 bp) + slippage (2 bp) per TP exit, at the cost
of TOUCH-ONLY targets never filling (strict penetration). The engine knob
(tp_as_limit, default off) models exactly that; unit-tested in
test_tp_limit.py.

PRE-REGISTERED cells: {TRIPLE_CONF, PULL_CONF} x {tp market, tp limit} on
MAJORS8 + the BLEND50_CONF combination in both modes. Reported: stats,
TP-exit counts (fills lost to the touch rule), total fees.

ADOPTION RULE (pre-registered): adopt tp_as_limit into the promoted stack
iff the blend improves CAGR AND Sharpe does not degrade by more than 0.02
AND the lost-TP share is under 10% (a large loss share means the win-path
depends on touch fills — fragile).

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/tp_limit.py
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
from core.strategies import triple_confirm_bidir
from research.cost_engine import (apply_funding_real, regime_mask, fng_persist,
                                  sharpe_m, stats, build, FEE_TAKER, FEE_MAKER)
from research.entry_families import pullback_in_trend
from research.regime_upgrades import wf_labels_conf
from research.vol_target import vt_mult

MAJORS8 = [f"{b}-USDT" for b in
           ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX"]]
TOTAL = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))
COMMON_START = pd.Timestamp("2023-08-17", tz="UTC")
WARMUP_D = 365
BPD = 6

BOOKS = ("TRIPLE_CONF", "PULL_CONF")
MODES = {"mkt": False, "lim": True}


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print("TP-AS-LIMIT  MAJORS8  books x {tp market, tp limit}", flush=True)
    fng = fetch_fear_greed()

    eqs = {(b, m): {} for b in BOOKS for m in MODES}
    tp_n = {(b, m): 0 for b in BOOKS for m in MODES}
    fees = {(b, m): 0.0 for b in BOOKS for m in MODES}
    for p in MAJORS8:
        t0 = time.time()
        df = fetch_ohlcv_bybit(p, "4h", days=DAYS)
        regs, conf = wf_labels_conf(df, BPD)
        fa = align_to_bars(fng, df.index)
        fund = fetch_funding(p, days=DAYS, source="auto")
        a = regs.reindex(df.index, method="ffill").fillna("CHOP")
        mult = np.where(a == "CHOP", 0.5, 1.0) * \
            vt_mult(df).reindex(df.index).fillna(1.0).to_numpy() * \
            (0.5 + 0.5 * conf.reindex(df.index).fillna(1.0).to_numpy())
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]
        raws = {"TRIPLE_CONF": triple_confirm_bidir(df, tp_mult=6.0),
                "PULL_CONF": pullback_in_trend(df)}
        for b in BOOKS:
            sig = fng_persist(regime_mask(raws[b], regs), fa)
            sig["risk_mult"] = mult
            for m, lim in MODES.items():
                cfg = EnhancedBTConfig(
                    starting_equity=100.0, risk_per_trade=0.020,
                    max_leverage=5.0, eq_decay_tiers=DEFAULT_DECAY_TIERS,
                    cooldown_bars=3, fee_rate=FEE_TAKER, fee_maker=FEE_MAKER,
                    entry_style="maker_close", tp_as_limit=lim)
                eq, tr = run_backtest_enhanced(dfe, sig[sig.index >= cut], cfg)
                eqs[(b, m)][p] = apply_funding_real(eq, tr, fund)
                tp_n[(b, m)] += sum(1 for t in tr if t.reason == "tp")
                fees[(b, m)] += sum(t.fees for t in tr)
        print(f"  {p:10s} {time.time()-t0:5.0f}s", flush=True)

    idx = None
    for p in MAJORS8:
        e = eqs[("TRIPLE_CONF", "mkt")][p]
        e = e[e.index >= COMMON_START]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    split = idx[int(len(idx) * 0.6)]
    i_idx, o_idx = idx[idx < split], idx[idx >= split]
    w = {p: 1 / 8 for p in MAJORS8}

    print("\n" + "=" * 100)
    print(f"TP MARKET vs LIMIT  {idx[0].date()}..{idx[-1].date()}  "
          f"MAJORS8 EW @ ${TOTAL:.0f}")
    print("=" * 100)
    print(f"{'cell':18s}{'final$':>8s}{'CAGR%':>8s}{'Sh(mo)':>8s}{'MDD%':>7s}"
          f"{'worst':>7s}{'IS Sh':>7s}{'OOS Sh':>8s}{'TP exits':>10s}{'fees':>8s}")
    ports = {}
    for b in BOOKS:
        for m in MODES:
            port = build(eqs[(b, m)], w, idx)
            ports[(b, m)] = port
            s = stats(port)
            i = stats(port.reindex(i_idx).dropna())
            o = stats(port.reindex(o_idx).dropna())
            print(f"{b + '/' + m:18s}{s['final']:8.0f}{s['cagr']*100:8.1f}"
                  f"{s['sh_m']:8.2f}{s['mdd']*100:7.1f}{s['worst_mo']:7.1f}"
                  f"{i['sh_m']:7.2f}{o['sh_m']:8.2f}{tp_n[(b, m)]:10d}"
                  f"{fees[(b, m)]:8.1f}")

    res = {}
    for m in MODES:
        nt = ports[("TRIPLE_CONF", m)] / ports[("TRIPLE_CONF", m)].iloc[0]
        np_ = ports[("PULL_CONF", m)] / ports[("PULL_CONF", m)].iloc[0]
        bl = (0.5 * nt + 0.5 * np_) * TOTAL
        s = stats(bl)
        o = stats(bl.reindex(o_idx).dropna())
        res[m] = (s, o)
        print(f"{'BLEND50_CONF/' + m:18s}{s['final']:8.0f}{s['cagr']*100:8.1f}"
              f"{s['sh_m']:8.2f}{s['mdd']*100:7.1f}{s['worst_mo']:7.1f}"
              f"{'':7s}{o['sh_m']:8.2f}")

    lost = {b: 1 - tp_n[(b, 'lim')] / max(tp_n[(b, 'mkt')], 1) for b in BOOKS}
    print(f"\n  TP fills lost to strict penetration: "
          + "  ".join(f"{b} {lost[b]*100:.1f}%" for b in BOOKS))
    ok = (res["lim"][0]["cagr"] > res["mkt"][0]["cagr"]
          and res["lim"][0]["sh_m"] >= res["mkt"][0]["sh_m"] - 0.02
          and all(v < 0.10 for v in lost.values()))
    print(f"  DECISION: {'ADOPT tp_as_limit' if ok else 'KEEP tp market'} "
          f"(blend CAGR {res['mkt'][0]['cagr']*100:.1f} -> "
          f"{res['lim'][0]['cagr']*100:.1f}, Sh {res['mkt'][0]['sh_m']:.2f} -> "
          f"{res['lim'][0]['sh_m']:.2f})")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
