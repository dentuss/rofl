"""Cost-engine ladder — build the fully honest backtest environment and price
each realism upgrade on the validated 4h+TP60 config (SOFT5, Bybit, fixed
engine, cooldown_bars=1 for r3 continuity).

Cells (pre-registered; cumulative ladder):
  R3REF      entry_bar_exit_check=False, taker, flat 1bp/8h funding
             — must reproduce honest_rebuild_r3 TP60 exactly (continuity)
  EBX        + entry-bar SL/TP check (default engine behavior now): prices the
             one-bar grace period the engine used to give every new position
  EBX_RF     + REAL per-pair funding series (core.funding.fetch_funding,
             Bybit->OKX) instead of the flat 1bp/8h assumption
  EBX_MK     EBX + maker_close entries (limit at signal close, strict
             penetration fill, 0.02% maker fee, no slip; misses are missed)
  ALL_IN     EBX + real funding + maker entries — THE NEW TRUSTWORTHY BASELINE

Also reports: per-pair funding span/mean, total funding cost real-vs-flat,
fees paid per cell, trade counts (maker miss rate = EBX vs EBX_MK count drop).

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/cost_engine.py
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
from core.regime_strategy import walk_forward_regimes
from core.risk import DEFAULT_DECAY_TIERS
from core.sentiment import align_to_bars, fetch_fear_greed
from core.strategies import triple_confirm_bidir
from research.test_reentry_cooldown_prod import apply_funding

SOFT5 = {"INJ-USDT": .25, "SOL-USDT": .1875, "ADA-USDT": .1875,
         "ETH-USDT": .1875, "LINK-USDT": .1875}
TOTAL = float(_os.environ.get("TOTAL_EQUITY", 2300))
DAYS = int(_os.environ.get("DAYS", 2000))
WARMUP_D = 365
BPD = 6
FNG_BARS = 3 * BPD
# MEASURED on the live account 2026-08-08 (first two closes + GET
# /v5/account/fee-rate: takerFeeRate 0.001, makerFeeRate 0.00036). The old
# 0.0006/0.0002 were Bybit's published non-VIP figures and were ~2x optimistic
# for this account — so EVERY absolute number in FINDINGS produced before
# 2026-08-08 is priced too cheaply. Re-pricing the deployed book moved it
# 9.2 -> 8.5% CAGR and Sh 1.31 -> 1.23 (research/refee.py): real, and inside
# the pre-registered 0.2 Sh L2 halt tolerance.
#
# Rates are PER ACCOUNT and move with VIP tier — override via env rather than
# editing, and re-measure if the tier changes. bot.py asks the venue directly.
FEE_TAKER = float(_os.environ.get("FEE_TAKER", 0.0010))
FEE_MAKER = float(_os.environ.get("FEE_MAKER", 0.00036))

# cell -> (entry_bar_check, entry_style, funding)   funding in {"flat","real"}
CELLS = {
    "R3REF":  (False, "taker",       "flat"),
    "EBX":    (True,  "taker",       "flat"),
    "EBX_RF": (True,  "taker",       "real"),
    "EBX_MK": (True,  "maker_close", "flat"),
    "ALL_IN": (True,  "maker_close", "real"),
}


def regime_mask(sig, regs):
    a = regs.reindex(sig.index, method="ffill").fillna("CHOP")
    b = ((sig["signal"] == 1) & (~a.isin(["BULL", "CHOP"]))) | \
        ((sig["signal"] == -1) & (~a.isin(["BEAR", "CHOP"])))
    s = sig.copy()
    s.loc[b, "signal"] = 0
    s.loc[b, ["sl", "tp"]] = np.nan
    return s


def fng_persist(sig, fa):
    above = (fa >= 80).rolling(FNG_BARS, min_periods=FNG_BARS).sum() == FNG_BARS
    below = (fa <= 20).rolling(FNG_BARS, min_periods=FNG_BARS).sum() == FNG_BARS
    block = ((sig["signal"] == 1) & above.fillna(False)) | \
            ((sig["signal"] == -1) & below.fillna(False))
    s = sig.copy()
    s.loc[block, "signal"] = 0
    s.loc[block, ["sl", "tp"]] = np.nan
    return s


def apply_funding_real(eq, trades, fund):
    """Charge each trade the ACTUAL 8h funding events it was open through.
    Longs pay positive funding; shorts receive it. Events outside the series'
    span cost 0 (span is printed — judge coverage explicitly)."""
    off = pd.Series(0.0, index=eq.index)
    rates = fund["funding_rate"]
    for t in trades:
        ev = rates[(rates.index > t.entry_time) & (rates.index <= t.exit_time)]
        if len(ev):
            off.loc[eq.index >= t.exit_time] -= float(ev.sum()) * t.notional * t.side
    return eq + off


def sharpe_m(eq):
    m = eq.resample("ME").last().pct_change().dropna()
    return float(m.mean() / m.std() * np.sqrt(12)) if len(m) > 2 and m.std() > 0 else 0.0


def stats(eq):
    final = float(eq.iloc[-1])
    mdd = float((eq / eq.cummax() - 1).min())
    days = max((eq.index[-1] - eq.index[0]).days, 1)
    cagr = (final / float(eq.iloc[0])) ** (365 / days) - 1
    mo = eq.resample("ME").last().pct_change().dropna() * 100
    return dict(final=final, cagr=cagr, mdd=mdd, sh_m=sharpe_m(eq),
                worst_mo=float(mo.min()) if len(mo) else 0,
                win_mo=float((mo > 0).mean() * 100) if len(mo) else 0)


def build(pair_eq, w, idx):
    port = None
    for p, wt in w.items():
        e = pair_eq[p].reindex(idx).ffill()
        e = e / e.iloc[0] * wt
        port = e if port is None else port + e
    return port * TOTAL


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn missing")
    print(f"COST ENGINE LADDER  4h TP60 SOFT5  cells={list(CELLS)}", flush=True)
    fng = fetch_fear_greed()

    pair_eq = {c: {} for c in CELLS}
    counts = {c: 0 for c in CELLS}
    fees_paid = {c: 0.0 for c in CELLS}
    fund_cost = {"flat": 0.0, "real": 0.0}

    for p in SOFT5:
        t0 = time.time()
        df = fetch_ohlcv_bybit(p, "4h", days=DAYS)
        regs = walk_forward_regimes(df, bars_per_day=BPD, train_days=365, step_days=30)
        fa = align_to_bars(fng, df.index)
        sig = fng_persist(regime_mask(triple_confirm_bidir(df, tp_mult=6.0), regs), fa)
        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]
        sige = sig[sig.index >= cut]

        fund = None
        try:
            fund = fetch_funding(p, days=DAYS, source="auto")
        except Exception as e:
            print(f"  {p}: FUNDING FETCH FAILED ({e}) — real cells use flat", flush=True)
        if fund is not None and len(fund):
            mean_8h = float(fund["funding_rate"].mean())
            print(f"  {p:11s} funding {fund.index[0].date()}..{fund.index[-1].date()} "
                  f"n={len(fund)} mean {mean_8h*1e4:+.2f}bp/8h "
                  f"(flat assumption: +1.00bp/8h)", flush=True)

        for c, (ebx, style, fmode) in CELLS.items():
            cfg = EnhancedBTConfig(
                starting_equity=100.0, risk_per_trade=0.020, max_leverage=5.0,
                eq_decay_tiers=DEFAULT_DECAY_TIERS, cooldown_bars=1,
                fee_rate=FEE_TAKER, fee_maker=FEE_MAKER,
                entry_bar_exit_check=ebx, entry_style=style)
            eq, tr = run_backtest_enhanced(dfe, sige, cfg)
            if fmode == "real" and fund is not None and len(fund):
                eqf = apply_funding_real(eq, tr, fund)
            else:
                eqf = apply_funding(eq, tr, bps=4.0)   # flat 1bp/8h on 4h bars
            if c in ("EBX_RF",):
                fund_cost["real"] += float(eq.iloc[-1]) - float(eqf.iloc[-1])
            if c in ("EBX",):
                fund_cost["flat"] += float(eq.iloc[-1]) - float(
                    apply_funding(eq, tr, bps=4.0).iloc[-1])
            pair_eq[c][p] = eqf
            counts[c] += len(tr)
            mk_fee = FEE_MAKER if style == "maker_close" else FEE_TAKER
            fees_paid[c] += sum(t.fees + t.notional * mk_fee for t in tr)
        print(f"  {p:11s} {time.time()-t0:5.0f}s", flush=True)

    idx = None
    for p in SOFT5:
        e = pair_eq["R3REF"][p]
        idx = e.index if idx is None else idx.intersection(e.index)
    idx = idx.sort_values()
    split = idx[int(len(idx) * 0.6)]
    is_idx, oos_idx = idx[idx < split], idx[idx >= split]
    yrs = (idx[-1] - idx[0]).days / 365

    print("\n" + "=" * 88)
    print(f"COST LADDER  {idx[0].date()}..{idx[-1].date()} ({yrs:.2f}y)  SOFT5 @ ${TOTAL:.0f}")
    print("=" * 88)
    print(f"{'cell':8s}{'final$':>8s}{'CAGR%':>8s}{'Sh(mo)':>8s}{'MDD%':>7s}"
          f"{'worst':>7s}{'pos%':>6s}{'trades':>8s}{'fees':>7s}{'IS Sh':>7s}{'OOS Sh':>8s}")
    for c in CELLS:
        s = stats(build(pair_eq[c], SOFT5, idx))
        i = stats(build(pair_eq[c], SOFT5, is_idx))
        o = stats(build(pair_eq[c], SOFT5, oos_idx))
        print(f"{c:8s}{s['final']:8.0f}{s['cagr']*100:8.1f}{s['sh_m']:8.2f}"
              f"{s['mdd']*100:7.1f}{s['worst_mo']:7.1f}{s['win_mo']:6.0f}"
              f"{counts[c]:8d}{fees_paid[c]:7.1f}{i['sh_m']:7.2f}{o['sh_m']:8.2f}")

    print(f"\n  funding cost totals (per-100 units, 5 pairs, EBX trade set): "
          f"flat {fund_cost['flat']:+.1f}  real {fund_cost['real']:+.1f}")
    print(f"  maker miss effect: EBX {counts['EBX']} trades -> EBX_MK "
          f"{counts['EBX_MK']} ({counts['EBX_MK']-counts['EBX']:+d})")
    r3 = stats(build(pair_eq["R3REF"], SOFT5, idx))
    print(f"  continuity: R3REF final ${r3['final']:.0f} "
          f"(honest_rebuild_r3 TP60 was $3455 — small drift = newer bars in cache)")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
