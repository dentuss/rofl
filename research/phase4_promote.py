"""Phase-4 promotion run — combine the two survivors honestly before adopting.

The regime_upgrades study validated CONF sizing (risk x (0.5 + 0.5 p_label))
on the TRIPLE-only book; pullback_validation validated the 50/50 TRIPLE+PULL
blend WITHOUT CONF. Adopting both at once would be an untested combination —
this run prices it.

PRE-REGISTERED cells (MAJORS8, adopted stack, common window):
  TRIPLE        reference (RSCD3+VT)
  TRIPLE_CONF   + confidence sizing            (validated in regime_upgrades)
  PULL          pullback book (RSCD3+VT)       (validated in pullback_validation)
  PULL_CONF     pullback + confidence sizing   (new cell)
  BLEND50       0.5 TRIPLE + 0.5 PULL          (validated adoption candidate)
  BLEND50_CONF  0.5 TRIPLE_CONF + 0.5 PULL_CONF (the candidate FINAL stack)
  BLEND75_CONF  0.75/0.25 (information only)

DECISION RULE (pre-registered): promote BLEND50_CONF iff it does not lose
Sharpe vs BLEND50 and OOS holds; otherwise promote BLEND50 and keep CONF
only on the triple book. No other cell can be promoted from this run.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/phase4_promote.py
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

BOOKS = ("TRIPLE", "TRIPLE_CONF", "PULL", "PULL_CONF")


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print(f"PHASE-4 PROMOTION RUN  MAJORS8  books={BOOKS}", flush=True)
    fng = fetch_fear_greed()

    eqs = {b: {} for b in BOOKS}
    for p in MAJORS8:
        t0 = time.time()
        df = fetch_ohlcv_bybit(p, "4h", days=DAYS)
        regs, conf = wf_labels_conf(df, BPD)
        fa = align_to_bars(fng, df.index)
        fund = fetch_funding(p, days=DAYS, source="auto")
        a = regs.reindex(df.index, method="ffill").fillna("CHOP")
        base_m = np.where(a == "CHOP", 0.5, 1.0) * \
            vt_mult(df).reindex(df.index).fillna(1.0).to_numpy()
        conf_m = base_m * (0.5 + 0.5 * conf.reindex(df.index).fillna(1.0)
                           .to_numpy())
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]
        raws = {"TRIPLE": triple_confirm_bidir(df, tp_mult=6.0),
                "PULL": pullback_in_trend(df)}
        for b in BOOKS:
            raw = raws["TRIPLE" if b.startswith("TRIPLE") else "PULL"]
            sig = fng_persist(regime_mask(raw, regs), fa)
            sig["risk_mult"] = conf_m if b.endswith("_CONF") else base_m
            cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                                   max_leverage=5.0,
                                   eq_decay_tiers=DEFAULT_DECAY_TIERS,
                                   cooldown_bars=3, fee_rate=FEE_TAKER,
                                   fee_maker=FEE_MAKER,
                                   entry_style="maker_close")
            eq, tr = run_backtest_enhanced(dfe, sig[sig.index >= cut], cfg)
            eqs[b][p] = apply_funding_real(eq, tr, fund)
        print(f"  {p:10s} {time.time()-t0:5.0f}s", flush=True)

    idx = None
    for p in MAJORS8:
        e = eqs["TRIPLE"][p][eqs["TRIPLE"][p].index >= COMMON_START]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    split = idx[int(len(idx) * 0.6)]
    i_idx, o_idx = idx[idx < split], idx[idx >= split]
    b3 = [idx[0] + (idx[-1] - idx[0]) * k / 3 for k in range(4)]
    w = {p: 1 / 8 for p in MAJORS8}

    ports = {b: build(eqs[b], w, idx) for b in BOOKS}
    nrm = {b: ports[b] / ports[b].iloc[0] for b in BOOKS}
    ports["BLEND50"] = (0.5 * nrm["TRIPLE"] + 0.5 * nrm["PULL"]) * TOTAL
    ports["BLEND50_CONF"] = (0.5 * nrm["TRIPLE_CONF"]
                             + 0.5 * nrm["PULL_CONF"]) * TOTAL
    ports["BLEND75_CONF"] = (0.75 * nrm["TRIPLE_CONF"]
                             + 0.25 * nrm["PULL_CONF"]) * TOTAL

    print("\n" + "=" * 96)
    print(f"PROMOTION CELLS  {idx[0].date()}..{idx[-1].date()}  "
          f"MAJORS8 EW @ ${TOTAL:.0f}")
    print("=" * 96)
    print(f"{'cell':14s}{'final$':>8s}{'CAGR%':>8s}{'Sh(mo)':>8s}{'MDD%':>7s}"
          f"{'worst':>7s}{'IS Sh':>7s}{'OOS Sh':>8s}{'thirds':>22s}")
    res = {}
    for name, port in ports.items():
        s = stats(port)
        i = stats(port.reindex(i_idx).dropna())
        o = stats(port.reindex(o_idx).dropna())
        th = "  ".join(
            f"{sharpe_m(port.reindex(idx[(idx >= b3[k]) & (idx < b3[k+1])]).dropna()):+.2f}"
            for k in range(3))
        res[name] = (s, o)
        print(f"{name:14s}{s['final']:8.0f}{s['cagr']*100:8.1f}{s['sh_m']:8.2f}"
              f"{s['mdd']*100:7.1f}{s['worst_mo']:7.1f}{i['sh_m']:7.2f}"
              f"{o['sh_m']:8.2f}{th:>22s}")

    ok = res["BLEND50_CONF"][0]["sh_m"] >= res["BLEND50"][0]["sh_m"] \
        and res["BLEND50_CONF"][1]["sh_m"] >= res["BLEND50"][1]["sh_m"] - 0.05
    print(f"\nDECISION: promote "
          f"{'BLEND50_CONF' if ok else 'BLEND50 (CONF on triple book only)'}")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
