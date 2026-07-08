"""MOONSHOT HEARTBEAT BATTERY — five aggressive-program ideas, event-study
form, honest costs, pre-registered kill bars. Purpose: detect a PULSE, not
adopt anything. A survivor here earns a full engine-level pre-registered
study + the complete gate battery before any capital discussion (ROADMAP
"Moonshot program" rules).

COSTS (pre-registered, deliberately harsh): every event trade is TAKER both
sides + slippage = 6+6 bp fees + 5+5 bp slip = 22 bp round trip. A fast edge
that cannot survive taker costs is not an edge for us.

MULTIPLE-TESTING HONESTY: 5 studies x ~3 horizons ~ 15 tests; at t>=2 expect
~1 false positive by chance. A heartbeat is a REASON TO STUDY, not a result.

STUDIES (all events non-overlapping within a symbol):
  A CRASH-FADE (15m, 6 majors): event = 1-bar return z < -3 AND volume z > 2
    (z over trailing 96 bars). LONG next bar, forward {4,8,16,32} bars.
    KILL: no horizon with mean_net>0 & t>=2, or <60% of years positive.
  B FUNDING-WINDOW (15m + real 8h funding): events = settlements with
    funding in its top/bottom decile (trailing per-symbol). Trade AGAINST
    the crowded side from the settlement bar (short when funding extreme
    positive, long when extreme negative), forward {4,8,16} bars.
    KILL: same bar as A.
  C SQUEEZE BREAKOUT (1h): squeeze = ATR14/close below trailing-720-bar
    20th pct; event = squeezed close breaking the 48-bar high (long) or low
    (short). Forward {12,24,48} bars, direction of the break.
    KILL: t<2 at all horizons OR does not beat a matched random-event null
    (95th pct of 200 draws).
  D BTC->ALTS LEAD-LAG (15m and 1h): event = BTC 1-bar |z|>2.5; trade the
    5-alt basket in BTC's direction next bar, forward {1,2,4,8} bars.
    KILL: t<2 everywhere.
  E CROSS-SECTIONAL REVERSAL (1d, QUAL23): residual-vs-BTC 2d return, rank
    daily; events = names in the extreme quintiles; LONG losers / SHORT
    winners, forward 2d residual return.
    KILL: pooled t<2 or <60% years positive.

Run:  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/moonshot_heartbeats.py
"""
from __future__ import annotations

import sys as _sys, os as _os, time
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

import numpy as np
import pandas as pd

from core.data import fetch_ohlcv_bybit
from core.funding import fetch_funding

MAJ6 = [f"{b}-USDT" for b in ["BTC", "ETH", "SOL", "XRP", "DOGE", "AVAX"]]
ALTS5 = MAJ6[1:]
QUAL23 = [f"{b}-USDT" for b in
          ["BTC", "ETH", "SOL", "ADA", "LINK", "AVAX", "NEAR", "AAVE", "GRT",
           "RUNE", "DOGE", "DOT", "ATOM", "LTC", "XRP", "BNB", "FIL", "OP",
           "UNI", "ETC", "BCH", "TRX", "SAND"]]
DAYS_FAST = int(_os.environ.get("DAYS_FAST", 500))
COST = 0.0022          # 22 bp round trip, taker+slip, pre-registered
RNG = np.random.default_rng(42)


def dedup(idx_positions: np.ndarray, min_gap: int) -> list[int]:
    out, last = [], -10**9
    for i in idx_positions:
        if i - last >= min_gap:
            out.append(int(i))
            last = i
    return out


def fwd_ret(close: pd.Series, i: int, h: int) -> float:
    j = min(i + 1 + h, len(close) - 1)        # enter next bar, hold h bars
    if j <= i + 1:
        return np.nan
    return float(close.iloc[j] / close.iloc[i + 1] - 1)


def report(name: str, rows: dict[int, list[tuple[pd.Timestamp, float]]],
           kill_note: str, null_95: dict[int, float] | None = None):
    print(f"\n--- {name} ---")
    print(f"{'hold':>6s}{'events':>8s}{'net_bp':>9s}{'t':>7s}{'yrs+':>6s}"
          + ("" if null_95 is None else f"{'null95_bp':>11s}") + "  verdict")
    alive = False
    for h, evs in rows.items():
        r = pd.Series([x[1] for x in evs], dtype=float).dropna() - COST
        if len(r) < 30:
            print(f"{h:>6d}{len(r):>8d}      too few events")
            continue
        t = float(r.mean() / r.std() * np.sqrt(len(r))) if r.std() > 0 else 0.0
        yr = pd.Series([x[1] for x in evs],
                       index=[x[0] for x in evs]).dropna() - COST
        ymeans = yr.groupby(yr.index.year).agg(["mean", "count"])
        ymeans = ymeans[ymeans["count"] >= 5]
        yrs_pos = float((ymeans["mean"] > 0).mean() * 100) if len(ymeans) else 0
        ok = r.mean() > 0 and t >= 2 and yrs_pos >= 60
        if null_95 is not None:
            ok = ok and r.mean() * 1e4 > null_95.get(h, np.inf)
        alive = alive or ok
        line = (f"{h:>6d}{len(r):>8d}{r.mean()*1e4:>9.1f}{t:>7.2f}"
                f"{yrs_pos:>5.0f}%")
        if null_95 is not None:
            line += f"{null_95.get(h, float('nan')):>11.1f}"
        print(line + ("   << HEARTBEAT" if ok else ""))
    print(f"  VERDICT: {'HEARTBEAT — earn a full study' if alive else 'DEAD'}"
          f"   [{kill_note}]")
    return alive


def main():
    print("MOONSHOT HEARTBEATS  cost=22bp RT taker (pre-registered)", flush=True)
    d15, d1h, fund = {}, {}, {}
    for p in MAJ6:
        t0 = time.time()
        d15[p] = fetch_ohlcv_bybit(p, "15m", days=DAYS_FAST)
        d1h[p] = fetch_ohlcv_bybit(p, "1h", days=DAYS_FAST)
        f = fetch_funding(p, days=DAYS_FAST, source="auto")
        fund[p] = f["funding_rate"] if f is not None and len(f) else \
            pd.Series(dtype=float)
        print(f"  {p:10s} 15m={len(d15[p])} 1h={len(d1h[p])} "
              f"fund={len(fund[p])}  {time.time()-t0:.0f}s", flush=True)

    verdicts = {}

    # A ------------------------------------------------------------- crash-fade
    rows = {h: [] for h in (4, 8, 16, 32)}
    for p in MAJ6:
        df = d15[p]
        r1 = df["close"].pct_change()
        z = (r1 - r1.rolling(96).mean()) / r1.rolling(96).std()
        vz = (df["volume"] - df["volume"].rolling(96).mean()) \
            / df["volume"].rolling(96).std()
        ev = np.flatnonzero(((z < -3) & (vz > 2)).to_numpy())
        for i in dedup(ev, 8):
            for h in rows:
                rows[h].append((df.index[i], fwd_ret(df["close"], i, h)))
    verdicts["A crash-fade"] = report(
        "A. CRASH-FADE 15m (long the panic bar)", rows,
        "kill: no horizon with net>0 & t>=2 & yrs+>=60%")

    # B --------------------------------------------------------- funding window
    rows = {h: [] for h in (4, 8, 16)}
    for p in MAJ6:
        df = d15[p]
        f = fund[p]
        if not len(f):
            continue
        lo = f.rolling(90 * 3, min_periods=60).quantile(0.10)
        hi = f.rolling(90 * 3, min_periods=60).quantile(0.90)
        for ts, val in f.items():
            side = 0
            if pd.notna(hi.get(ts)) and val >= hi[ts] and val > 0:
                side = -1                       # crowded longs pay -> fade short
            elif pd.notna(lo.get(ts)) and val <= lo[ts] and val < 0:
                side = 1
            if side == 0:
                continue
            pos = df.index.searchsorted(ts)
            if pos >= len(df) - 33 or pos < 1:
                continue
            for h in rows:
                r = fwd_ret(df["close"], pos - 1, h)
                rows[h].append((ts, side * r if r == r else np.nan))
    verdicts["B funding-window"] = report(
        "B. FUNDING-SETTLEMENT FADE 15m (against the crowded side)", rows,
        "kill: no horizon with net>0 & t>=2 & yrs+>=60%")

    # C --------------------------------------------------------------- squeeze
    rows = {h: [] for h in (12, 24, 48)}
    n_ev_per_sym = {}
    for p in MAJ6:
        df = d1h[p]
        tr = pd.concat([df["high"] - df["low"],
                        (df["high"] - df["close"].shift()).abs(),
                        (df["low"] - df["close"].shift()).abs()], axis=1) \
            .max(axis=1)
        atr = tr.rolling(14).mean()
        na = (atr / df["close"])
        squeezed = na < na.rolling(720, min_periods=300).quantile(0.20)
        hi48 = df["high"].rolling(48).max().shift(1)
        lo48 = df["low"].rolling(48).min().shift(1)
        brk_up = squeezed & (df["close"] > hi48)
        brk_dn = squeezed & (df["close"] < lo48)
        evs = []
        for sgn, mask in ((1, brk_up), (-1, brk_dn)):
            for i in np.flatnonzero(mask.to_numpy()):
                evs.append((i, sgn))
        evs.sort()
        kept, last = [], -10**9
        for i, sgn in evs:
            if i - last >= 24:
                kept.append((i, sgn))
                last = i
        n_ev_per_sym[p] = len(kept)
        for i, sgn in kept:
            for h in rows:
                r = fwd_ret(df["close"], i, h)
                rows[h].append((df.index[i], sgn * r if r == r else np.nan))
    # matched random null (same per-symbol event counts, random bars)
    null_means = {h: [] for h in rows}
    for _ in range(200):
        for h in null_means:
            acc = []
            for p in MAJ6:
                df = d1h[p]
                n = n_ev_per_sym.get(p, 0)
                if n == 0 or len(df) < 800:
                    continue
                picks = RNG.integers(720, len(df) - 50, size=n)
                sgns = RNG.choice([-1, 1], size=n)
                acc += [sgns[k] * fwd_ret(df["close"], int(picks[k]), h)
                        for k in range(n)]
            m = np.nanmean(acc) - COST if acc else np.nan
            null_means[h].append(m * 1e4)
    null95 = {h: float(np.nanpercentile(v, 95)) for h, v in null_means.items()}
    verdicts["C squeeze"] = report(
        "C. VOL-SQUEEZE BREAKOUT 1h (direction of the break)", rows,
        "kill: t<2 everywhere OR fails the 200-draw random null", null_95=null95)

    # D --------------------------------------------------------------- lead-lag
    for tf_name, dd, horizons in (("15m", d15, (1, 2, 4, 8)),
                                  ("1h", d1h, (1, 2, 4, 8))):
        rows = {h: [] for h in horizons}
        btc = dd["BTC-USDT"]
        rb = btc["close"].pct_change()
        zb = (rb - rb.rolling(96).mean()) / rb.rolling(96).std()
        ev = dedup(np.flatnonzero((zb.abs() > 2.5).to_numpy()), 4)
        for i in ev:
            sgn = 1 if rb.iloc[i] > 0 else -1
            ts = btc.index[i]
            for h in rows:
                acc = []
                for a in ALTS5:
                    df = dd[a]
                    pos = df.index.searchsorted(ts)
                    if pos != len(df) and df.index[pos] == ts \
                            and pos < len(df) - h - 2:
                        r = fwd_ret(df["close"], pos, h)
                        if r == r:
                            acc.append(sgn * r)
                if acc:
                    rows[h].append((ts, float(np.mean(acc))))
        verdicts[f"D lead-lag {tf_name}"] = report(
            f"D. BTC->ALTS LEAD-LAG {tf_name} (follow BTC's shock)", rows,
            "kill: t<2 everywhere")

    # E ------------------------------------------------------------ xs-reversal
    closes = {}
    for p in QUAL23:
        closes[p] = fetch_ohlcv_bybit(p, "1d", days=2000)["close"]
    C = pd.DataFrame(closes)
    r2 = C.pct_change(2)
    resid = r2.sub(r2["BTC-USDT"], axis=0).drop(columns=["BTC-USDT"])
    fwd2 = C.pct_change(2).shift(-3)          # enter next day, hold 2d
    fwd2 = fwd2.sub(fwd2["BTC-USDT"], axis=0).drop(columns=["BTC-USDT"])
    rank = resid.rank(axis=1, pct=True)
    rows = {2: []}
    for ts in resid.index[10:-4]:
        rr, ff = rank.loc[ts], fwd2.loc[ts]
        for name in resid.columns:
            if rr.get(name) != rr.get(name) or ff.get(name) != ff.get(name):
                continue
            if rr[name] <= 0.2:
                rows[2].append((ts, float(ff[name])))      # long losers
            elif rr[name] >= 0.8:
                rows[2].append((ts, float(-ff[name])))     # short winners
    verdicts["E xs-reversal"] = report(
        "E. CROSS-SECTIONAL 2d REVERSAL vs BTC (1d, QUAL23)", rows,
        "kill: pooled t<2 or yrs+<60%")

    print("\n" + "=" * 72)
    print("SCOREBOARD")
    print("=" * 72)
    for k, v in verdicts.items():
        print(f"  {'HEARTBEAT' if v else 'dead     '}  {k}")
    print("\nA heartbeat here is a hunting license, not an edge: next step is a"
          "\nfull pre-registered engine study with the gate battery (and for the"
          "\nfast ones, the tick collector's data).")


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
