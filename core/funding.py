"""Historical perpetual funding rates, with on-disk caching.

Funding is charged every 8h on USDT-perps. A long position PAYS positive
funding; a short RECEIVES it. The backtester uses this to model the real
carrying cost instead of a flat constant.

Sources (tried in order for source="auto"):
  - bybit : api.bybit.com  — preferred on the production EC2 box (Singapore).
            Geo-blocked from some regions (e.g. US/EU CI runners).
  - okx   : okx.com        — reachable from most regions; good for research.

If no source is reachable, callers fall back to a flat constant rate.

Each source returns a DataFrame indexed by UTC funding timestamp with a
single column 'funding_rate' (per-8h fraction, e.g. 0.0001 = 0.01%).
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import requests

CACHE_DIR = Path(__file__).parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)


def _to_bybit_symbol(symbol: str) -> str:
    # INJ-USDT / INJ/USDT -> INJUSDT
    return symbol.replace("-", "").replace("/", "")


def _to_okx_inst(symbol: str) -> str:
    # INJ-USDT / INJ/USDT -> INJ-USDT-SWAP
    base = symbol.replace("/", "-").upper()
    return f"{base}-SWAP"


def _fetch_bybit(symbol: str, days: int) -> pd.DataFrame:
    sym = _to_bybit_symbol(symbol)
    end = int(time.time() * 1000)
    start = end - days * 86400_000
    rows = []
    cursor_end = end
    for _ in range(200):  # safety cap on pages
        r = requests.get("https://api.bybit.com/v5/market/funding/history",
                         params={"category": "linear", "symbol": sym,
                                 "startTime": start, "endTime": cursor_end,
                                 "limit": 200}, timeout=20)
        r.raise_for_status()
        data = (r.json().get("result") or {}).get("list") or []
        if not data:
            break
        for d in data:
            rows.append((int(d["fundingRateTimestamp"]), float(d["fundingRate"])))
        oldest = min(int(d["fundingRateTimestamp"]) for d in data)
        if oldest <= start or len(data) < 200:
            break
        cursor_end = oldest - 1
    if not rows:
        raise RuntimeError("bybit returned no funding rows")
    return _rows_to_df(rows)


def _fetch_okx(symbol: str, days: int) -> pd.DataFrame:
    inst = _to_okx_inst(symbol)
    start_ms = int((time.time() - days * 86400) * 1000)
    rows = []
    after = ""  # OKX pages backward via 'after' = ts cursor (older than)
    for _ in range(400):  # 100/page
        params = {"instId": inst, "limit": 100}
        if after:
            params["after"] = after
        r = requests.get("https://www.okx.com/api/v5/public/funding-rate-history",
                         params=params, timeout=20)
        r.raise_for_status()
        data = r.json().get("data") or []
        if not data:
            break
        for d in data:
            rows.append((int(d["fundingTime"]), float(d["realizedRate"])))
        oldest = min(int(d["fundingTime"]) for d in data)
        if oldest <= start_ms or len(data) < 100:
            break
        after = str(oldest)
    if not rows:
        raise RuntimeError("okx returned no funding rows")
    return _rows_to_df(rows)


def _rows_to_df(rows: list[tuple[int, float]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", "funding_rate"]).drop_duplicates("ts")
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.set_index("dt")[["funding_rate"]].sort_index()


def fetch_funding(symbol: str, days: int = 5 * 365,
                  source: str = "auto", use_cache: bool = True) -> pd.DataFrame:
    """Return historical funding for `symbol`. Empty DataFrame if unavailable."""
    cache_file = CACHE_DIR / f"funding_{_to_bybit_symbol(symbol)}.parquet"
    if use_cache and cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < 12 * 3600:
            return pd.read_parquet(cache_file)

    order = ["bybit", "okx"] if source == "auto" else [source]
    fetchers = {"bybit": _fetch_bybit, "okx": _fetch_okx}
    last_err = None
    for src in order:
        try:
            df = fetchers[src](symbol, days)
            if use_cache:
                df.to_parquet(cache_file)
            return df
        except Exception as e:
            last_err = e
            continue
    # Nothing worked — return empty; caller falls back to a constant.
    print(f"funding fetch failed ({symbol}): {last_err}")
    return pd.DataFrame(columns=["funding_rate"])


def per_bar_funding(funding_df: pd.DataFrame, bar_index: pd.DatetimeIndex,
                    fallback_bps_per_8h: float = 1.0) -> pd.Series:
    """Map 8h funding events onto a bar index.

    Returns a per-bar Series: the funding rate (fraction) charged AT that bar,
    nonzero only on bars that contain a funding timestamp. A long position open
    across that bar pays `rate * notional`; a short receives it.

    If funding_df is empty, applies the flat fallback at every bar (so the
    per-bar sum over an 8h span ~= fallback_bps_per_8h).
    """
    out = pd.Series(0.0, index=bar_index, name="funding")
    if funding_df is None or funding_df.empty:
        # Spread the flat 8h rate evenly across bars (assume 1h bars => /8).
        bar_minutes = _infer_bar_minutes(bar_index)
        out[:] = (fallback_bps_per_8h / 1e4) * (bar_minutes / (8 * 60))
        return out
    # Snap each funding event to the bar that contains it (floor to bar grid).
    fr = funding_df["funding_rate"]
    # For each funding timestamp, find the last bar <= ts and assign.
    idx = bar_index
    pos = idx.searchsorted(fr.index, side="right") - 1
    for p, rate in zip(pos, fr.values):
        if 0 <= p < len(idx):
            out.iloc[p] += float(rate)
    return out


def _infer_bar_minutes(bar_index: pd.DatetimeIndex) -> float:
    if len(bar_index) < 2:
        return 60.0
    deltas = (bar_index[1:] - bar_index[:-1]).to_numpy()
    # median delta in minutes
    import numpy as np
    return float(np.median(deltas) / np.timedelta64(1, "m"))


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "INJ-USDT"
    df = fetch_funding(sym, days=5 * 365, source="auto", use_cache=False)
    if df.empty:
        print("no funding data available")
    else:
        print(f"{sym}: {len(df)} funding events, "
              f"{df.index[0].date()} → {df.index[-1].date()}")
        print(f"  mean per-8h rate: {df['funding_rate'].mean()*100:.4f}%  "
              f"(annualized ~{df['funding_rate'].mean()*3*365*100:.1f}%)")
        print(f"  min {df['funding_rate'].min()*100:.3f}%  "
              f"max {df['funding_rate'].max()*100:.3f}%  "
              f"std {df['funding_rate'].std()*100:.3f}pp")
        print(f"  share positive (longs pay): {(df['funding_rate']>0).mean()*100:.0f}%")
