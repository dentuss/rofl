"""OHLCV data fetching with on-disk caching.

Uses KuCoin's public API: no auth, no geo-block, returns 1500 candles per call.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pandas as pd
import requests

CACHE_DIR = Path(__file__).parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)

# KuCoin interval strings
KU_INTERVAL = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1hour",
    "4h": "4hour",
    "1d": "1day",
}

INTERVAL_SEC = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}


def _kucoin_fetch(symbol: str, interval: str, start: int, end: int) -> list[list]:
    url = "https://api.kucoin.com/api/v1/market/candles"
    params = {
        "symbol": symbol,
        "type": KU_INTERVAL[interval],
        "startAt": start,
        "endAt": end,
    }
    last_err: Exception | None = None
    for attempt in range(5):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code in (429, 503):
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            j = r.json()
            if j.get("code") != "200000":
                raise RuntimeError(f"KuCoin error: {j}")
            return j.get("data") or []
        except Exception as e:
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"KuCoin failed after retries: {last_err}")


def fetch_ohlcv(symbol: str, interval: str, days: int = 30, use_cache: bool = True) -> pd.DataFrame:
    """Fetch OHLCV; symbol like 'BTC-USDT'. Returns df indexed by datetime UTC.

    Paginates KuCoin (which returns up to 1500 candles per call) so that we can
    pull arbitrary lookbacks. Older data first, newest last.
    """
    cache_file = CACHE_DIR / f"{symbol}_{interval}_{days}d.parquet"
    if use_cache and cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < 3600:  # 1h cache
            return pd.read_parquet(cache_file)

    sec = INTERVAL_SEC[interval]
    end = int(time.time())
    start = end - days * 86400
    rows: list[list] = []
    cursor = end
    while cursor > start:
        chunk_start = max(start, cursor - 1500 * sec)
        data = _kucoin_fetch(symbol, interval, chunk_start, cursor)
        if not data:
            break
        rows.extend(data)
        oldest = int(data[-1][0])
        if oldest <= chunk_start:
            cursor = oldest - sec
        else:
            cursor = chunk_start
        time.sleep(0.6)  # be polite (KuCoin throttles aggressively)

    if not rows:
        raise RuntimeError(f"No data returned for {symbol} {interval}")

    df = pd.DataFrame(rows, columns=["ts", "open", "close", "high", "low", "volume", "turnover"])
    df = df.astype({"ts": "int64", **{c: "float64" for c in ["open", "close", "high", "low", "volume", "turnover"]}})
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df = df.set_index("dt")[["open", "high", "low", "close", "volume"]]

    if use_cache:
        df.to_parquet(cache_file)
    return df


if __name__ == "__main__":
    for sym in ("BTC-USDT", "ETH-USDT", "SOL-USDT"):
        for tf in ("15m", "1h"):
            df = fetch_ohlcv(sym, tf, days=30)
            print(f"{sym} {tf}: {len(df)} candles, {df.index[0]} -> {df.index[-1]}")
