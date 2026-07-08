"""Regime layer upgrades — the GMM is the strongest verified overlay; can two
cheap, pre-registerable refinements buy more Sharpe on MAJORS8/RSCD3+VT?

PRE-REGISTERED cells (all walk-forward, zero new fitted constants):
  BASE         adopted stack (own-pair walk-forward regimes, chop x0.5, VT)
  CONF         sizing scaled by GMM posterior confidence of the assigned
               label: risk_mult *= (0.5 + 0.5 * p_label). p in [1/3, 1] so
               the multiplier is in [0.67, 1.0] — pure de-risking of
               low-conviction bars; judged on Sharpe/MDD, not CAGR.
               Bars before the first prediction window get p := 1 (neutral).
  POOLED       BTC's walk-forward regime series drives EVERY name's
               directional mask + CHOP half-sizing (one macro clock instead
               of 8 noisy per-pair fits); own-pair VT unchanged.
  POOLED_CONF  POOLED + BTC-confidence sizing.

Adoption bar: a cell replaces BASE only if full Sh(mo) improves AND OOS
holds AND MDD does not degrade materially.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/regime_upgrades.py
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
from core.regime import build_features, feature_matrix, fit_gmm, predict_regimes
from core.risk import DEFAULT_DECAY_TIERS
from core.sentiment import align_to_bars, fetch_fear_greed
from core.strategies import triple_confirm_bidir
from research.cost_engine import (apply_funding_real, regime_mask, fng_persist,
                                  sharpe_m, stats, build, FEE_TAKER, FEE_MAKER)
from research.vol_target import vt_mult

MAJORS8 = [f"{b}-USDT" for b in
           ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX"]]
TOTAL = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))
COMMON_START = pd.Timestamp("2023-08-17", tz="UTC")
WARMUP_D = 365
BPD = 6


def wf_labels_conf(df: pd.DataFrame, bpd: int, train_days: int = 365,
                   step_days: int = 30):
    """Walk-forward labels (identical logic to walk_forward_regimes) plus the
    posterior probability of the assigned label. Pre-window bars: CHOP / 1.0."""
    fm = feature_matrix(build_features(df, bars_per_day=bpd))
    labels = pd.Series("CHOP", index=df.index)
    conf = pd.Series(1.0, index=df.index)
    tb, sb = train_days * bpd, step_days * bpd
    cur = tb
    while cur < len(df):
        tr = fm.iloc[max(0, cur - tb):cur]
        if len(tr.dropna()) < 200:
            cur += sb
            continue
        try:
            gmm, mapping = fit_gmm(tr, n_components=3)
        except Exception:
            cur += sb
            continue
        end = min(cur + sb, len(df))
        te = fm.iloc[cur:end]
        preds = predict_regimes(gmm, mapping, te)
        labels.loc[preds.index] = preds.values
        valid = te.replace([np.inf, -np.inf], np.nan).dropna()
        if len(valid):
            pm = gmm.predict_proba(valid.values).max(axis=1)
            conf.loc[valid.index] = pm
        cur = end
    return labels, conf


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print("REGIME UPGRADES  MAJORS8  cells=[BASE, CONF, POOLED, POOLED_CONF]",
          flush=True)
    fng = fetch_fear_greed()

    btc_df = fetch_ohlcv_bybit("BTC-USDT", "4h", days=DAYS)
    btc_regs, btc_conf = wf_labels_conf(btc_df, BPD)
    print(f"  BTC pooled clock ready  dist={btc_regs.value_counts().to_dict()}",
          flush=True)

    CELLS = ("BASE", "CONF", "POOLED", "POOLED_CONF")
    eqs = {c: {} for c in CELLS}
    for p in MAJORS8:
        t0 = time.time()
        df = fetch_ohlcv_bybit(p, "4h", days=DAYS)
        own_regs, own_conf = wf_labels_conf(df, BPD)
        fa = align_to_bars(fng, df.index)
        fund = fetch_funding(p, days=DAYS, source="auto")
        vt = vt_mult(df).reindex(df.index).fillna(1.0)
        raw = triple_confirm_bidir(df, tp_mult=6.0)
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]

        pooled_regs = btc_regs.reindex(df.index, method="ffill").fillna("CHOP")
        pooled_conf = btc_conf.reindex(df.index, method="ffill").fillna(1.0)

        variants = {
            "BASE":        (own_regs, None),
            "CONF":        (own_regs, own_conf),
            "POOLED":      (pooled_regs, None),
            "POOLED_CONF": (pooled_regs, pooled_conf),
        }
        for cell, (regs, conf) in variants.items():
            sig = fng_persist(regime_mask(raw, regs), fa)
            a = regs.reindex(sig.index, method="ffill").fillna("CHOP")
            m = np.where(a == "CHOP", 0.5, 1.0) * vt.to_numpy()
            if conf is not None:
                m = m * (0.5 + 0.5 * conf.reindex(sig.index).fillna(1.0)
                         .to_numpy())
            sig["risk_mult"] = m
            cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                                   max_leverage=5.0,
                                   eq_decay_tiers=DEFAULT_DECAY_TIERS,
                                   cooldown_bars=3, fee_rate=FEE_TAKER,
                                   fee_maker=FEE_MAKER,
                                   entry_style="maker_close")
            eq, tr = run_backtest_enhanced(dfe, sig[sig.index >= cut], cfg)
            eqs[cell][p] = apply_funding_real(eq, tr, fund)
        print(f"  {p:10s} {time.time()-t0:5.0f}s", flush=True)

    idx = None
    for p in MAJORS8:
        e = eqs["BASE"][p][eqs["BASE"][p].index >= COMMON_START]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    split = idx[int(len(idx) * 0.6)]
    i_idx, o_idx = idx[idx < split], idx[idx >= split]
    b3 = [idx[0] + (idx[-1] - idx[0]) * k / 3 for k in range(4)]
    w = {p: 1 / 8 for p in MAJORS8}

    print("\n" + "=" * 96)
    print(f"REGIME UPGRADES  {idx[0].date()}..{idx[-1].date()}  "
          f"MAJORS8 EW @ ${TOTAL:.0f}")
    print("=" * 96)
    print(f"{'cell':13s}{'final$':>8s}{'CAGR%':>8s}{'Sh(mo)':>8s}{'MDD%':>7s}"
          f"{'worst':>7s}{'IS Sh':>7s}{'OOS Sh':>8s}{'thirds':>22s}")
    for cell in CELLS:
        s = stats(build(eqs[cell], w, idx))
        i = stats(build(eqs[cell], w, i_idx))
        o = stats(build(eqs[cell], w, o_idx))
        th = "  ".join(
            f"{sharpe_m(build(eqs[cell], w, idx[(idx >= b3[k]) & (idx < b3[k+1])])):+.2f}"
            for k in range(3))
        print(f"{cell:13s}{s['final']:8.0f}{s['cagr']*100:8.1f}{s['sh_m']:8.2f}"
              f"{s['mdd']*100:7.1f}{s['worst_mo']:7.1f}{i['sh_m']:7.2f}"
              f"{o['sh_m']:8.2f}{th:>22s}")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
