"""TRADE-FLOW INFORMATION CONTENT — 6.4 years of Bybit tick trades.

An IC SCREEN, deliberately not a strategy. Modelled on research/funding_signal.py,
which measured IC ~= 0 for funding-as-signal and rejected it before any
backtest was written. Same discipline here: if the information is not there,
no amount of strategy construction rescues it, and we stop cheaply.

WHY A SCREEN AND NOT A BACKTEST. MAJORS8 x 6.4y of tick trades is ~280 GB —
a census is impossible. A pre-registered SAMPLE of days cannot produce an
equity curve (the days are disjoint), but it estimates predictive information
perfectly well, because IC is a per-observation statistic. If IC clears the
bar, a full backtest earns its download budget. If it does not, we have saved
~6 hours of downloading and learned the same thing.

THE STANDING LESSON THIS MUST ANSWER TO. The tier-1 moonshot battery killed
6 of 6 ideas: "the signals exist (lead-lag gross was +3-8bp, real) but they
are 3-7x below our cost floor — the moat is execution cost, not signal
discovery." So an IC that is statistically real but worth less than the
round-trip cost is a REJECTION, not a finding. Measured cost floor is now
13.6 bp round trip (maker 3.6 in, taker 10.0 out).

PRE-REGISTERED CELLS — four features, fixed before the first download. Each is
computed per 4h bar from that bar's ticks, then correlated with the NEXT bar's
return. No parameter search: each feature has exactly one parameterisation.

  F1  cvd_bar        (buy_vol - sell_vol) / total_vol, this bar
  F2  cvd_6bar       same, aggregated over the trailing 6 bars (~24h)
  F3  large_imb      F1 restricted to trades >= the 95th pct trade size,
                     computed per symbol-day (the "whale flow" hypothesis)
  F4  count_imb      (n_buy - n_sell) / n_trades, this bar

TARGET: next-bar (4h) log return. Reported alongside same-bar return purely as
a CONTEMPORANEOUS CONTROL — flow and price move together mechanically, so a
large same-bar correlation with a null next-bar correlation is the signature
of no predictive content, and is the expected outcome.

SAMPLE (fixed in advance): BTCUSDT + ETHUSDT, every 20th calendar day from
2020-03-25 to 2026-08-08. Two symbols is a FEASIBILITY screen, not a universe
claim — G4 generalisation across MAJORS8 is owed only if the screen passes.

BAR TO CLEAR, declared before running:
  * |IC| >= 0.03 on next-bar return (funding_signal rejected at mean -0.02),
    AND consistent sign across both symbols,
  * AND the implied edge per trade must exceed 13.6 bp round trip.
Anything less is a REJECTION and gets a FINDINGS entry as prominent as a win.

Run:  PYTHONIOENCODING=utf-8 ./.venv/bin/python research/tradeflow_ic.py
"""
from __future__ import annotations

import sys as _sys, os as _os, time
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

from pathlib import Path

import numpy as np
import pandas as pd

from core.bybit_archive import available, load_trades

SYMS = ["BTCUSDT", "ETHUSDT"]
START, END = "2020-03-25", "2026-08-08"
STEP_DAYS = int(_os.environ.get("STEP_DAYS", 20))
CACHE = Path(_os.environ.get("ARCH_CACHE", "/tmp/arch_cache"))
COST_BP = 13.6          # measured round trip: 3.6 maker in + 10.0 taker out
IC_BAR = 0.03


def bar_features(tr: pd.DataFrame) -> pd.DataFrame:
    """Aggregate one day of ticks into 4h bars + the four pre-registered features."""
    tr = tr.copy()
    tr["buy"] = (tr["side"] == "Buy")
    big = tr["size"] >= tr["size"].quantile(0.95)     # per symbol-day, as registered
    tr["bs"] = np.where(tr.buy, tr["size"], 0.0)
    tr["ss"] = np.where(~tr.buy, tr["size"], 0.0)
    tr["bs_big"] = np.where(tr.buy & big, tr["size"], 0.0)
    tr["ss_big"] = np.where((~tr.buy) & big, tr["size"], 0.0)
    tr["nb"] = tr.buy.astype(float)
    g = tr.set_index("ts").resample("4h")
    d = g.agg(vol=("size", "sum"), bs=("bs", "sum"), ss=("ss", "sum"),
              bsb=("bs_big", "sum"), ssb=("ss_big", "sum"),
              n=("size", "size"), nb=("nb", "sum"),
              close=("price", "last"))
    d = d[d["vol"] > 0]
    d["cvd_bar"] = (d.bs - d.ss) / d.vol
    big_tot = d.bsb + d.ssb
    d["large_imb"] = np.where(big_tot > 0, (d.bsb - d.ssb) / big_tot.replace(0, np.nan), np.nan)
    d["count_imb"] = np.where(d.n > 0, (2 * d.nb - d.n) / d.n, np.nan)
    return d


def main() -> None:
    days = pd.date_range(START, END, freq=f"{STEP_DAYS}D").strftime("%Y-%m-%d")
    print(f"TRADE-FLOW IC — {len(days)} sampled days x {len(SYMS)} symbols "
          f"(every {STEP_DAYS}d, {START}..{END})", flush=True)
    frames = {}
    for sym in SYMS:
        rows, got, miss, t0 = [], 0, 0, time.time()
        for i, day in enumerate(days):
            try:
                tr = load_trades(sym, day, cache=CACHE)
            except Exception:
                miss += 1
                continue
            if tr.empty:
                miss += 1
                continue
            b = bar_features(tr)
            b["day"] = day
            rows.append(b)
            got += 1
            if (i + 1) % 25 == 0:
                print(f"  {sym} {i+1}/{len(days)} days  ok={got} miss={miss}  "
                      f"{time.time()-t0:5.0f}s", flush=True)
        if rows:
            d = pd.concat(rows).sort_index()
            d["cvd_6bar"] = d["cvd_bar"].rolling(6).mean()
            d["ret"] = np.log(d["close"]).diff()
            d["fwd"] = d["ret"].shift(-1)
            # only keep forward returns INSIDE a sampled day (days are disjoint)
            d.loc[d["day"] != d["day"].shift(-1), "fwd"] = np.nan
            frames[sym] = d
            print(f"  {sym}: {got} days, {len(d):,} bars, "
                  f"{d['fwd'].notna().sum():,} usable forward returns", flush=True)

    if not frames:
        print("\nno data collected")
        return

    feats = ["cvd_bar", "cvd_6bar", "large_imb", "count_imb"]
    print("\n" + "=" * 92)
    print("SPEARMAN IC — feature vs NEXT-bar return (and same-bar control)")
    print("=" * 92)
    print(f"  {'feature':12s}" + "".join(f"{s:>22s}" for s in SYMS))
    print(f"  {'':12s}" + "".join(f"{'next':>11s}{'same':>11s}" for _ in SYMS))
    table = {}
    for f in feats:
        line = f"  {f:12s}"
        table[f] = {}
        for sym in SYMS:
            d = frames[sym]
            m = d[[f, "fwd", "ret"]].dropna()
            ic = m[f].corr(m["fwd"], method="spearman") if len(m) > 30 else np.nan
            cc = m[f].corr(m["ret"], method="spearman") if len(m) > 30 else np.nan
            table[f][sym] = (ic, len(m))
            line += f"{ic:>11.3f}{cc:>11.3f}"
        print(line)

    print("\n" + "=" * 92)
    print("VERDICT vs the pre-registered bar")
    print("=" * 92)
    print(f"  bar: |IC| >= {IC_BAR}, same sign on both symbols, edge > {COST_BP} bp round trip")
    any_pass = False
    for f in feats:
        ics = [table[f][s][0] for s in SYMS]
        n = min(table[f][s][1] for s in SYMS)
        ok_mag = all(abs(x) >= IC_BAR for x in ics if np.isfinite(x))
        ok_sign = len({np.sign(x) for x in ics if np.isfinite(x)}) == 1
        # crude edge translation: IC * sigma(fwd) captured per trade, in bp
        sig = float(np.nanmedian([frames[s]["fwd"].std() for s in SYMS])) * 1e4
        edge = abs(np.nanmean(ics)) * sig
        ok_cost = edge > COST_BP
        verdict = "PASS" if (ok_mag and ok_sign and ok_cost) else "reject"
        any_pass |= (verdict == "PASS")
        print(f"  {f:12s} IC {ics[0]:+.3f}/{ics[1]:+.3f}  n={n:,}  "
              f"implied edge {edge:5.1f} bp vs {COST_BP} bp cost  -> {verdict}"
              f"{'' if ok_mag else '  [|IC| below bar]'}"
              f"{'' if ok_sign else '  [sign disagrees]'}")
    print()
    if not any_pass:
        print("  NO CELL CLEARS THE BAR. Consistent with the tier-1 moonshot")
        print("  conclusion: order-flow information is real but sits below the")
        print("  cost floor. No backtest is owed; the download budget is saved.")
    else:
        print("  A cell cleared the screen. It has earned a full backtest and the")
        print("  gate battery — NOT adoption. G4 across MAJORS8 is the next step.")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
