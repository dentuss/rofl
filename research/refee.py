"""RE-PRICE the deployed book with MEASURED Bybit fees — the L2 cost gate.

Not an experiment: no new cells, no strategy change, no parameter search. The
identical deployed config (research/deploy_report.py) run twice, changing only
the fee constants, to answer the pre-registered ROADMAP L2 question:

    "If measured costs degrade the edge >0.2 Sh, HALT and re-price."

WHY. The first two live stops closed on 2026-08-08 and Bybit's closed-PnL
gives ground truth for the first time:
    XRP  openFee 0.02594 / 72.0473 = 3.60 bp   closeFee 0.07368 / 73.678 = 10.00 bp
    DOGE openFee 0.02916 / 81.0074 = 3.60 bp   closeFee 0.08267 / 82.673 = 10.00 bp
and GET /v5/account/fee-rate confirms it outright: takerFeeRate 0.001,
makerFeeRate 0.00036. The cost model has always assumed 6 bp taker / 2 bp
maker — i.e. roughly HALF the real cost.

SLIPPAGE, measured on the same two fills (n=2, weak):
    DOGE SL trigger 0.07048 -> filled 0.07048   (0.0 bp)
    XRP  SL trigger 1.0438  -> filled 1.0436    (favourable by ~2 bp)
so the modelled 2 bp taker slip is NOT showing up and partly offsets the fee
rise. Both cells below are therefore run at the model's 2 bp slip (unchanged,
conservative) — lowering it on two observations would be exactly the kind of
flattering adjustment the ledger exists to prevent.

CELLS (one dimension, two points):
    MODEL     taker 6.0 bp, maker 2.0 bp   — every FINDINGS number to date
    MEASURED  taker 10.0 bp, maker 3.6 bp  — the account's real rates

Run:  PYTHONIOENCODING=utf-8 ./.venv/bin/python research/refee.py
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
from research.cost_engine import apply_funding_real, regime_mask, fng_persist, build
from research.regime_upgrades import wf_labels_conf
from research.vol_target import vt_mult

MAJORS8 = [f"{b}-USDT" for b in
           ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX"]]
TOTAL = float(_os.environ.get("TOTAL_EQUITY", 1800.00))
DAYS = int(_os.environ.get("DAYS", 2000))
COMMON_START = pd.Timestamp("2023-08-17", tz="UTC")
WARMUP_D, BPD = 365, 6

CELLS = [("MODEL    6.0bp/2.0bp", 0.0006, 0.0002),
         ("MEASURED 10.0bp/3.6bp", 0.0010, 0.00036)]


def stats(r: pd.Series) -> dict:
    eq = TOTAL * (1 + r).cumprod()
    yrs = max((r.index[-1] - r.index[0]).days, 1) / 365
    mo = eq.resample("ME").last().pct_change().dropna()
    sh = lambda s: float(s.mean() / s.std() * np.sqrt(12)) if len(s) > 2 and s.std() > 0 else 0.0
    n = len(mo)
    return dict(final=float(eq.iloc[-1]),
                cagr=((float(eq.iloc[-1]) / TOTAL) ** (1 / yrs) - 1) * 100,
                sh=sh(mo), mdd=float((eq / eq.cummax() - 1).min()) * 100,
                worst_m=float(mo.min()) * 100, win=float((mo > 0).mean() * 100),
                is_sh=sh(mo.iloc[:int(n * .6)]), oos_sh=sh(mo.iloc[int(n * .6):]))


def main() -> None:
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print("RE-PRICE — deployed book at MODEL vs MEASURED Bybit fees", flush=True)
    fng = fetch_fear_greed()
    eq = {n: {"t": {}, "p": {}} for n, _, _ in CELLS}

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
        sigs = {}
        for tag, raw in [("t", triple_confirm_bidir(df, tp_mult=6.0)),
                         ("p", pullback_in_trend(df))]:
            s = fng_persist(regime_mask(raw, regs), fa)
            s["risk_mult"] = mult
            sigs[tag] = s[s.index >= cut]
        for name, taker, maker in CELLS:
            for tag in ("t", "p"):
                cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                                       max_leverage=5.0,
                                       eq_decay_tiers=DEFAULT_DECAY_TIERS,
                                       cooldown_bars=3, fee_rate=taker,
                                       fee_maker=maker, entry_style="maker_close",
                                       tp_as_limit=True)
                e, tr = run_backtest_enhanced(dfe, sigs[tag], cfg)
                eq[name][tag][p] = apply_funding_real(e, tr, fund)
        print(f"  {p:10s} {time.time()-t0:5.0f}s", flush=True)

    idx = None
    for p in MAJORS8:
        e = eq[CELLS[0][0]]["t"][p]
        e = e[e.index >= COMMON_START]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    w = {p: 1 / 8 for p in MAJORS8}

    rows = {}
    for name, _, _ in CELLS:
        pt, pp = build(eq[name]["t"], w, idx), build(eq[name]["p"], w, idx)
        blend = 0.5 * pt / pt.iloc[0] + 0.5 * pp / pp.iloc[0]
        rows[name] = stats(blend.resample("1D").last().pct_change().dropna())

    print("\n" + "=" * 100)
    print(f"BLEND50_CONF, unit weights, {idx[0].date()}..{idx[-1].date()}")
    print("=" * 100)
    print(f"{'cell':24s}{'final$':>9s}{'CAGR%':>8s}{'Sh(mo)':>8s}{'dMDD%':>8s}"
          f"{'worstM%':>9s}{'win%':>7s}{'IS':>7s}{'OOS':>7s}")
    for name, _, _ in CELLS:
        s = rows[name]
        print(f"{name:24s}{s['final']:>9.0f}{s['cagr']:>8.1f}{s['sh']:>8.2f}"
              f"{s['mdd']:>8.1f}{s['worst_m']:>9.1f}{s['win']:>7.0f}"
              f"{s['is_sh']:>7.2f}{s['oos_sh']:>7.2f}")

    a, b = rows[CELLS[0][0]], rows[CELLS[1][0]]
    d_sh, d_cagr = b["sh"] - a["sh"], b["cagr"] - a["cagr"]
    print("\n" + "=" * 100)
    print("L2 COST GATE — pre-registered: >0.2 Sh degradation => HALT and re-price")
    print("=" * 100)
    print(f"  dSharpe  {d_sh:+.2f}      dCAGR {d_cagr:+.1f}pp      "
          f"dMDD {b['mdd']-a['mdd']:+.1f}pp")
    print(f"  VERDICT: {'*** BREACH — halt and re-price ***' if d_sh <= -0.20 else 'within tolerance (no halt)'}")
    print(f"\n  Full-history anchor is Sh ~1.2; at measured fees the common-window")
    print(f"  number becomes {b['sh']:.2f}. Sizing conversations should use this row.")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
